import os
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis_agent import apk
from jarvis_agent.store import Store
from jarvis_agent.tasks import ApplyError, TaskContext, apply, explain_install_error, run_task
from jarvis_agent.transport import FakeTransport
from test_events_tasks import DEVICE_TABLE, NOW, sectioned

REAL_MANIFEST = (Path(__file__).parent / "fixtures" / "jarvis_v0.1.8_manifest.axml").read_bytes()

_ANDROID_IDS = {"versionCode": 0x0101021B, "versionName": 0x0101021C,
                "minSdkVersion": 0x0101020C, "versionCodeMajor": 0x01010576}


def encode_axml(package: str, code: int, name: str = "", min_sdk: int | None = None, major: int = 0,
                split: str = "", utf16: bool = False, obfuscate: bool = False) -> bytes:
    """テスト用の最小 AXML エンコーダ（実 APK に無い UTF-16 / 難読化 / versionCodeMajor / split を作る）。"""
    android = list(_ANDROID_IDS)  # リソース ID の付く属性名は文字列プールの先頭に置く決まり
    strings = [("" if obfuscate else s) for s in android] + [
        "package", "split", "manifest", "uses-sdk", "http://schemas.android.com/apk/res/android",
        package, name, split]
    idx = {s: i for i, s in enumerate(strings) if s}
    idx.update({s: i for i, s in enumerate(android)})

    data, offsets = b"", []
    for s in strings:
        offsets.append(len(data))
        if utf16:
            data += struct.pack("<H", len(s)) + s.encode("utf-16-le") + b"\0\0"
        else:
            b = s.encode()
            data += bytes([len(s), len(b)]) + b + b"\0"
    data += b"\0" * (-len(data) % 4)
    hdr = 28
    strings_start = hdr + 4 * len(strings)
    pool = struct.pack("<HHIIIIII", 0x0001, hdr, strings_start + len(data), len(strings), 0,
                       0 if utf16 else 0x100, strings_start, 0)
    pool += struct.pack(f"<{len(strings)}I", *offsets) + data

    ids = list(_ANDROID_IDS.values())
    resmap = struct.pack("<HHI", 0x0180, 8, 8 + 4 * len(ids)) + struct.pack(f"<{len(ids)}I", *ids)
    ns = idx["http://schemas.android.com/apk/res/android"]

    def attr(key: str, value, android_ns: bool = True) -> bytes:
        if isinstance(value, str):
            return struct.pack("<IIIHBBI", ns if android_ns else 0xFFFFFFFF, idx[key], idx[value], 8, 0, 0x03, idx[value])
        return struct.pack("<IIIHBBI", ns if android_ns else 0xFFFFFFFF, idx[key], 0xFFFFFFFF, 8, 0, 0x10, value)

    def element(tag: str, attrs: list[bytes]) -> bytes:
        body = struct.pack("<IIHHHHHH", 0xFFFFFFFF, idx[tag], 20, 20, len(attrs), 0, 0, 0) + b"".join(attrs)
        return struct.pack("<HHIII", 0x0102, 16, 16 + len(body), 1, 0xFFFFFFFF) + body

    m_attrs = [attr("versionCode", code), attr("package", package, android_ns=False)]
    if name:
        m_attrs.append(attr("versionName", name))
    if major:
        m_attrs.append(attr("versionCodeMajor", major))
    if split:
        m_attrs.append(attr("split", split, android_ns=False))
    body = pool + resmap + element("manifest", m_attrs)
    if min_sdk is not None:
        body += element("uses-sdk", [attr("minSdkVersion", min_sdk)])
    return struct.pack("<HHI", 0x0003, 8, 8 + len(body)) + body


