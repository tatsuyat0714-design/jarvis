import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis_agent import ui
from jarvis_agent.agent import Action, Agent, LLMPlanner, PlanError, ScriptedPlanner, State, parse_action
from jarvis_agent.transport import FakeTransport

FIX = Path(__file__).parent / "fixtures"
XML = (FIX / "settings_storage.xml").read_text()
DEV = (FIX / "pixel_a14.txt").read_text()


class UiParserTest(unittest.TestCase):
    def test_find_keyword_and_center(self):
        hits = ui.find(XML, ["キャッシュ"])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].center, (810, 680))
        self.assertTrue(hits[0].clickable)

    def test_matches_desc_resource_id_and_case(self):
        self.assertEqual(ui.find(XML, ["戻る"])[0].cls, "android.widget.ImageButton")
        self.assertEqual(len(ui.find(XML, ["FORCE_STOP"])), 1)

    def test_filters(self):
        self.assertEqual([n.resource_id.split("/")[-1] for n in ui.find(XML, ["settings"], editable=True)],
                         ["pin", "search_action_bar"])
        # 文言の無い押せるコンテナは子の文言で見つかる
        self.assertEqual(ui.find(XML, ["ストレージを消去"], clickable=True)[0].resource_id,
                         "com.android.settings:id/button1")
        self.assertEqual(ui.find(XML, ["設定を検索"])[0].label(), "入力欄「設定を検索」")

    def test_zero_area_nodes_skipped(self):
        self.assertEqual(ui.find(XML, ["hidden"]), [])

    def test_password_text_never_exposed(self):
        pin = [n for n in ui.iter_nodes(XML) if n.password][0]
        self.assertEqual(pin.text, "")
        self.assertEqual(ui.find(XML, ["secret123"]), [])
        screen, _ = ui.summarize(XML)
        self.assertNotIn("secret123", screen)

    def test_summarize(self):
        screen, h = ui.summarize(XML)
        self.assertTrue(screen.startswith("[screen] com.android.settings"))
        self.assertIn("ボタン「キャッシュを削除」", screen)
        self.assertIn("ボタン「ストレージを消去」", screen)
        self.assertNotIn("強制停止", screen)  # enabled=false
        self.assertEqual(len(h), 16)
        self.assertNotEqual(h, ui.summarize(XML.replace("Chrome", "LINE"))[1])

    def test_broken_xml(self):
        with self.assertRaises(ui.UiError):
            list(ui.iter_nodes("<hierarchy><node"))

    def test_dump_strips_noise(self):
        t = FakeTransport(lambda s: "UI hierchary dumped to: /sdcard/x\n" + XML)
        self.assertTrue(ui.dump(t).startswith("<?xml"))
        with self.assertRaises(ui.UiError):
            ui.dump(FakeTransport(lambda s: "ERROR: null root node"))

    def test_input_escaping(self):
        self.assertEqual(ui.encode_input_text("hello world"), "hello%sworld")
        self.assertEqual(ui.encode_input_text("a;rm -rf /"), "'a;rm%s-rf%s/'")
        with self.assertRaises(ui.UiError):
            ui.encode_input_text("日本語")

    def test_type_refuses_password_and_non_editable(self):
        t = FakeTransport(lambda s: "")
        pin = [n for n in ui.iter_nodes(XML) if n.password][0]
        with self.assertRaises(ui.UiError):
            ui.type_text(t, pin, "1234")
        with self.assertRaises(ui.UiError):
            ui.type_text(t, ui.find(XML, ["キャッシュ"])[0], "x")
        self.assertEqual(t.calls, [])

    def test_launch_and_key_validation(self):
        t = FakeTransport(lambda s: "")
        with self.assertRaises(ui.UiError):
            ui.launch(t, "com.x; reboot")
        with self.assertRaises(ui.UiError):
            ui.key(t, "KEYCODE_BACK; reboot")
        ui.launch(t, "com.android.settings")
        self.assertIn("monkey -p com.android.settings", t.calls[0])


class ParseActionTest(unittest.TestCase):
    def test_extracts_json_from_chatter(self):
        a = parse_action('はい。{"action": "tap", "target": "戻る", "reason": "前の画面へ"} 以上')
        self.assertEqual((a.type, a.target), ("tap", "戻る"))

    def test_rejects(self):
        for raw in ["no json", '{"action": "shell", "target": "reboot"}', '{"action": "tap"}',
                    '{"action": "key", "target": "KEYCODE_POWER"}', "{bad json}"]:
            with self.assertRaises(PlanError, msg=raw):
                parse_action(raw)

    def test_risky(self):
        self.assertTrue(Action("tap", "キャッシュを削除").risky)
        self.assertTrue(Action("tap", "Uninstall").risky)
        self.assertFalse(Action("tap", "戻る").risky)


