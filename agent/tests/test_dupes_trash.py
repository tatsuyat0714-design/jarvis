import io
import json
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
from jarvis_agent import dupes as dp
from jarvis_agent import trash
from jarvis_agent.store import Store
from jarvis_agent.tasks import Proposal, TaskContext, apply, run_task
from jarvis_agent.transport import FakeTransport
from test_events_tasks import NOW


def fi(path: str, mtime: float = 0) -> dp.FileInfo:
    return dp.FileInfo(path, 100, mtime)


class KeepRuleTest(unittest.TestCase):
    def test_location_rank(self):
        r = dp.location_rank
        self.assertLess(r("/sd/DCIM/Camera/a.jpg"), r("/sd/Pictures/a.jpg"))
        self.assertLess(r("/sd/Pictures/a.jpg"), r("/sd/misc/a.jpg"))
        self.assertLess(r("/sd/misc/a.jpg"), r("/sd/Download/a.jpg"))
        self.assertLess(r("/sd/Download/a.jpg"), r("/sd/Android/media/com.whatsapp/WhatsApp/Media/a.jpg"))
        # アプリのコピーは、親が Pictures でも消してよい側
        self.assertEqual(r("/sd/Pictures/LINE/a.jpg"), r("/sd/Download/LINE/a.jpg"))

    def test_keeps_camera_original_over_messaging_copy(self):
        g = dp.DupGroup("h", 100, [fi("/sd/Android/media/com.whatsapp/WhatsApp/Media/IMG-1.jpg", 1),
                                   fi("/sd/Download/IMG_1.jpg", 2), fi("/sd/DCIM/Camera/IMG_1 (1).jpg", 3)])
        self.assertEqual(g.files[dp.suggest_keep(g)].path, "/sd/DCIM/Camera/IMG_1 (1).jpg")

    def test_copy_mark_and_age_break_ties(self):
        g = dp.DupGroup("h", 100, [fi("/sd/DCIM/a (1).jpg", 1), fi("/sd/DCIM/a.jpg", 5)])
        self.assertEqual(dp.suggest_keep(g), 1)
        g = dp.DupGroup("h", 100, [fi("/sd/DCIM/b.jpg", 5), fi("/sd/DCIM/a.jpg", 1)])
        self.assertEqual(dp.suggest_keep(g), 1)

    def test_protected_wins_and_is_never_trashed(self):
        g = dp.DupGroup("h", 100, [fi("/sd/DCIM/a.jpg"), fi("/sd/Backup/a.jpg"), fi("/sd/Download/a.jpg")])
        self.assertEqual(dp.suggest_keep(g, ["/sd/Backup"]), 1)
        # 別の 1 つを残す判断でも、保護フォルダのファイルは移さない
        self.assertEqual([f.path for f in dp.to_trash(g, 0, ["/sd/Backup"])], ["/sd/Download/a.jpg"])
        self.assertTrue(dp.is_protected("/sd/Backup/x/y.jpg", ["/sd/Backup"]))
        self.assertFalse(dp.is_protected("/sd/Backup2/y.jpg", ["/sd/Backup"]))

    def test_llm_sees_protect_mark(self):
        g = dp.DupGroup("h", 100, [fi("/sd/DCIM/a.jpg"), fi("/sd/Backup/a.jpg")])
        lines = dp.render_groups([g], protect=["/sd/Backup"]).splitlines()
        self.assertFalse(lines[1].endswith("[保護]"))  # 0: /sd/DCIM/a.jpg
        self.assertTrue(lines[2].endswith("[保護]"))   # 1: /sd/Backup/a.jpg

    def test_prune_cache(self):
        cache = {"/a": [1, 1, "x"], "/b": [1, 1, "y"]}
        self.assertEqual(dp.prune_cache(cache, ["/a"]), 1)
        self.assertEqual(list(cache), ["/a"])


class Fs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sd = self.root / "sd"
        self.tdir = str(self.root / "trash")

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, rel: str, data: bytes, mtime: float | None = None) -> str:
        p = self.sd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        if mtime:
            os.utime(p, (mtime, mtime))
        return str(p)


class WalkTest(Fs):
    def test_exclude_is_folder_based(self):
        self.make("DCIM/a.jpg", b"x" * 10)
        self.make("DCIM2/a.jpg", b"x" * 10)
        found = [f.path for f in dp.walk([str(self.sd)], 1, exclude=[str(self.sd / "DCIM")])]
        self.assertEqual([os.path.basename(os.path.dirname(p)) for p in found], ["DCIM2"])


