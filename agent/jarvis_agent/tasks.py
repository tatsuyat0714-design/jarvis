"""定型タスク。

タスクは端末を「調べて報告し、提案を作る」だけで、変更はしない。
変更は Proposal を apply() に渡したときだけ起こる。Proposal はシェル文字列ではなく
(種類, 引数) の構造データで、apply() 側で引数を検証してからコマンドを組み立てる。
保存された承認待ちの提案ファイルが書き換えられても、任意のコマンドは実行されない。

  optimize : キャッシュ・メモリ・裏で常習的に重いアプリの監視と、終了・制限      （軽い）
  unused   : 使用履歴から未使用アプリを抽出し、無効化 → アンインストールの段階で整理（重い）
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
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from . import apk
from . import dupes as dp
from . import trash
from .device import SECTIONS, parse_focus, parse_meminfo, parse_storage, run_sections
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


class TaskCancelled(Exception):
    """daemon が中断を求めた（画面が ON になった・充電が外れた）。"""


@dataclass
class TaskContext:
    transport: Transport
    store: Store
    llm: LLM | None = None
    now: float = field(default_factory=time.time)
    cancel: threading.Event | None = None

    def check_cancel(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise TaskCancelled()

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


# 使ったと見なす時刻: 前面に出した（Used）・画面に見えた（Visible）・他アプリやウィジェットから
# コンポーネントが使われた（ComponentUsed）。キーボードやウィジェットのように「開かない」アプリもある
_USAGE_LINE = re.compile(r"package=(\S+)(.*)")
_USAGE_KEYS = re.compile(r'(lastTimeUsed|lastTimeVisible|lastTimeComponentUsed)="([^"]*)"')
USAGE_CMD = "dumpsys usagestats | grep -E 'package=[^ ]+ totalTime'"


def parse_usage(text: str) -> dict[str, float]:
    """dumpsys usagestats の各期間（日・週・月・年）から、パッケージごとの最終使用時刻の最大値。"""
    last: dict[str, float] = {}
    for line in text.splitlines():
        m = _USAGE_LINE.search(line)
        if not m:
            continue
        pkg = m.group(1)
        for _, ts in _USAGE_KEYS.findall(m.group(2)):
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

_LIST = re.compile(r"\[(.*)\]")


def parse_diskstats_arrays(text: str) -> dict[str, list]:
    """dumpsys diskstats の `Package Names: [...]` / `App Sizes: [...]` などの配列行。"""
    out: dict[str, list] = {}
    for line in text.splitlines():
        key, _, val = line.partition(":")
        if val.strip().startswith("["):
            try:
                arr = json.loads(val.strip())
            except json.JSONDecodeError:
                continue
            if isinstance(arr, list):
                out[key.strip()] = arr
    return out


def parse_app_sizes(text: str) -> dict[str, int]:
    """アプリ本体 + データのバイト数（diskstats の集計値。キャッシュは含めない）。"""
    arr = parse_diskstats_arrays(text)
    names = arr.get("Package Names", [])
    app = arr.get("App Sizes", [])
    data = arr.get("App Data Sizes", [0] * len(names))
    if not (len(names) == len(app) == len(data)):
        return {}
    return {str(n): int(a) + int(d) for n, a, d in zip(names, app, data)
            if isinstance(a, (int, float)) and isinstance(d, (int, float))}


def parse_diskstats(text: str) -> tuple[int | None, dict[str, int]]:
    """dumpsys diskstats の App Cache Size と Package Names / Cache Sizes（システムが 1 日 1 回程度集計）。"""
    total, names, sizes = None, [], []
    for line in text.splitlines():
        key, _, val = line.partition(":")
        val = val.strip()
        if key == "App Cache Size" and val.isdigit():
            total = int(val)
        elif key in ("Package Names", "Cache Sizes"):
            try:
                arr = json.loads(val)
            except json.JSONDecodeError:
                continue
            if key == "Package Names":
                names = [str(x) for x in arr]
            else:
                sizes = [int(x) for x in arr if isinstance(x, (int, float))]
    per_app = dict(zip(names, sizes)) if len(names) == len(sizes) else {}
    if total is None and per_app:
        total = sum(per_app.values())
    return total, per_app


def parse_component_pkg(text: str) -> str | None:
    """`com.x.y/.Main` や `com.x.y/com.x.y.Ime` から先頭のパッケージ名を取る。"""
    m = re.search(r"([a-zA-Z]\w*(?:\.\w+)+)/", text)
    return m.group(1) if m else None


def parse_buckets(text: str) -> dict[str, int]:
    out = {}
    for line in text.splitlines():
        pkg, _, val = line.partition("=")
        m = re.search(r"\d+", val)
        if m and valid_package(pkg.strip()):
            out[pkg.strip()] = int(m.group(0))
    return out


BUCKET_RESTRICTED = 45
_MB = 1024 * 1024


def _record_samples(ctx: TaskContext, bg: list[tuple[str, int]]) -> dict[str, list]:
    """バックグラウンドのメモリ使用量を記録し、古いものを捨てた履歴を返す。"""
    window = ctx.cfg["bg_window_days"] * 86400
    hist: dict[str, list] = ctx.store.state.setdefault("bg_samples", {})
    for pkg, mb in bg[:20]:
        if mb >= ctx.cfg["bg_rss_mb"] // 2:
            hist.setdefault(pkg, []).append([ctx.now, mb])
    for pkg in list(hist):
        hist[pkg] = [s for s in hist[pkg] if ctx.now - s[0] <= window][-12:]
        if not hist[pkg]:
            del hist[pkg]
    ctx.store.save()
    return hist


def _chronic(ctx: TaskContext, hist: dict[str, list]) -> set[str]:
    th = ctx.cfg["bg_rss_mb"]
    return {p for p, samples in hist.items()
            if sum(1 for _, mb in samples if mb >= th) >= ctx.cfg["bg_chronic_count"]}


def optimize(ctx: TaskContext) -> Report:
    raw = run_sections(ctx.transport, {
        "df": "df /data | tail -n 1",
        "ps": "ps -A -o RSS,NAME | sort -rn | head -n 40",
        "user": "pm list packages -3",
        "focus": SECTIONS["focus"],
        "mem": "head -n 3 /proc/meminfo",
        "sdk": "getprop ro.build.version.sdk",
        "home": "cmd package resolve-activity --brief -a android.intent.action.MAIN "
                "-c android.intent.category.HOME | tail -n 1",
        "ime": "settings get secure default_input_method",
        "disk": "dumpsys diskstats | grep -E '^(App Cache Size|Package Names|Cache Sizes):'",
    }, timeout=30)
    cfg = ctx.cfg
    storage = parse_storage(raw.get("df", ""))
    mem = parse_meminfo(raw.get("mem", ""))
    sdk_s = raw.get("sdk", "").strip()
    sdk = int(sdk_s) if sdk_s.isdigit() else None
    fg = parse_focus(raw.get("focus", "")).get("package")
    users = parse_pkg_list(raw.get("user", ""))
    rss = parse_ps(raw.get("ps", ""))
    cache_total, cache_per_app = parse_diskstats(raw.get("disk", ""))

    # 止めると使い勝手が壊れるもの: 前面アプリ・ホーム・キーボード・エージェント自身と設定で除外したもの
    exclude = {fg, parse_component_pkg(raw.get("home", "")), parse_component_pkg(raw.get("ime", "")),
               *cfg["unused_ignore"], *cfg["optimize_ignore"]} - {None}
    bg = sorted(((p, mb) for p, mb in rss.items() if p in users and p not in exclude), key=lambda x: -x[1])
    hist = _record_samples(ctx, bg)
    chronic = _chronic(ctx, hist)

    rep = Report("optimize", "")
    det = rep.details

    # ---- メモリ
    pressure = False
    if mem["total_mb"] and mem["available_mb"] is not None:
        pct = mem["available_mb"] * 100 // mem["total_mb"]
        pressure = pct < cfg["mem_low_pct"]
        det.append(f"メモリ: 空き {mem['available_mb']}MB / {mem['total_mb']}MB（{pct}%）"
                   + ("・不足気味" if pressure else ""))
    th = cfg["bg_rss_mb"]
    for p, mb in bg:
        if mb < th:
            break
        why = []
        if p in chronic:
            why.append(f"直近 {cfg['bg_window_days']} 日で {len(hist.get(p, []))} 回、裏で大きく使用")
        if pressure:
            why.append("メモリ不足")
        if why:
            rep.proposals.append(Proposal("kill_bg", {"package": p}, f"{p} のバックグラウンドプロセスを終了",
                                          "low", f"{mb}MB・" + "・".join(why)))
    det += [f"裏で使用中: {p} {mb}MB" + ("（常習）" if p in chronic else "") for p, mb in bg[:5]]

    # ---- 常習的に裏で重いのに、ほとんど使われていないアプリは「制限」バケットを提案（Android 12 以降）
    restricted: dict[str, dict] = ctx.store.state.get("restricted", {})
    cands = sorted(p for p in chronic
                   if p in users and p not in exclude and p not in restricted and valid_package(p))
    if cands and sdk and sdk >= 31:
        raw2 = run_sections(ctx.transport, {
            "usage": USAGE_CMD,
            "bucket": "for p in " + " ".join(cands) + "; do echo \"$p=$(am get-standby-bucket $p)\"; done",
        }, timeout=40)
        usage = parse_usage(raw2.get("usage", ""))
        buckets = parse_buckets(raw2.get("bucket", ""))
        cutoff = ctx.now - cfg["restrict_unused_days"] * 86400
        for p in cands:
            b = buckets.get(p)
            last = usage.get(p)
            # 使用履歴が読めないアプリは判断材料が無いので提案しない
            if b is None or b >= BUCKET_RESTRICTED or not usage or (last and last > cutoff):
                continue
            ago = f"{int((ctx.now - last) // 86400)}日間開いていない" if last else "使用記録なし"
            rep.proposals.append(Proposal(
                "restrict_bg", {"package": p}, f"{p} のバックグラウンド動作を制限",
                "high", f"常習的に裏で重い・{ago}。通知や同期が遅れることがある（restricted undo で戻せる）"))
    if restricted:
        det.append(f"制限中のアプリ: {', '.join(sorted(restricted))}")

    # ---- キャッシュ: 大きいとき・空きが少ないときだけ提案する
    low_storage = storage["used_pct"] is not None and storage["used_pct"] >= cfg["storage_low_pct"]
    if cache_total is not None:
        det.append(f"キャッシュ合計: {cache_total / _MB:.0f}MB（システムの集計値）")
        top = sorted(cache_per_app.items(), key=lambda x: -x[1])[:5]
        det += [f"キャッシュ: {p} {sz / _MB:.0f}MB" for p, sz in top if sz >= 10 * _MB]
    big_cache = cache_total is not None and cache_total >= cfg["cache_min_mb"] * _MB
    if big_cache or low_storage:
        why = []
        if big_cache:
            why.append(f"キャッシュ {cache_total / _MB:.0f}MB")
        if low_storage:
            why.append(f"ストレージ使用率 {storage['used_pct']}%")
        rep.proposals.append(Proposal("trim_caches", {}, "全アプリのキャッシュを削除", "low", "・".join(why)))

    free = f"空き {storage['free_gb']}GB、" if storage["free_gb"] is not None else ""
    n_kill = sum(1 for p in rep.proposals if p.kind == "kill_bg")
    n_res = sum(1 for p in rep.proposals if p.kind == "restrict_bg")
    rep.summary = (f"{free}終了候補 {n_kill} 件・制限候補 {n_res} 件"
                   f"・キャッシュ削除{'を提案' if any(p.kind == 'trim_caches' for p in rep.proposals) else 'は不要'}")
    return rep


# 端末の仕組みに組み込まれていて、開かなくても使われているアプリ（消すと電話・SMS・入力・
# ホーム・パスワード管理・VPN・ユーザー補助・端末管理・置いてあるウィジェットが壊れる）
_ROLES = "HOME SMS DIALER BROWSER ASSISTANT CALL_SCREENING CALL_REDIRECTION EMERGENCY"
INTEGRATED_SECTIONS = {
    "roles": f"for r in {_ROLES}; do cmd role get-role-holders --user 0 android.app.role.$r; done",
    "settings": "for k in default_input_method enabled_input_methods enabled_accessibility_services "
                "enabled_notification_listeners autofill_service always_on_vpn_app; do "
                "settings get secure $k; done",
    "admins": "dumpsys device_policy | grep -oE '[a-zA-Z][a-zA-Z0-9_.]+/[a-zA-Z0-9_.$]+'",
    "widgets": "dumpsys appwidget | sed -n '/^Widgets:/,/^Hosts:/p' | grep -oE 'ComponentInfo\\{[^/}]+'",
    "wallpaper": "dumpsys wallpaper | grep -m1 -oE 'mWallpaperComponent=[^ ]+'",
}
_PKG_TOKEN = re.compile(r"(?<![\w.])([a-zA-Z]\w*(?:\.\w+)+)(?=/|\s|$|:|,)")


def parse_integrated(raw: dict[str, str]) -> set[str]:
    """役割の保持アプリ・有効なキーボード / ユーザー補助 / 通知リスナー / 自動入力 / VPN /
    端末管理アプリ・配置済みウィジェット・ライブ壁紙のパッケージ名を集める（多めに拾う方が安全）。"""
    out: set[str] = set()
    for key in INTEGRATED_SECTIONS:
        text = raw.get(key, "").replace("ComponentInfo{", " ").replace("mWallpaperComponent=", " ")
        for tok in _PKG_TOKEN.findall(text):
            if valid_package(tok):
                out.add(tok)
    return out


def _integrated_packages(ctx: TaskContext) -> set[str]:
    return parse_integrated(run_sections(ctx.transport, INTEGRATED_SECTIONS, timeout=30))


def unused(ctx: TaskContext) -> Report:
    cfg = ctx.cfg
    raw = run_sections(ctx.transport, {
        "user": "pm list packages -3",
        "disabled": "pm list packages -3 -d",
        "usage": USAGE_CMD,
        "disk": "dumpsys diskstats | grep -E '^(Package Names|App Sizes|App Data Sizes):'",
        **INTEGRATED_SECTIONS,
    }, timeout=60)
    users = parse_pkg_list(raw.get("user", ""))
    usage = parse_usage(raw.get("usage", ""))
    if not users or not usage:
        # 使用履歴が読めないまま判定すると「全アプリ未使用」になるので、提案は出さない
        raise TaskError("アプリ一覧または使用履歴を取得できませんでした")
    ctx.check_cancel()
    disabled_now = parse_pkg_list(raw.get("disabled", ""))
    sizes = parse_app_sizes(raw.get("disk", ""))
    integrated = parse_integrated(raw)

    days = cfg["unused_days"]
    cutoff = ctx.now - days * 86400
    ignore = set(cfg["unused_ignore"])
    ours: dict[str, dict] = ctx.store.state.get("disabled", {})
    # 利用者が自分で有効に戻したアプリは記録から外す（次からは通常の判定に戻る）
    for p in [p for p in ours if p not in disabled_now]:
        del ours[p]
    ctx.store.save()

    rep = Report("unused", "")
    kept = [p for p in users - ignore if p in integrated and usage.get(p, 0) < cutoff]
    if kept:
        rep.details.append("端末の機能に組み込まれているため対象外: " + ", ".join(sorted(kept)))

    # 1) 無効化して一定期間たっても有効に戻されていない → アンインストールを提案
    after = cfg["unused_uninstall_after_days"]
    for p, rec in sorted(ours.items()):
        if p in disabled_now and p not in integrated and ctx.now - rec.get("t", ctx.now) >= after * 86400:
            rep.proposals.append(Proposal(
                "uninstall", {"package": p}, f"{p} をアンインストール", "high",
                f"無効化して {int((ctx.now - rec['t']) // 86400)} 日、有効に戻されていない" + _fmt_size(sizes.get(p))))

    # 2) 未使用 → 無効化（データは残り、disabled undo で戻せる）か、設定によりアンインストール
    cands = [p for p in users - ignore - integrated - disabled_now
             if usage.get(p, 0) < cutoff and valid_package(p)]
    cands = sorted(cands, key=lambda p: -sizes.get(p, 0))[:40]
    installed: dict[str, float] = {}
    if cands:
        # インストール直後で未使用なだけのアプリを除くため、候補だけインストール日時を見る
        raw2 = run_sections(ctx.transport, {p: f"dumpsys package {p} | grep -m1 firstInstallTime"
                                            for p in cands}, timeout=40)
        for p, text in raw2.items():
            m = re.search(r"firstInstallTime=(.+)", text)
            if m and (t := _parse_time(m.group(1))):
                installed[p] = t
    action = "uninstall" if cfg["unused_action"] == "uninstall" else "disable_app"
    total = 0
    for p in cands:
        if installed.get(p, 0) > cutoff:
            continue
        last = usage.get(p)
        why = (f"{int((ctx.now - last) // 86400)}日間未使用" if last else "使用記録なし") + _fmt_size(sizes.get(p))
        total += sizes.get(p, 0)
        if action == "uninstall":
            rep.proposals.append(Proposal("uninstall", {"package": p}, f"{p} をアンインストール", "high", why))
        else:
            rep.proposals.append(Proposal("disable_app", {"package": p}, f"{p} を無効化", "high",
                                          why + "。データは残り、disabled undo で戻せる"))
        rep.details.append(f"{p}: {why}")
    size_s = f"（合計 {total / 1024**3:.1f}GB）" if total else ""
    rep.summary = (f"ユーザーアプリ {len(users)} 件中、{days}日以上未使用 "
                   f"{sum(1 for x in rep.proposals if x.args['package'] in cands)} 件{size_s}"
                   + (f"、無効化中 {len(ours)} 件" if ours else ""))
    return rep


def _fmt_size(n: int | None) -> str:
    if not n:
        return ""
    return f"・{n / 1024**3:.1f}GB" if n >= 1024**3 else f"・{n / 1024**2:.0f}MB"


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

    ctx.check_cancel()
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
    protect = ctx.cfg["dupe_protect"]
    files = []
    for i, f in enumerate(dp.walk(ctx.cfg["dupe_roots"], ctx.cfg["dupe_min_bytes"],
                                  exclude=[ctx.cfg["trash_dir"], *ctx.cfg["dupe_exclude"]])):
        if i % 200 == 0:
            ctx.check_cancel()
        files.append(f)
    try:
        groups = dp.find_duplicates(files, ctx.store.hashes, check=ctx.check_cancel)
    finally:
        ctx.store.save_hashes()  # 中断しても、そこまでに計算したハッシュは次回に使う
    dp.prune_cache(ctx.store.hashes, (f.path for f in files))
    ctx.store.save_hashes()
    decisions = (dp.judge_with_llm(ctx.llm, groups, protect=protect) if ctx.llm
                 else dp.judge_heuristic(groups, protect))

    rep = Report("dupes", "")
    wasted = 0
    for g, d in zip(groups, decisions):
        if d.keep is None:
            rep.details.append(f"保留: {g.files[0].path} ほか {len(g.files) - 1} 件（{d.reason}）")
            continue
        keep = g.files[d.keep]
        others = [f.path for f in dp.to_trash(g, d.keep, protect)]
        if not others:
            rep.details.append(f"すべて保護フォルダ内のため対象外: {keep.path} ほか {len(g.files) - 1} 件")
            continue
        size = g.size * len(others)
        wasted += size
        rep.proposals.append(Proposal(
            "trash", {"keep": keep.path, "files": others, "sha256": g.sha256},
            f"{os.path.basename(keep.path)} の重複 {len(others)} 件をゴミ箱へ（{dp._fmt_size(size)}）",
            "high", f"残す: {keep.path}（{d.by}: {d.reason}）"))
    rep.summary = (f"{len(files)} ファイル中、重複 {len(groups)} グループ、"
                   f"整理すると {dp._fmt_size(wasted)} 空く")
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
    if k == "restrict_bg":
        return _apply_restrict(a, ctx)
    if k == "disable_app":
        return _apply_disable(a, ctx)
    if k == "uninstall":
        pkg = _need_pkg(a)
        _need_user_app(ctx, pkg)
        _need_not_integrated(ctx, pkg)
        res = t.shell(f"pm uninstall --user 0 {pkg}", timeout=60)
        if "Success" not in res.stdout:
            raise ApplyError(f"アンインストール失敗: {res.stdout.strip() or res.stderr.strip()}")
        return f"{pkg} をアンインストールしました"
    if k == "install_apk":
        return _apply_install(a, ctx)
    if k == "trash":
        return _apply_trash(a, ctx)
    raise ApplyError(f"未知の提案: {k}")


def _need_not_integrated(ctx: TaskContext, pkg: str) -> None:
    # 提案後に既定の SMS アプリやキーボードにされていることもあるので、実行直前に確かめ直す
    if pkg in _integrated_packages(ctx):
        raise ApplyError(f"{pkg} は端末の機能（既定アプリ・キーボード・ユーザー補助など）に使われています")


def _disabled_packages(ctx: TaskContext) -> set[str]:
    return parse_pkg_list(ctx.transport.shell("pm list packages -3 -d").stdout)


def _apply_disable(a: dict, ctx: TaskContext) -> str:
    pkg = _need_pkg(a)
    if pkg in ctx.cfg["unused_ignore"]:
        raise ApplyError(f"{pkg} は除外設定に入っています")
    _need_user_app(ctx, pkg)
    _need_not_integrated(ctx, pkg)
    if pkg in _disabled_packages(ctx):
        raise ApplyError(f"{pkg} は既に無効です")
    ctx.transport.shell(f"pm disable-user --user 0 {pkg}")
    if pkg not in _disabled_packages(ctx):
        raise ApplyError("無効化できませんでした")
    ctx.store.state.setdefault("disabled", {})[pkg] = {"t": ctx.now}
    ctx.store.save()
    return f"{pkg} を無効化しました（データは残っています。元に戻す: disabled undo {pkg}）"


def enable_app(pkg: str, ctx: TaskContext) -> str:
    """disable_app の取り消し。"""
    if pkg not in ctx.store.state.get("disabled", {}):
        raise ApplyError(f"{pkg} はこのエージェントが無効化したアプリではありません")
    if not valid_package(pkg):
        raise ApplyError(f"不正なパッケージ名: {pkg!r}")
    if pkg in _disabled_packages(ctx):
        ctx.transport.shell(f"pm enable --user 0 {pkg}")
        if pkg in _disabled_packages(ctx):
            raise ApplyError(f"{pkg} を有効に戻せませんでした")
        msg = f"{pkg} を有効に戻しました"
    else:
        msg = f"{pkg} は既に有効でした（記録を消しました）"
    del ctx.store.state["disabled"][pkg]
    ctx.store.save()
    return msg


def _get_bucket(ctx: TaskContext, pkg: str) -> int | None:
    m = re.search(r"\d+", ctx.transport.shell(f"am get-standby-bucket {pkg}").stdout)
    return int(m.group(0)) if m else None


def _apply_restrict(a: dict, ctx: TaskContext) -> str:
    pkg = _need_pkg(a)
    _need_user_app(ctx, pkg)
    if pkg in ctx.cfg["unused_ignore"] or pkg in ctx.cfg["optimize_ignore"]:
        raise ApplyError(f"{pkg} は除外設定に入っています")
    prev = _get_bucket(ctx, pkg)
    if prev is None:
        raise ApplyError("現在のバケットを読めませんでした（Android 12 以降が必要）")
    if prev >= BUCKET_RESTRICTED:
        raise ApplyError(f"{pkg} は既に制限されています")
    ctx.transport.shell(f"am set-standby-bucket {pkg} restricted")
    if _get_bucket(ctx, pkg) != BUCKET_RESTRICTED:
        raise ApplyError("制限を設定できませんでした")
    ctx.store.state.setdefault("restricted", {})[pkg] = {"prev": prev, "t": ctx.now}
    ctx.store.save()
    return f"{pkg} のバックグラウンド動作を制限しました（元に戻す: restricted undo {pkg}）"


_RESTORABLE_BUCKETS = {5, 10, 20, 30, 40}


def unrestrict(pkg: str, ctx: TaskContext) -> str:
    """restrict_bg を取り消す。利用者がアプリを開いて既にバケットが上がっていれば記録を消すだけ。"""
    rec = ctx.store.state.get("restricted", {}).get(pkg)
    if rec is None:
        raise ApplyError(f"{pkg} はこのエージェントが制限したアプリではありません")
    if not valid_package(pkg):
        raise ApplyError(f"不正なパッケージ名: {pkg!r}")
    cur = _get_bucket(ctx, pkg)
    msg = f"{pkg} は既に制限が外れていました（記録を消しました）"
    if cur is not None and cur >= BUCKET_RESTRICTED:
        prev = rec.get("prev")
        prev = prev if prev in _RESTORABLE_BUCKETS else 40
        ctx.transport.shell(f"am set-standby-bucket {pkg} {prev}")
        msg = f"{pkg} の制限を解除しました"
    del ctx.store.state["restricted"][pkg]
    ctx.store.save()
    return msg


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
    moved, skipped = 0, []
    for f in files:
        if dp.is_protected(f, ctx.cfg["dupe_protect"]):
            skipped.append(f"{os.path.basename(f)}（保護フォルダ）")
            continue
        try:
            if dp.sha256_file(f) != sha:
                skipped.append(f"{os.path.basename(f)}（中身が変わっている）")
                continue
        except OSError:
            skipped.append(f"{os.path.basename(f)}（見つからない）")
            continue
        try:
            trash.move_to_trash(ctx.cfg["trash_dir"], ctx.now, f, sha, keep)
        except trash.TrashError as e:
            skipped.append(f"{os.path.basename(f)}（{e}）")
            continue
        moved += 1
    batch = os.path.basename(trash.batch_dir(ctx.cfg["trash_dir"], ctx.now))
    msg = f"{moved} 件をゴミ箱（{batch}）へ移動しました。残したファイル: {keep}"
    if skipped:
        msg += "。移さなかったもの: " + "、".join(skipped)
    return msg
