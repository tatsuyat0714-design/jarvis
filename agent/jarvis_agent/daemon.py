"""イベント → ルール → タスク の常駐ディスパッチャ。

構成:
    イベント源（logcat / ポーリング）─スレッド→ キュー ─→ メインループ ─→ ルール判定
                                                     │                      │
                                                     └ タイマー（画面 OFF が  └→ ワーカー（1 本のスレッドで
                                                       続いたら screen_idle）     タスクを順に実行・中断可能）

既定ルール:
  充電開始       → updates / unused / dupes（約 1 日 1 回）。画面を使っている間は、
                   画面 OFF が idle_minutes 分続く（screen_idle）まで待つ
  screen_idle    → optimize（6 時間に 1 回）。画面を一瞬消しただけでは動かない
  電池低下       → optimize（2 時間に 1 回）
  画面 ON        → 承認待ちの提案があれば通知（3 時間に 1 回）

中断: 画面が ON になったら「画面 OFF の間だけ」のルールを、充電が外れたら「充電中だけ」の
ルールを中断する。中断したルールは実行済みにせず、条件がそろう次の機会にやり直す。
重いタスクは、充電中または電池 50% 以上、かつ電池温度が heavy_max_temp_c 未満のときだけ実行する。
提案は無人では実行しない。低リスクの提案だけ、設定 auto_apply_low_risk が true なら自動実行する。
"""

from __future__ import annotations

import dataclasses
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable

from . import events as ev
from .device import DeviceProbe
from .tasks import TASKS, Report, TaskCancelled, TaskContext, apply, run_task
from .transport import TransportError

HEAVY_MIN_LEVEL = 50


@dataclass(frozen=True)
class Rule:
    name: str
    on: str
    tasks: tuple[str, ...]
    cooldown_s: float
    require_charging: bool = False
    idle_only: bool = False        # 画面を使っている間は、画面 OFF が続く（screen_idle）まで先送り
    notify_pending: bool = False
    hours: tuple[int, int] | None = None   # 実行してよい時間帯 [開始, 終了)。日をまたいでもよい（22, 6）
    enabled: bool = True


DEFAULT_RULES: tuple[Rule, ...] = (
    Rule("charging-maintenance", ev.CHARGING_START, ("updates", "unused", "dupes"), 20 * 3600,
         require_charging=True, idle_only=True),
    Rule("idle-cleanup", ev.SCREEN_IDLE, ("optimize",), 6 * 3600),
    Rule("low-battery", ev.BATTERY_LOW, ("optimize",), 2 * 3600),
    Rule("screen-on-digest", ev.SCREEN_ON, (), 3 * 3600, notify_pending=True),
)


def apply_overrides(rules: tuple[Rule, ...], overrides: dict) -> tuple[tuple[Rule, ...], list[str]]:
    """設定ファイルの "rules": {名前: {enabled, cooldown_h, tasks, hours}} を既定ルールに重ねる。"""
    by_name = {r.name: r for r in rules}
    warnings = []
    for name, ov in (overrides or {}).items():
        r = by_name.get(name)
        if r is None or not isinstance(ov, dict):
            warnings.append(f"未知のルール: {name}")
            continue
        changes: dict = {}
        if isinstance(ov.get("enabled"), bool):
            changes["enabled"] = ov["enabled"]
        if isinstance(ov.get("cooldown_h"), (int, float)) and ov["cooldown_h"] >= 0:
            changes["cooldown_s"] = float(ov["cooldown_h"]) * 3600
        if "tasks" in ov:
            tasks = ov["tasks"]
            if isinstance(tasks, list) and all(t in TASKS for t in tasks):
                changes["tasks"] = tuple(tasks)
            else:
                warnings.append(f"{name}: 未知のタスクを含むため tasks は無視しました")
        if "hours" in ov:
            h = ov["hours"]
            if h is None:
                changes["hours"] = None
            elif (isinstance(h, list) and len(h) == 2 and all(isinstance(x, int) and 0 <= x <= 24 for x in h)):
                changes["hours"] = (h[0], h[1])
            else:
                warnings.append(f"{name}: hours は [開始, 終了]（0〜24 の整数）で指定してください")
        by_name[name] = dataclasses.replace(r, **changes)
    return tuple(by_name[r.name] for r in rules), warnings


