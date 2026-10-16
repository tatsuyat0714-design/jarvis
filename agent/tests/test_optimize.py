import io
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis_agent import __main__ as cli
from jarvis_agent.store import Store
from jarvis_agent.tasks import (ApplyError, TaskContext, apply, parse_buckets, parse_component_pkg,
                                parse_diskstats, run_task, unrestrict)
from jarvis_agent.transport import FakeTransport
from test_events_tasks import NOW, sectioned

HOME = "com.google.android.apps.nexuslauncher/.NexusLauncherActivity"
IME = "com.google.android.inputmethod.latin/com.android.inputmethod.latin.LatinIME"
USERS = ["com.hog.game", "com.small.app", "com.google.android.apps.nexuslauncher",
         "com.google.android.inputmethod.latin", "com.termux", "com.android.chrome"]
MB = 1024 * 1024


def ps(rows: dict[str, int]) -> str:
    return "RSS NAME\n" + "\n".join(f"{mb * 1024} {name}" for name, mb in rows.items())


def meminfo(avail_mb: int, total_mb: int = 8000) -> str:
    return f"MemTotal: {total_mb * 1024} kB\nMemFree: 1 kB\nMemAvailable: {avail_mb * 1024} kB"


def diskstats(cache: dict[str, int]) -> str:
    names = ",".join(f'"{p}"' for p in cache)
    sizes = ",".join(str(v * MB) for v in cache.values())
    return f"App Cache Size: {sum(cache.values()) * MB}\nPackage Names: [{names}]\nCache Sizes: [{sizes}]"


class Phone:
    """optimize 用の擬似端末。バケットは am set-standby-bucket で変わる。"""

    def __init__(self, **kw):
        self.sdk = kw.get("sdk", 34)
        self.avail = kw.get("avail", 4000)
        self.used_pct = kw.get("used_pct", 50)
        self.cache = kw.get("cache", {"com.android.chrome": 100})
        self.rss = kw.get("rss", {"com.hog.game": 600, "com.hog.game:sync": 100, "com.small.app": 80,
                                  "com.google.android.apps.nexuslauncher": 700,
                                  "com.google.android.inputmethod.latin": 400, "com.termux": 500,
                                  "com.android.chrome": 900, "system_server": 1200})
        self.fg = kw.get("fg", "com.android.chrome")
        self.last_used = kw.get("last_used", {"com.hog.game": "2026-09-01 10:00:00"})
        self.buckets = kw.get("buckets", {"com.hog.game": 10})
        self.set_ok = kw.get("set_ok", True)
        self.t = FakeTransport(self.respond)

    def respond(self, script: str) -> str:
        m = None
        if script.startswith("am get-standby-bucket "):
            return str(self.buckets.get(script.split()[-1], 10))
        if script.startswith("am set-standby-bucket "):
            _, _, pkg, val = script.split()
            if self.set_ok:
                self.buckets[pkg] = 45 if val == "restricted" else int(val)
            return ""
        if script.startswith("pm list packages -3"):
            return "\n".join(f"package:{p}" for p in USERS)
        usage = "\n".join(f'package={p} totalTimeUsed="01:00" lastTimeUsed="{t}"' for p, t in self.last_used.items())
        total = 100_000_000
        table = {
            "df /data": f"/dev/block/dm-1 {total} {total * self.used_pct // 100} {total - total * self.used_pct // 100} {self.used_pct}% /data",
            "ps -A": ps(self.rss),
            "pm list packages -3": "\n".join(f"package:{p}" for p in USERS),
            "dumpsys activity": f"  topResumedActivity=ActivityRecord{{1 u0 {self.fg}/.Main t1}}",
            "/proc/meminfo": meminfo(self.avail),
            "getprop ro.build.version.sdk": str(self.sdk),
            "resolve-activity": HOME,
            "default_input_method": IME,
            "dumpsys diskstats": diskstats(self.cache),
            "dumpsys usagestats": usage,
            "am get-standby-bucket": "\n".join(f"{p}={self.buckets.get(p, 10)}" for p in USERS),
        }
        if "@@JARVIS:" in script and "for p in" in script:
            # バケット取得のループは、対象パッケージだけを返す
            pk = script.split("for p in ", 1)[1].split(";", 1)[0].split()
            table["am get-standby-bucket"] = "\n".join(f"{p}={self.buckets.get(p, 10)}" for p in pk)
        return sectioned(table)(script)


