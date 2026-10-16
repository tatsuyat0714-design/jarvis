import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis_agent import dupes as dp
from jarvis_agent import events as ev
from jarvis_agent.daemon import Dispatcher, Rule
from jarvis_agent.device import DeviceProbe
from jarvis_agent.store import Store
from jarvis_agent.tasks import (ApplyError, Proposal, TaskContext, TaskError, apply, parse_badging,
                                parse_ps, parse_usage, parse_versions, run_task)
from jarvis_agent.transport import FakeTransport

DEV = (Path(__file__).parent / "fixtures" / "pixel_a14.txt").read_text()
NOW = time.mktime(time.strptime("2026-10-16 12:00:00", "%Y-%m-%d %H:%M:%S"))

USER_PKGS = "package:com.android.chrome\npackage:jp.naver.line.android\npackage:com.old.game\npackage:com.new.app\npackage:com.termux\n"
USAGE = """
  package=com.android.chrome totalTimeUsed="10:00" lastTimeUsed="2026-10-16 09:00:00" totalTimeVisible="1"
  package=jp.naver.line.android totalTimeUsed="05:00" lastTimeUsed="2026-10-10 20:00:00"
  package=com.old.game totalTimeUsed="01:00" lastTimeUsed="2026-06-01 10:00:00"
  package=com.old.game totalTimeUsed="00:00" lastTimeUsed="1970-01-01 09:00:00"
"""
VERSIONS = """  Package [com.android.chrome] (1a2b):
    versionCode=612300 minSdk=29 targetSdk=34
    versionName=120.0.1
    lastVersionCode=1
  Package [jp.naver.line.android] (3c4d):
    versionCode=1400 minSdk=28 targetSdk=34
    versionName=14.0.0
  Package [com.android.systemui] (5e):
    versionCode=34 minSdk=34 targetSdk=34
    versionName=14
"""
PS = """RSS NAME
812000 com.android.chrome
420000 jp.naver.line.android
120000 jp.naver.line.android:push
900000 system_server
50000 com.old.game
"""


def sectioned(table: dict[str, str]):
    """run_sections が作るスクリプトを解釈し、コマンドに含まれる文字列で応答を選ぶ擬似端末。"""
    def respond(script: str) -> str:
        if "@@JARVIS:" not in script:
            for k, v in table.items():
                if k in script:
                    return v
            return ""
        out = []
        for name, cmd in re.findall(r"echo '@@JARVIS:([^']+)'; \{ (.*?); \} 2>/dev/null", script):
            out.append(f"@@JARVIS:{name}")
            out.append(next((v for k, v in table.items() if k in cmd), ""))
        return "\n".join(out)
    return respond


DEVICE_TABLE = {
    "pm list packages -3 -d": "",          # 無効化されたアプリ（先に書かないと下の行に一致してしまう）
    "pm list packages -3": USER_PKGS,
    "dumpsys usagestats": USAGE,
    "dumpsys package com.old.game": "    firstInstallTime=2025-01-01 10:00:00",
    "dumpsys package com.new.app": "    firstInstallTime=2026-10-15 10:00:00",
    "dumpsys package packages": VERSIONS,
    "ps -A": PS,
    "df /data": "/dev/block/dm-48  115206272 61834532  53240284  54% /data",
    "dumpsys activity": "  topResumedActivity=ActivityRecord{1 u0 com.android.chrome/.Main t1}",
    "dumpsys diskstats": 'App Cache Size: 2147483648\nPackage Names: ["com.android.chrome","jp.naver.line.android"]\nCache Sizes: [1610612736,536870912]',
    "/proc/meminfo": "MemTotal:        7814036 kB\nMemFree:          112504 kB\nMemAvailable:     560112 kB",
    "pm uninstall": "Success",
    "pm install": "Success",
}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "home")
        self.t = FakeTransport(sectioned(DEVICE_TABLE))
        self.ctx = TaskContext(self.t, self.store, now=NOW)

    def tearDown(self):
        self.tmp.cleanup()


