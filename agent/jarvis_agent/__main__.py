"""メインコントローラー。

  python -m jarvis_agent probe [--json]          端末情報を取得して LLM 用コンテキストを表示
  python -m jarvis_agent ask "質問" [--echo]     端末情報を添えてローカル LLM に質問
  python -m jarvis_agent capture out.txt         生の取得結果を保存（パーサの不具合報告・テスト用）
  python -m jarvis_agent ui screen               今の画面の操作可能な要素を一覧
  python -m jarvis_agent ui find 設定 [--tap]    キーワードに一致する要素と座標（--tap で最上位をタップ）
  python -m jarvis_agent run "目標"              ステートマシンで目標達成まで操作（危険な操作は確認）
  python -m jarvis_agent task optimize|unused|updates|dupes [--llm] [--save]
                                                 タスクを実行し、提案を 1 件ずつ確認して実行
  python -m jarvis_agent pending [--apply]       承認待ちの提案を表示（--apply で確認しながら実行）
  python -m jarvis_agent events [--poll]         イベントを表示するだけ（トリガーの動作確認）
  python -m jarvis_agent daemon [--poll]         イベント駆動で常駐し、ルールに従ってタスクを実行

共通: --fixture FILE / --ui-fixture FILE で実機の代わりに保存済みの出力を使う。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import events as ev
from . import ui
from .agent import Action, Agent, LLMPlanner, StepLog
from .daemon import Dispatcher, run_forever
from .context import build_messages, render
from .device import DeviceProbe, build_script
from .llm import DEFAULT_URL, EchoLLM, LLMError, LocalLLM
from .store import Store
from .tasks import TASKS, ApplyError, Proposal, TaskContext, TaskError, apply, run_task
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


def confirm_proposal(p: Proposal) -> bool:
    if not sys.stdin.isatty():
        return False
    mark = "⚠" if p.risk == "high" else "・"
    ans = input(f"{mark} {p.title}（{p.reason}）を実行しますか？ [y/N] ")
    return ans.strip().lower() in ("y", "yes")


def review(proposals: list[Proposal], ctx: TaskContext, store: Store | None = None) -> None:
    """提案を 1 件ずつ確認して実行する。承認しなかったものはそのまま残す。"""
    for p in proposals:
        if not confirm_proposal(p):
            continue
        try:
            print("  → " + apply(p, ctx))
            if store:
                store.remove_pending(p.id)
        except (ApplyError, TransportError, OSError) as e:
            print(f"  → 失敗: {e}", file=sys.stderr)


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

    tk = sub.add_parser("task")
    tk.add_argument("name", choices=sorted(TASKS))
    tk.add_argument("--llm", action="store_true", help="dupes の判断にローカル LLM を使う")
    tk.add_argument("--llm-url", default=DEFAULT_URL)
    tk.add_argument("--save", action="store_true", help="確認せず承認待ちに保存")

    pd = sub.add_parser("pending")
    pd.add_argument("--apply", action="store_true")

    for name in ("events", "daemon"):
        d = sub.add_parser(name)
        d.add_argument("--poll", action="store_true", help="logcat を使わずポーリングで監視")

    args = ap.parse_args(argv)
    transport = make_transport(args.fixture, args.ui_fixture)
    probe = DeviceProbe(transport)

    try:
        if args.cmd in ("task", "pending", "daemon"):
            store = Store()
            llm = LocalLLM(args.llm_url, max_tokens=512) if getattr(args, "llm", False) else None
            ctx = TaskContext(transport, store, llm)

            if args.cmd == "task":
                rep = run_task(args.name, ctx)
                print(f"{rep.summary}  ({rep.elapsed_ms:.0f}ms)")
                for d in rep.details:
                    print(f"  {d}")
                if args.save or not sys.stdin.isatty():
                    n = store.add_pending([p.to_dict() for p in rep.proposals])
                    print(f"承認待ちに {n} 件保存しました")
                else:
                    review(rep.proposals, ctx)
                return 0

            if args.cmd == "pending":
                items = [Proposal.from_dict(d) for d in store.pending()]
                if not items:
                    print("承認待ちの提案はありません")
                for p in items:
                    print(f"[{p.id}] {p.risk:<4} {p.task:<8} {p.title}（{p.reason}）")
                if args.apply:
                    review(items, ctx, store)
                return 0

            tracker = ev.StateTracker()
            run_forever(ctx, Dispatcher(ctx, tracker), poll=args.poll)
            return 0

        if args.cmd == "events":
            tracker = ev.StateTracker()
            tracker.seed(probe)
            print(f"初期状態: {tracker.state}", file=sys.stderr)
            src = (ev.PollingSource(probe, tracker) if args.poll else ev.LogcatSource(transport, tracker))
            for e in src.events():
                print(f"{time.strftime('%H:%M:%S', time.localtime(e.t))} {e.kind} {e.data or ''}", flush=True)
            return 0
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
    except KeyboardInterrupt:
        return 130
    except (TransportError, LLMError, ui.UiError, TaskError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
