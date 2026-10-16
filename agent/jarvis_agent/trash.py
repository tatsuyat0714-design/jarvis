"""重複整理のゴミ箱。

移動は「バッチ」（1 回の確認セッション＝1 つの日時フォルダ）単位で記録する:

    <trash_dir>/
      .nomedia                       ギャラリーに表示させない
      20261016-123248/
        manifest.json                元の場所・移動先・SHA-256・残したファイル
        storage/emulated/0/Download/IMG_0001 (1).jpg

復元では manifest を読み、元の場所へ戻す。manifest は共有ストレージにあるので、
他のアプリに書き換えられても困らないよう、戻し先は「重複整理の対象フォルダ内」に限り、
移動元はそのバッチのフォルダ内に限る。中身が SHA-256 と一致しないものは戻さない。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field

MANIFEST = "manifest.json"
_BATCH_NAME = re.compile(r"\d{8}-\d{6}")


class TrashError(RuntimeError):
    pass


def _under(path: str, root: str) -> bool:
    rp, rr = os.path.realpath(path), os.path.realpath(root)
    return rp != rr and os.path.commonpath([rp, rr]) == rr


def _under_any(path: str, roots: list[str]) -> bool:
    return any(_under(path, r) for r in roots)


@dataclass
class Batch:
    id: str
    path: str
    created: float
    items: list[dict] = field(default_factory=list)
    has_manifest: bool = True

    @property
    def size(self) -> int:
        total = 0
        for dirpath, _, names in os.walk(self.path):
            for n in names:
                if n != MANIFEST:
                    try:
                        total += os.path.getsize(os.path.join(dirpath, n))
                    except OSError:
                        pass
        return total

    @property
    def file_count(self) -> int:
        if self.has_manifest:
            return len(self.items)
        return sum(len([n for n in names if n != MANIFEST]) for _, _, names in os.walk(self.path))


def batch_dir(trash_dir: str, now: float) -> str:
    return os.path.join(trash_dir, time.strftime("%Y%m%d-%H%M%S", time.localtime(now)))


def _write_manifest(bdir: str, data: dict) -> None:
    tmp = os.path.join(bdir, MANIFEST + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(bdir, MANIFEST))


def _read_manifest(bdir: str) -> dict | None:
    try:
        with open(os.path.join(bdir, MANIFEST), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) and isinstance(data.get("items"), list) else None
    except (OSError, json.JSONDecodeError):
        return None


def move_to_trash(trash_dir: str, now: float, original: str, sha256: str, kept: str) -> str:
    """1 ファイルをバッチへ移し、manifest に追記する。移動先のパスを返す。"""
    os.makedirs(trash_dir, exist_ok=True)
    nomedia = os.path.join(trash_dir, ".nomedia")
    if not os.path.exists(nomedia):
        open(nomedia, "w").close()
    bdir = batch_dir(trash_dir, now)
    dest = os.path.join(bdir, os.path.realpath(original).lstrip("/"))
    if os.path.exists(dest):
        raise TrashError(f"ゴミ箱に同じパスのファイルがあります: {dest}")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    data = _read_manifest(bdir) or {"created": now, "items": []}
    shutil.move(original, dest)
    data["items"].append({"original": original, "trashed": dest, "sha256": sha256, "kept": kept})
    _write_manifest(bdir, data)
    return dest


def list_batches(trash_dir: str) -> list[Batch]:
    """新しい順。v0.3 以前の manifest の無いバッチも has_manifest=False で返す（復元はできない）。"""
    out = []
    try:
        names = os.listdir(trash_dir)
    except OSError:
        return []
    for name in names:
        bdir = os.path.join(trash_dir, name)
        if not (_BATCH_NAME.fullmatch(name) and os.path.isdir(bdir)):
            continue
        data = _read_manifest(bdir)
        try:
            created = time.mktime(time.strptime(name, "%Y%m%d-%H%M%S"))
        except ValueError:
            continue
        if data is None:
            out.append(Batch(name, bdir, created, [], has_manifest=False))
        else:
            out.append(Batch(name, bdir, float(data.get("created", created)), data["items"]))
    out.sort(key=lambda b: b.id, reverse=True)
    return out


def _prune_empty_dirs(root: str) -> None:
    for dirpath, _, _ in sorted(os.walk(root), key=lambda w: len(w[0]), reverse=True):
        try:
            os.rmdir(dirpath)  # 空でなければ失敗するだけ
        except OSError:
            pass


def restore(batch: Batch, allowed_roots: list[str], sha256_file) -> tuple[int, list[str]]:
    """バッチのファイルを元の場所へ戻す。戻せた件数と、戻さなかった理由の一覧を返す。"""
    if not batch.has_manifest:
        raise TrashError("記録（manifest）の無い古いバッチは復元できません")
    restored, skipped, remaining = 0, [], []
    for it in batch.items:
        orig, trashed, sha = str(it.get("original", "")), str(it.get("trashed", "")), str(it.get("sha256", ""))
        reason = None
        if not _under_any(orig, allowed_roots):
            reason = "戻し先が重複整理の対象フォルダの外"
        elif not _under(trashed, batch.path):
            reason = "移動元がこのバッチの外"
        elif not os.path.isfile(trashed):
            reason = "ゴミ箱に見つからない"
        elif os.path.exists(orig):
            reason = "元の場所に同名のファイルがある"
        else:
            try:
                if sha256_file(trashed) != sha:
                    reason = "中身が記録と違う"
            except OSError:
                reason = "読めない"
        if reason:
            skipped.append(f"{orig}: {reason}")
            remaining.append(it)
            continue
        os.makedirs(os.path.dirname(orig), exist_ok=True)
        shutil.move(trashed, orig)
        restored += 1
    batch.items = remaining
    if remaining:
        _write_manifest(batch.path, {"created": batch.created, "items": remaining})
    else:
        try:
            os.remove(os.path.join(batch.path, MANIFEST))
        except OSError:
            pass
    _prune_empty_dirs(batch.path)
    return restored, skipped


def delete_batch(trash_dir: str, batch: Batch) -> None:
    """バッチを完全に削除する（取り消し不可）。ゴミ箱直下の日時フォルダ以外は消さない。"""
    real_trash = os.path.realpath(trash_dir)
    real = os.path.realpath(batch.path)
    if os.path.dirname(real) != real_trash or not _BATCH_NAME.fullmatch(os.path.basename(real)):
        raise TrashError(f"ゴミ箱のバッチではありません: {batch.path}")
    shutil.rmtree(real)


def older_than(batches: list[Batch], days: float, now: float) -> list[Batch]:
    return [b for b in batches if now - b.created >= days * 86400]