# ---------------------------------------------------------------- events

class EventsTest(unittest.TestCase):
    def test_parse_logcat(self):
        p = ev.parse_logcat_line
        self.assertEqual(p("I/screen_toggled( 1234): 1"), {"screen_on": True})
        self.assertEqual(p("I/screen_toggled(  999): 0"), {"screen_on": False})
        self.assertEqual(p("I/battery_status( 1500): [2,2,1,2,Li-ion]"), {"plugged": True})
        self.assertEqual(p("I/battery_status( 1500): [3,2,1,0,Li-ion]"), {"plugged": False})
        self.assertEqual(p("I/battery_level( 1500): [14,3700,301]"), {"level": 14})
        self.assertEqual(p("I/wm_resume_activity( 1500): [0,1234,42,com.android.chrome/com.google.Main,resume]"),
                         {"package": "com.android.chrome"})
        self.assertIsNone(p("--------- beginning of events"))
        self.assertIsNone(p("I/battery_status( 1): [garbage]"))

    def test_tracker_emits_only_on_change(self):
        tr = ev.StateTracker()
        self.assertEqual(tr.update(screen_on=True, plugged=False, level=50, package="a.b"), [])  # 基準値
        self.assertEqual(tr.update(screen_on=True), [])
        self.assertEqual([e.kind for e in tr.update(screen_on=False)], [ev.SCREEN_OFF])
        self.assertEqual([e.kind for e in tr.update(plugged=True)], [ev.CHARGING_START])
        self.assertEqual(tr.update(package="c.d")[0].data, {"package": "c.d", "previous": "a.b"})

    def test_battery_low_only_when_crossing_and_unplugged(self):
        tr = ev.StateTracker(low_level=15)
        tr.update(plugged=False, level=20)
        self.assertEqual([e.kind for e in tr.update(level=15)], [ev.BATTERY_LOW])
        self.assertEqual(tr.update(level=14), [])  # 既に下回っている
        tr.update(plugged=True, level=30)
        self.assertEqual(tr.update(level=10), [])  # 充電中は出さない

    def test_logcat_source(self):
        lines = ["I/screen_toggled( 1): 0", "noise", "I/battery_status( 1): [2,2,1,1,Li-ion]",
                 "I/screen_toggled( 1): 0"]
        t = FakeTransport(lambda s: DEV, stream_lines=lines)
        tr = ev.StateTracker()
        tr.seed(DeviceProbe(t))  # fixture: 画面 ON・USB 給電中
        self.assertEqual([e.kind for e in ev.LogcatSource(t, tr).events()], [ev.SCREEN_OFF])
        self.assertIn("logcat -b events", t.calls[-1])

    def test_polling_backs_off_when_screen_off(self):
        outputs = [DEV, DEV.replace("mWakefulness=Awake", "mWakefulness=Asleep")]
        t = FakeTransport(lambda s: outputs.pop(0) if len(outputs) > 1 else outputs[0])
        sleeps = []
        tr = ev.StateTracker()
        src = ev.PollingSource(DeviceProbe(t), tr, interval_s=60, screen_off_interval_s=300,
                               sleep=sleeps.append, max_polls=2)
        self.assertEqual([e.kind for e in src.events()], [ev.SCREEN_OFF])
        self.assertEqual(sleeps, [60, 300])


# ---------------------------------------------------------------- parsers

class ParserTest(unittest.TestCase):
    def test_usage_takes_max_and_ignores_epoch(self):
        u = parse_usage(USAGE)
        self.assertEqual(time.strftime("%Y-%m-%d", time.localtime(u["com.old.game"])), "2026-06-01")
        self.assertNotIn("com.new.app", u)

    def test_versions_first_occurrence(self):
        v = parse_versions(VERSIONS)
        self.assertEqual(v["com.android.chrome"], (612300, "120.0.1"))
        self.assertEqual(len(v), 3)

    def test_ps_groups_services(self):
        r = parse_ps(PS)
        self.assertEqual(r["jp.naver.line.android"], 410 + 117)
        self.assertNotIn("NAME", r)

    def test_badging(self):
        self.assertEqual(parse_badging("package: name='com.x.y' versionCode='42' versionName='1.2' platformBuildVersionName=''"),
                         ("com.x.y", 42, "1.2"))


