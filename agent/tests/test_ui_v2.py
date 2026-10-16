import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
from xml.sax.saxutils import quoteattr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis_agent import __main__ as cli
from jarvis_agent import ui
from jarvis_agent.agent import (Action, Agent, PlanError, ScriptedPlanner, State, load_macro, parse_action)
from jarvis_agent.transport import FakeTransport
from test_events_tasks import DEV


def node(text="", desc="", rid="", cls="android.widget.TextView", pkg="com.android.settings",
         bounds=(0, 0, 100, 100), clickable=True, checkable=False, checked=False, scrollable=False,
         children=""):
    a = {"text": text, "content-desc": desc, "resource-id": rid, "class": cls, "package": pkg,
         "bounds": "[{},{}][{},{}]".format(*bounds), "clickable": str(clickable).lower(),
         "enabled": "true", "checkable": str(checkable).lower(), "checked": str(checked).lower(),
         "scrollable": str(scrollable).lower(), "password": "false"}
    attrs = " ".join(f"{k}={quoteattr(v)}" for k, v in a.items())
    return f"<node {attrs}>{children}</node>"


def screen(*nodes, pkg="com.android.settings", scroll=False):
    # 実際のダンプでは、1 つのウィンドウの要素はすべて同じアプリのパッケージ名を持つ
    children = "".join(nodes).replace('package="com.android.settings"', f'package="{pkg}"')
    root = node(cls="android.widget.FrameLayout", pkg=pkg, bounds=(0, 0, 1080, 2400), clickable=False,
                scrollable=scroll, children=children)
    return f"<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation=\"0\">{root}</hierarchy>"


class Phone:
    """タップ・スワイプのたびに次の画面へ進む擬似端末（screens の最後で止まる）。"""

    def __init__(self, screens, advance_on=("input tap", "input swipe", "input keyevent", "monkey")):
        self.screens = list(screens)
        self.i = 0
        self.advance_on = advance_on
        self.t = FakeTransport(self.respond)

    def respond(self, script):
        if script.startswith(self.advance_on):
            self.i = min(self.i + 1, len(self.screens) - 1)
            return ""
        if "uiautomator" in script:
            return self.screens[self.i]
        return DEV if "@@JARVIS" in script else ""

    def taps(self):
        return [c for c in self.t.calls if c.startswith("input tap")]


def agent(phone, actions, confirm=lambda a: False, **kw):
    return Agent(phone.t, ScriptedPlanner(actions), confirm=confirm, sleep=lambda s: None, **kw)