class TrashTest(Fs):
    def setUp(self):
        super().setUp()
        self.data = os.urandom(5000)
        self.sha = dp.sha256_file(self.make("keep/a.jpg", self.data))
        self.dups = [self.make(f"Download/a{i}.jpg", self.data) for i in range(2)]

    def batch(self):
        return trash.list_batches(self.tdir)[0]

    def test_move_records_manifest_and_nomedia(self):
        for f in self.dups:
            trash.move_to_trash(self.tdir, NOW, f, self.sha, str(self.sd / "keep/a.jpg"))
        self.assertTrue(os.path.exists(os.path.join(self.tdir, ".nomedia")))
        b = self.batch()
        self.assertEqual(b.id, time.strftime("%Y%m%d-%H%M%S", time.localtime(NOW)))
        self.assertEqual([it["original"] for it in b.items], self.dups)  # 同じバッチに追記される
        self.assertEqual(b.size, 2 * len(self.data))
        self.assertFalse(any(os.path.exists(f) for f in self.dups))

    def test_restore_roundtrip_and_cleanup(self):
        for f in self.dups:
            trash.move_to_trash(self.tdir, NOW, f, self.sha, "")
        n, skipped = trash.restore(self.batch(), [str(self.sd)], dp.sha256_file)
        self.assertEqual((n, skipped), (2, []))
        self.assertTrue(all(Path(f).read_bytes() == self.data for f in self.dups))
        self.assertEqual(trash.list_batches(self.tdir), [])  # 空になったバッチは消える

    def test_restore_skips_unsafe_or_conflicting(self):
        for f in self.dups:
            trash.move_to_trash(self.tdir, NOW, f, self.sha, "")
        b = self.batch()
        Path(self.dups[0]).write_bytes(b"new file at original path")       # 元の場所に別のファイル
        with open(b.items[1]["trashed"], "ab") as fh:                       # ゴミ箱の中身が変わった
            fh.write(b"!")
        n, skipped = trash.restore(b, [str(self.sd)], dp.sha256_file)
        self.assertEqual(n, 0)
        self.assertIn("元の場所に同名のファイルがある", skipped[0])
        self.assertIn("中身が記録と違う", skipped[1])
        self.assertEqual(len(self.batch().items), 2)  # 戻せなかったものは記録に残る

    def test_tampered_manifest_cannot_escape(self):
        trash.move_to_trash(self.tdir, NOW, self.dups[0], self.sha, "")
        b = self.batch()
        evil_target = str(self.root / "termux_home" / ".bashrc")
        outside = self.make("../outside.bin", self.data)
        b.items = [{"original": evil_target, "trashed": b.items[0]["trashed"], "sha256": self.sha},
                   {"original": str(self.sd / "x.jpg"), "trashed": outside, "sha256": self.sha}]
        n, skipped = trash.restore(b, [str(self.sd)], dp.sha256_file)
        self.assertEqual(n, 0)
        self.assertIn("対象フォルダの外", skipped[0])
        self.assertIn("このバッチの外", skipped[1])
        self.assertFalse(os.path.exists(evil_target))
        self.assertTrue(os.path.exists(outside))

    def test_legacy_batch_listed_but_not_restorable(self):
        legacy = Path(self.tdir) / "20250101-000000" / "sd" / "x.jpg"
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b"abc")
        b = trash.list_batches(self.tdir)[0]
        self.assertFalse(b.has_manifest)
        self.assertEqual(b.file_count, 1)
        with self.assertRaises(trash.TrashError):
            trash.restore(b, [str(self.sd)], dp.sha256_file)

    def test_purge_only_old_batches_inside_trash(self):
        trash.move_to_trash(self.tdir, NOW - 40 * 86400, self.dups[0], self.sha, "")
        trash.move_to_trash(self.tdir, NOW, self.dups[1], self.sha, "")
        batches = trash.list_batches(self.tdir)
        old = trash.older_than(batches, 30, NOW)
        self.assertEqual(len(old), 1)
        trash.delete_batch(self.tdir, old[0])
        self.assertEqual(len(trash.list_batches(self.tdir)), 1)
        for bad in [trash.Batch("x", self.tdir, 0), trash.Batch("20250101-000000", str(self.sd), 0)]:
            with self.assertRaises(trash.TrashError):
                trash.delete_batch(self.tdir, bad)
        self.assertTrue(os.path.isdir(self.sd))


