"""デバイス側シェルへの経路。

実機では bin/adbw.sh（ADB over TCP / Shizuku rish の切り替えを担当）を叩く。
テストやデバイス無しの開発では FakeTransport に差し替える。
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping, Protocol

WRAPPER = Path(__file__).resolve().parent.parent / "bin" / "adbw.sh"


@dataclass(frozen=True)
class ShellResult:
    stdout: str
    stderr: str
    returncode: int
    elapsed_ms: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class TransportError(RuntimeError):
    pass


class Transport(Protocol):
    def shell(self, script: str, timeout: float = 10.0) -> ShellResult: ...

    def stream(self, script: str) -> Iterator[str]:
        """終わらないコマンド（logcat 等）の出力を 1 行ずつ返す。"""
        ...


class WrapperTransport:
    """adbw.sh 経由でデバイス側シェルを実行する。"""

    def __init__(self, wrapper: Path = WRAPPER, env: Mapping[str, str] | None = None):
        self.wrapper = wrapper
        self.env = {**os.environ, **(env or {})}

    def shell(self, script: str, timeout: float = 10.0) -> ShellResult:
        env = {**self.env, "JARVIS_TIMEOUT": str(int(timeout))}
        start = time.perf_counter()
        try:
            proc = subprocess.run(
                ["bash", str(self.wrapper), "shell", script],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout + 2,  # ラッパー側 timeout が先に効く。こちらは保険
                env=env,
            )
        except subprocess.TimeoutExpired as e:
            raise TransportError(f"timeout after {timeout}s") from e
        elapsed = (time.perf_counter() - start) * 1000
        if proc.returncode == 2 and proc.stderr.startswith("adbw:"):
            # ラッパー自身のエラー（接続不可など）はデバイス側の失敗と区別する
            raise TransportError(proc.stderr.strip())
        return ShellResult(proc.stdout, proc.stderr, proc.returncode, elapsed)

    def stream(self, script: str) -> Iterator[str]:
        proc = subprocess.Popen(["bash", str(self.wrapper), "stream", script], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, errors="replace", env=self.env)
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                yield line.rstrip("\n")
        finally:
            proc.kill()
            proc.wait()
        # ここに来るのはストリームが自然に終わったとき（接続切れ・ラッパーのエラー）
        err = proc.stderr.read() if proc.stderr else ""
        raise TransportError(err.strip() or f"stream ended (exit {proc.returncode})")


class FakeTransport:
    """スクリプト文字列を受け取り stdout を返す関数で応答する。"""

    def __init__(self, responder: Callable[[str], str], stream_lines: list[str] | None = None):
        self.responder = responder
        self.stream_lines = stream_lines or []
        self.calls: list[str] = []

    def shell(self, script: str, timeout: float = 10.0) -> ShellResult:
        self.calls.append(script)
        return ShellResult(self.responder(script), "", 0, 0.0)

    def stream(self, script: str) -> Iterator[str]:
        self.calls.append(script)
        yield from self.stream_lines