class ParseTest(unittest.TestCase):
    def test_diskstats(self):
        total, per = parse_diskstats(diskstats({"a.b": 300, "c.d": 50}))
        self.assertEqual(total, 350 * MB)
        self.assertEqual(per, {"a.b": 300 * MB, "c.d": 50 * MB})
        self.assertEqual(parse_diskstats(""), (None, {}))
        # 名前とサイズの数が合わないときは個別の内訳を使わない
        self.assertEqual(parse_diskstats('Package Names: ["a.b"]\nCache Sizes: [1,2]')[1], {})

    def test_component_and_buckets(self):
        self.assertEqual(parse_component_pkg(HOME), "com.google.android.apps.nexuslauncher")
        self.assertEqual(parse_component_pkg(IME), "com.google.android.inputmethod.latin")
        self.assertIsNone(parse_component_pkg("null"))
        self.assertEqual(parse_buckets("a.b=40\nc.d=Bucket 45\nbad;rm=1\n"), {"a.b": 40, "c.d": 45})


class OptimizeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "home")

    def tearDown(self):
        self.tmp.cleanup()

    def run_opt(self, phone: Phone, now: float = NOW):
        return run_task("optimize", TaskContext(phone.t, self.store, now=now))

    def kinds(self, rep):
        return [(p.kind, p.args.get("package")) for p in rep.proposals]

    def test_quiet_when_nothing_to_do(self):
        rep = self.run_opt(Phone())
        self.assertEqual(rep.proposals, [])
        self.assertIn("キャッシュ削除は不要", rep.summary)
        self.assertIn("裏で使用中: com.hog.game 700MB", rep.details)  # pkg:service はまとめる

    def test_cache_proposed_when_large_or_storage_low(self):
        rep = self.run_opt(Phone(cache={"com.android.chrome": 400, "com.hog.game": 300}))
        self.assertIn(("trim_caches", None), self.kinds(rep))
        self.assertIn("キャッシュ: com.android.chrome 400MB", rep.details)
        rep = self.run_opt(Phone(used_pct=92))
        self.assertIn("ストレージ使用率 92%", rep.proposals[0].reason)

    def test_pressure_kills_heavy_but_never_launcher_ime_termux_fg(self):
        rep = self.run_opt(Phone(avail=500))
        self.assertEqual(self.kinds(rep), [("kill_bg", "com.hog.game")])
        self.assertIn("メモリ不足", rep.proposals[0].reason)
        self.assertIn("不足気味", rep.details[0])

    def test_optimize_ignore(self):
        self.store.config["optimize_ignore"] = ["com.hog.game"]
        self.assertEqual(self.run_opt(Phone(avail=500)).proposals, [])

    def test_chronic_after_repeated_samples_then_restrict(self):
        phone = Phone()
        for i in range(2):
            self.assertEqual(self.run_opt(phone, NOW + i * 3600).proposals, [])
        rep = self.run_opt(phone, NOW + 2 * 3600)
        self.assertIn(("kill_bg", "com.hog.game"), self.kinds(rep))
        self.assertIn(("restrict_bg", "com.hog.game"), self.kinds(rep))
        restrict = [p for p in rep.proposals if p.kind == "restrict_bg"][0]
        self.assertEqual(restrict.risk, "high")
        self.assertIn("45日間開いていない", restrict.reason)

    def test_samples_expire(self):
        phone = Phone()
        self.run_opt(phone, NOW)
        self.run_opt(phone, NOW + 3600)
        # 3 日の窓の外になってからの 3 回目は「常習」にならない
        rep = self.run_opt(phone, NOW + 5 * 86400)
        self.assertEqual(rep.proposals, [])
        self.assertEqual(len(self.store.state["bg_samples"]["com.hog.game"]), 1)

    def chronic(self, phone):
        for i in range(3):
            rep = self.run_opt(phone, NOW + i * 3600)
        return self.kinds(rep)

    def test_no_restrict_when_recently_used_old_android_or_no_usage(self):
        self.assertNotIn(("restrict_bg", "com.hog.game"),
                         self.chronic(Phone(last_used={"com.hog.game": "2026-10-15 10:00:00"})))
        self.store.state.clear()
        self.assertNotIn(("restrict_bg", "com.hog.game"), self.chronic(Phone(sdk=30)))
        self.store.state.clear()
        self.assertNotIn(("restrict_bg", "com.hog.game"), self.chronic(Phone(last_used={})))
        self.store.state.clear()
        self.assertNotIn(("restrict_bg", "com.hog.game"), self.chronic(Phone(buckets={"com.hog.game": 45})))