def in_hours(hours: tuple[int, int] | None, t: float) -> bool:
    if hours is None:
        return True
    start, end = hours
    h = time.localtime(t).tm_hour
    return start <= h < end if start <= end else (h >= start or h < end)


def termux_notify(title: str, text: str) -> None:
    if shutil.which("termux-notification"):
        subprocess.run(["termux-notification", "--id", "jarvis-agent", "--title", title, "--content", text],
                       timeout=10, check=False)
    else:
        print(f"[通知] {title}: {text}", file=sys.stderr)


# ---------------------------------------------------------------- runners

class InlineRunner:
    """その場で実行する（テスト・単発実行用）。"""

    def submit(self, fn: Callable[[], list[Report]]) -> list[Report]:
        return fn()

    def queued(self) -> int:
        return 0


class ThreadRunner:
    """1 本のワーカースレッドで順に実行する。イベント処理（タイマー・中断）を止めないため。"""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._loop, name="jarvis-worker", daemon=True).start()

    def submit(self, fn: Callable[[], list[Report]]) -> list[Report]:
        self.q.put(fn)
        return []

    def queued(self) -> int:
        return self.q.qsize()

    def _loop(self) -> None:
        while True:
            fn = self.q.get()
            try:
                fn()
            except Exception as ex:  # ワーカーは止めない
                print(f"worker error: {ex}", file=sys.stderr)


# ---------------------------------------------------------------- dispatcher

