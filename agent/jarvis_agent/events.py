"""イベント駆動トリガーの入力側。

第一候補は `logcat -b events` の常駐ストリーム。プロセスは read でブロックしているだけなので、
イベントが来ない間は CPU を使わない（ポーリングより電池に優しい）。
logcat が使えない環境では PollingSource（間隔を空けた FAST プローブ）に切り替える。

どちらも生の値を StateTracker に流し、「変化したときだけ」イベントにする。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator

from .device import DeviceProbe, DeviceSnapshot
from .transport import Transport

SCREEN_ON = "screen_on"
SCREEN_OFF = "screen_off"
CHARGING_START = "charging_start"
CHARGING_STOP = "charging_stop"
BATTERY_LOW = "battery_low"
APP_FOREGROUND = "app_foreground"
# 合成イベント: 画面 OFF が一定時間続いた（端末側には無い。daemon のタイマーが出す）
SCREEN_IDLE = "screen_idle"
EVENT_KINDS = (SCREEN_ON, SCREEN_OFF, SCREEN_IDLE, CHARGING_START, CHARGING_STOP, BATTERY_LOW, APP_FOREGROUND)

# 必要なタグ以外は端末側で捨てる（*:S）。-T に端末の現在時刻を渡し、過去ログを流さず「今から」を読む
LOGCAT_CMD = ("logcat -b events -v brief -T \"$(date '+%m-%d %H:%M:%S.000')\" "
              "screen_toggled:I battery_status:I battery_level:I "
              "wm_resume_activity:I am_resume_activity:I '*:S'")

# I/battery_status( 1234): [2,2,1,2,Li-ion]
_LINE = re.compile(r"^[VDIWE]/(\w+)\(\s*\d+\):\s*(.*)$")
_COMPONENT = re.compile(r"([a-zA-Z][\w.]*)/[\w.$]+")


@dataclass(frozen=True)
class Event:
    kind: str
    data: dict = field(default_factory=dict)
    t: float = field(default_factory=time.time)


@dataclass
class DeviceState:
    screen_on: bool | None = None
    plugged: bool | None = None
    level: int | None = None
    package: str | None = None
    temp_c: float | None = None   # 電池温度（重いタスクを避ける判断に使う）


class StateTracker:
    """値の変化をイベントに変換する。最初に観測した値は基準にするだけでイベントにしない。"""

    def __init__(self, low_level: int = 15):
        self.state = DeviceState()
        self.low_level = low_level

    def update(self, screen_on: bool | None = None, plugged: bool | None = None,
               level: int | None = None, package: str | None = None,
               temp_c: float | None = None) -> list[Event]:
        s, out = self.state, []
        if temp_c is not None:
            s.temp_c = temp_c
        if screen_on is not None:
            if s.screen_on is not None and screen_on != s.screen_on:
                out.append(Event(SCREEN_ON if screen_on else SCREEN_OFF))
            s.screen_on = screen_on
        if plugged is not None:
            if s.plugged is not None and plugged != s.plugged:
                out.append(Event(CHARGING_START if plugged else CHARGING_STOP))
            s.plugged = plugged
        if level is not None:
            if s.level is not None and s.level > self.low_level >= level and not s.plugged:
                out.append(Event(BATTERY_LOW, {"level": level}))
            s.level = level
        if package:
            if s.package is not None and package != s.package:
                out.append(Event(APP_FOREGROUND, {"package": package, "previous": s.package}))
            s.package = package
        return out

    def update_from(self, snap: DeviceSnapshot) -> list[Event]:
        battery = snap.data.get("battery")
        return self.update(screen_on=snap.get("power", "screen_on"),
                           plugged=bool(battery["plugged"]) if battery else None,
                           level=snap.get("battery", "level"), package=snap.get("focus", "package"),
                           temp_c=snap.get("battery", "temp_c"))

    def seed(self, probe: DeviceProbe) -> list[Event]:
        """端末の今の状態を読み込む。前に知っていた状態との差（切断中に起きた変化）をイベントで返す。"""
        return self.update_from(probe.snapshot(DeviceProbe.FAST, force=True))


def parse_logcat_line(line: str) -> dict | None:
    """logcat の 1 行を StateTracker.update の引数に変換する。対象外なら None。"""
    m = _LINE.match(line.strip())
    if not m:
        return None
    tag, body = m.groups()
    fields = body.strip().strip("[]").split(",")
    try:
        if tag == "screen_toggled":
            return {"screen_on": fields[0].strip() == "1"}
        if tag == "battery_status":   # [status, health, present, plugged, technology]
            return {"plugged": int(fields[3]) != 0}
        if tag == "battery_level":    # [level, voltage, temperature（0.1℃単位）]
            out = {"level": int(fields[0])}
            if len(fields) > 2 and fields[2].strip().lstrip("-").isdigit():
                out["temp_c"] = int(fields[2]) / 10
            return out
        if tag in ("wm_resume_activity", "am_resume_activity"):
            c = _COMPONENT.search(body)
            return {"package": c.group(1)} if c else None
    except (IndexError, ValueError):
        return None
    return None


class LogcatSource:
    def __init__(self, transport: Transport, tracker: StateTracker):
        self.transport = transport
        self.tracker = tracker

    def events(self) -> Iterator[Event]:
        for line in self.transport.stream(LOGCAT_CMD):
            upd = parse_logcat_line(line)
            if upd:
                yield from self.tracker.update(**upd)


class PollingSource:
    """logcat が使えない時の代替。画面 OFF 中は間隔を伸ばして電池を節約する。"""

    def __init__(self, probe: DeviceProbe, tracker: StateTracker, interval_s: float = 60,
                 screen_off_interval_s: float = 300, sleep: Callable[[float], None] = time.sleep,
                 max_polls: int | None = None):
        self.probe = probe
        self.tracker = tracker
        self.interval_s = interval_s
        self.screen_off_interval_s = screen_off_interval_s
        self.sleep = sleep
        self.max_polls = max_polls

    def events(self) -> Iterator[Event]:
        n = 0
        while self.max_polls is None or n < self.max_polls:
            n += 1
            yield from self.tracker.update_from(self.probe.snapshot(DeviceProbe.FAST, force=True))
            off = self.tracker.state.screen_on is False
            self.sleep(self.screen_off_interval_s if off else self.interval_s)
