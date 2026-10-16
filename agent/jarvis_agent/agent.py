"""エージェントのステートマシン。

    IDLE ─start─> OBSERVE ─> PLAN ─┬─> ACT ─> VERIFY ─> OBSERVE …（次の一手）
                                    ├─> CONFIRM ─承認─> ACT
                                    │           └拒否─> DONE
                                    └─ done ───> DONE
    どこで失敗しても ERROR。max_steps を超えても ERROR（無限ループ防止）。

1 ステップ = 1 アクション。毎回画面を観測し直してから次を決めるので、
想定外の画面（ダイアログ等）が出ても計画が古いまま突き進まない。

安全のための確認:
- 危険な操作の判定は、プランナーが書いたキーワードだけでなく、実際に押す要素の文言でも行う
- allowed_packages を指定すると、それ以外のアプリに移った時点で止める（勝手に別のアプリを操作しない）
- 画面が 3 回続けて変わらない・同じ画面で同じ操作を 3 回くり返したら止める
"""

from __future__ import annotations

import enum
import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from . import ui
from .context import render
from .device import DeviceProbe
from .llm import LLM
from .transport import Transport


class State(enum.Enum):
    IDLE = "IDLE"
    OBSERVE = "OBSERVE"
    PLAN = "PLAN"
    CONFIRM = "CONFIRM"
    ACT = "ACT"
    VERIFY = "VERIFY"
    DONE = "DONE"
    ERROR = "ERROR"


# ---------------------------------------------------------------- actions

ACTION_TYPES = ("tap", "type", "key", "launch", "scroll", "toggle", "wait", "done")
# 操作中に前面に出てもよいシステムの画面（通知・権限ダイアログ・キーボードなど）
SYSTEM_PACKAGES = {"com.android.systemui", "android", "com.android.permissioncontroller",
                   "com.google.android.permissioncontroller"}
_ALLOWED_KEYS = {"KEYCODE_BACK", "KEYCODE_HOME", "KEYCODE_ENTER", "KEYCODE_APP_SWITCH"}
# 対象の文言にこれらを含む操作は取り消せない可能性があるため、必ず人が確認する
RISKY_WORDS = ("削除", "消去", "アンインストール", "初期化", "購入", "支払", "送金", "送信", "注文", "解約",
               "delete", "remove", "uninstall", "erase", "reset", "buy", "purchase", "pay", "send", "order")


@dataclass(frozen=True)
class Action:
    type: str
    target: str = ""     # tap/type/toggle: 要素を探すキーワード, key: KEYCODE, launch: パッケージ名,
                         # scroll: down / up, wait: 現れるのを待つ要素（空なら 1 秒待つ）
    text: str = ""       # type: 入力する文字列, toggle: on / off
    reason: str = ""
    expect: str = ""     # マクロ用: 操作のあとに現れるはずの要素（現れなければ止める）

    def describe(self) -> str:
        if self.type == "type":
            return f"「{self.target}」に「{self.text}」を入力"
        if self.type == "toggle":
            return f"「{self.target}」を {self.text.upper()} にする"
        if self.type == "wait":
            return f"「{self.target}」が出るまで待つ" if self.target else "待機"
        return {"tap": f"「{self.target}」をタップ", "key": f"{self.target} を送信",
                "launch": f"{self.target} を起動", "scroll": f"{'下' if self.target == 'down' else '上'}へスクロール",
                "done": "完了"}[self.type]

    @property
    def risky(self) -> bool:
        return is_risky(f"{self.target} {self.text}")


def is_risky(text: str) -> bool:
    hay = ui.normalize(text)
    return any(w in hay for w in RISKY_WORDS)


def node_words(n: ui.UiNode) -> str:
    return " ".join((n.text, n.desc, n.inner, n.hint, n.resource_id.rsplit("/", 1)[-1]))


class PlanError(ValueError):
    pass


def parse_action(raw: str) -> Action:
    """LLM 出力から最初の JSON オブジェクトを取り出し、許可された形かを検査する。"""
    m = re.search(r"\{.*?\}", raw, re.S)
    if not m:
        raise PlanError(f"JSON がありません: {raw[:80]!r}")
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise PlanError(f"JSON が不正: {e}") from e
    t = obj.get("action")
    if t not in ACTION_TYPES:
        raise PlanError(f"未知のアクション: {t!r}")
    a = Action(t, str(obj.get("target", "")), str(obj.get("text", "")), str(obj.get("reason", "")),
               str(obj.get("expect", "")))
    return validate_action(a)