def write_apk(path: Path, manifest: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("AndroidManifest.xml", manifest)
        z.writestr("classes.dex", os.urandom(64))
    return str(path)


class ApkParserTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_real_apk_manifest(self):
        # GitHub Releases の jarvis-v0.1.8.apk から取り出した実物（app/build.gradle の値と一致すること）
        info = apk.read_apk(write_apk(self.dir / "j.apk", REAL_MANIFEST))
        self.assertEqual(info, apk.ApkInfo("com.tatsuya.jarvis", 8, "0.1.8", 26))

    def test_utf16_obfuscated_and_major(self):
        m = encode_axml("com.x.y", 5, "2.0", min_sdk=24, major=1, utf16=True, obfuscate=True)
        info = apk.read_apk(write_apk(self.dir / "a.apk", m))
        self.assertEqual(info, apk.ApkInfo("com.x.y", (1 << 32) | 5, "2.0", 24))

    def test_utf8_encoder_matches(self):
        info = apk.read_apk(write_apk(self.dir / "a.apk", encode_axml("com.x.y", 7, "1.7")))
        self.assertEqual((info.package, info.version_code, info.version_name, info.min_sdk), ("com.x.y", 7, "1.7", None))

    def test_split(self):
        info = apk.read_apk(write_apk(self.dir / "s.apk", encode_axml("com.x.y", 7, split="config.arm64_v8a")))
        self.assertEqual(info.split, "config.arm64_v8a")

    def test_broken(self):
        bad = self.dir / "bad.apk"
        bad.write_bytes(b"not a zip")
        for path in [str(bad), write_apk(self.dir / "t.apk", REAL_MANIFEST[:40]),
                     write_apk(self.dir / "x.apk", b"<manifest/>")]:
            with self.assertRaises(apk.ApkError, msg=path):
                apk.read_apk(path)
        with zipfile.ZipFile(self.dir / "nomani.apk", "w") as z:
            z.writestr("classes.dex", b"")
        with self.assertRaises(apk.ApkError):
            apk.read_apk(str(self.dir / "nomani.apk"))


class Device:
    """インストールで版が変わる擬似端末。"""

    def __init__(self, installed: dict[str, int], pm_output: str = "Success", bump: bool = True):
        self.installed = dict(installed)
        self.pm_output = pm_output
        self.bump = bump
        self.pending_code: int | None = None
        self.t = FakeTransport(self.respond)

    def respond(self, script: str) -> str:
        if script.startswith("pm install"):
            if "Success" in self.pm_output and self.bump and self.pending_code:
                self.installed["com.android.chrome"] = self.pending_code
            return self.pm_output
        if script.startswith("dumpsys package com.android.chrome"):
            return f"    versionCode={self.installed['com.android.chrome']} minSdk=29 targetSdk=34"
        return sectioned({**DEVICE_TABLE, "getprop ro.build.version.sdk": "34"})(script)


class UpdatesTaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.apks = root / "apks"
        self.store = Store(root / "home")
        self.store.config["apk_dir"] = str(self.apks)
        self.dev = Device({"com.android.chrome": 612300})
        self.ctx = TaskContext(self.dev.t, self.store, now=NOW)
        a = self.apks
        self.newest = write_apk(a / "chrome-121.apk", encode_axml("com.android.chrome", 612400, "121.0.0", 29))
        write_apk(a / "chrome-120.5.apk", encode_axml("com.android.chrome", 612350, "120.5", 29))
        write_apk(a / "line-old.apk", encode_axml("jp.naver.line.android", 1300, "13.0"))
        write_apk(a / "unknown.apk", encode_axml("com.not.installed", 1, "1"))
        write_apk(a / "split.apk", encode_axml("com.android.chrome", 999999, split="config.xxhdpi"))
        (a / "broken.apk").write_bytes(b"zzz")
        (a / "notes.txt").write_text("x")

    def tearDown(self):
        self.tmp.cleanup()

    def proposal(self):
        rep = run_task("updates", self.ctx)
        self.assertEqual(len(rep.proposals), 1, rep.details)
        return rep, rep.proposals[0]

    def test_proposes_only_newest_compatible(self):
        rep, p = self.proposal()
        self.assertEqual(p.args["path"], self.newest)
        self.assertEqual(p.args["version_code"], 612400)
        self.assertEqual(p.args["sha256"], apk.sha256(self.newest))
        self.assertIn("120.0.1 → 121.0.0", p.title)
        joined = "\n".join(rep.details)
        self.assertIn("端末の方が新しい: line-old.apk", joined)
        self.assertIn("未インストールのため対象外: unknown.apk", joined)
        self.assertIn("分割 APK の一部のため対象外: split.apk", joined)
        self.assertIn("読めない APK: broken.apk", joined)

    def test_min_sdk_too_high(self):
        write_apk(self.apks / "line-new.apk", encode_axml("jp.naver.line.android", 1500, "15.0", min_sdk=99))
        rep = run_task("updates", self.ctx)
        self.assertNotIn("jp.naver.line.android", [p.args["package"] for p in rep.proposals])
        self.assertIn("この端末では動かない: line-new.apk", "\n".join(rep.details))

    def test_apply_installs_and_verifies(self):
        _, p = self.proposal()
        self.dev.pending_code = 612400
        msg = apply(p, self.ctx)
        self.assertIn("120.0.1 → 121.0.0 に更新しました", msg)
        self.assertIn(f"pm install -r {self.newest}", self.dev.t.calls)
        self.assertEqual(self.store.state["inventory"]["com.android.chrome"], [612400, "121.0.0"])

    def test_apply_refuses_changed_apk(self):
        _, p = self.proposal()
        with open(self.newest, "ab") as f:
            f.write(b"tampered")
        with self.assertRaises(ApplyError):
            apply(p, self.ctx)
        self.assertFalse(any(c.startswith("pm install") for c in self.dev.t.calls))

    def test_apply_refuses_swapped_apk_same_hash_claim(self):
        _, p = self.proposal()
        p.args["version_code"] = 1  # 保存された提案が書き換えられた場合
        with self.assertRaises(ApplyError):
            apply(p, self.ctx)

    def test_apply_refuses_when_already_updated(self):
        _, p = self.proposal()
        self.dev.installed["com.android.chrome"] = 612400  # 提案後に Play ストアで更新された
        with self.assertRaises(ApplyError) as cm:
            apply(p, self.ctx)
        self.assertIn("既に同じか新しい版", str(cm.exception))
        self.assertFalse(any(c.startswith("pm install") for c in self.dev.t.calls))

    def test_apply_explains_failure(self):
        _, p = self.proposal()
        self.dev.pm_output = "Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: Existing package signatures do not match]"
        with self.assertRaises(ApplyError) as cm:
            apply(p, self.ctx)
        self.assertIn("署名が端末のアプリと違います", str(cm.exception))

    def test_apply_detects_silent_failure(self):
        _, p = self.proposal()
        self.dev.bump = False  # "Success" なのに版が変わらない
        with self.assertRaises(ApplyError) as cm:
            apply(p, self.ctx)
        self.assertIn("インストール後の版が想定と違います", str(cm.exception))

    def test_old_format_proposal_rejected(self):
        _, p = self.proposal()
        p.args.pop("sha256")
        with self.assertRaises(ApplyError):
            apply(p, self.ctx)

    def test_explain(self):
        self.assertIn("APK が壊れています", explain_install_error("Failure [INSTALL_PARSE_FAILED_BAD_MANIFEST]"))
        self.assertEqual(explain_install_error("Failure [INSTALL_FAILED_FOO]"), "INSTALL_FAILED_FOO")
        self.assertEqual(explain_install_error(""), "不明なエラー")


if __name__ == "__main__":
    unittest.main()
