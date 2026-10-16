"""デバイス情報の取得とパース。

レイテンシ対策として、全セクションを 1 回の shell 呼び出しにまとめて実行し、
区切り行 `@@JARVIS:<name>` で出力を切り分ける（adb shell 1 往復 ≒ 数十 ms のため）。
dumpsys は重いので grep で必要行だけに絞ってから転送する。
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

from .transport import Transport

MARK = "@@JARVIS:"

# name -> デバイス側で実行するシェル断片
SECTIONS: dict[str, str] = {
    "props": (
        "for p in ro.product.manufacturer ro.product.model ro.build.version.release "
        "ro.build.version.sdk ro.product.cpu.abi; do echo \"$p=$(getprop $p)\"; done"
    ),
    "battery": "dumpsys battery",
    "power": "dumpsys power | grep -E 'mWakefulness=|mIsPowered=' | head -n 2",
    "focus": (
        "dumpsys activity activities | grep -E 'topResumedActivity|mResumedActivity' | head -n 1;"
        " dumpsys window | grep -E 'mCurrentFocus' | head -n 1"
    ),
    "meminfo": "head -n 3 /proc/meminfo",
    "storage": "df /data | tail -n 1",
    "uptime": "cat /proc/uptime",
    "packages": "pm list packages -3 | wc -l",
    "wifi": "dumpsys wifi | grep -E '^Wi-Fi is|mWifiInfo SSID' | head -n 2",
}


def build_script(names: list[str], table: dict[str, str] = SECTIONS) -> str:
    parts = []
    for n in names:
        parts.append(f"echo '{MARK}{n}'; {{ {table[n]}; }} 2>/dev/null")
    return "; ".join(parts)


def run_sections(transport: Transport, table: dict[str, str], timeout: float = 20.0) -> dict[str, str]:
    """任意のコマンド表を 1 回の shell 呼び出しで実行し、セクションごとの出力を返す。"""
    return split_sections(transport.shell(build_script(list(table), table), timeout=timeout).stdout)


def split_sections(stdout: str) -> dict[str, str]:
    out: dict[str, list[str]] = {}
    cur: str | None = None
    for line in stdout.splitlines():
        if line.startswith(MARK):
            cur = line[len(MARK):].strip()
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


# ---------------------------------------------------------------- parsers

def _kv(text: str, sep: str) -> dict[str, str]:
    d = {}
    for line in text.splitlines():
        if sep in line:
            k, v = line.split(sep, 1)
            d[k.strip()] = v.strip()
    return d


def parse_props(text: str) -> dict:
    kv = _kv(text, "=")
    sdk = kv.get("ro.build.version.sdk", "")
    return {
        "manufacturer": kv.get("ro.product.manufacturer") or None,
        "model": kv.get("ro.product.model") or None,
        "android": kv.get("ro.build.version.release") or None,
        "sdk": int(sdk) if sdk.isdigit() else None,
        "abi": kv.get("ro.product.cpu.abi") or None,
    }


_BATTERY_STATUS = {"1": "unknown", "2": "charging", "3": "discharging", "4": "not_charging", "5": "full"}


def parse_battery(text: str) -> dict:
    kv = _kv(text, ":")
    plugged = [s for s in ("AC", "USB", "Wireless", "Dock")
               if kv.get(f"{s} powered", "").lower() == "true"]
    level, scale = kv.get("level"), kv.get("scale", "100")
    pct = None
    if level and level.isdigit() and scale.isdigit() and int(scale):
        pct = round(int(level) * 100 / int(scale))
    temp = kv.get("temperature")
    return {
        "level": pct,
        "status": _BATTERY_STATUS.get(kv.get("status", ""), None),
        "plugged": plugged[0].lower() if plugged else None,
        "temp_c": int(temp) / 10 if temp and temp.lstrip("-").isdigit() else None,
    }


def parse_power(text: str) -> dict:
    m = re.search(r"mWakefulness=(\w+)", text)
    p = re.search(r"mIsPowered=(\w+)", text)
    return {
        "screen_on": (m.group(1) == "Awake") if m else None,
        "powered": (p.group(1) == "true") if p else None,
    }


# u0 com.example.app/.MainActivity  /  u0 com.example.app/com.example.app.Main
_COMPONENT = re.compile(r"\s([a-zA-Z][\w.]*)/([\w.$]+)")


def parse_focus(text: str) -> dict:
    for line in text.splitlines():
        m = _COMPONENT.search(line)
        if m:
            pkg, act = m.groups()
            if act.startswith("."):
                act = pkg + act
            return {"package": pkg, "activity": act}
    return {"package": None, "activity": None}


def parse_meminfo(text: str) -> dict:
    kv = {k: int(v.split()[0]) for k, v in _kv(text, ":").items() if v.split()[0].isdigit()}
    total, avail = kv.get("MemTotal"), kv.get("MemAvailable", kv.get("MemFree"))
    return {
        "total_mb": total // 1024 if total else None,
        "available_mb": avail // 1024 if avail else None,
    }


def parse_storage(text: str) -> dict:
    # toybox df: Filesystem 1K-blocks Used Available Use% Mounted on
    cols = text.split()
    nums = [c for c in cols if c.isdigit()]
    if len(nums) >= 3:
        total, used, free = (int(n) for n in nums[:3])
        return {"total_gb": round(total / 1024**2, 1), "free_gb": round(free / 1024**2, 1),
                "used_pct": round(used * 100 / total) if total else None}
    return {"total_gb": None, "free_gb": None, "used_pct": None}


def parse_uptime(text: str) -> dict:
    try:
        return {"uptime_h": round(float(text.split()[0]) / 3600, 1)}
    except (IndexError, ValueError):
        return {"uptime_h": None}


def parse_packages(text: str) -> dict:
    t = text.strip()
    return {"user_apps": int(t) if t.isdigit() else None}


def parse_wifi(text: str) -> dict:
    enabled = None
    m = re.search(r"Wi-Fi is (\w+)", text)
    if m:
        enabled = m.group(1) == "enabled"
    ssid = re.search(r'SSID: "?([^",]*)"?,', text)
    return {"wifi_enabled": enabled,
            "ssid": ssid.group(1) if ssid and ssid.group(1) not in ("", "<unknown ssid>") else None}


PARSERS: dict[str, Callable[[str], dict]] = {
    "props": parse_props,
    "battery": parse_battery,
    "power": parse_power,
    "focus": parse_focus,
    "meminfo": parse_meminfo,
    "storage": parse_storage,
    "uptime": parse_uptime,
    "packages": parse_packages,
    "wifi": parse_wifi,
}


# ---------------------------------------------------------------- snapshot

@dataclass
class DeviceSnapshot:
    taken_at: float
    elapsed_ms: float
    data: dict[str, dict] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    def get(self, section: str, key: str):
        return self.data.get(section, {}).get(key)

    def to_dict(self) -> dict:
        return asdict(self)


class DeviceProbe:
    """デバイス状態をまとめて取得する。ttl 秒以内の再取得はキャッシュを返す。"""

    # 頻繁に変わる / 変わらないで分け、イベント駆動時は fast だけ取り直せるようにする
    FAST = ["battery", "power", "focus", "meminfo"]
    SLOW = ["props", "storage", "uptime", "packages", "wifi"]

    def __init__(self, transport: Transport, ttl: float = 5.0):
        self.transport = transport
        self.ttl = ttl
        self._cache: DeviceSnapshot | None = None

    def snapshot(self, sections: list[str] | None = None, force: bool = False) -> DeviceSnapshot:
        names = sections or (self.FAST + self.SLOW)
        now = time.time()
        if (not force and self._cache and now - self._cache.taken_at < self.ttl
                and all(n in self._cache.data for n in names)):
            return self._cache

        res = self.transport.shell(build_script(names))
        raw = split_sections(res.stdout)
        snap = DeviceSnapshot(taken_at=now, elapsed_ms=res.elapsed_ms)
        # 部分取得の場合は前回値を引き継ぐ（SLOW を毎回取らなくて済む）
        if sections and self._cache:
            snap.data.update(self._cache.data)
        for n in names:
            if n not in raw:
                snap.errors[n] = "no output"
                continue
            try:
                snap.data[n] = PARSERS[n](raw[n])
            except Exception as e:  # パーサ 1 つの失敗で全体を落とさない
                snap.errors[n] = f"{type(e).__name__}: {e}"
        self._cache = snap
        return snap