# ---------------------------------------------------------------- tasks

class TaskTest(Base):
    def test_optimize(self):
        rep = run_task("optimize", self.ctx)
        kinds = [(p.kind, p.args.get("package")) for p in rep.proposals]
        self.assertIn(("trim_caches", None), kinds)
        self.assertIn(("kill_bg", "jp.naver.line.android"), kinds)
        self.assertNotIn(("kill_bg", "com.android.chrome"), kinds)   # 前面アプリ
        self.assertNotIn(("kill_bg", "system_server"), kinds)        # ユーザーアプリではない
        self.assertTrue(all(p.risk == "low" for p in rep.proposals))

    def test_unused(self):
        rep = run_task("unused", self.ctx)
        pkgs = [p.args["package"] for p in rep.proposals]
        self.assertEqual(pkgs, ["com.old.game"])  # new.app はインストール直後、termux は除外
        self.assertEqual(rep.proposals[0].risk, "high")
        self.assertIn("137日間未使用", rep.proposals[0].reason)

    def test_unused_refuses_without_usage(self):
        t = FakeTransport(sectioned({**DEVICE_TABLE, "dumpsys usagestats": ""}))
        with self.assertRaises(TaskError):
            run_task("unused", TaskContext(t, self.store, now=NOW))

    def test_updates_diff(self):
        rep = run_task("updates", self.ctx)
        self.assertIn("初回", rep.summary)
        self.assertNotIn("com.android.systemui", self.store.state["inventory"])
        newer = VERSIONS.replace("versionCode=612300", "versionCode=612400").replace("120.0.1", "121.0.0")
        t = FakeTransport(sectioned({**DEVICE_TABLE, "dumpsys package packages": newer}))
        rep2 = run_task("updates", TaskContext(t, Store(self.root / "home"), now=NOW))
        self.assertIn("更新: com.android.chrome 120.0.1 → 121.0.0", rep2.details)