class Screens:
    """タップ回数で画面が変わる擬似端末。"""

    def __init__(self, screens):
        self.screens = screens
        self.i = 0
        self.t = FakeTransport(self.respond)

    def respond(self, script):
        if script.startswith("input tap") or script.startswith("input keyevent"):
            self.i = min(self.i + 1, len(self.screens) - 1)
            return ""
        if "uiautomator" in script:
            return self.screens[self.i]
        return DEV if "@@JARVIS" in script else ""


def make_agent(dev, actions, confirm=lambda a: False, **kw):
    return Agent(dev.t, ScriptedPlanner(actions), confirm=confirm, sleep=lambda s: None, **kw)


class AgentTest(unittest.TestCase):
    def test_happy_path_states(self):
        dev = Screens([XML, XML.replace("Chrome", "LINE")])
        ag = make_agent(dev, [Action("tap", "戻る")])
        self.assertEqual(ag.run("戻る"), State.DONE)
        self.assertEqual([e.state for e in ag.log],
                         [State.OBSERVE, State.PLAN, State.ACT, State.VERIFY, State.OBSERVE, State.PLAN, State.DONE])
        self.assertIn("input tap 70 115", dev.t.calls)
        self.assertEqual(ag.history, ["「戻る」をタップ"])

    def test_dump_reused_between_verify_and_observe(self):
        dev = Screens([XML, XML.replace("Chrome", "LINE")])
        make_agent(dev, [Action("tap", "戻る")]).run("x")
        # 最初の観測 1 回 + 操作後の安定確認 2 回（変化した → もう一度取って同じなら落ち着いた）。
        # 次の OBSERVE は安定確認のダンプを流用するので増えない
        self.assertEqual(sum("uiautomator" in c for c in dev.t.calls), 3)

    def test_prefers_clickable_parent_over_label(self):
        dev = Screens([XML, XML])
        make_agent(dev, [Action("tap", "ストレージを消去")], confirm=lambda a: True).run("x")
        self.assertIn("input tap 270 680", dev.t.calls)

    def test_risky_denied_does_not_act(self):
        dev = Screens([XML, XML])
        ag = make_agent(dev, [Action("tap", "キャッシュを削除")])
        self.assertEqual(ag.run("キャッシュ削除"), State.DONE)
        self.assertIn(State.CONFIRM, [e.state for e in ag.log])
        self.assertFalse(any(c.startswith("input") for c in dev.t.calls))
        self.assertIn("拒否", ag.result)

    def test_risky_approved_acts(self):
        dev = Screens([XML, XML])
        seen = []
        ag = make_agent(dev, [Action("tap", "キャッシュを削除")], confirm=lambda a: seen.append(a) or True)
        self.assertEqual(ag.run("x"), State.DONE)
        self.assertEqual(len(seen), 1)
        self.assertIn("input tap 810 680", dev.t.calls)

    def test_missing_target_and_disabled_are_errors(self):
        for target in ["存在しない", "強制停止"]:
            ag = make_agent(Screens([XML]), [Action("tap", target)])
            self.assertEqual(ag.run("x"), State.ERROR, target)

    def test_unchanged_screen_noted(self):
        ag = make_agent(Screens([XML]), [Action("key", "KEYCODE_BACK")])
        ag.run("x")
        self.assertIn("画面変化なし", ag.history[0])

    def test_max_steps(self):
        ag = make_agent(Screens([XML]), [Action("wait")] * 10, max_steps=3)
        self.assertEqual(ag.run("x"), State.ERROR)


class FakeLLM:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.seen = []

    def stream(self, messages):
        self.seen.append(messages)
        yield self.outputs.pop(0)


class LLMPlannerTest(unittest.TestCase):
    def test_retry_on_bad_format(self):
        llm = FakeLLM(["わかりました", '{"action": "done", "reason": "完了"}'])
        dev = Screens([XML])
        ag = Agent(dev.t, LLMPlanner(llm), sleep=lambda s: None)
        self.assertEqual(ag.run("x"), State.DONE)
        self.assertEqual(len(llm.seen), 2)
        prompt = llm.seen[0][1]["content"]
        self.assertIn("[device]", prompt)
        self.assertIn("[screen] com.android.settings", prompt)
        self.assertNotIn("secret123", prompt)

    def test_gives_up(self):
        ag = Agent(Screens([XML]).t, LLMPlanner(FakeLLM(["x", "y"])), sleep=lambda s: None)
        self.assertEqual(ag.run("x"), State.ERROR)


if __name__ == "__main__":
    unittest.main()