class MatchTest(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(ui.normalize("Ｗｉ－Ｆｉ　設定"), "wi-fi 設定")

    def test_exact_beats_contains_even_if_bigger(self):
        xml = screen(node("詳細な Wi-Fi 設定", bounds=(0, 0, 100, 50)),
                     node("Wi-Fi を使う", bounds=(0, 60, 100, 110)),
                     node("Wi-Fi", bounds=(0, 200, 1000, 400)),
                     node("", rid="com.x:id/wifi_icon", bounds=(0, 500, 10, 510)))
        hits = ui.find(xml, ["ｗｉ－ｆｉ"])
        self.assertEqual([h.text for h in hits[:3]], ["Wi-Fi", "Wi-Fi を使う", "詳細な Wi-Fi 設定"])
        self.assertEqual(ui.find(xml, ["wifi_icon"])[0].resource_id, "com.x:id/wifi_icon")

    def test_switch_label_hash_and_scroll_note(self):
        on = screen(node("Wi-Fi", cls="android.widget.Switch", checkable=True, checked=True), scroll=True)
        off = on.replace('checked="true"', 'checked="false"')
        s_on, h_on = ui.summarize(on)
        s_off, h_off = ui.summarize(off)
        self.assertIn("スイッチ「Wi-Fi」ON", s_on)
        self.assertIn("スイッチ「Wi-Fi」OFF", s_off)
        self.assertIn("スクロール", s_on)
        self.assertNotEqual(h_on, h_off)  # 切り替えは画面の変化として数える

    def test_screen_package(self):
        self.assertEqual(ui.screen_package(screen(node("x"), pkg="com.a.b")), "com.a.b")


class ScrollWaitTest(unittest.TestCase):
    def test_scroll_swipes_inside_largest_list(self):
        p = Phone([screen(node("a"), scroll=True)])
        self.assertTrue(ui.scroll(p.t, p.screens[0], "down"))
        self.assertEqual(p.t.calls[-1], "input swipe 540 1800 540 600 300")
        self.assertFalse(ui.scroll(p.t, screen(node("a")), "down"))
        with self.assertRaises(ui.UiError):
            ui.scroll(p.t, p.screens[0], "left")

    def test_find_scrolling_finds_below_fold(self):
        s1 = screen(node("ネットワーク"), scroll=True)
        s2 = screen(node("ディスプレイ"), scroll=True)
        s3 = screen(node("システム"), scroll=True)
        p = Phone([s1, s2, s3])
        found, xml = ui.find_scrolling(p.t, s1, ["システム"], sleep=lambda s: None)
        self.assertEqual(found.text, "システム")
        self.assertEqual(sum(c.startswith("input swipe") for c in p.t.calls), 2)

    def test_find_scrolling_stops_at_end_of_list(self):
        s1 = screen(node("a"), scroll=True)
        p = Phone([s1])  # 何度スワイプしても同じ画面 = 一覧の端
        found, _ = ui.find_scrolling(p.t, s1, ["無い"], sleep=lambda s: None)
        self.assertIsNone(found)
        self.assertEqual(sum(c.startswith("input swipe") for c in p.t.calls), 1)

    def test_wait_for(self):
        loading = screen(node("読み込み中"))
        ready = screen(node("完了"))
        seq = [loading, loading, ready]
        t = FakeTransport(lambda s: seq.pop(0) if len(seq) > 1 else seq[0])
        n, _ = ui.wait_for(t, ["完了"], sleep=lambda s: None)
        self.assertEqual(n.text, "完了")
        t2 = FakeTransport(lambda s: loading)
        with self.assertRaises(ui.UiError):
            ui.wait_for(t2, ["完了"], timeout=1, interval=0.5, sleep=lambda s: None)
        self.assertEqual(len(t2.calls), 3)  # 取り直しの回数は timeout / interval + 1


class ActionTest(unittest.TestCase):
    def test_parse_new_actions(self):
        self.assertEqual(parse_action('{"action":"scroll","target":"down"}').type, "scroll")
        self.assertEqual(parse_action('{"action":"toggle","target":"Wi-Fi","text":"on"}').text, "on")
        for bad in ['{"action":"scroll","target":"left"}', '{"action":"toggle","target":"x","text":"maybe"}',
                    '{"action":"toggle","text":"on"}']:
            with self.assertRaises(PlanError, msg=bad):
                parse_action(bad)

    def test_load_macro(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.json"
            p.write_text(json.dumps([{"action": "launch", "target": "com.android.settings", "expect": "Wi-Fi"},
                                     {"action": "toggle", "target": "Wi-Fi", "text": "on"}]))
            steps = load_macro(str(p))
            self.assertEqual([s.type for s in steps], ["launch", "toggle"])
            self.assertEqual(steps[0].expect, "Wi-Fi")
            p.write_text(json.dumps([{"action": "launch", "target": "a.b"}, {"action": "rm"}]))
            with self.assertRaises(PlanError) as cm:
                load_macro(str(p))
            self.assertIn("2 番目", str(cm.exception))
            p.write_text("{}")
            with self.assertRaises(PlanError):
                load_macro(str(p))


class AgentV2Test(unittest.TestCase):
    def test_toggle_skips_when_already_in_state(self):
        on = screen(node("Wi-Fi", cls="android.widget.Switch", checkable=True, checked=True,
                         bounds=(0, 0, 200, 100)))
        p = Phone([on])
        ag = agent(p, [Action("toggle", "Wi-Fi", "on")])
        self.assertEqual(ag.run("x"), State.DONE)
        self.assertEqual(p.taps(), [])
        self.assertIn("既に ON", ag.history[0])
        self.assertNotIn("画面変化なし", ag.history[0])

    def test_toggle_taps_switch_not_label(self):
        off = screen(node("Wi-Fi", bounds=(0, 0, 800, 100)),
                     node("", rid="android:id/switch_widget", cls="android.widget.Switch",
                          checkable=True, bounds=(900, 0, 1000, 100),
                          children=node("Wi-Fi", clickable=False, bounds=(900, 0, 1000, 100))))
        on = off.replace('checked="false"', 'checked="true"', 1)
        p = Phone([off, screen(node("Wi-Fi", cls="android.widget.Switch", checkable=True, checked=True))])
        agent(p, [Action("toggle", "Wi-Fi", "on")]).run("x")
        self.assertEqual(p.taps(), ["input tap 950 50"])

    def test_toggle_prefers_checkable_row_over_smaller_label(self):
        # 設定アプリでは「行全体」がスイッチ（checkable）で、文言は中の小さな TextView にある
        row = node("", cls="android.widget.LinearLayout", checkable=True, bounds=(0, 0, 1080, 150),
                   children=node("Wi-Fi", clickable=False, bounds=(40, 40, 300, 110)))
        p = Phone([screen(row), screen(node("Wi-Fi", checkable=True, checked=True))])
        agent(p, [Action("toggle", "Wi-Fi", "on")]).run("x")
        self.assertEqual(p.taps(), ["input tap 540 75"])

    def test_risk_checked_on_resolved_element(self):
        # プランナーは id で指定したが、押す要素の文言は「削除」
        xml = screen(node("削除", rid="com.x:id/button2", cls="android.widget.Button"))
        p = Phone([xml])
        asked = []
        ag = agent(p, [Action("tap", "button2")], confirm=lambda a: asked.append(a) or False)
        self.assertEqual(ag.run("x"), State.DONE)
        self.assertEqual(len(asked), 1)
        self.assertIn("ボタン「削除」", ag.result)
        self.assertEqual(p.taps(), [])

    def test_tap_scrolls_to_find(self):
        s1 = screen(node("ネットワーク"), scroll=True)
        s2 = screen(node("システム", bounds=(0, 1000, 1080, 1100)), scroll=True)
        s3 = screen(node("システムの詳細"))
        p = Phone([s1, s2, s3])
        ag = agent(p, [Action("tap", "システム")])
        self.assertEqual(ag.run("x"), State.DONE)
        self.assertIn("input tap 540 1050", p.taps())

    def test_scrolled_then_tap_did_nothing_counts_as_no_change(self):
        s1 = screen(node("ネットワーク"), scroll=True)
        s2 = screen(node("システム", bounds=(0, 1000, 1080, 1100)), scroll=True)
        p = Phone([s1, s2], advance_on=("input swipe",))  # タップしても画面は変わらない
        ag = agent(p, [Action("tap", "システム")])
        ag.run("x")
        self.assertIn("画面変化なし", ag.history[0])

    def test_stops_when_screen_never_changes(self):
        xml = screen(node("A"), node("B", bounds=(0, 200, 100, 300)), node("C", bounds=(0, 400, 100, 500)))
        p = Phone([xml])
        ag = agent(p, [Action("tap", "A"), Action("tap", "B"), Action("tap", "C"), Action("tap", "A")])
        self.assertEqual(ag.run("x"), State.ERROR)
        self.assertIn("3 回続けて画面が変わらない", ag.result)
        self.assertEqual(len(p.taps()), 3)

    def test_stops_on_repeated_loop(self):
        s1 = screen(node("X"))
        s2 = screen(node("戻る"))
        p = Phone([s1, s2, s1, s2, s1, s2])
        p.screens = [s1, s2] * 5
        ag = agent(p, [Action("tap", "X"), Action("tap", "戻る")] * 4, max_steps=20)
        self.assertEqual(ag.run("x"), State.ERROR)
        self.assertIn("3 回くり返した", ag.result)
        self.assertTrue(any("2 回目" in h for h in ag.history))

    def test_allowed_packages(self):
        s1 = screen(node("リンク"), pkg="com.android.settings")
        s2 = screen(node("ページ"), pkg="com.android.chrome")
        p = Phone([s1, s2])
        ag = agent(p, [Action("tap", "リンク"), Action("tap", "ページ")], allowed_packages={"com.android.settings"})
        self.assertEqual(ag.run("x"), State.ERROR)
        self.assertIn("com.android.chrome", ag.result)
        self.assertEqual(len(p.taps()), 1)
        # システムのダイアログは許可
        p2 = Phone([s1, screen(node("許可"), pkg="com.android.permissioncontroller")])
        ag2 = agent(p2, [Action("tap", "リンク")], allowed_packages={"com.android.settings"})
        self.assertEqual(ag2.run("x"), State.DONE)

    def test_starting_from_termux_is_fine(self):
        # コマンドを打った Termux が前面のまま始まり、設定を起動して操作する
        p = Phone([screen(node("$"), pkg="com.termux"), screen(node("Wi-Fi"))])
        ag = agent(p, [Action("launch", "com.android.settings"), Action("tap", "Wi-Fi")],
                   allowed_packages={"com.android.settings"})
        self.assertEqual(ag.run("x"), State.DONE, ag.result)
        # 戻るで始めた Termux に戻るのも構わない
        p2 = Phone([screen(node("$"), pkg="com.termux"), screen(node("Wi-Fi")), screen(node("$"), pkg="com.termux")])
        ag2 = agent(p2, [Action("launch", "com.android.settings"), Action("key", "KEYCODE_BACK")],
                    allowed_packages={"com.android.settings"})
        self.assertEqual(ag2.run("x"), State.DONE, ag2.result)

    def test_disallowed_launch_refused_before_launching(self):
        p = Phone([screen(node("a"))])
        ag = agent(p, [Action("launch", "com.android.chrome")], allowed_packages={"com.android.settings"})
        self.assertEqual(ag.run("x"), State.ERROR)
        self.assertFalse(any("monkey" in c for c in p.t.calls))

    def test_expect(self):
        p = Phone([screen(node("ホーム")), screen(node("ネットワーク"))])
        ag = agent(p, [Action("tap", "ホーム", expect="ネットワーク")])
        self.assertEqual(ag.run("x"), State.DONE)
        p2 = Phone([screen(node("ホーム")), screen(node("別の画面"))])
        ag2 = agent(p2, [Action("tap", "ホーム", expect="ネットワーク")])
        self.assertEqual(ag2.run("x"), State.ERROR)
        self.assertIn("現れませんでした", ag2.result)

    def test_settle_dumps(self):
        p = Phone([screen(node("A")), screen(node("B"))])
        ag = agent(p, [])
        before = ui.summarize(p.screens[0])[1]
        p.i = 1
        n = len(p.t.calls)
        xml, changed = ag.settle(before)
        self.assertTrue(changed)
        self.assertEqual(len(p.t.calls) - n, 2)  # 変化 → もう一度同じ → 落ち着いた
        p.i = 0
        n = len(p.t.calls)
        _, changed = ag.settle(before)
        self.assertFalse(changed)
        self.assertEqual(len(p.t.calls) - n, 3)  # 変化しないなら最後まで待つ

    def test_timings_recorded(self):
        p = Phone([screen(node("A")), screen(node("B"))])
        ag = agent(p, [Action("tap", "A")])
        ag.run("x")
        timed = [e for e in ag.log if e.state in (State.ACT, State.VERIFY)]
        self.assertEqual(len(timed), 2)
        self.assertTrue(all(e.ms is not None and e.ms >= 0 for e in timed))


class CliTest(unittest.TestCase):
    def test_run_macro_with_app_guard(self):
        p = Phone([screen(node("ホーム"), pkg="com.google.android.apps.nexuslauncher"),
                   screen(node("ネットワーク"), pkg="com.android.settings"),
                   screen(node("Wi-Fi"), pkg="com.android.settings")])
        with tempfile.TemporaryDirectory() as d:
            m = Path(d) / "m.json"
            m.write_text(json.dumps([{"action": "launch", "target": "com.android.settings"},
                                     {"action": "tap", "target": "ネットワーク", "expect": "Wi-Fi"}]))
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(cli, "make_transport", lambda *a: p.t), redirect_stdout(out), redirect_stderr(err):
                rc = cli.main(["run", "--macro", str(m)])
            self.assertEqual(rc, 0, err.getvalue())
            self.assertIn("ms)", err.getvalue())
            with mock.patch.object(cli, "make_transport", lambda *a: p.t), redirect_stdout(io.StringIO()), \
                 redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["run"]), 2)
                self.assertEqual(cli.main(["run", "x", "--app", "bad;pkg"]), 2)
                m.write_text("[1]")
                self.assertEqual(cli.main(["run", "--macro", str(m)]), 1)


if __name__ == "__main__":
    unittest.main()
