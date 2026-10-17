"""ローカル推論サーバーへのクライアント。

llama.cpp の `llama-server` と MLC LLM の `mlc_llm serve` はどちらも
OpenAI 互換の /v1/chat/completions を提供するので、1 つのクライアントで両対応する。
オフライン完結を保証するため、ループバック以外のホストへの接続は拒否する。
体感速度のためストリーミング（SSE）で最初のトークンから順に返す。
"""

from __future__ import annotations

import ipaddress
import json
import socket
import time
from dataclasses import dataclass
from typing import Iterator, Protocol
from urllib.parse import urlparse

DEFAULT_URL = "http://127.0.0.1:8080"  # llama-server の既定ポート

_opener = None


def _open(req, timeout: float):
    """urllib（ssl・email を連れてくる。約 10MB）は初回の推論で読み込む。常駐 daemon は LLM を使わなければ読まない。
    環境変数の HTTP(S)_PROXY は無視する（ローカル宛てが外に出ないように）。"""
    global _opener
    import urllib.request
    if _opener is None:
        _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return _opener.open(req, timeout=timeout)


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    def stream(self, messages: list[dict]) -> Iterator[str]: ...


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass
class Stats:
    first_token_ms: float | None = None
    total_ms: float = 0.0
    chunks: int = 0


class LocalLLM:
    def __init__(self, base_url: str = DEFAULT_URL, model: str = "local",
                 max_tokens: int = 256, temperature: float = 0.2, timeout: float = 60.0):
        host = urlparse(base_url).hostname or ""
        if not _is_loopback(host):
            raise LLMError(f"ローカル以外の推論先は禁止: {base_url}")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.last = Stats()

    def health(self) -> bool:
        import urllib.error
        try:
            with _open(self.base_url + "/v1/models", timeout=2) as r:
                return r.status == 200
        except (urllib.error.URLError, socket.timeout, ConnectionError):
            return False

    def stream(self, messages: list[dict]) -> Iterator[str]:
        import urllib.error
        import urllib.request
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": True,
            "cache_prompt": True,  # llama-server: 共通プレフィックス（system）の KV を再利用
        }).encode()
        req = urllib.request.Request(self.base_url + "/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        self.last = Stats()
        start = time.perf_counter()
        try:
            resp = _open(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            raise LLMError(f"HTTP {e.code}: {e.read()[:200]!r}") from e
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            raise LLMError(f"推論サーバーに接続できません ({self.base_url}): {e}") from e
        with resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    delta = json.loads(payload)["choices"][0].get("delta", {}).get("content")
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
                if delta:
                    if self.last.first_token_ms is None:
                        self.last.first_token_ms = (time.perf_counter() - start) * 1000
                    self.last.chunks += 1
                    yield delta
        self.last.total_ms = (time.perf_counter() - start) * 1000

    def complete(self, messages: list[dict]) -> str:
        return "".join(self.stream(messages))


class EchoLLM:
    """推論サーバー無しで配線を確認するためのダミー。受け取ったコンテキストをそのまま返す。"""

    def stream(self, messages: list[dict]) -> Iterator[str]:
        yield messages[-1]["content"]
