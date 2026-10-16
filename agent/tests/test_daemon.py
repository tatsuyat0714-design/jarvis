import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis_agent import __main__ as cli
from jarvis_agent import events as ev
from jarvis_agent import tasks
from jarvis_agent.daemon import (DEFAULT_RULES, DaemonError, Dispatcher, Rule, ThreadRunner, acquire_lock,
                                 apply_overrides, in_hours, run_forever, write_status_file)
from jarvis_agent.store import Store
from jarvis_agent.tasks import Report, TaskCancelled, TaskContext, run_task
from jarvis_agent.transport import FakeTransport
from test_events_tasks import DEV, DEVICE_TABLE, NOW, sectioned


def at_hour(h: int) -> float:
    lt = time.localtime(NOW)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, 0, 0, 0, 0, -1))


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "home")
        self.t = FakeTransport(sectioned(DEVICE_TABLE))
        self.ctx = TaskContext(self.t, self.store, now=NOW)
        self.clock = Clock()
        self.logs: list[str] = []
        self.ran: list[str] = []

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, rules=DEFAULT_RULES, screen_on=False, plugged=True, level=80, temp=30.0, **kw):
        tr = ev.StateTracker()
        tr.update(screen_on=screen_on, plugged=plugged, level=level, temp_c=temp)
        return Dispatcher(self.ctx, tr, rules, notify=lambda a, b: None, log=self.logs.append,
                          clock=self.clock, **kw)

    def fake_tasks(self, **fns):
        """TASKS を差し替える。値は (関数, 重いか)。"""
        def wrap(name, fn):
            def run(ctx):
                self.ran.append(name)
                fn(ctx)
                return Report(name, "ok")
            return run
        return mock.patch.dict(tasks.TASKS, {n: (wrap(n, f), heavy) for n, (f, heavy) in fns.items()})


class IdleTimerTest(Base):
    def test_idle_fires_after_minutes_and_runs_cleanup(self):
        d = self.make(screen_on=True)
        d.tracker.update(screen_on=False)
        self.assertEqual(d.handle(ev.Event(ev.SCREEN_OFF)), [])
        self.assertEqual(d.next_timer_in(self.clock()), 300)
        self.assertFalse(d.is_idle())
        self.assertEqual(d.fire_timers(self.clock.t + 299), [])
        reps = d.fire_timers(self.clock.t + 300)
        self.assertEqual([r.task for r in reps], ["optimize"])
        self.assertIsNone(d.next_timer_in(self.clock()))

    def test_screen_on_before_idle_cancels_timer(self):
        d = self.make(screen_on=True)
        d.tracker.update(screen_on=False)
        d.handle(ev.Event(ev.SCREEN_OFF))
        d.tracker.update(screen_on=True)
        d.handle(ev.Event(ev.SCREEN_ON))
        self.assertIsNone(d.next_timer_in(self.clock()))
        self.assertEqual(d.fire_timers(self.clock.t + 1000), [])

    def test_charging_right_after_screen_off_waits_for_idle(self):
        rules = (Rule("m", ev.CHARGING_START, ("updates",), 3600, require_charging=True, idle_only=True),)
        d = self.make(rules, screen_on=True)
        d.tracker.update(screen_on=False)
        d.handle(ev.Event(ev.SCREEN_OFF))
        self.assertEqual(d.handle(ev.Event(ev.CHARGING_START)), [])   # 消した直後はまだ動かない
        self.assertIn("m", d.deferred)
        self.assertEqual([r.task for r in d.fire_timers(self.clock.t + 300)], ["updates"])

    def test_already_idle_at_start_runs_immediately(self):
        rules = (Rule("m", ev.CHARGING_START, ("updates",), 3600, require_charging=True, idle_only=True),)
        d = self.make(rules, screen_on=False)
        self.assertEqual([r.task for r in d.handle(ev.Event(ev.CHARGING_START))], ["updates"])


