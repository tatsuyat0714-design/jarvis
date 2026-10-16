"""重複ファイル検出と「どれを残すか」の判断。

1. サイズでグループ化（同サイズが 1 つしかないファイルは読まずに除外）
2. 先頭 64KiB の SHA-256 で絞り込み（大きな動画を全読みする前に安く弾く）
3. 全体の SHA-256 で確定
ハッシュは (サイズ, 更新時刻) が同じ限りキャッシュを再利用する。

判断は 2 通り:
- suggest_keep : ヒューリスティック（コピー印の無い名前 → DCIM 優先 → 古い順 → 短いパス）
- judge_with_llm : ローカル LLM に番号で選ばせる。LLM はパスを書かず「残す番号」だけ返すので、
                   想定外のファイルを指定できない。応答が不正なグループはヒューリスティックに戻す。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Iterator

from .llm import LLM

PARTIAL_BYTES = 64 * 1024
_CHUNK = 1024 * 1024
_COPY_MARK = re.compile(r"(\s?\(\d+\)|\s-\s?コピー|のコピー|[\s_-]copy(\s?\d+)?)$", re.I)


@dataclass(frozen=True)
class FileInfo:
    path: str
    size: int
    mtime: float


@dataclass
class DupGroup:
    sha256: str
    size: int
    files: list[FileInfo]

    @property
    def wasted(self) -> int:
        return self.size * (len(self.files) - 1)


def walk(roots: Iterable[str], min_bytes: int = 1, exclude: Iterable[str] = ()) -> Iterator[FileInfo]:
    """隠しディレクトリ（.thumbnails 等）とシンボリックリンクは辿らない。"""
    excl = tuple(os.path.realpath(e) for e in exclude)
    seen: set[tuple[int, int]] = set()
    stack = [r for r in roots if os.path.isdir(r)]
    while stack:
        d = stack.pop()
        rd = os.path.realpath(d)
        if any(os.path.commonpath([rd, e]) == e for e in excl):  # 前方一致ではなくフォルダ単位で比べる
            continue
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if e.name.startswith("."):
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                elif e.is_file(follow_symlinks=False):
                    st = e.stat(follow_symlinks=False)
                    if st.st_size >= min_bytes and (st.st_dev, st.st_ino) not in seen:
                        seen.add((st.st_dev, st.st_ino))  # ハードリンクは同じ実体
                        yield FileInfo(e.path, st.st_size, st.st_mtime)
            except OSError:
                continue


def sha256_file(path: str, limit: int | None = None) -> str:
    h = hashlib.sha256()
    remaining = limit
    with open(path, "rb") as f:
        while remaining is None or remaining > 0:
            buf = f.read(_CHUNK if remaining is None else min(_CHUNK, remaining))
            if not buf:
                break
            h.update(buf)
            if remaining is not None:
                remaining -= len(buf)
    return h.hexdigest()


def find_duplicates(files: Iterable[FileInfo], cache: dict[str, list] | None = None) -> list[DupGroup]:
    cache = cache if cache is not None else {}
    by_size: dict[int, list[FileInfo]] = defaultdict(list)
    for f in files:
        by_size[f.size].append(f)

    def full_hash(f: FileInfo) -> str | None:
        c = cache.get(f.path)
        if c and c[0] == f.size and c[1] == f.mtime:
            return c[2]
        try:
            h = sha256_file(f.path)
        except OSError:
            return None
        cache[f.path] = [f.size, f.mtime, h]
        return h

    groups: list[DupGroup] = []
    for size, same in by_size.items():
        if len(same) < 2:
            continue
        candidates = [same]
        if size > PARTIAL_BYTES:
            by_head: dict[str, list[FileInfo]] = defaultdict(list)
            for f in same:
                try:
                    by_head[sha256_file(f.path, PARTIAL_BYTES)].append(f)
                except OSError:
                    continue
            candidates = [g for g in by_head.values() if len(g) > 1]
        for cand in candidates:
            by_full: dict[str, list[FileInfo]] = defaultdict(list)
            for f in cand:
                h = full_hash(f)
                if h:
                    by_full[h].append(f)
            for h, g in by_full.items():
                if len(g) > 1:
                    groups.append(DupGroup(h, size, sorted(g, key=lambda f: f.path)))
    groups.sort(key=lambda g: g.wasted, reverse=True)
    return groups


# ---------------------------------------------------------------- decide

# 残す候補としての場所の優先度（小さいほど残す）。撮影した元ファイル → 整理済みのフォルダ → その他 →
# ダウンロード → メッセージアプリが保存したコピー、の順
_LOCATION_RANK: tuple[tuple[re.Pattern, int], ...] = (
    (re.compile(r"/DCIM/"), 0),
    (re.compile(r"/(Pictures|Movies|Music|Documents|Recordings|Podcasts|Audiobooks)/"), 1),
    (re.compile(r"/Download/"), 3),
    (re.compile(r"/(Android/media|LINE|WhatsApp|Telegram|Messenger)/", re.I), 4),
)


def location_rank(path: str) -> int:
    """消してよい側の印（ダウンロード・アプリのコピー）が 1 つでもあれば、親フォルダが何でもそちらを優先する。"""
    ranks = [r for pat, r in _LOCATION_RANK if pat.search(path)]
    worse = [r for r in ranks if r > 2]
    better = [r for r in ranks if r < 2]
    return max(worse) if worse else min(better, default=2)


def is_protected(path: str, protect: Iterable[str]) -> bool:
    rp = os.path.realpath(path)
    for p in protect:
        rr = os.path.realpath(p)
        if os.path.commonpath([rp, rr]) == rr:
            return True
    return False


def _keep_score(f: FileInfo, protect: Iterable[str]) -> tuple:
    stem = os.path.splitext(os.path.basename(f.path))[0]
    return (not is_protected(f.path, protect), location_rank(f.path), bool(_COPY_MARK.search(stem)),
            f.mtime, len(f.path), f.path)


def suggest_keep(g: DupGroup, protect: Iterable[str] = ()) -> int:
    protect = list(protect)
    return min(range(len(g.files)), key=lambda i: _keep_score(g.files[i], protect))


def to_trash(g: DupGroup, keep: int, protect: Iterable[str] = ()) -> list[FileInfo]:
    """ゴミ箱へ移すファイル。残す 1 つと、保護フォルダ内のファイルは移さない。"""
    protect = list(protect)
    return [f for i, f in enumerate(g.files) if i != keep and not is_protected(f.path, protect)]


JUDGE_PROMPT = """重複ファイル（中身が完全に同一）のグループごとに、残すファイルを 1 つ選ぶ。
残す以外のファイルはゴミ箱へ移される（完全削除ではない。[保護] の付いたファイルは常に残る）。
選び方: 元の撮影・保存場所（DCIM 等）、コピー印の無い名前、古い日付を優先。
LINE・WhatsApp などのアプリが保存したコピーやダウンロードは、消してよい側になりやすい。
迷うグループ（別アプリが参照していそう等）は keep を null にして残す。
出力は JSON 配列のみ: [{"g": グループ番号, "keep": ファイル番号 または null, "reason": "短い理由"}]"""


def _fmt_size(n: int) -> str:
    return f"{n / 1024**2:.1f}MB" if n >= 1024**2 else f"{n / 1024:.0f}KB"


def render_groups(groups: list[DupGroup], offset: int = 0, protect: Iterable[str] = ()) -> str:
    protect = list(protect)
    out = []
    for gi, g in enumerate(groups, start=offset + 1):
        out.append(f"グループ{gi} ({_fmt_size(g.size)} × {len(g.files)})")
        for fi, f in enumerate(g.files):
            mark = " [保護]" if is_protected(f.path, protect) else ""
            out.append(f" {fi}: {f.path} ({time.strftime('%Y-%m-%d', time.localtime(f.mtime))}){mark}")
    return "\n".join(out)


@dataclass(frozen=True)
class Decision:
    keep: int | None   # None = 判断保留（何も消さない）
    reason: str
    by: str            # "llm" | "heuristic"


def parse_judgement(raw: str, groups: list[DupGroup], offset: int = 0) -> dict[int, Decision]:
    """LLM の出力を検証する。範囲外の番号や壊れた要素は捨てる（呼び出し側でヒューリスティックに戻る）。"""
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end < start:
        return {}
    try:
        items = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return {}
    out: dict[int, Decision] = {}
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or not isinstance(it.get("g"), int):
            continue
        gi = it["g"] - 1 - offset
        if not 0 <= gi < len(groups):
            continue
        keep = it.get("keep")
        if keep is not None and not (isinstance(keep, int) and 0 <= keep < len(groups[gi].files)):
            continue
        out[gi] = Decision(keep, str(it.get("reason", ""))[:100], "llm")
    return out


def judge_with_llm(llm: LLM, groups: list[DupGroup], batch: int = 8,
                   protect: Iterable[str] = ()) -> list[Decision]:
    protect = list(protect)
    decisions: list[Decision | None] = [None] * len(groups)
    for off in range(0, len(groups), batch):
        chunk = groups[off:off + batch]
        msgs = [{"role": "system", "content": JUDGE_PROMPT},
                {"role": "user", "content": render_groups(chunk, off, protect)}]
        try:
            got = parse_judgement("".join(llm.stream(msgs)), chunk, off)
        except Exception:
            got = {}
        for gi, d in got.items():
            decisions[off + gi] = d
    return [d or Decision(suggest_keep(g, protect), "LLM 応答なし→既定ルール", "heuristic")
            for d, g in zip(decisions, groups)]


def judge_heuristic(groups: list[DupGroup], protect: Iterable[str] = ()) -> list[Decision]:
    protect = list(protect)
    return [Decision(suggest_keep(g, protect), "保護・保存場所・コピー印なし・古い順で選択", "heuristic")
            for g in groups]


def prune_cache(cache: dict[str, list], seen: Iterable[str]) -> int:
    """今回の走査で見つからなかったファイル（削除・移動済み）のハッシュをキャッシュから消す。"""
    keep = set(seen)
    stale = [p for p in cache if p not in keep]
    for p in stale:
        del cache[p]
    return len(stale)
