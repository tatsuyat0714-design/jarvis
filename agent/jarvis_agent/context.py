"""DeviceSnapshot を LLM へ渡すコンテキストに変換する。

ローカルの小型モデルはプロンプト処理（prefill）が遅延の大半を占めるため、
JSON ではなく 1 行 1 項目の短いテキストにして、トークン数を抑える。
値が取れなかった項目は行ごと省略する（「不明」を書くだけでもトークンを食う）。
"""

from __future__ import annotations

import time

from .device import DeviceSnapshot

SYSTEM_PROMPT = """あなたは Android 端末上で動くオンデバイス・エージェント「ジャービス」。
[device] ブロックは端末から今取得した実測値。推測で値を補わないこと。
回答は日本語で簡潔に。端末操作が必要な場合は、実行せずに提案だけを返すこと。"""


def _fmt(v, unit: str = "") -> str:
    return f"{v}{unit}"


def render(snap: DeviceSnapshot, now: float | None = None) -> str:
    d = snap.get
    lines: list[str] = []

    def add(label: str, *parts):
        parts = [p for p in parts if p]
        if parts:
            lines.append(f"{label}: " + " ".join(parts))

    model = " ".join(x for x in (d("props", "manufacturer"), d("props", "model")) if x)
    android = d("props", "android")
    add("端末", model, f"Android {android}" if android else None,
        f"(SDK {d('props', 'sdk')})" if d("props", "sdk") else None)

    lvl = d("battery", "level")
    add("電池", _fmt(lvl, "%") if lvl is not None else None,
        d("battery", "status"),
        f"via {d('battery', 'plugged')}" if d("battery", "plugged") else None,
        _fmt(d("battery", "temp_c"), "°C") if d("battery", "temp_c") is not None else None)

    so = d("power", "screen_on")
    add("画面", None if so is None else ("ON" if so else "OFF"))

    add("前面アプリ", d("focus", "package"))

    tot, av = d("meminfo", "total_mb"), d("meminfo", "available_mb")
    if tot and av is not None:
        add("メモリ", f"空き {av}MB / {tot}MB")

    tg, fg = d("storage", "total_gb"), d("storage", "free_gb")
    if tg and fg is not None:
        add("ストレージ", f"空き {fg}GB / {tg}GB ({d('storage', 'used_pct')}%使用)")

    add("ユーザーアプリ数", _fmt(d("packages", "user_apps")) if d("packages", "user_apps") is not None else None)

    we = d("wifi", "wifi_enabled")
    add("Wi-Fi", None if we is None else ("ON" if we else "OFF"), d("wifi", "ssid"))

    up = d("uptime", "uptime_h")
    add("起動からの時間", _fmt(up, "h") if up is not None else None)

    now = now or time.time()
    lines.append(f"取得時刻: {time.strftime('%Y-%m-%d %H:%M', time.localtime(snap.taken_at))}"
                 f" ({max(0, round(now - snap.taken_at))}秒前)")
    if snap.errors:
        lines.append("取得失敗: " + ",".join(sorted(snap.errors)))
    return "[device]\n" + "\n".join(lines)


def build_messages(snap: DeviceSnapshot, user_text: str, system: str = SYSTEM_PROMPT) -> list[dict]:
    """OpenAI 互換 chat 形式（llama.cpp llama-server / MLC LLM serve の両方が受け付ける）。"""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"{render(snap)}\n\n{user_text}"},
    ]
