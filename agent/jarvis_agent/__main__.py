"""メインコントローラー（最小構成）。

  python -m jarvis_agent probe [--json]          端末情報を取得して LLM 用コンテキストを表示
  python -m jarvis_agent ask "質問" [--echo]     端末情報を添えてローカル LLM に質問
  python -m jarvis_agent capture out.txt         生の取得結果を保存（パーサの不具合報告・テスト用）

共通: --fixture FILE で実機の代わりに capture 済みの出力を使う。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .context import build_messages, render
from .device import DeviceProbe, build_script
from .llm import DEFAULT_URL, EchoLLM, LLMError, LocalLLM
from .transport import FakeTransport, TransportError, WrapperTransport


def make_transport(fixture: str | None):
    if fixture:
        text = Path(fixture).read_text(encoding="utf-8")
        return FakeTransport(lambda _script: text)
    return WrapperTransport()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="jarvis_agent")
    ap.add_argument("--fixture", help="実機の代わりに使う capture 済み出力")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe")
    p.add_argument("--json", action="store_true")
    p.add_argument("--fast", action="store_true", help="変化しやすい項目だけ取得")

    a = sub.add_parser("ask")
    a.add_argument("text")
    a.add_argument("--llm-url", default=DEFAULT_URL)
    a.add_argument("--echo", action="store_true", help="LLM を呼ばず送信内容を表示")
    a.add_argument("--max-tokens", type=int, default=256)

    c = sub.add_parser("capture")
    c.add_argument("out")

    args = ap.parse_args(argv)
    transport = make_transport(args.fixture)
    probe = DeviceProbe(transport)

    try:
        if args.cmd == "capture":
            res = transport.shell(build_script(DeviceProbe.FAST + DeviceProbe.SLOW))
            Path(args.out).write_text(res.stdout, encoding="utf-8")
            print(f"saved {len(res.stdout)} bytes in {res.elapsed_ms:.0f}ms -> {args.out}")
            return 0

        snap = probe.snapshot(DeviceProbe.FAST if args.cmd == "probe" and args.fast else None)

        if args.cmd == "probe":
            if args.json:
                print(json.dumps(snap.to_dict(), ensure_ascii=False, indent=2))
            else:
                print(render(snap))
                print(f"\n(取得 {snap.elapsed_ms:.0f}ms)", file=sys.stderr)
            return 0

        messages = build_messages(snap, args.text)
        llm = EchoLLM() if args.echo else LocalLLM(args.llm_url, max_tokens=args.max_tokens)
        for chunk in llm.stream(messages):
            sys.stdout.write(chunk)
            sys.stdout.flush()
        print()
        if isinstance(llm, LocalLLM):
            st = llm.last
            ft = f"{st.first_token_ms:.0f}ms" if st.first_token_ms is not None else "-"
            print(f"(端末取得 {snap.elapsed_ms:.0f}ms / 初トークン {ft} / 合計 {st.total_ms:.0f}ms)",
                  file=sys.stderr)
        return 0
    except (TransportError, LLMError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
