"""定型タスク。

タスクは端末を「調べて報告し、提案を作る」だけで、変更はしない。
変更は Proposal を apply() に渡したときだけ起こる。Proposal はシェル文字列ではなく
(種類, 引数) の構造データで、apply() 側で引数を検証してからコマンドを組み立てる。
保存された承認待ちの提案ファイルが書き換えられても、任意のコマンドは実行されない。

  optimize : キャッシュ削除・バックグラウンドの大きなプロセスの監視        （軽い）
  unused   : 使用履歴（usagestats）から未使用アプリを抽出                  （重い）
  updates  : インストール済みバージョンの差分、ローカル APK の検証付き更新  （重い）
  dupes    : 重複ファイル検出と、LLM / 既定ルールによる「残す 1 つ」の判断   （重い）
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from . import apk
from . import dupes as dp
from .device import SECTIONS, parse_focus, parse_storage, run_sections
from .llm import LLM
from .store import Store
from .transport import Transport
from .ui import valid_package


@dataclass
class Proposal:
    kind: str
    args: dict
    title: str
    risk: str = "high"   # low: 取り消し不要な軽い操作 / high: 必ず人が確認
    reason: str = ""
    task: str = ""

    @property
    def id(self) -> str:
        return hashlib.sha1(f"{self.kind}{json.dumps(self.args, sort_keys=True)}".encode()).hexdigest()[:10]

    def to_dict(self) -> dict:
        return {**asdict(self), "id": self.id}

    @classmethod
    def from_dict(cls, d: dict) -> Proposal:
        return cls(d["kind"], d["args"], d["title"], d.get("risk", "high"), d.get("reason", ""), d.get("task", ""))


@dataclass
class Report:
    task: str
    summary: str
    details: list[str] = field(default_factory=list)
    proposals: list[Proposal] = field(default_factory=list)
    elapsed_ms: float = 0.0


@dataclass
class TaskContext:
    transport: Transport
    store: Store
    llm: LLM | None = None
    now: float = field(default_factory=time.time)

    @property
    def cfg(self) -> dict:
        return self.store.config


class TaskError(RuntimeError):
    pass


# ---------------------------------------------------------------- parsers

def parse_pkg_list(text: str) -> set[str]:
    return {line[8:].strip() for line in text.splitlines() if line.startswith("package:")}


def parse_ps(text: str) -> dict[str, int]:
    """`ps -A -o RSS,NAME` → パッケージごとの RSS 合計（MB）。pkg:service 形式はまとめる。"""
    total: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            pkg = parts[1].strip().split(":", 1)[0]
            total[pkg] = total.get(pkg, 0) + int(parts[0]) // 1024
    return total


def _parse_time(s: str) -> float | None:
    try:
        t = time.mktime(time.strptime(s.strip(), "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None
    return t if t > 946684800 else None  # 2000 年より前 = 未記録


_USAGE = re.compile(r'package=(\S+) totalTimeUsed="[^"]*" lastTimeUsed="([^"]*)"')


def parse_usage(text: str) -> dict[str, float]:
    """dumpsys usagestats の各期間（日・週・月・年）から、パッケージごとの最終使用時刻の最大値。"""
    last: dict[str, float] = {}
    for pkg, ts in _USAGE.findall(text):
        t = _parse_time(ts)
        if t and t > last.get(pkg, 0):
            last[pkg] = t
    return last


def parse_versions(text: str) -> dict[str, tuple[int, str]]:
    """dumpsys package packages の Package [..] / versionCode= / versionName= 行。最初の出現を採用。"""
    out: dict[str, tuple[int, str]] = {}
    cur, code, name = None, None, ""

    def flush():
        if cur and code is not None and cur not in out:
            out[cur] = (code, name)

    for line in text.splitlines():
        m = re.match(r"\s*Package \[([\w.]+)\]", line)
        if m:
            flush()
            cur, code, name = m.group(1), None, ""
            continue
        m = re.match(r"\s*versionCode=(\d+)", line)
        if m and code is None:
            code = int(m.group(1))
        m = re.match(r"\s*versionName=(\S*)", line)
        if m and not name:
            name = m.group(1)
    flush()
    return out


def parse_badging(text: str) -> tuple[str, int, str] | None:
    m = re.search(r"package: name='([\w.]+)' versionCode='(\d+)'(?: versionName='([^']*)')?", text)
    return (m.group(1), int(m.group(2)), m.group(3) or "") if m else None


# ---------------------------------------------------------------- tasks

def optimize(ctx: TaskContext) -> Report:
    raw = run_sections(ctx.transport, {
        "df": "df /data | tail -n 1",
        "ps": "ps -A -o RSS,NAME | sort -rn | head -n 40",
        "user": "pm list packages -3",
        "focus": SECTIONS["focus"],
    })
    storage = parse_storage(raw.get("df", ""))
    fg = parse_focus(raw.get("focus", "")).get("package")
    users = parse_pkg_list(raw.get("user", ""))
    rss = parse_ps(raw.get("ps", ""))
    bg = sorted(((p, mb) for p, mb in rss.items() if p in users and p != fg), key=lambda x: -x[1])
    heavy = [(p, mb) for p, mb in bg if mb >= ctx.cfg["bg_rss_mb"]]

    free = f"空き {storage['free_gb']}GB、" if storage["free_gb"] is not None else ""
    rep = Report("optimize", f"{free}バックグラウンドの大きなアプリ {len(heavy)} 件")
    rep.details = [f"{p}: {mb}MB" for p, mb in bg[:5]]
    used = f"ストレージ使用率 {storage['used_pct']}%" if storage["used_pct"] is not None else ""
    rep.proposals.append(Proposal("trim_caches", {}, "全アプリのキャッシュを削除", "low", used))
    for p, mb in heavy:
        rep.proposals.append(Proposal("kill_bg", {"package": p}, f"{p} のバックグラウンドプロセスを終了",
                                      "low", f"{mb}MB 使用中・前面ではない"))
    return rep


def unused(ctx: TaskContext) -> Report:
    raw = run_sections(ctx.transport, {
        "user": "pm list packages -3",
        "usage": "dumpsys usagestats | grep -oE 'package=[^ ]+ totalTimeUsed=\"[^\"]*\" lastTimeUsed=\"[^\"]*\"'",
    }, timeout=40)
    users = parse_pkg_list(raw.get("user", ""))
    usage = parse_usage(raw.get("usage", ""))
    if not users or not usage:
        # 使用履歴が読めないまま判定すると「全アプリ未使用」になるので、提案は出さない
        raise TaskError("アプリ一覧または使用履歴を取得できませんでした")

    days = ctx.cfg["unused_days"]
    cutoff = ctx.now - days * 86400
    ignore = set(ctx.cfg["unused_ignore"])
    cands = sorted(p for p in users - ignore if usage.get(p, 0) < cutoff)[:40]

    # インストール直後で未使用なだけのアプリを除くため、候補だけインストール日時を見る
    installed: dict[str, float] = {}
    if cands:
        raw2 = run_sections(ctx.transport, {p: f"dumpsys package {p} | grep -m1 firstInstallTime"
                                            for p in cands}, timeout=40)
        for p, text in raw2.items():
            m = re.search(r"firstInstallTime=(.+)", text)
            if m and (t := _parse_time(m.group(1))):
                installed[p] = t

    rep = Report("unused", "")
    for p in cands:
        if installed.get(p, 0) > cutoff:
            continue
        last = usage.get(p)
        why = f"{int((ctx.now - last) // 86400)}日間未使用" if last else "使用記録なし"
        rep.proposals.append(Proposal("uninstall", {"package": p}, f"{p} をアンインストール", "high", why))
        rep.details.append(f"{p}: {why}")
    rep.summary = f"ユーザーアプリ {len(users)} 件中、{days}日以上未使用 {len(rep.proposals)} 件"
    return rep


def _read_local_apk(path: str) -> apk.ApkInfo | None:
    """まず自前のパーサーで読み、読めなければ aapt があればそれで読む。"""
    try:
        return apk.read_apk(path)
    except apk.ApkError:
        pass
    tool = shutil.which("aapt") or shutil.which("aapt2")
    if not tool:
        return None
    try:
        r = subprocess.run([tool, "dump", "badging", path], capture_output=True, text=True,
                           errors="replace", timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return None
    b = parse_badging(r.stdout)
    return apk.ApkInfo(b[0], b[1], b[2], None) if b else None


def _local_apks(apk_dir: str) -> tuple[dict[str, tuple[str, apk.ApkInfo]], list[str]]:
    """apk_dir 直下の APK を読み、パッケージごとに最も新しい版だけを残す。"""
    best: dict[str, tuple[str, apk.ApkInfo]] = {}
    notes: list[str] = []
    if not os.path.isdir(apk_dir):
        return best, notes
    for name in sorted(os.listdir(apk_dir)):
        if not name.endswith(".apk"):
            continue
        path = os.path.join(apk_dir, name)
        info = _read_local_apk(path)
        if info is None:
            notes.append(f"読めない APK: {name}")
        elif info.split:
            notes.append(f"分割 APK の一部のため対象外: {name}（{info.split}）")
        elif info.package not in best or info.version_code > best[info.package][1].version_code:
            best[info.package] = (path, info)
    return best, notes


def updates(ctx: TaskContext) -> Report:
    raw = run_sections(ctx.transport, {
        "user": "pm list packages -3",
        "pkgs": "dumpsys package packages | grep -E 'Package \\[|^ *versionCode=|^ *versionName='",
        "sdk": "getprop ro.build.version.sdk",
    }, timeout=40)
    users = parse_pkg_list(raw.get("user", ""))
    versions = parse_versions(raw.get("pkgs", ""))
    inv = {p: list(v) for p, v in versions.items() if p in users}
    if not inv:
        raise TaskError("バージョン情報を取得できませんでした")
    sdk_s = raw.get("sdk", "").strip()
    sdk = int(sdk_s) if sdk_s.isdigit() else None

    rep = Report("updates", "")
    prev: dict[str, list] = ctx.store.state.get("inventory", {})
    updated = [p for p in inv if p in prev and inv[p][0] > prev[p][0]]
    added = [p for p in inv if prev and p not in prev]
    removed = [p for p in prev if p not in inv]
    rep.details += [f"更新: {p} {prev[p][1]} → {inv[p][1]}" for p in updated]
    rep.details += [f"追加: {p} {inv[p][1]}" for p in added]
    rep.details += [f"削除: {p}" for p in removed]
    ctx.store.state["inventory"] = inv
    ctx.store.save()

    apks, notes = _local_apks(ctx.cfg["apk_dir"])
    rep.details += notes
    for pkg, (path, info) in sorted(apks.items()):
        fname = os.path.basename(path)
        if pkg not in inv:
            rep.details.append(f"未インストールのため対象外: {fname}（{pkg}）")
            continue
        cur_code, cur_name = inv[pkg]
        if info.version_code <= cur_code:
            if info.version_code < cur_code:
                rep.details.append(f"端末の方が新しい: {fname}（{info.version_name} < {cur_name}）")
            continue
        if info.min_sdk and sdk and info.min_sdk > sdk:
            rep.details.append(f"この端末では動かない: {fname}（Android SDK {info.min_sdk} 以上が必要、端末は {sdk}）")
            continue
        rep.proposals.append(Proposal(
            "install_apk",
            {"path": path, "package": pkg, "version_code": info.version_code, "sha256": apk.sha256(path)},
            f"{pkg} を {cur_name} → {info.version_name or info.version_code} に更新", "high",
            f"ローカル APK: {fname}"))
    first = "（初回のため基準を記録）" if not prev else ""
    rep.summary = (f"アプリ {len(inv)} 件{first}: 更新 {len(updated)} / 追加 {len(added)} / 削除 {len(removed)}"
                   f"、ローカル APK で更新可能 {len(rep.proposals)} 件")
    return rep


def dupes(ctx: TaskContext) -> Report:
    files = dp.walk(ctx.cfg["dupe_roots"], ctx.cfg["dupe_min_bytes"], exclude=[ctx.cfg["trash_dir"]])
    groups = dp.find_duplicates(files, ctx.store.hashes)
    ctx.store.save_hashes()
    decisions = dp.judge_with_llm(ctx.llm, groups) if ctx.llm else dp.judge_heuristic(groups)

    rep = Report("dupes", "")
    for g, d in zip(groups, decisions):
        if d.keep is None:
            rep.details.append(f"保留: {g.files[0].path} ほか {len(g.files) - 1} 件（{d.reason}）")
            continue
        keep = g.files[d.keep]
        others = [f.path for i, f in enumerate(g.files) if i != d.keep]
        rep.proposals.append(Proposal(
            "trash", {"keep": keep.path, "files": others, "sha256": g.sha256},
            f"{os.path.basename(keep.path)} の重複 {len(others)} 件をゴミ箱へ（{dp._fmt_size(g.wasted)}）",
            "high", f"残す: {keep.path}（{d.by}: {d.reason}）"))
    wasted = sum(g.wasted for g, d in zip(groups, decisions) if d.keep is not None)
    rep.summary = f"重複 {len(groups)} グループ、整理すると {dp._fmt_size(wasted)} 空く"
    return rep


TASKS: dict[str, tuple[Callable[[TaskContext], Report], bool]] = {
    # name: (関数, 重いか) — 重いタスクは充電中または電池残量が多いときだけ自動実行する
    "optimize": (optimize, False),
    "unused": (unused, True),
    "updates": (updates, True),
    "dupes": (dupes, True),
}


def run_task(name: str, ctx: TaskContext) -> Report:
    fn, _ = TASKS[name]
    start = time.perf_counter()
    rep = fn(ctx)
    rep.elapsed_ms = (time.perf_counter() - start) * 1000
    for p in rep.proposals:
        p.task = name
    return rep


# ---------------------------------------------------------------- apply

class ApplyError(RuntimeError):
    pass


def _need_pkg(args: dict) -> str:
    p = str(args.get("package", ""))
    if not valid_package(p):
        raise ApplyError(f"不正なパッケージ名: {p!r}")
    return p


def _need_user_app(ctx: TaskContext, pkg: str) -> None:
    # システムアプリを対象にしないよう、実行直前に「ユーザーアプリか」を確認し直す
    res = ctx.transport.shell("pm list packages -3")
    if pkg not in parse_pkg_list(res.stdout):
        raise ApplyError(f"{pkg} はユーザーアプリとしてインストールされていません")


def _under(path: str, roots: list[str]) -> bool:
    rp = os.path.realpath(path)
    for r in roots:
        rr = os.path.realpath(r)
        if os.path.commonpath([rp, rr]) == rr and rp != rr:
            return True
    return False


def _free_gb(ctx: TaskContext) -> float | None:
    return parse_storage(ctx.transport.shell("df /data | tail -n 1").stdout)["free_gb"]


def apply(p: Proposal, ctx: TaskContext) -> str:
    k, a = p.kind, p.args
    t = ctx.transport
    if k == "trim_caches":
        before = _free_gb(ctx)
        t.shell("pm trim-caches 999G", timeout=60)
        after = _free_gb(ctx)
        freed = f"（{after - before:+.1f}GB）" if before is not None and after is not None else ""
        return f"キャッシュを削除しました{freed}"
    if k == "kill_bg":
        pkg = _need_pkg(a)
        _need_user_app(ctx, pkg)
        t.shell(f"am kill {pkg}")
        return f"{pkg} のバックグラウンドプロセスを終了しました"
    if k == "uninstall":
        pkg = _need_pkg(a)
        _need_user_app(ctx, pkg)
        res = t.shell(f"pm uninstall --user 0 {pkg}", timeout=60)
        if "Success" not in res.stdout:
            raise ApplyError(f"アンインストール失敗: {res.stdout.strip() or res.stderr.strip()}")
        return f"{pkg} をアンインストールしました"
    if k == "install_apk":
        return _apply_install(a, ctx)
    if k == "trash":
        return _apply_trash(a, ctx)
    raise ApplyError(f"未知の提案: {k}")


# pm install の失敗コード → 利用者向けの説明
_INSTALL_ERRORS = {
    "INSTALL_FAILED_UPDATE_INCOMPATIBLE": "署名が端末のアプリと違います（提供元が違う APK の可能性）",
    "INSTALL_FAILED_VERSION_DOWNGRADE": "端末の版より古い APK です",
    "INSTALL_FAILED_OLDER_SDK": "この端末の Android のバージョンでは動きません",
    "INSTALL_FAILED_INSUFFICIENT_STORAGE": "空き容量が足りません",
    "INSTALL_FAILED_NO_MATCHING_ABIS": "この端末の CPU 向けの APK ではありません",
    "INSTALL_FAILED_MISSING_SPLIT": "分割 APK の一部しかありません（全体が必要）",
    "INSTALL_PARSE_FAILED_NO_CERTIFICATES": "APK に署名がありません",
}


def explain_install_error(output: str) -> str:
    m = re.search(r"(INSTALL_[A-Z_]+)", output)
    if not m:
        return output.strip() or "不明なエラー"
    code = m.group(1)
    hint = _INSTALL_ERRORS.get(code) or ("APK が壊れています" if code.startswith("INSTALL_PARSE_FAILED") else "")
    return f"{hint}（{code}）" if hint else code


def _installed_version(ctx: TaskContext, pkg: str) -> int | None:
    out = ctx.transport.shell(f"dumpsys package {pkg} | grep -m1 -E '^ *versionCode='").stdout
    m = re.search(r"versionCode=(\d+)", out)
    return int(m.group(1)) if m else None


def _apply_install(a: dict, ctx: TaskContext) -> str:
    path = str(a.get("path", ""))
    if not (path.endswith(".apk") and _under(path, [ctx.cfg["apk_dir"]])):
        raise ApplyError(f"apk_dir 外の APK は扱いません: {path}")
    if not a.get("sha256"):
        raise ApplyError("古い形式の提案です。task updates をやり直してください")
    pkg = _need_pkg(a)
    # 提案を作った後に APK が差し替えられていないか、中身を読み直して確かめる
    try:
        if apk.sha256(path) != a["sha256"]:
            raise ApplyError("提案を作った後に APK が変わっています。task updates をやり直してください")
        info = apk.read_apk(path)
    except OSError as e:
        raise ApplyError(f"APK が読めません: {path}") from e
    except apk.ApkError as e:
        raise ApplyError(str(e)) from e
    if info.package != pkg or info.version_code != a.get("version_code"):
        raise ApplyError("APK の中身が提案と一致しません")
    _need_user_app(ctx, pkg)
    before = _installed_version(ctx, pkg)
    if before is not None and before >= info.version_code:
        raise ApplyError(f"{pkg} は既に同じか新しい版です")

    res = ctx.transport.shell(f"pm install -r {shlex.quote(path)}", timeout=120)
    if "Success" not in res.stdout:
        raise ApplyError("インストール失敗: " + explain_install_error(res.stdout + res.stderr))
    after = _installed_version(ctx, pkg)
    if after != info.version_code:
        raise ApplyError(f"インストール後の版が想定と違います（{after} / 期待 {info.version_code}）")
    inv = ctx.store.state.setdefault("inventory", {})
    old_name = inv.get(pkg, [None, "?"])[1]
    inv[pkg] = [info.version_code, info.version_name]
    ctx.store.save()
    return f"{pkg} を {old_name} → {info.version_name or info.version_code} に更新しました"


def _apply_trash(a: dict, ctx: TaskContext) -> str:
    roots = ctx.cfg["dupe_roots"]
    keep, files, sha = str(a.get("keep", "")), [str(f) for f in a.get("files", [])], str(a.get("sha256", ""))
    paths = [keep, *files]
    if not files or keep in files or not all(_under(x, roots) for x in paths):
        raise ApplyError("対象パスが不正です")
    # 提案作成後に中身が変わっていないか、残す側が存在するかを読み直して確かめる
    try:
        if dp.sha256_file(keep) != sha:
            raise ApplyError(f"残す側のファイルが変わっています: {keep}")
    except OSError as e:
        raise ApplyError(f"残す側のファイルが読めません: {keep}") from e
    dest_root = os.path.join(ctx.cfg["trash_dir"], time.strftime("%Y%m%d-%H%M%S", time.localtime(ctx.now)))
    moved = 0
    for f in files:
        try:
            if dp.sha256_file(f) != sha:
                continue
        except OSError:
            continue
        dest = os.path.join(dest_root, os.path.realpath(f).lstrip("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(f, dest)
        moved += 1
    return f"{moved} 件をゴミ箱（{dest_root}）へ移動しました。残したファイル: {keep}"
