"""メインコントローラー。

  python -m jarvis_agent probe [--json]          端末情報を取得して LLM 用コンテキストを表示
  python -m jarvis_agent ask "質問" [--echo]     端末情報を添えてローカル LLM に質問
  python -m jarvis_agent capture out.txt         生の取得結果を保存（パーサの不具合報告・テスト用）
  python -m jarvis_agent ui screen               今の画面の操作可能な要素を一覧
  python -m jarvis_agent ui find 設定 [--tap]    キーワードに一致する要素と座標（--tap で最上位をタップ）
  python -m jarvis_agent run "目標"              ステートマシンで目標達成まで操作（危険な操作は確認）

共通: --fixture FILE / --ui-fixture FILE で実機の代わりに保存済みの出力を使う。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import ui
from .agent import Action, Agent, LLMPlanner, StepLog
from .context import build_messages, render
from .device import DeviceProbe, build_script
from .llm import DEFAULT_URL, EchoLLM, LLMError, LocalLLM
from .transport import FakeTransport, TransportError, WrapperTransport


def make_transport(fixture: str | None, ui_fixture: str | None = None):
    if not (fixture or ui_fixture):
        return WrapperTransport()
    dev = Path(fixture).read_text(encoding="utf-8") if fixture else ""
    xml = Path(ui_fixture).read_text(encoding="utf-8") if ui_fixture else ""
    return FakeTransport(lambda script: xml if "uiautomator" in script else dev)


def ask_confirm(a: Action) -> bool:
    if not sys.stdin.isatty():
        return False  # 無人実行では危険な操作を常に拒否
    ans = input(f"⚠ {a.describe()}（理由: {a.reason or '-'}）を実行しますか？ [y/N] ")
    return ans.strip().lower() in ("y", "yes")


def print_log(e: StepLog) -> None:
    print(f"[{e.state.value:<7}] {e.detail}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="jarvis_agent")
    ap.add_argument("--fixture", help="実機の代わりに使う capture 済み出力")
    ap.add_argument("--ui-fixture", help="実機の代わりに使う uiautomator dump の XML")
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

    u = sub.add_parser("ui")
    usub = u.add_subparsers(dest="ui_cmd", required=True)
    usub.add_parser("screen")
    uf = usub.add_parser("find")
    uf.add_argument("keywords", nargs="+")
    uf.add_argument("--tap", action="store_true")

    r = sub.add_parser("run")
    r.add_argument("goal")
    r.add_argument("--llm-url", default=DEFAULT_URL)
    r.add_argument("--max-steps", type=int, default=15)

    args = ap.parse_args(argv)
    transport = make_transport(args.fixture, args.ui_fixture)
    probe = DeviceProbe(transport)

    try:
        if args.cmd == "ui":
            xml = ui.dump(transport)
            if args.ui_cmd == "screen":
                print(ui.summarize(xml)[0])
                return 0
            nodes = ui.find(xml, args.keywords)
            for n in nodes:
                print(f"{n.label()}  center={n.center}  id={n.resource_id or '-'}")
            if not nodes:
                print("見つかりません", file=sys.stderr)
                return 1
            if args.tap:
                ui.tap(transport, *nodes[0].center)
            return 0

        if args.cmd == "run":
            agent = Agent(transport, LLMPlanner(LocalLLM(args.llm_url, max_tokens=128)),
                          confirm=ask_confirm, max_steps=args.max_steps, on_log=print_log)
            state = agent.run(args.goal)
            print(agent.result or state.value)
            return 0 if state.value == "DONE" else 1
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
    except (TransportError, LLMError, ui.UiError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
