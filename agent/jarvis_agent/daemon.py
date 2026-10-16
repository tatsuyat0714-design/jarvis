"""イベント → ルール → タスク の常駐ディスパッチャ。

既定ルール:
  充電開始   → updates / unused / dupes（1 日 1 回。画面 ON 中なら OFF になるまで待つ）
  画面 OFF   → optimize（6 時間に 1 回）
  電池低下   → optimize（2 時間に 1 回）
  画面 ON    → 承認待ちの提案があれば通知（3 時間に 1 回）

重いタスクは「充電中」または「電池 50% 以上」のときだけ実行する。
提案は無人では実行しない。低リスクの提案だけ、設定 auto_apply_low_risk が true なら自動実行する。
それ以外は承認待ちに保存し、`python -m jarvis_agent pending` で確認・実行する。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable

from . import events as ev
from .device import DeviceProbe
from .tasks import TASKS, Report, TaskContext, apply, run_task
from .transport import TransportError

HEAVY_MIN_LEVEL = 50


@dataclass(frozen=True)
class Rule:
    name: str
    on: str
    tasks: tuple[str, ...]
    cooldown_s: float
    require_charging: bool = False
    idle_only: bool = False        # 画面 ON 中なら、次の画面 OFF まで先送り
    notify_pending: bool = False


DEFAULT_RULES: tuple[Rule, ...] = (
    Rule("charging-maintenance", ev.CHARGING_START, ("updates", "unused", "dupes"), 20 * 3600,
         require_charging=True, idle_only=True),
    Rule("screen-off-cleanup", ev.SCREEN_OFF, ("optimize",), 6 * 3600),
    Rule("low-battery", ev.BATTERY_LOW, ("optimize",), 2 * 3600),
    Rule("screen-on-digest", ev.SCREEN_ON, (), 3 * 3600, notify_pending=True),
)


def termux_notify(title: str, text: str) -> None:
    if shutil.which("termux-notification"):
        subprocess.run(["termux-notification", "--id", "jarvis-agent", "--title", title, "--content", text],
                       timeout=10, check=False)
    else:
        print(f"[通知] {title}: {text}", file=sys.stderr)


class Dispatcher:
    def __init__(self, ctx: TaskContext, tracker: ev.StateTracker, rules: tuple[Rule, ...] = DEFAULT_RULES,
                 notify: Callable[[str, str], None] = termux_notify,
                 log: Callable[[str], None] = lambda s: print(s, file=sys.stderr)):
        self.ctx = ctx
        self.tracker = tracker
        self.rules = rules
        self.notify = notify
        self.log = log
        self.deferred: dict[str, Rule] = {}

    def handle(self, e: ev.Event) -> list[Report]:
        self.log(f"event {e.kind} {e.data or ''}")
        reports: list[Report] = []
        st = self.tracker.state
        if e.kind == ev.SCREEN_OFF and self.deferred:
            for r in list(self.deferred.values()):
                self.deferred.pop(r.name)
                reports += self._fire(r)
        if e.kind == ev.CHARGING_STOP:
            self.deferred = {k: r for k, r in self.deferred.items() if not r.require_charging}
        for r in self.rules:
            if r.on != e.kind:
                continue
            if r.idle_only and st.screen_on:
                self.deferred[r.name] = r
                self.log(f"rule {r.name}: 画面 ON のため OFF まで先送り")
                continue
            reports += self._fire(r)
        return reports

    def _fire(self, r: Rule) -> list[Report]:
        now = time.time()
        if now - self.ctx.store.last_run(f"rule:{r.name}") < r.cooldown_s:
            return []
        st = self.tracker.state
        if r.require_charging and not st.plugged:
            return []
        self.ctx.store.mark_run(f"rule:{r.name}", now)

        if r.notify_pending and (n := len(self.ctx.store.pending())):
            self.notify("ジャービス", f"承認待ちの提案が {n} 件あります")

        reports = []
        for name in r.tasks:
            _, heavy = TASKS[name]
            if heavy and not st.plugged and (st.level or 0) < HEAVY_MIN_LEVEL:
                self.log(f"skip {name}: 電池残量が少ないため")
                continue
            self.ctx.now = now
            try:
                rep = run_task(name, self.ctx)
            except Exception as ex:  # 1 タスクの失敗で常駐を止めない
                self.log(f"task {name} failed: {ex}")
                self.ctx.store.log({"task": name, "error": str(ex)})
                continue
            reports.append(rep)
            self._handle_proposals(rep)
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


def run_forever(ctx: TaskContext, dispatcher: Dispatcher, poll: bool = False,
                sleep: Callable[[float], None] = time.sleep, max_failures: int = 3) -> None:
    """logcat ストリームで待ち受ける。切断が続いたらポーリングに切り替える。"""
    probe = DeviceProbe(ctx.transport)
    failures = 0
    while True:
        try:
            dispatcher.tracker.seed(probe)
            if poll or failures >= max_failures:
                source = ev.PollingSource(probe, dispatcher.tracker, sleep=sleep)
            else:
                source = ev.LogcatSource(ctx.transport, dispatcher.tracker)
            for e in source.events():
                failures = 0
                dispatcher.handle(e)
        except TransportError as ex:
            failures += 1
            wait = min(300, 5 * 2 ** failures)
            dispatcher.log(f"接続エラー（{failures} 回目、{wait}秒後に再接続）: {ex}")
            sleep(wait)