class DupesTest(Base):
    def make(self, rel: str, data: bytes, mtime: float | None = None) -> str:
        p = self.root / "sd" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        if mtime:
            os.utime(p, (mtime, mtime))
        return str(p)

    def setUp(self):
        super().setUp()
        big = os.urandom(200_000)
        self.orig = self.make("DCIM/Camera/IMG_001.jpg", big, NOW - 1000)
        self.copy = self.make("Download/IMG_001 (1).jpg", big, NOW - 10)
        # 同サイズで先頭 64KiB も同じだが末尾が違う → 重複ではない
        self.near = self.make("Pictures/near.jpg", big[:-1] + bytes([big[-1] ^ 1]))
        self.make("Pictures/.thumbnails/t.jpg", big)        # 隠しディレクトリは見ない
        self.make("Documents/small.txt", b"x" * 10)
        self.make("Documents/small2.txt", b"x" * 10)        # min_bytes 未満
        self.store.config.update(dupe_roots=[str(self.root / "sd")], dupe_min_bytes=1000,
                                 trash_dir=str(self.root / "trash"))

    def test_finds_exact_duplicates_only(self):
        groups = dp.find_duplicates(dp.walk([str(self.root / "sd")], 1000))
        self.assertEqual(len(groups), 1)
        self.assertEqual({f.path for f in groups[0].files}, {self.orig, self.copy})
        self.assertEqual(groups[0].wasted, 200_000)

    def test_hash_cache_reused(self):
        cache: dict = {}
        dp.find_duplicates(dp.walk([str(self.root / "sd")], 1000), cache)
        self.assertIn(self.orig, cache)
        cache[self.orig][2] = cache[self.copy][2] = "f" * 64  # キャッシュが使われれば結果に出る
        g = dp.find_duplicates(dp.walk([str(self.root / "sd")], 1000), cache)
        self.assertEqual(g[0].sha256, "f" * 64)

    def test_heuristic_keeps_original(self):
        rep = run_task("dupes", self.ctx)
        self.assertEqual(len(rep.proposals), 1)
        p = rep.proposals[0]
        self.assertEqual(p.args["keep"], self.orig)
        self.assertEqual(p.args["files"], [self.copy])

    def test_llm_judgement_validated(self):
        groups = dp.find_duplicates(dp.walk([str(self.root / "sd")], 1000))
        self.assertEqual(dp.parse_judgement('[{"g":1,"keep":5}]', groups), {})        # 範囲外
        self.assertEqual(dp.parse_judgement('[{"g":9,"keep":0}]', groups), {})        # 存在しないグループ
        self.assertEqual(dp.parse_judgement("はい", groups), {})
        d = dp.parse_judgement('結果: [{"g":1,"keep":null,"reason":"迷う"}]', groups)
        self.assertIsNone(d[0].keep)

    def test_llm_hold_and_fallback(self):
        class LLM:
            def __init__(self, out): self.out = out
            def stream(self, m): yield self.out
        rep = run_task("dupes", TaskContext(self.t, self.store, LLM('[{"g":1,"keep":null,"reason":"迷う"}]'), NOW))
        self.assertEqual(rep.proposals, [])
        self.assertIn("保留", rep.details[0])
        rep = run_task("dupes", TaskContext(self.t, self.store, LLM("壊れた出力"), NOW))
        self.assertEqual(rep.proposals[0].args["keep"], self.orig)  # 既定ルールに戻る
        self.assertIn("heuristic", rep.proposals[0].reason)

    def test_trash_moves_and_verifies(self):
        p = run_task("dupes", self.ctx).proposals[0]
        msg = apply(p, self.ctx)
        self.assertFalse(os.path.exists(self.copy))
        self.assertTrue(os.path.exists(self.orig))
        self.assertIn("1 件", msg)
        moved = list((self.root / "trash").rglob("*.jpg"))
        self.assertEqual(len(moved), 1)

    def test_trash_refuses_when_keep_changed_or_outside_roots(self):
        p = run_task("dupes", self.ctx).proposals[0]
        Path(self.orig).write_bytes(b"changed")
        with self.assertRaises(ApplyError):
            apply(p, self.ctx)
        self.assertTrue(os.path.exists(self.copy))
        bad = Proposal("trash", {"keep": self.orig, "files": ["/etc/hosts"], "sha256": "x"}, "t")
        with self.assertRaises(ApplyError):
            apply(bad, self.ctx)
        same = Proposal("trash", {"keep": self.orig, "files": [self.orig], "sha256": "x"}, "t")
        with self.assertRaises(ApplyError):
            apply(same, self.ctx)


class ApplyTest(Base):
    def test_rejects_bad_or_system_package(self):
        with self.assertRaises(ApplyError):
            apply(Proposal("uninstall", {"package": "com.x; reboot"}, "t"), self.ctx)
        with self.assertRaises(ApplyError):
            apply(Proposal("uninstall", {"package": "com.android.systemui"}, "t"), self.ctx)
        self.assertFalse(any("pm uninstall" in c for c in self.t.calls))

    def test_uninstall_user_app(self):
        apply(Proposal("uninstall", {"package": "com.old.game"}, "t"), self.ctx)
        self.assertIn("pm uninstall --user 0 com.old.game", self.t.calls)

    def test_install_only_from_apk_dir(self):
        self.store.config["apk_dir"] = str(self.root / "apks")
        with self.assertRaises(ApplyError):
            apply(Proposal("install_apk", {"path": "/data/local/tmp/x.apk"}, "t"), self.ctx)

    def test_unknown_kind(self):
        with self.assertRaises(ApplyError):
            apply(Proposal("shell", {"cmd": "reboot"}, "t"), self.ctx)

    def test_proposal_roundtrip(self):
        p = Proposal("kill_bg", {"package": "a.b"}, "t", "low", "r", "optimize")
        self.assertEqual(Proposal.from_dict(p.to_dict()).id, p.id)