def validate_action(a: Action) -> Action:
    t = a.type
    if t not in ACTION_TYPES:
        raise PlanError(f"未知のアクション: {t!r}")
    if t in ("tap", "type", "launch", "toggle") and not a.target:
        raise PlanError(f"{t} には target が必要です")
    if t == "key" and a.target not in _ALLOWED_KEYS:
        raise PlanError(f"許可されていないキー: {a.target}")
    if t == "scroll" and a.target not in ("down", "up"):
        raise PlanError("scroll の target は down か up")
    if t == "toggle" and a.text not in ("on", "off"):
        raise PlanError("toggle の text は on か off")
    return a


def load_macro(path: str) -> list[Action]:
    """定型手順（マクロ）の JSON: [{"action": "launch", "target": "com.android.settings",
    "expect": "ネットワーク"}, ...]。LLM を使わず、そのまま順に実行する（確認のルールは同じ）。"""
    with open(path, encoding="utf-8") as f:
        items = json.load(f)
    if not isinstance(items, list) or not items:
        raise PlanError("マクロは操作の配列で書いてください")
    out = []
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict):
            raise PlanError(f"{i} 番目の操作が不正です")
        try:
            out.append(validate_action(Action(str(it.get("action", "")), str(it.get("target", "")),
                                              str(it.get("text", "")), str(it.get("reason", "")),
                                              str(it.get("expect", "")))))
        except PlanError as e:
            raise PlanError(f"{i} 番目: {e}") from e
    return out


# ---------------------------------------------------------------- planners

@dataclass
class Observation:
    device: str
    screen: str
    screen_hash: str
    xml: str = field(repr=False, default="")


class Planner(Protocol):
    def next_action(self, goal: str, obs: Observation, history: list[str]) -> Action: ...


PLANNER_PROMPT = """あなたは Android 端末を操作するエージェント。目標に向けて次の一手だけを決める。
次の JSON を 1 つだけ出力する。説明文は書かない。
{"action": "tap|type|key|launch|scroll|toggle|wait|done", "target": "...", "text": "...", "reason": "..."}
- tap: target は [screen] に列挙された要素の文言（画面に無ければ自動で下へスクロールして探す）
- type: target は入力欄の文言、text は入力する文字列（ASCII のみ）
- toggle: target はスイッチの文言、text は on か off（既にその状態なら何もしない）
- scroll: target は down（続きを見る）か up
- key: target は KEYCODE_BACK / KEYCODE_HOME / KEYCODE_ENTER / KEYCODE_APP_SWITCH
- launch: target はパッケージ名
- wait: target に要素の文言を書くとそれが出るまで待つ（読み込み中など）
- 同じ操作で画面が変わらなかったら、別の操作を試す
- 目標を達成済み、または進められない場合は done"""


class LLMPlanner:
    def __init__(self, llm: LLM, retries: int = 1):
        self.llm = llm
        self.retries = retries

    def next_action(self, goal: str, obs: Observation, history: list[str]) -> Action:
        hist = "\n".join(f"{i + 1}. {h}" for i, h in enumerate(history[-6:])) or "なし"
        user = f"{obs.device}\n\n{obs.screen}\n\n[これまでの操作]\n{hist}\n\n[目標]\n{goal}"
        msgs = [{"role": "system", "content": PLANNER_PROMPT}, {"role": "user", "content": user}]
        err: Exception | None = None
        for _ in range(self.retries + 1):
            out = "".join(self.llm.stream(msgs))
            try:
                return parse_action(out)
            except PlanError as e:
                err = e
                msgs = msgs + [{"role": "assistant", "content": out},
                               {"role": "user", "content": f"形式エラー: {e}。JSON 1 つだけを出力して。"}]
        raise PlanError(str(err))


class ScriptedPlanner:
    """決まった手順を順に返す（定型タスク・テスト用）。"""

    def __init__(self, actions: list[Action]):
        self.actions = list(actions)

    def next_action(self, goal: str, obs: Observation, history: list[str]) -> Action:
        return self.actions.pop(0) if self.actions else Action("done")


# ---------------------------------------------------------------- agent

ConfirmFn = Callable[[Action], bool]


def deny_all(_a: Action) -> bool:
    return False