class CancelTest(Base):
    RULE = (Rule("m", ev.CHARGING_START, ("a", "b"), 3600, require_charging=True, idle_only=True),)

    def test_screen_on_cancels_running_maintenance_and_retries_later(self):
        d = self.make(self.RULE)

        def user_returns(ctx):
            d.tracker.update(screen_on=True)
            d.handle(ev.Event(ev.SCREEN_ON))   # 実行中に画面が ON になった
            ctx.check_cancel()

        with self.fake_tasks(a=(user_returns, True), b=(lambda c: None, True)):
            self.assertEqual(d.handle(ev.Event(ev.CHARGING_START)), [])
        self.assertEqual(self.ran, ["a"])                      # b は実行しない
        self.assertEqual(self.store.last_run("rule:m"), 0)     # 実行済みにしない
        self.assertIn("m", d.deferred)                         # 次の idle でやり直す
        self.assertTrue(any("中断" in s for s in self.logs))
        self.assertFalse(d.cancel.is_set())                    # 次のルールに中断が残らない

    def test_unplug_cancels_and_does_not_retry(self):
        d = self.make(self.RULE)

        def unplugged(ctx):
            d.tracker.update(plugged=False)
            d.handle(ev.Event(ev.CHARGING_STOP))
            ctx.check_cancel()

        with self.fake_tasks(a=(unplugged, True), b=(lambda c: None, True)):
            d.handle(ev.Event(ev.CHARGING_START))
        self.assertEqual(self.ran, ["a"])
        self.assertNotIn("m", d.deferred)

    def test_screen_on_does_not_cancel_non_idle_rule(self):
        rules = (Rule("x", ev.BATTERY_LOW, ("a", "b"), 0),)
        d = self.make(rules)

        def screen_on(ctx):
            d.tracker.update(screen_on=True)
            d.handle(ev.Event(ev.SCREEN_ON))
            ctx.check_cancel()

        with self.fake_tasks(a=(screen_on, False), b=(lambda c: None, False)):
            d.handle(ev.Event(ev.BATTERY_LOW))
        self.assertEqual(self.ran, ["a", "b"])

    def test_dupes_scan_stops_on_cancel_but_keeps_hashes(self):
        sd = self.root / "sd"
        sd.mkdir()
        for i in range(3):
            (sd / f"{i}.bin").write_bytes(b"x" * 5000)
        self.store.config.update(dupe_roots=[str(sd)], dupe_min_bytes=1000)
        self.ctx.cancel = threading.Event()
        calls = []
        real = tasks.dp.sha256_file

        def hash_then_cancel(path, limit=None):
            calls.append(path)
            if len(calls) == 2:
                self.ctx.cancel.set()
            return real(path, limit)

        with mock.patch.object(tasks.dp, "sha256_file", hash_then_cancel):
            with self.assertRaises(TaskCancelled):
                run_task("dupes", self.ctx)
        self.assertLess(len(calls), 3)
        self.assertTrue((self.store.root / "hashes.json").exists())  # 途中までのハッシュを保存


class GuardTest(Base):
    def test_hot_battery_skips_heavy(self):
        rules = (Rule("x", ev.SCREEN_IDLE, ("heavy", "light"), 0),)
        d = self.make(rules, temp=42.0)
        with self.fake_tasks(heavy=(lambda c: None, True), light=(lambda c: None, False)):
            d.handle(ev.Event(ev.SCREEN_IDLE))
        self.assertEqual(self.ran, ["light"])
        self.assertTrue(any("熱い" in s for s in self.logs))

    def test_hours_window(self):
        self.assertTrue(in_hours((0, 6), at_hour(3)))
        self.assertFalse(in_hours((0, 6), at_hour(6)))
        self.assertTrue(in_hours((22, 6), at_hour(23)))
        self.assertTrue(in_hours((22, 6), at_hour(2)))
        self.assertFalse(in_hours((22, 6), at_hour(12)))
        self.assertTrue(in_hours(None, at_hour(12)))

    def test_idle_rule_outside_hours_waits(self):
        rules = (Rule("m", ev.CHARGING_START, ("a",), 3600, require_charging=True, idle_only=True, hours=(0, 6)),)
        self.clock.t = at_hour(14)
        d = self.make(rules)
        with self.fake_tasks(a=(lambda c: None, True)):
            d.handle(ev.Event(ev.CHARGING_START))
            self.assertEqual(self.ran, [])
            self.assertIn("m", d.deferred)
            self.clock.t = at_hour(2)
            d.handle(ev.Event(ev.SCREEN_IDLE))
        self.assertEqual(self.ran, ["a"])

    def test_disabled_rule(self):
        rules = (Rule("x", ev.SCREEN_IDLE, ("a",), 0, enabled=False),)
        with self.fake_tasks(a=(lambda c: None, False)):
            self.make(rules).handle(ev.Event(ev.SCREEN_IDLE))
        self.assertEqual(self.ran, [])


class OverrideTest(unittest.TestCase):
    def test_overrides(self):
        rules, warns = apply_overrides(DEFAULT_RULES, {
            "idle-cleanup": {"enabled": False},
            "charging-maintenance": {"cooldown_h": 48, "tasks": ["dupes"], "hours": [1, 5]},
            "low-battery": {"tasks": ["rm -rf"], "hours": [1, 99]},
            "nope": {"enabled": False},
        })
        r = {x.name: x for x in rules}
        self.assertFalse(r["idle-cleanup"].enabled)
        self.assertEqual(r["charging-maintenance"].cooldown_s, 48 * 3600)
        self.assertEqual(r["charging-maintenance"].tasks, ("dupes",))
        self.assertEqual(r["charging-maintenance"].hours, (1, 5))
        self.assertEqual(r["low-battery"].tasks, ("optimize",))  # 不正な値は無視
        self.assertIsNone(r["low-battery"].hours)
        self.assertEqual(len(warns), 3)
        self.assertEqual([x.name for x in rules], [x.name for x in DEFAULT_RULES])


