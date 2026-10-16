import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis_agent import device
from jarvis_agent.context import build_messages, render
from jarvis_agent.device import DeviceProbe, build_script, split_sections
from jarvis_agent.llm import LLMError, LocalLLM
from jarvis_agent.transport import FakeTransport

FIXTURE = (Path(__file__).parent / "fixtures" / "pixel_a14.txt").read_text()


class ParserTest(unittest.TestCase):
    def setUp(self):
        self.snap = DeviceProbe(FakeTransport(lambda s: FIXTURE)).snapshot()

    def test_no_errors(self):
        self.assertEqual(self.snap.errors, {})

    def test_values(self):
        g = self.snap.get
        self.assertEqual(g("props", "model"), "Pixel 7")
        self.assertEqual(g("props", "sdk"), 34)
        self.assertEqual(g("battery", "level"), 82)
        self.assertEqual(g("battery", "status"), "charging")
        self.assertEqual(g("battery", "plugged"), "usb")
        self.assertEqual(g("battery", "temp_c"), 31.2)
        self.assertTrue(g("power", "screen_on"))
        self.assertEqual(g("focus", "package"), "com.android.chrome")
        self.assertEqual(g("meminfo", "available_mb"), 2890)
        self.assertEqual(g("storage", "used_pct"), 54)
        self.assertEqual(g("packages", "user_apps"), 87)
        self.assertEqual(g("wifi", "ssid"), "HomeNet-5G")
        self.assertEqual(g("uptime", "uptime_h"), 50.9)

    def test_relative_activity(self):
        r = device.parse_focus("mResumedActivity: ActivityRecord{1 u0 jp.naver.line.android/.activity.SplashActivity t3}")
        self.assertEqual(r["activity"], "jp.naver.line.android.activity.SplashActivity")

    def test_missing_section_is_error_not_crash(self):
        partial = FIXTURE.split("@@JARVIS:wifi")[0]
        snap = DeviceProbe(FakeTransport(lambda s: partial)).snapshot()
        self.assertIn("wifi", snap.errors)
        self.assertEqual(snap.get("battery", "level"), 82)

    def test_garbage_does_not_crash(self):
        snap = DeviceProbe(FakeTransport(lambda s: "\n".join(
            f"@@JARVIS:{n}\n???" for n in device.SECTIONS))).snapshot()
        self.assertIsNone(snap.get("battery", "level"))
        self.assertIn("[device]", render(snap))


class ProbeTest(unittest.TestCase):
    def test_single_round_trip_and_cache(self):
        t = FakeTransport(lambda s: FIXTURE)
        p = DeviceProbe(t, ttl=60)
        p.snapshot()
        p.snapshot()
        p.snapshot(DeviceProbe.FAST)  # キャッシュ内に揃っている
        self.assertEqual(len(t.calls), 1)
        for name in device.SECTIONS:
            self.assertIn(f"@@JARVIS:{name}", t.calls[0])

    def test_fast_refresh_keeps_slow(self):
        t = FakeTransport(lambda s: FIXTURE)
        p = DeviceProbe(t, ttl=0)
        p.snapshot()
        snap = p.snapshot(DeviceProbe.FAST)
        self.assertNotIn("dumpsys wifi", t.calls[1])
        self.assertEqual(snap.get("props", "model"), "Pixel 7")

    def test_split_roundtrip(self):
        script = build_script(["uptime"])
        self.assertTrue(script.startswith("echo '@@JARVIS:uptime'"))
        self.assertEqual(split_sections("junk\n@@JARVIS:a\n1\n2\n@@JARVIS:b\n"), {"a": "1\n2", "b": ""})


class ContextTest(unittest.TestCase):
    def test_render_compact(self):
        snap = DeviceProbe(FakeTransport(lambda s: FIXTURE)).snapshot()
        text = render(snap, now=snap.taken_at)
        self.assertIn("端末: Google Pixel 7 Android 14 (SDK 34)", text)
        self.assertIn("電池: 82% charging via usb 31.2°C", text)
        self.assertIn("前面アプリ: com.android.chrome", text)
        self.assertLess(len(text), 400)
        msgs = build_messages(snap, "電池大丈夫？")
        self.assertEqual(msgs[0]["role"], "system")
        self.assertTrue(msgs[1]["content"].endswith("電池大丈夫？"))


class _SSE(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert body["stream"] is True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for tok in ["はい", "、", "大丈夫です"]:
            chunk = {"choices": [{"delta": {"content": tok}}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *a):
        pass


class LLMTest(unittest.TestCase):
    def test_rejects_remote_host(self):
        with self.assertRaises(LLMError):
            LocalLLM("http://api.example.com:8080")
        with self.assertRaises(LLMError):
            LocalLLM("http://192.168.1.10:8080")

    def test_streaming(self):
        srv = HTTPServer(("127.0.0.1", 0), _SSE)
        threading.Thread(target=srv.handle_request, daemon=True).start()
        llm = LocalLLM(f"http://127.0.0.1:{srv.server_port}")
        self.assertEqual(llm.complete([{"role": "user", "content": "x"}]), "はい、大丈夫です")
        self.assertEqual(llm.last.chunks, 3)
        self.assertIsNotNone(llm.last.first_token_ms)
        srv.server_close()

    def test_unreachable(self):
        with self.assertRaises(LLMError):
            LocalLLM("http://127.0.0.1:1").complete([{"role": "user", "content": "x"}])


if __name__ == "__main__":
    unittest.main()