@dataclass
class StepLog:
    state: State
    detail: str
    t: float = field(default_factory=time.time)
    ms: float | None = None   # その段階にかかった時間（PLAN: 判断、ACT: 操作、VERIFY: 画面の安定待ち）


class StopRun(Exception):
    """安全のための停止（許可していないアプリへの移動・進まない・くり返し）。"""


class Agent:
    STUCK_LIMIT = 3      # 画面が変わらない操作がこの回数続いたら止める
    REPEAT_LIMIT = 3     # 同じ画面で同じ操作をこの回数くり返したら止める
    SETTLE_STEPS = (0.25, 0.4, 0.7)   # 操作後に画面を取り直す間隔（変化して落ち着いたら打ち切る）

    def __init__(self, transport: Transport, planner: Planner, confirm: ConfirmFn = deny_all,
                 max_steps: int = 15, on_log: Callable[[StepLog], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 allowed_packages: set[str] | None = None):
        self.t = transport
        self.planner = planner
        self.confirm = confirm
        self.max_steps = max_steps
        self.on_log = on_log
        self.sleep = sleep
        self.allowed = set(allowed_packages) if allowed_packages else None
        self.probe = DeviceProbe(transport)
        self.state = State.IDLE
        self.log: list[StepLog] = []
        self.history: list[str] = []
        self.result = ""

    def _go(self, state: State, detail: str = "", ms: float | None = None) -> None:
        self.state = state
        entry = StepLog(state, detail, ms=ms)
        self.log.append(entry)
        if self.on_log:
            self.on_log(entry)

    def observe(self, xml: str | None = None) -> Observation:
        snap = self.probe.snapshot(DeviceProbe.FAST)
        xml = xml if xml is not None else ui.dump(self.t)
        screen, h = ui.summarize(xml)
        return Observation(render(snap), screen, h, xml)

    def _resolve(self, a: Action, obs: Observation) -> tuple[ui.UiNode, str]:
        """操作する要素を決める。画面に無ければ一覧をスクロールして探す。戻り値の XML は探した後の画面。"""
        filters = {"editable": True} if a.type == "type" else {}

        def pick(xml: str) -> ui.UiNode | None:
            nodes = [n for n in ui.find(xml, [a.target], **filters) if n.enabled]
            if a.type == "tap":
                # 一致の良さは保ったまま、文言だけの TextView より押せる要素を優先
                best = ui.match_score(nodes[0], ui.normalize(a.target)) if nodes else None
                nodes = sorted((n for n in nodes if ui.match_score(n, ui.normalize(a.target)) == best),
                               key=lambda n: (not n.clickable, n.area)) or nodes
            elif a.type == "toggle":
                nodes.sort(key=lambda n: not n.checkable)
            return nodes[0] if nodes else None

        node = pick(obs.xml)
        xml = obs.xml
        if node is None:
            found, xml = ui.find_scrolling(self.t, obs.xml, [a.target], sleep=self.sleep, **filters)
            node = pick(xml) if found else None
        if node is None:
            raise ui.UiError(f"画面に「{a.target}」が見つかりません（スクロールしても無い）")
        return node, xml

    def act(self, a: Action, node: ui.UiNode | None, xml: str) -> str:
        """操作を実行し、履歴に残す補足を返す。"""
        if a.type == "tap":
            ui.tap(self.t, *node.center)
        elif a.type == "type":
            ui.type_text(self.t, node, a.text)
        elif a.type == "toggle":
            want = a.text == "on"
            if node.checkable and node.checked == want:
                return f"（既に {a.text.upper()}）"
            ui.tap(self.t, *node.center)
        elif a.type == "key":
            ui.key(self.t, a.target)
        elif a.type == "launch":
            ui.launch(self.t, a.target)
        elif a.type == "scroll":
            if not ui.scroll(self.t, xml, a.target):
                return "（スクロールできる一覧が無い）"
        elif a.type == "wait":
            if a.target:
                ui.wait_for(self.t, [a.target], sleep=self.sleep)
            else:
                self.sleep(1.0)
        return ""

    def settle(self, before: str) -> tuple[str, bool]:
        """操作後の画面を取り直す。変化したら、もう一度取り直して同じなら落ち着いたとみなす。
        固定時間待つより、すぐ変わる画面では速く、遅い画面では取りこぼしにくい。"""
        prev_hash, changed, xml = before, False, ""
        for wait in self.SETTLE_STEPS:
            self.sleep(wait)
            try:
                xml = ui.dump(self.t)
            except ui.UiError:
                continue  # アニメーション中は dump が失敗することがある
            h = ui.summarize(xml)[1]
            if h != before:
                if changed and h == prev_hash:
                    break  # 変化したあと 2 回同じ → 落ち着いた
                changed = True
            elif not changed and wait == self.SETTLE_STEPS[-1]:
                break
            prev_hash = h
        if not xml:
            xml = ui.dump(self.t)
        return xml, ui.summarize(xml)[1] != before

    def _check_package(self, xml: str, start_pkg: str) -> None:
        """操作の結果、許可していないアプリに移っていたら止める。
        始めた時の画面（コマンドを打った Termux など）に戻るのは構わない。"""
        if self.allowed is None:
            return
        pkg = ui.screen_package(xml)
        if pkg and pkg not in self.allowed and pkg not in SYSTEM_PACKAGES and pkg != start_pkg:
            raise StopRun(f"許可していないアプリ（{pkg}）に移ったので止めました")

    def run(self, goal: str) -> State:
        self.history.clear()
        self._go(State.OBSERVE, goal)
        xml: str | None = None  # VERIFY で取ったダンプを次の OBSERVE に流用（dump は 1 回 0.5〜1 秒かかる）
        stuck = 0
        seen: dict[tuple[str, str], int] = {}
        start_pkg: str | None = None
        try:
            for _ in range(self.max_steps):
                obs = self.observe(xml)
                if start_pkg is None:
                    start_pkg = ui.screen_package(obs.xml)

                t0 = time.perf_counter()
                self._go(State.PLAN, obs.screen_hash)
                a = self.planner.next_action(goal, obs, self.history)
                plan_ms = (time.perf_counter() - t0) * 1000
                if a.type == "done":
                    self.result = a.reason
                    self._go(State.DONE, a.reason, plan_ms)
                    return self.state
                if a.type == "launch" and self.allowed is not None and a.target not in self.allowed:
                    raise StopRun(f"許可していないアプリ（{a.target}）は起動しません")

                key = (obs.screen_hash, a.describe())
                seen[key] = seen.get(key, 0) + 1
                if seen[key] >= self.REPEAT_LIMIT:
                    raise StopRun(f"同じ画面で「{a.describe()}」を {seen[key]} 回くり返したので止めました")

                node, cur_xml = (self._resolve(a, obs) if a.type in ("tap", "type", "toggle")
                                 else (None, obs.xml))
                # 危険かどうかは、キーワードだけでなく実際に押す要素の文言でも判断する
                if a.risky or (node is not None and is_risky(node_words(node))):
                    what = a.describe() + (f"（押す要素: {node.label()}）" if node is not None else "")
                    self._go(State.CONFIRM, what)
                    if not self.confirm(a):
                        self.result = f"ユーザーが拒否: {what}"
                        self._go(State.DONE, self.result)
                        return self.state

                t0 = time.perf_counter()
                note = self.act(a, node, cur_xml)
                self._go(State.ACT, a.describe() + note, (time.perf_counter() - t0) * 1000)

                t0 = time.perf_counter()
                # 探すためにスクロールした場合は、スクロール後の画面と比べる（タップが空振りでも「変化あり」にしない）
                before = obs.screen_hash if cur_xml is obs.xml else ui.summarize(cur_xml)[1]
                xml, changed = self.settle(before)
                self._go(State.VERIFY, "画面が変化" if changed else "画面変化なし", (time.perf_counter() - t0) * 1000)
                if a.expect:
                    _, xml = ui.wait_for(self.t, [a.expect], sleep=self.sleep)
                    changed = True
                self._check_package(xml, start_pkg)

                no_change_expected = a.type == "wait" or note.startswith("（既に")
                stuck = 0 if changed or no_change_expected else stuck + 1
                if stuck >= self.STUCK_LIMIT:
                    raise StopRun(f"{stuck} 回続けて画面が変わらないので止めました")
                if seen[key] == self.REPEAT_LIMIT - 1:
                    note += "（同じ画面で同じ操作は 2 回目。次は別の操作を）"
                self.history.append(a.describe() + note + ("" if changed or no_change_expected else "（画面変化なし）"))
                self._go(State.OBSERVE)
            raise PlanError(f"{self.max_steps} 手以内に完了しませんでした")
        except Exception as e:
            self.result = f"{type(e).__name__}: {e}"
            self._go(State.ERROR, self.result)
            return self.state
