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
from jarvis_agent import daemon
from jarvis_agent.daemon import (DEFAULT_RULES, DaemonError, Dispatcher, Rule, ThreadRunner, acquire_lock,
                                 apply_overrides, in_hours, needs_foreground, run_forever, write_status_file)
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
        with mock.patch.dict(os.environ, {"JARVIS_HOME": str(self.root / "home")}), redirect_stdout(out), \
             mock.patch.object(cli.time, "time", lambda: NOW + 100):
            self.assertEqual(cli.main(["status"]), 0)
        text = out.getvalue()
        self.assertIn("daemon: 動作中", text)
        self.assertIn("最後の変化は 100 秒前", text)
        self.assertIn("あと 200 秒", text)   # 書いた後に経過した分を差し引く
        self.assertIn("idle-cleanup: screen_idle → optimize", text)
        self.assertIn("screen_off", text)

    def test_log_rotation(self):
        self.store.LOG_MAX_BYTES = 200
        for i in range(20):
            self.store.log({"i": i, "pad": "x" * 20})
        self.assertTrue((self.store.root / "log.jsonl.1").exists())
        self.assertLess((self.store.root / "log.jsonl").stat().st_size, 400)


class LightweightTest(Base):
    """常駐中の負荷: 定期的に起きない・書かない・必要な分だけ読む・タスクの後はメモリを手放す。"""

    def run_daemon(self, rules, stream_lines, poll=False):
        t = FakeTransport(sectioned(DEVICE_TABLE), stream_lines=stream_lines)
        ctx = TaskContext(t, self.store, now=NOW)
        d = Dispatcher(ctx, ev.StateTracker(), rules, notify=lambda a, b: None, log=self.logs.append)
        statuses, timeouts = [], []
        real_get = daemon.queue.Queue.get

        def get(q, block=True, timeout=None):
            timeouts.append(timeout)
            return real_get(q, block, timeout)

        with mock.patch.object(daemon.queue.Queue, "get", get):
            run_forever(ctx, d, poll=poll, sleep=lambda s: None, max_reconnects=0, write_status=statuses.append)
        return t, d, statuses, timeouts

    def test_sleeps_until_event_and_writes_status_only_on_change(self):
        lines = (["I/screen_toggled( 1): 1"] + ["I/battery_level( 1): [80,4000,300]"] * 5
                 + ["I/screen_toggled( 1): 0"])
        t, d, statuses, timeouts = self.run_daemon((), lines)
        self.assertIsNone(timeouts[0])                  # タイマーが無い間は時間切れなしで待つ
        self.assertIn(300, [round(x) for x in timeouts if x is not None])  # 画面 OFF 後は idle タイマーまで
        keys = [{k: v for k, v in s.items() if k != "updated"} for s in statuses]
        self.assertTrue(all(a != b for a, b in zip(keys, keys[1:])))   # 同じ中身を 2 回書かない
        self.assertLessEqual(len(statuses), 3)
        n = len(statuses)
        d.on_change()   # ワーカーからの通知でも、変化が無ければ書かない
        d.on_change()
        self.assertEqual(len(statuses), n)
        d.tracker.update(level=50)
        d.on_change()
        self.assertEqual(len(statuses), n + 1)

    def test_reads_only_needed_tags_and_sections(self):
        t, *_ = self.run_daemon(DEFAULT_RULES, [])
        logcat = [c for c in t.calls if c.startswith("logcat")]
        self.assertTrue(logcat)
        self.assertNotIn("resume_activity", logcat[0])
        self.assertFalse(any("dumpsys activity" in c or "/proc/meminfo" in c for c in t.calls))
        rules = DEFAULT_RULES + (Rule("fg", ev.APP_FOREGROUND, (), 0),)
        self.assertTrue(needs_foreground(rules))
        self.assertFalse(needs_foreground(DEFAULT_RULES + (Rule("fg", ev.APP_FOREGROUND, (), 0, enabled=False),)))
        t, *_ = self.run_daemon(rules, [])
        self.assertIn("resume_activity", [c for c in t.calls if c.startswith("logcat")][0])
        self.assertTrue(any("dumpsys activity" in c for c in t.calls))

    def test_polling_uses_light_sections(self):
        src = ev.PollingSource(mock.Mock(), ev.StateTracker(), sections=ev.WATCH_SECTIONS, sleep=lambda s: None,
                               max_polls=1)
        src.probe.snapshot.return_value = mock.Mock(data={}, get=lambda *a: None)
        list(src.events())
        self.assertEqual(src.probe.snapshot.call_args.args[0], ["battery", "power"])

    def test_worker_runs_at_lower_priority(self):
        base = os.getpriority(os.PRIO_PROCESS, 0)
        got = threading.Event()
        seen = []

        def job():
            seen.append(os.getpriority(os.PRIO_PROCESS, threading.get_native_id()))
            got.set()
            return []
        ThreadRunner().submit(job)
        self.assertTrue(got.wait(5))
        self.assertEqual(seen[0], min(19, base + daemon.WORKER_NICE))

    def test_releases_hash_cache_after_rule(self):
        self.store.hashes["/x"] = [1, 2, "h"]
        d = self.make((Rule("x", ev.SCREEN_IDLE, ("a",), 0),))
        with self.fake_tasks(a=(lambda c: None, False)):
            d.handle(ev.Event(ev.SCREEN_IDLE))
        self.assertIsNone(self.store._hashes)

    def test_wake_lock_only_while_tasks_run(self):
        calls = []
        rules = (Rule("x", ev.SCREEN_IDLE, ("a",), 0), Rule("n", ev.SCREEN_ON, (), 0, notify_pending=True))
        d = self.make(rules, wake_lock=calls.append)
        held = []
        with self.fake_tasks(a=(lambda c: held.append(list(calls)), False)):
            d.handle(ev.Event(ev.SCREEN_IDLE))
        self.assertEqual(held, [[True]])          # タスクの実行中は取っている
        self.assertEqual(calls, [True, False])    # 終わったら放す
        d.handle(ev.Event(ev.SCREEN_ON))          # 通知だけのルールでは取らない
        self.assertEqual(calls, [True, False])

    def test_wake_lock_released_when_task_fails_or_cancelled(self):
        calls = []
        d = self.make((Rule("x", ev.SCREEN_IDLE, ("a", "b"), 0),), wake_lock=calls.append)

        def boom(c):
            raise RuntimeError("x")

        def cancel(c):
            raise TaskCancelled()
        with self.fake_tasks(a=(boom, False), b=(cancel, False)):
            d.handle(ev.Event(ev.SCREEN_IDLE))
        self.assertEqual(calls, [True, False])

    def test_wake_lock_mode_never_or_always(self):
        for mode in ("never", "always"):
            calls = []
            self.store.config["wake_lock"] = mode
            d = self.make((Rule("x", ev.SCREEN_IDLE, ("a",), 0),), wake_lock=calls.append)
            with self.fake_tasks(a=(lambda c: None, False)):
                d.handle(ev.Event(ev.SCREEN_IDLE))
            self.assertEqual(calls, [], mode)   # always は起動時に 1 回取るだけ（CLI 側）

    def test_hashing_drops_page_cache(self):
        from jarvis_agent import dupes
        f = self.root / "big.bin"
        f.write_bytes(b"x" * 1000)
        with mock.patch.object(dupes.os, "posix_fadvise", create=True) as fa:
            dupes.sha256_file(str(f))
        self.assertEqual(fa.call_args.args[1:], (0, 0, dupes.os.POSIX_FADV_DONTNEED))

    def test_daemon_does_not_load_http_client(self):
        import subprocess
        code = "import sys, jarvis_agent.__main__; print('urllib.request' in sys.modules)"
        out = subprocess.run([sys.executable, "-c", code], cwd=str(Path(__file__).resolve().parent.parent),
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(out.strip(), "False")


if __name__ == "__main__":
    unittest.main()