class Dispatcher:
    def __init__(self, ctx: TaskContext, tracker: ev.StateTracker, rules: tuple[Rule, ...] = DEFAULT_RULES,
                 notify: Callable[[str, str], None] = termux_notify,
                 log: Callable[[str], None] = lambda s: print(s, file=sys.stderr),
                 runner=None, clock: Callable[[], float] = time.time):
        self.ctx = ctx
        self.tracker = tracker
        self.rules = rules
        self.notify = notify
        self.log = log
        self.runner = runner or InlineRunner()
        self.clock = clock
        self._lock = threading.Lock()
        self.deferred: dict[str, Rule] = {}
        self.idle_due: float | None = None
        self.running: Rule | None = None
        self.running_task: str | None = None
        self.cancel = threading.Event()
        self.recent: list[str] = []
        self.on_change: Callable[[], None] = lambda: None   # 状態ファイルの更新など
        ctx.cancel = self.cancel

    # ---- events / timers (メインスレッド)
    def handle(self, e: ev.Event) -> list[Report]:
        now = self.clock()
        self.log(f"event {e.kind} {e.data or ''}")
        self.recent = (self.recent + [f"{time.strftime('%H:%M:%S', time.localtime(now))} {e.kind}"])[-10:]
        if e.kind == ev.SCREEN_OFF:
            self.idle_due = now + self.ctx.cfg["idle_minutes"] * 60
        elif e.kind == ev.SCREEN_ON:
            self.idle_due = None
            self._cancel_if(lambda r: r.idle_only, "画面が ON になった")
        elif e.kind == ev.CHARGING_STOP:
            self._cancel_if(lambda r: r.require_charging, "充電が外れた")
            with self._lock:
                self.deferred = {k: r for k, r in self.deferred.items() if not r.require_charging}

        reports: list[Report] = []
        if e.kind == ev.SCREEN_IDLE:
            with self._lock:
                waiting, self.deferred = list(self.deferred.values()), {}
            for r in waiting:
                reports += self.runner.submit(lambda r=r: self._run_rule(r))
        for r in self.rules:
            if r.on != e.kind or not r.enabled:
                continue
            if r.idle_only and not self.is_idle():
                self._defer(r, "画面 OFF が続くまで先送り")
                continue
            reports += self.runner.submit(lambda r=r: self._run_rule(r))
        return reports

    def is_idle(self) -> bool:
        """画面 OFF が idle_minutes 続いた状態（起動時に既に OFF なら idle とみなす）。"""
        return self.tracker.state.screen_on is False and self.idle_due is None

    def next_timer_in(self, now: float) -> float | None:
        return None if self.idle_due is None else max(0.0, self.idle_due - now)

    def fire_timers(self, now: float) -> list[Report]:
        if self.idle_due is not None and now >= self.idle_due:
            self.idle_due = None
            if self.tracker.state.screen_on is False:
                return self.handle(ev.Event(ev.SCREEN_IDLE, t=now))
        return []

    def _defer(self, r: Rule, why: str) -> None:
        with self._lock:
            new = r.name not in self.deferred
            self.deferred[r.name] = r
        if new:
            self.log(f"rule {r.name}: {why}")

    def _cancel_if(self, pred: Callable[[Rule], bool], why: str) -> None:
        # 実行中のルールとその中断トークンを組で読む（ワーカーが次のルールに移る瞬間と競合しないように）
        with self._lock:
            r, token = self.running, self.cancel
        if r is not None and pred(r) and not token.is_set():
            self.log(f"rule {r.name}: {why}ため中断します")
            token.set()

    # ---- ルールの実行（ワーカースレッド）
    def _run_rule(self, r: Rule) -> list[Report]:
        now = self.clock()
        st = self.tracker.state
        if now - self.ctx.store.last_run(f"rule:{r.name}") < r.cooldown_s:
            return []
        if r.require_charging and not st.plugged:
            return []
        if r.idle_only and not self.is_idle():
            self._defer(r, "画面 OFF が続くまで先送り")
            return []
        if not in_hours(r.hours, now):
            if r.idle_only:
                self._defer(r, f"実行時間帯 {r.hours[0]}〜{r.hours[1]} 時を待つ")
            return []

        if r.notify_pending and (n := len(self.ctx.store.pending())):
            self.notify("ジャービス", f"承認待ちの提案が {n} 件あります")

        token = threading.Event()   # ルールの実行ごとに新しい中断トークン
        with self._lock:
            self.running, self.cancel = r, token
            self.ctx.cancel = token
        self.on_change()
        reports: list[Report] = []
        cancelled = False
        try:
            for name in r.tasks:
                if token.is_set():
                    cancelled = True
                    break
                _, heavy = TASKS[name]
                if heavy and not st.plugged and (st.level or 0) < HEAVY_MIN_LEVEL:
                    self.log(f"skip {name}: 電池残量が少ないため")
                    continue
                if heavy and st.temp_c is not None and st.temp_c >= self.ctx.cfg["heavy_max_temp_c"]:
                    self.log(f"skip {name}: 電池が熱いため（{st.temp_c:.0f}℃）")
                    continue
                self.ctx.now = self.clock()
                self.running_task = name
                try:
                    rep = run_task(name, self.ctx)
                except TaskCancelled:
                    self.log(f"task {name}: 中断しました")
                    cancelled = True
                    break
                except Exception as ex:  # 1 タスクの失敗で常駐を止めない
                    self.log(f"task {name} failed: {ex}")
                    self.ctx.store.log({"task": name, "error": str(ex)})
                    continue
                finally:
                    self.running_task = None
                reports.append(rep)
                self._handle_proposals(rep)
        finally:
            with self._lock:
                self.running = None
                self.cancel = threading.Event()
                self.ctx.cancel = self.cancel
        if cancelled:
            # 実行済みにはしない。条件がそろう次の機会にやり直す
            if r.idle_only and (not r.require_charging or self.tracker.state.plugged):
                self._defer(r, "中断したので次の機会にやり直す")
        else:
            self.ctx.store.mark_run(f"rule:{r.name}", now)
        self.on_change()
        return reports

    def _handle_proposals(self, rep: Report) -> None:
        auto = self.ctx.cfg.get("auto_apply_low_risk", False)
        pending, applied = [], []
        for p in rep.proposals:
            if auto and p.risk == "low":
                try:
                    applied.append(apply(p, self.ctx))
                except Exception as ex:
                    self.log(f"apply {p.kind} failed: {ex}")
            else:
                pending.append(p.to_dict())
        added = self.ctx.store.add_pending(pending)
        self.ctx.store.log({"task": rep.task, "summary": rep.summary, "applied": applied,
                            "pending_added": added, "ms": round(rep.elapsed_ms)})
        self.log(f"task {rep.task}: {rep.summary}（自動実行 {len(applied)} / 承認待ち +{added}）")
        if added:
            self.notify(f"ジャービス: {rep.task}", f"{rep.summary}。承認待ち {added} 件")

    # ---- 状態の見える化
    def status(self) -> dict:
        s = self.tracker.state
        return {
            "pid": os.getpid(),
            "updated": self.clock(),
            "device": {"screen_on": s.screen_on, "plugged": s.plugged, "level": s.level,
                       "temp_c": s.temp_c, "package": s.package},
            "running": f"{self.running.name}:{self.running_task}" if self.running else None,
            "queued": self.runner.queued(),
            "deferred": sorted(self.deferred),
            "idle_in_s": None if self.idle_due is None else max(0, round(self.idle_due - self.clock())),
            "recent_events": self.recent,
            "rules": [{"name": r.name, "on": r.on, "enabled": r.enabled, "tasks": list(r.tasks),
                       "last_run": self.ctx.store.last_run(f"rule:{r.name}") or None} for r in self.rules],
        }