class RestrictApplyTest(OptimizeTest):
    def proposal(self, phone):
        for i in range(3):
            rep = self.run_opt(phone, NOW + i * 3600)
        return [p for p in rep.proposals if p.kind == "restrict_bg"][0]

    def test_restrict_and_undo(self):
        phone = Phone(buckets={"com.hog.game": 30})
        ctx = TaskContext(phone.t, self.store, now=NOW)
        msg = apply(self.proposal(phone), ctx)
        self.assertIn("restricted undo com.hog.game", msg)
        self.assertEqual(phone.buckets["com.hog.game"], 45)
        self.assertEqual(self.store.state["restricted"]["com.hog.game"]["prev"], 30)
        # 制限中は再提案しない
        self.assertNotIn(("restrict_bg", "com.hog.game"), self.kinds(self.run_opt(phone, NOW + 4 * 3600)))
        self.assertIn("制限を解除しました", unrestrict("com.hog.game", ctx))
        self.assertEqual(phone.buckets["com.hog.game"], 30)
        self.assertNotIn("com.hog.game", self.store.state["restricted"])

    def test_undo_after_user_opened_app(self):
        phone = Phone()
        ctx = TaskContext(phone.t, self.store, now=NOW)
        apply(self.proposal(phone), ctx)
        phone.buckets["com.hog.game"] = 10  # 利用者がアプリを開くとシステムがバケットを上げる
        n = len(phone.t.calls)
        self.assertIn("既に制限が外れていました", unrestrict("com.hog.game", ctx))
        self.assertFalse(any(c.startswith("am set-standby-bucket") for c in phone.t.calls[n:]))

    def test_refusals(self):
        phone = Phone(set_ok=False)
        ctx = TaskContext(phone.t, self.store, now=NOW)
        p = self.proposal(phone)
        with self.assertRaises(ApplyError):
            apply(p, ctx)  # 設定できなかったら記録しない
        self.assertNotIn("com.hog.game", self.store.state.get("restricted", {}))
        with self.assertRaises(ApplyError):
            unrestrict("com.not.ours", ctx)
        p.args["package"] = "com.android.systemui"
        with self.assertRaises(ApplyError):
            apply(p, ctx)  # ユーザーアプリでなければ触らない

    def test_cli_list_and_undo_all(self):
        phone = Phone()
        apply(self.proposal(phone), TaskContext(phone.t, self.store, now=NOW))
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"JARVIS_HOME": str(self.store.root)}), \
             mock.patch.object(cli, "make_transport", lambda *a: phone.t), redirect_stdout(out):
            self.assertEqual(cli.main(["restricted"]), 0)
            self.assertEqual(cli.main(["restricted", "undo", "all"]), 0)
        self.assertIn("com.hog.game", out.getvalue())
        self.assertIn("制限を解除しました", out.getvalue())
        self.assertEqual(Store(self.store.root).state["restricted"], {})


if __name__ == "__main__":
    unittest.main()
