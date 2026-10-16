"""エージェントのステートマシン。

    IDLE ─start─> OBSERVE ─> PLAN ─┬─> ACT ─> VERIFY ─> OBSERVE …（次の一手）
                                    ├─> CONFIRM ─承認─> ACT
                                    │           └拒否─> DONE
                                    └─ done ───> DONE
    どこで失敗しても ERROR。max_steps を超えても ERROR（無限ループ防止）。

1 ステップ = 1 アクション。毎回画面を観測し直してから次を決めるので、
想定外の画面（ダイアログ等）が出ても計画が古いまま突き進まない。
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

ACTION_TYPES = ("tap", "type", "key", "launch", "wait", "done")
_ALLOWED_KEYS = {"KEYCODE_BACK", "KEYCODE_HOME", "KEYCODE_ENTER", "KEYCODE_APP_SWITCH"}
# 対象の文言にこれらを含む操作は取り消せない可能性があるため、必ず人が確認する
RISKY_WORDS = ("削除", "消去", "アンインストール", "初期化", "購入", "支払", "送金", "送信", "注文", "解約",
               "delete", "remove", "uninstall", "erase", "reset", "buy", "purchase", "pay", "send", "order")


@dataclass(frozen=True)
class Action:
    type: str
    target: str = ""     # tap/type: 要素を探すキーワード, key: KEYCODE, launch: パッケージ名
    text: str = ""       # type: 入力する文字列
    reason: str = ""

    def describe(self) -> str:
        if self.type == "type":
            return f"「{self.target}」に「{self.text}」を入力"
        return {"tap": f"「{self.target}」をタップ", "key": f"{self.target} を送信",
                "launch": f"{self.target} を起動", "wait": "待機", "done": "完了"}[self.type]

    @property
    def risky(self) -> bool:
        hay = f"{self.target} {self.text}".lower()
        return any(w in hay for w in RISKY_WORDS)


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
    a = Action(t, str(obj.get("target", "")), str(obj.get("text", "")), str(obj.get("reason", "")))
    if t in ("tap", "type", "launch") and not a.target:
        raise PlanError(f"{t} には target が必要です")
    if t == "key" and a.target not in _ALLOWED_KEYS:
        raise PlanError(f"許可されていないキー: {a.target}")
    return a


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
{"action": "tap|type|key|launch|wait|done", "target": "...", "text": "...", "reason": "..."}
- tap: target は [screen] に列挙された要素の文言
- type: target は入力欄の文言、text は入力する文字列（ASCII のみ）
- key: target は KEYCODE_BACK / KEYCODE_HOME / KEYCODE_ENTER / KEYCODE_APP_SWITCH
- launch: target はパッケージ名
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


class Agent:
    def __init__(self, transport: Transport, planner: Planner, confirm: ConfirmFn = deny_all,
                 max_steps: int = 15, settle_s: float = 0.6,
                 on_log: Callable[[StepLog], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.t = transport
        self.planner = planner
        self.confirm = confirm
        self.max_steps = max_steps
        self.settle_s = settle_s
        self.on_log = on_log
        self.sleep = sleep
        self.probe = DeviceProbe(transport)
        self.state = State.IDLE
        self.log: list[StepLog] = []
        self.history: list[str] = []
        self.result = ""

    def _go(self, state: State, detail: str = "") -> None:
        self.state = state
        entry = StepLog(state, detail)
        self.log.append(entry)
        if self.on_log:
            self.on_log(entry)

    def observe(self, xml: str | None = None) -> Observation:
        snap = self.probe.snapshot(DeviceProbe.FAST)
        xml = xml if xml is not None else ui.dump(self.t)
        screen, h = ui.summarize(xml)
        return Observation(render(snap), screen, h, xml)

    def _resolve(self, a: Action, obs: Observation) -> ui.UiNode:
        nodes = ui.find(obs.xml, [a.target], editable=True if a.type == "type" else None)
        if a.type == "tap":
            # 文言だけの TextView より、押せる要素を優先
            nodes.sort(key=lambda n: (not n.clickable, n.area))
        nodes = [n for n in nodes if n.enabled]
        if not nodes:
            raise ui.UiError(f"画面に「{a.target}」が見つかりません")
        return nodes[0]

    def act(self, a: Action, obs: Observation) -> None:
        if a.type == "tap":
            ui.tap(self.t, *self._resolve(a, obs).center)
        elif a.type == "type":
            ui.type_text(self.t, self._resolve(a, obs), a.text)
        elif a.type == "key":
            ui.key(self.t, a.target)
        elif a.type == "launch":
            ui.launch(self.t, a.target)
        elif a.type == "wait":
            self.sleep(1.0)

    def run(self, goal: str) -> State:
        self.history.clear()
        self._go(State.OBSERVE, goal)
        xml: str | None = None  # VERIFY で取ったダンプを次の OBSERVE に流用（dump は 1 回 0.5〜1 秒かかる）
        try:
            for _ in range(self.max_steps):
                obs = self.observe(xml)

                self._go(State.PLAN, obs.screen_hash)
                a = self.planner.next_action(goal, obs, self.history)
                if a.type == "done":
                    self.result = a.reason
                    self._go(State.DONE, a.reason)
                    return self.state

                if a.risky:
                    self._go(State.CONFIRM, a.describe())
                    if not self.confirm(a):
                        self.result = f"ユーザーが拒否: {a.describe()}"
                        self._go(State.DONE, self.result)
                        return self.state

                self._go(State.ACT, a.describe())
                self.act(a, obs)

                self._go(State.VERIFY)
                self.sleep(self.settle_s)
                xml = ui.dump(self.t)
                changed = ui.summarize(xml)[1] != obs.screen_hash
                self.history.append(a.describe() + ("" if changed or a.type == "wait" else "（画面変化なし）"))
                self._go(State.OBSERVE)
            raise PlanError(f"{self.max_steps} 手以内に完了しませんでした")
        except Exception as e:
            self.result = f"{type(e).__name__}: {e}"
            self._go(State.ERROR, self.result)
            return self.state