# ---------------------------------------------------------------- 常駐

class DaemonError(RuntimeError):
    pass


def acquire_lock(path: str):
    """二重起動を防ぐ（Termux:Boot と手動起動が重なるとタスクが 2 回走る）。戻り値は開いたままにしておく。"""
    import fcntl
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f = open(path, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.seek(0)
        other = f.read().strip()
        f.close()
        raise DaemonError(f"daemon は既に動いています（pid {other or '?'}）")
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f


_END = object()


def _pump(source, q: queue.Queue) -> None:
    try:
        for e in source.events():
            q.put(e)
        q.put(_END)
    except Exception as ex:  # 接続エラーなどはメインループで扱う
        q.put(ex)


def run_forever(ctx: TaskContext, dispatcher: Dispatcher, poll: bool = False,
                sleep: Callable[[float], None] = time.sleep, max_failures: int = 3,
                write_status: Callable[[dict], None] | None = None,
                max_reconnects: int | None = None) -> None:
    """イベント源をスレッドで読み、メインループでイベントとタイマーを処理する。
    切断したら再接続し、切断中に起きた変化（充電の抜き差し等）もイベントとして処理する。
    3 回続けて失敗したらポーリングに切り替える。"""
    probe = DeviceProbe(ctx.transport)
    failures = reconnects = 0
    if write_status:
        # ワーカーがタスクを始めた・終えた時にも書く（メインループは最大 60 秒眠るため）
        dispatcher.on_change = lambda: write_status(dispatcher.status())
    while max_reconnects is None or reconnects <= max_reconnects:
        try:
            for e in dispatcher.tracker.seed(probe):
                dispatcher.handle(e)
            if poll or failures >= max_failures:
                source = ev.PollingSource(probe, dispatcher.tracker, sleep=sleep)
            else:
                source = ev.LogcatSource(ctx.transport, dispatcher.tracker)
            q: queue.Queue = queue.Queue()
            threading.Thread(target=_pump, args=(source, q), name="jarvis-events", daemon=True).start()
            while True:
                if write_status:
                    write_status(dispatcher.status())
                timeout = dispatcher.next_timer_in(dispatcher.clock())
                try:
                    item = q.get(timeout=60 if timeout is None else min(timeout, 60))
                except queue.Empty:
                    item = None
                if isinstance(item, Exception):
                    raise item
                if item is _END:
                    raise TransportError("イベントの読み取りが終了しました")
                if item is not None:
                    failures = 0
                    dispatcher.handle(item)
                dispatcher.fire_timers(dispatcher.clock())
        except TransportError as ex:
            failures += 1
            reconnects += 1
            wait = min(300, 5 * 2 ** failures)
            dispatcher.log(f"接続エラー（{failures} 回目、{wait}秒後に再接続）: {ex}")
            sleep(wait)


def write_status_file(path: str) -> Callable[[dict], None]:
    def write(data: dict) -> None:
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except OSError:
            pass
    return write
