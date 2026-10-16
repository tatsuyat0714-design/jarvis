"""エージェントの永続状態（~/.jarvis/）。

- state.json   : タスクの最終実行時刻・アプリ一覧（バージョン差分用）・承認待ちの提案
- config.json  : 利用者の設定（無ければ既定値）
- hashes.json  : 重複検出のハッシュキャッシュ（パス・サイズ・更新時刻が同じなら再計算しない）
- log.jsonl    : タスク実行ログ

書き込みは一時ファイル→rename で行い、途中で落ちても壊れた JSON を残さない。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    # 重複検出の対象（Termux で termux-setup-storage 済みなら /sdcard が読める）
    "dupe_roots": ["/sdcard/DCIM", "/sdcard/Pictures", "/sdcard/Download",
                   "/sdcard/Documents", "/sdcard/Movies", "/sdcard/Music"],
    "dupe_min_bytes": 1024 * 1024,
    "trash_dir": "/sdcard/.jarvis_trash",
    # 保護フォルダ: ここにあるファイルは重複でもゴミ箱へ移さない（残す側にだけなる）
    "dupe_protect": [],
    # 走査しないフォルダ
    "dupe_exclude": [],
    "unused_days": 30,
    # 未使用判定から外すアプリ（エージェント自身と、その動作に必要なもの）
    "unused_ignore": ["com.termux", "com.termux.api", "com.termux.boot", "moe.shizuku.privileged.api",
                      "com.tatsuya.jarvis"],
    "apk_dir": "/sdcard/Download",
    # optimize: この MB 以上をバックグラウンドで使うアプリを「重い」とみなす
    "bg_rss_mb": 300,
    # bg_window_days 日のうち bg_chronic_count 回以上重かったアプリを「常習」とする
    "bg_window_days": 3,
    "bg_chronic_count": 3,
    # 空きメモリがこの % を下回ったら、常習でなくても重いアプリの終了を提案する
    "mem_low_pct": 10,
    # 常習で、この日数開いていないアプリにバックグラウンド制限を提案する
    "restrict_unused_days": 7,
    # キャッシュ合計がこの MB 以上、またはストレージ使用率がこの % 以上のときだけキャッシュ削除を提案する
    "cache_min_mb": 500,
    "storage_low_pct": 85,
    # optimize で触らないアプリ（unused_ignore に加えて）
    "optimize_ignore": [],
    # 低リスクの提案（キャッシュ削除・バックグラウンドプロセス終了）を無人時に自動実行するか
    "auto_apply_low_risk": False,
}


def home() -> Path:
    return Path(os.environ.get("JARVIS_HOME", Path.home() / ".jarvis"))


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


class Store:
    def __init__(self, root: Path | None = None):
        self.root = root or home()
        self.state_path = self.root / "state.json"
        self.state: dict[str, Any] = _read(self.state_path, {})
        self.config: dict[str, Any] = {**DEFAULT_CONFIG, **_read(self.root / "config.json", {})}
        self._hashes: dict[str, list] | None = None

    def save(self) -> None:
        _write(self.state_path, self.state)

    # ---- cooldown
    def last_run(self, key: str) -> float:
        return self.state.get("last_run", {}).get(key, 0.0)

    def mark_run(self, key: str, t: float | None = None) -> None:
        self.state.setdefault("last_run", {})[key] = t or time.time()
        self.save()

    # ---- pending proposals
    def pending(self) -> list[dict]:
        return self.state.get("pending", [])

    def add_pending(self, items: list[dict]) -> int:
        cur = self.state.setdefault("pending", [])
        ids = {p["id"] for p in cur}
        new = [p for p in items if p["id"] not in ids]
        cur.extend(new)
        self.save()
        return len(new)

    def remove_pending(self, pid: str) -> None:
        self.state["pending"] = [p for p in self.pending() if p["id"] != pid]
        self.save()

    # ---- hash cache
    @property
    def hashes(self) -> dict[str, list]:
        if self._hashes is None:
            self._hashes = _read(self.root / "hashes.json", {})
        return self._hashes

    def save_hashes(self) -> None:
        if self._hashes is not None:
            _write(self.root / "hashes.json", self._hashes)

    # ---- log
    def log(self, entry: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / "log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), **entry}, ensure_ascii=False) + "\n")