class RunnerTest(Base):
    def test_thread_runner_keeps_event_loop_free(self):
        rules = (Rule("x", ev.SCREEN_IDLE, ("slow",), 0),)
        started, release = threading.Event(), threading.Event()

        def slow(ctx):
            started.set()
            release.wait(5)

        d = self.make(rules, runner=ThreadRunner())
        with self.fake_tasks(slow=(slow, False)):
            self.assertEqual(d.handle(ev.Event(ev.SCREEN_IDLE)), [])  # すぐ戻る
            self.assertTrue(started.wait(5))
            self.assertEqual(d.status()["running"], "x:slow")
            d.handle(ev.Event(ev.APP_FOREGROUND, {"package": "a.b"}))  # 実行中もイベントを処理できる
            release.set()
            for _ in range(100):
                if d.running is None:
                    break
                time.sleep(0.02)
        self.assertIsNone(d.running)
        self.assertGreater(self.store.last_run("rule:x"), 0)

    def test_on_change_called_when_rule_starts_and_ends(self):
        rules = (Rule("x", ev.SCREEN_IDLE, ("a",), 0),)
        d = self.make(rules)
        seen = []
        d.on_change = lambda: seen.append(d.status()["running"])
        with self.fake_tasks(a=(lambda c: None, False)):
            d.handle(ev.Event(ev.SCREEN_IDLE))
        self.assertEqual(seen, ["x:None", None])


class ResyncTest(Base):
    def test_seed_returns_changes_missed_while_disconnected(self):
        tr = ev.StateTracker()
        unplugged = DEV.replace("USB powered: true", "USB powered: false")
        outputs = [unplugged, DEV]
        from jarvis_agent.device import DeviceProbe
        probe = DeviceProbe(FakeTransport(lambda s: outputs.pop(0)))
        self.assertEqual(tr.seed(probe), [])
        self.assertEqual([e.kind for e in tr.seed(probe)], [ev.CHARGING_START])

    def test_run_forever_handles_missed_events_and_reconnects(self):
        unplugged = DEV.replace("USB powered: true", "USB powered: false")
        snaps = [unplugged, DEV]

        def respond(script):
            if "@@JARVIS:battery" in script:
                return snaps.pop(0) if len(snaps) > 1 else snaps[0]
            return sectioned(DEVICE_TABLE)(script)

        t = FakeTransport(respond, stream_lines=["I/screen_toggled( 1): 0"])
        ctx = TaskContext(t, self.store, now=NOW)
        rules = (Rule("c", ev.CHARGING_START, (), 0),)
        d = Dispatcher(ctx, ev.StateTracker(), rules, notify=lambda a, b: None, log=self.logs.append)
        statuses = []
        run_forever(ctx, d, sleep=lambda s: None, max_reconnects=1, write_status=statuses.append)
        joined = "\n".join(self.logs)
        self.assertIn("event screen_off", joined)             # 1 回目のストリーム
        self.assertIn("event charging_start", joined)         # 再接続時に、切断中の変化を拾う
        self.assertIn("接続エラー", joined)
        self.assertTrue(statuses and statuses[-1]["pid"] == os.getpid())


class LockStatusTest(Base):
    def test_lock(self):
        path = str(self.root / "home" / "daemon.lock")
        f = acquire_lock(path)
        with self.assertRaises(DaemonError) as cm:
            acquire_lock(path)
        self.assertIn(str(os.getpid()), str(cm.exception))
        f.close()
        acquire_lock(path).close()

    def test_status_cli(self):
        d = self.make()
        d.handle(ev.Event(ev.SCREEN_OFF))
        (self.root / "home").mkdir(exist_ok=True)
        write_status_file(str(self.root / "home" / "status.json"))(d.status())
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"JARVIS_HOME": str(self.root / "home")}), redirect_stdout(out):
            self.assertEqual(cli.main(["status"]), 0)
        text = out.getvalue()
        self.assertIn("daemon: 動作中", text)
        self.assertIn("あと 300 秒", text)
        self.assertIn("idle-cleanup: screen_idle → optimize", text)
        self.assertIn("screen_off", text)

    def test_log_rotation(self):
        self.store.LOG_MAX_BYTES = 200
        for i in range(20):
            self.store.log({"i": i, "pad": "x" * 20})
        self.assertTrue((self.store.root / "log.jsonl.1").exists())
        self.assertLess((self.store.root / "log.jsonl").stat().st_size, 400)


if __name__ == "__main__":
    unittest.main()
