import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis_agent import __main__ as cli
from jarvis_agent.store import Store
from jarvis_agent.tasks import (ApplyError, Proposal, TaskContext, apply, enable_app, parse_app_sizes,
                                parse_integrated, parse_usage, run_task)
from jarvis_agent.transport import FakeTransport
from test_events_tasks import NOW, sectioned

OLD = "2026-06-01 10:00:00"      # 137 日前
RECENT = "2026-10-15 10:00:00"   # 前日
GB = 1024 ** 3

INTEGRATED_APPS = ["com.sms.app", "com.kb.app", "com.a11y.app", "com.widget.app", "com.admin.app",
                   "com.vpn.app", "com.fill.app", "com.wall.app", "com.notif.app"]


class Phone:
    def __init__(self, **kw):
        self.users = kw.get("users", ["com.old.game", "com.old.tool", "com.component.only", "com.new.app",
                                      "com.termux", *INTEGRATED_APPS])
        self.disabled = set(kw.get("disabled", []))
        self.disable_ok = kw.get("disable_ok", True)
        self.sms_holder = kw.get("sms_holder", "com.sms.app")
        self.usage = kw.get("usage", {p: OLD for p in self.users})
        self.usage.setdefault("com.component.only", OLD)
        self.sizes = kw.get("sizes", {"com.old.game": (2 * GB, GB // 2), "com.old.tool": (50 << 20, 10 << 20)})
        self.t = FakeTransport(self.respond)

    def respond(self, script: str) -> str:
        if script.startswith("pm disable-user"):
            if self.disable_ok:
                self.disabled.add(script.split()[-1])
            return f"Package {script.split()[-1]} new state: disabled-user"
        if script.startswith("pm enable"):
            self.disabled.discard(script.split()[-1])
            return ""
        lines = []
        for p in self.users:
            extra = ' lastTimeComponentUsed="%s"' % RECENT if p == "com.component.only" else ""
            lines.append(f'  package={p} totalTimeUsed="00:10" lastTimeUsed="{self.usage.get(p, OLD)}" '
                         f'totalTimeVisible="00:10" lastTimeVisible="{self.usage.get(p, OLD)}"{extra}')
        names = list(self.sizes)
        disk = (f'Package Names: [{",".join(chr(34) + n + chr(34) for n in names)}]\n'
                f'App Sizes: [{",".join(str(a) for a, _ in self.sizes.values())}]\n'
                f'App Data Sizes: [{",".join(str(d) for _, d in self.sizes.values())}]')
        table = {
            "pm list packages -3 -d": "\n".join(f"package:{p}" for p in sorted(self.disabled)),
            "pm list packages -3": "\n".join(f"package:{p}" for p in self.users),
            "dumpsys usagestats": "\n".join(lines),
            "dumpsys diskstats": disk,
            "cmd role get-role-holders": f"com.google.android.apps.nexuslauncher\n{self.sms_holder}",
            "settings get secure": ("com.kb.app/.Ime\ncom.kb.app/.Ime:com.google.android.inputmethod.latin/"
                                    "com.android.inputmethod.latin.LatinIME\ncom.a11y.app/com.a11y.app.Svc\n"
                                    "com.notif.app/.Listener\ncom.fill.app/.Autofill\ncom.vpn.app"),
            "dumpsys device_policy": "com.admin.app/.AdminReceiver",
            "dumpsys appwidget": "ComponentInfo{com.widget.app",
            "dumpsys wallpaper": "mWallpaperComponent=ComponentInfo{com.wall.app/com.wall.app.LiveWall}",
            "dumpsys package com.new.app": "    firstInstallTime=2026-10-15 09:00:00",
            "firstInstallTime": "    firstInstallTime=2025-01-01 10:00:00",
            "pm uninstall": "Success",
        }
        return sectioned(table)(script)


class ParseTest(unittest.TestCase):
    def test_usage_takes_latest_of_used_visible_component(self):
        u = parse_usage(f'  package=a.b totalTimeUsed="0" lastTimeUsed="{OLD}" lastTimeVisible="{OLD}" '
                        f'lastTimeComponentUsed="{RECENT}"\n  package=c.d totalTimeUsed="0" lastTimeUsed="{OLD}"')
        self.assertGreater(u["a.b"], u["c.d"])

    def test_app_sizes(self):
        self.assertEqual(parse_app_sizes('Package Names: ["a.b"]\nApp Sizes: [10]\nApp Data Sizes: [5]'), {"a.b": 15})
        self.assertEqual(parse_app_sizes('Package Names: ["a.b"]\nApp Sizes: [10,1]\nApp Data Sizes: [5]'), {})

    def test_integrated(self):
        got = parse_integrated({"roles": "com.sms.app\n", "settings": "com.kb.app/.Ime:com.x.y/.Z\nnull\ncom.vpn.app",
                                "widgets": "ComponentInfo{com.widget.app",
                                "wallpaper": "mWallpaperComponent=ComponentInfo{com.wall.app/com.wall.app.L}"})
        self.assertTrue({"com.sms.app", "com.kb.app", "com.x.y", "com.vpn.app", "com.widget.app",
                         "com.wall.app"} <= got)
        self.assertNotIn("null", got)


class UnusedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "home")

    def tearDown(self):
        self.tmp.cleanup()

    def run_unused(self, phone, now=NOW):
        return run_task("unused", TaskContext(phone.t, self.store, now=now))

    def test_candidates_exclude_integrated_component_used_new_and_ignored(self):
        rep = self.run_unused(Phone())
        self.assertEqual([(p.kind, p.args["package"]) for p in rep.proposals],
                         [("disable_app", "com.old.game"), ("disable_app", "com.old.tool")])  # 大きい順
        self.assertIn("2.5GB", rep.proposals[0].reason)
        self.assertIn("disabled undo", rep.proposals[0].reason)
        self.assertIn("合計 2.6GB", rep.summary)
        joined = "\n".join(rep.details)
        for p in INTEGRATED_APPS:
            self.assertIn(p, joined)  # 対象外として報告される
        self.assertNotIn("com.component.only:", joined)  # ウィジェット等から使われている

    def test_sorted_by_size_not_name(self):
        rep = self.run_unused(Phone(sizes={"com.old.game": (10 << 20, 0), "com.old.tool": (3 * GB, 0)}))
        self.assertEqual([p.args["package"] for p in rep.proposals], ["com.old.tool", "com.old.game"])

    def test_uninstall_action_config(self):
        self.store.config["unused_action"] = "uninstall"
        rep = self.run_unused(Phone())
        self.assertEqual({p.kind for p in rep.proposals}, {"uninstall"})

    def test_staged_uninstall_after_disabled_period(self):
        self.store.state["disabled"] = {"com.old.game": {"t": NOW - 40 * 86400},
                                        "com.old.tool": {"t": NOW - 5 * 86400}}
        rep = self.run_unused(Phone(disabled=["com.old.game", "com.old.tool"]))
        self.assertEqual([(p.kind, p.args["package"]) for p in rep.proposals], [("uninstall", "com.old.game")])
        self.assertIn("無効化して 40 日", rep.proposals[0].reason)
        self.assertIn("無効化中 2 件", rep.summary)

    def test_reenabled_by_user_forgets_record(self):
        self.store.state["disabled"] = {"com.old.game": {"t": NOW - 40 * 86400}}
        rep = self.run_unused(Phone(disabled=[]))
        self.assertNotIn("com.old.game", self.store.state["disabled"])
        self.assertIn(("disable_app", "com.old.game"), [(p.kind, p.args["package"]) for p in rep.proposals])

    def test_disabled_by_user_not_touched(self):
        rep = self.run_unused(Phone(disabled=["com.old.game"]))
        self.assertNotIn("com.old.game", [p.args["package"] for p in rep.proposals])


class ApplyTest(UnusedTest):
    def test_disable_and_undo(self):
        phone = Phone()
        ctx = TaskContext(phone.t, self.store, now=NOW)
        p = self.run_unused(phone).proposals[0]
        self.assertIn("disabled undo com.old.game", apply(p, ctx))
        self.assertIn("com.old.game", phone.disabled)
        self.assertEqual(self.store.state["disabled"]["com.old.game"]["t"], NOW)
        self.assertIn("有効に戻しました", enable_app("com.old.game", ctx))
        self.assertNotIn("com.old.game", phone.disabled)
        with self.assertRaises(ApplyError):
            enable_app("com.old.game", ctx)  # 記録が無いものは触らない

    def test_refuse_when_became_integrated_after_proposal(self):
        phone = Phone()
        p = self.run_unused(phone).proposals[0]
        phone.sms_holder = "com.old.game"  # 提案後に既定の SMS アプリにされた
        for kind in ("disable_app", "uninstall"):
            with self.assertRaises(ApplyError, msg=kind) as cm:
                apply(Proposal(kind, p.args, "t"), TaskContext(phone.t, self.store, now=NOW))
            self.assertIn("端末の機能", str(cm.exception))
        self.assertFalse(any(c.startswith(("pm disable-user", "pm uninstall")) for c in phone.t.calls))

    def test_refuse_when_disable_fails_or_already_disabled(self):
        phone = Phone(disable_ok=False)
        ctx = TaskContext(phone.t, self.store, now=NOW)
        p = self.run_unused(phone).proposals[0]
        with self.assertRaises(ApplyError):
            apply(p, ctx)
        self.assertNotIn("com.old.game", self.store.state.get("disabled", {}))
        phone.disabled.add("com.old.game")
        with self.assertRaises(ApplyError):
            apply(p, ctx)

    def test_refuse_ignored(self):
        with self.assertRaises(ApplyError):
            apply(Proposal("disable_app", {"package": "com.termux"}, "t"),
                  TaskContext(Phone().t, self.store, now=NOW))


class DismissTest(UnusedTest):
    def test_dismissed_not_readded_until_expiry(self):
        item = Proposal("disable_app", {"package": "com.old.game"}, "t").to_dict()
        self.assertEqual(self.store.add_pending([item], NOW), 1)
        self.store.dismiss(item["id"], NOW)
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.store.add_pending([item], NOW + 86400), 0)
        self.assertEqual(self.store.add_pending([item], NOW + 91 * 86400), 1)

    def test_cli_dismiss_and_disabled(self):
        phone = Phone()
        ctx = TaskContext(phone.t, self.store, now=NOW)
        props = self.run_unused(phone).proposals
        self.store.add_pending([p.to_dict() for p in props])
        apply(props[0], ctx)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"JARVIS_HOME": str(self.store.root)}), \
             mock.patch.object(cli, "make_transport", lambda *a: phone.t), redirect_stdout(out):
            self.assertEqual(cli.main(["pending", "--dismiss", props[1].id]), 0)
            self.assertEqual(cli.main(["pending", "--dismiss", "nope"]), 1)
            self.assertEqual(cli.main(["disabled"]), 0)
            self.assertEqual(cli.main(["disabled", "undo", "all"]), 0)
        text = out.getvalue()
        self.assertIn("見送りました: com.old.tool を無効化", text)
        self.assertIn("com.old.game  ", text)
        self.assertIn("有効に戻しました", text)
        st = Store(self.store.root)
        self.assertIn(props[1].id, st.state["dismissed"])
        self.assertNotIn(props[1].id, [p["id"] for p in st.pending()])


if __name__ == "__main__":
    unittest.main()