# ---------------------------------------------------------------- dispatcher

class DispatcherTest(Base):
    def make(self, rules=None, plugged=True, screen_on=False, level=80):
        tr = ev.StateTracker()
        tr.update(screen_on=screen_on, plugged=plugged, level=level)
        self.notes = []
        kw = {"rules": rules} if rules else {}
        return Dispatcher(self.ctx, tr, notify=lambda a, b: self.notes.append(b), log=lambda s: None, **kw)

    def test_screen_off_runs_optimize_and_saves_pending(self):
        d = self.make()
        reps = d.handle(ev.Event(ev.SCREEN_OFF))
        self.assertEqual([r.task for r in reps], ["optimize"])
        self.assertTrue(self.store.pending())
        self.assertFalse(any("trim-caches" in c for c in self.t.calls))  # 無人では実行しない
        self.assertEqual(d.handle(ev.Event(ev.SCREEN_OFF)), [])          # クールダウン

    def test_auto_apply_low_risk(self):
        self.store.config["auto_apply_low_risk"] = True
        self.make().handle(ev.Event(ev.SCREEN_OFF))
        self.assertTrue(any("pm trim-caches" in c for c in self.t.calls))
        self.assertTrue(any("am kill jp.naver.line.android" in c for c in self.t.calls))
        self.assertEqual(self.store.pending(), [])

    def test_charging_deferred_until_screen_off(self):
        rules = (Rule("m", ev.CHARGING_START, ("updates",), 3600, require_charging=True, idle_only=True),)
        d = self.make(rules, screen_on=True)
        self.assertEqual(d.handle(ev.Event(ev.CHARGING_START)), [])
        self.assertIn("m", d.deferred)
        d.tracker.update(screen_on=False)
        self.assertEqual([r.task for r in d.handle(ev.Event(ev.SCREEN_OFF))], ["updates"])
        self.assertEqual(d.deferred, {})

    def test_deferred_dropped_when_unplugged(self):
        rules = (Rule("m", ev.CHARGING_START, ("updates",), 3600, require_charging=True, idle_only=True),)
        d = self.make(rules, screen_on=True)
        d.handle(ev.Event(ev.CHARGING_START))
        d.tracker.update(plugged=False)
        d.handle(ev.Event(ev.CHARGING_STOP))
        self.assertEqual(d.handle(ev.Event(ev.SCREEN_OFF)), [])

    def test_heavy_skipped_on_low_battery(self):
        rules = (Rule("x", ev.SCREEN_OFF, ("updates", "optimize"), 0),)
        reps = self.make(rules, plugged=False, level=30).handle(ev.Event(ev.SCREEN_OFF))
        self.assertEqual([r.task for r in reps], ["optimize"])

    def test_task_failure_does_not_stop(self):
        self.t.responder = sectioned({**DEVICE_TABLE, "dumpsys usagestats": ""})
        rules = (Rule("x", ev.SCREEN_OFF, ("unused", "optimize"), 0),)
        reps = self.make(rules).handle(ev.Event(ev.SCREEN_OFF))
        self.assertEqual([r.task for r in reps], ["optimize"])

    def test_screen_on_digest(self):
        self.store.add_pending([Proposal("trim_caches", {}, "t").to_dict()])
        self.make(screen_on=False).handle(ev.Event(ev.SCREEN_ON))
        self.assertIn("承認待ちの提案が 1 件あります", self.notes)


if __name__ == "__main__":
    unittest.main()