class TaskFlowTest(Fs):
    def setUp(self):
        super().setUp()
        self.store = Store(self.root / "home")
        self.store.config.update(dupe_roots=[str(self.sd)], dupe_min_bytes=1000, trash_dir=self.tdir)
        self.ctx = TaskContext(FakeTransport(lambda s: ""), self.store, now=NOW)
        data = os.urandom(4000)
        self.orig = self.make("DCIM/Camera/IMG_1.jpg", data, NOW - 100)
        self.wa = self.make("Android/media/com.whatsapp/WhatsApp/Media/IMG-WA0001.jpg", data, NOW - 200)
        self.dl = self.make("Download/IMG_1.jpg", data, NOW - 50)

    def test_dupes_then_restore(self):
        rep = run_task("dupes", self.ctx)
        p = rep.proposals[0]
        self.assertEqual(p.args["keep"], self.orig)  # WhatsApp の方が古いが、カメラの元ファイルを残す
        self.assertEqual(sorted(p.args["files"]), sorted([self.wa, self.dl]))
        self.assertIn("2 件をゴミ箱", apply(p, self.ctx))
        n, _ = trash.restore(trash.list_batches(self.tdir)[0], [str(self.sd)], dp.sha256_file)
        self.assertEqual(n, 2)
        self.assertTrue(os.path.exists(self.wa) and os.path.exists(self.dl))

    def test_protect_and_exclude(self):
        self.store.config["dupe_protect"] = [str(self.sd / "Download")]
        p = run_task("dupes", self.ctx).proposals[0]
        self.assertNotIn(self.dl, p.args["files"])
        self.store.config["dupe_exclude"] = [str(self.sd / "Android")]
        self.store.config["dupe_protect"] = [str(self.sd)]
        rep = run_task("dupes", self.ctx)
        self.assertEqual(rep.proposals, [])
        self.assertIn("すべて保護フォルダ内", rep.details[0])

    def test_apply_respects_protect_added_later(self):
        p = run_task("dupes", self.ctx).proposals[0]
        self.store.config["dupe_protect"] = [str(self.sd / "Download")]
        msg = apply(p, self.ctx)
        self.assertTrue(os.path.exists(self.dl))
        self.assertIn("保護フォルダ", msg)

    def test_apply_reports_changed_file(self):
        p = run_task("dupes", self.ctx).proposals[0]
        Path(self.dl).write_bytes(b"edited")
        msg = apply(p, self.ctx)
        self.assertIn("中身が変わっている", msg)
        self.assertTrue(os.path.exists(self.dl))

    def test_cache_pruned_after_files_vanish(self):
        run_task("dupes", self.ctx)
        self.assertIn(self.dl, self.store.hashes)
        os.remove(self.dl)
        run_task("dupes", self.ctx)
        self.assertNotIn(self.dl, self.store.hashes)


class ReviewTest(unittest.TestCase):
    def test_bulk_approve_same_kind_only(self):
        props = [Proposal("trash", {"n": i}, f"t{i}") for i in range(3)] + [Proposal("uninstall", {"package": "a.b"}, "u")]
        answers = iter(["a", "n"])
        applied = []
        with mock.patch.object(cli.sys.stdin, "isatty", return_value=True), \
             mock.patch("builtins.input", lambda _: next(answers)), \
             mock.patch.object(cli, "apply", lambda p, ctx: applied.append(p.kind) or "ok"), \
             redirect_stdout(io.StringIO()):
            cli.review(props, ctx=None)
        self.assertEqual(applied, ["trash", "trash", "trash"])  # uninstall は別に聞かれて n

    def test_no_bulk_for_irreversible(self):
        props = [Proposal("uninstall", {"package": f"a.b{i}"}, f"u{i}") for i in range(3)]
        answers = iter(["a", "a", "a"])
        applied = []
        with mock.patch.object(cli.sys.stdin, "isatty", return_value=True), \
             mock.patch("builtins.input", lambda _: next(answers)), \
             mock.patch.object(cli, "apply", lambda p, ctx: applied.append(p) or "ok"), \
             redirect_stdout(io.StringIO()):
            cli.review(props, ctx=None)
        self.assertEqual(applied, [])  # a は選択肢に無く、y 以外なので 1 件も実行しない

    def test_a_ignored_when_single(self):
        with mock.patch.object(cli.sys.stdin, "isatty", return_value=True), \
             mock.patch("builtins.input", lambda _: "a"):
            self.assertEqual(cli.confirm_proposal(Proposal("trash", {}, "t"), 0), "n")


class TrashCliTest(Fs):
    def setUp(self):
        super().setUp()
        home = self.root / "home"
        home.mkdir()
        (home / "config.json").write_text(json.dumps({"dupe_roots": [str(self.sd)], "trash_dir": self.tdir}))
        self.env = mock.patch.dict(os.environ, {"JARVIS_HOME": str(home)})
        self.env.start()
        data = os.urandom(3000)
        sha = dp.sha256_file(self.make("keep.jpg", data))
        self.f = self.make("Download/dup.jpg", data)
        trash.move_to_trash(self.tdir, time.time() - 40 * 86400, self.f, sha, "")

    def tearDown(self):
        self.env.stop()
        super().tearDown()

    def run_cli(self, *args, tty=False, answer="n"):
        out = io.StringIO()
        with mock.patch.object(cli.sys.stdin, "isatty", return_value=tty), \
             mock.patch("builtins.input", lambda _: answer), redirect_stdout(out):
            rc = cli.main(["trash", *args])
        return rc, out.getvalue()

    def test_list_and_restore_latest(self):
        rc, out = self.run_cli("list")
        self.assertIn("1 件", out)
        rc, out = self.run_cli("restore", "latest")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(self.f))

    def test_purge_needs_confirmation(self):
        rc, out = self.run_cli("purge", tty=False, answer="y")
        self.assertEqual(rc, 1)  # 端末で確認できないとき（daemon・パイプ）は、入力が y でも消さない
        self.assertEqual(len(trash.list_batches(self.tdir)), 1)
        rc, out = self.run_cli("purge", tty=True, answer="y")
        self.assertEqual(rc, 0)
        self.assertEqual(trash.list_batches(self.tdir), [])
        rc, out = self.run_cli("purge", "--days", "0")
        self.assertIn("ありません", out)


if __name__ == "__main__":
    unittest.main()
