"""UI 解析（uiautomator dump の XML）と基本操作。

全ノードを木として組み立てず、iterparse でノードを 1 つずつ流しながら
キーワードに一致したものだけを拾う。一致しないノードは即座に捨てるのでメモリも抑えられる。

パスワード欄（password="true"）の文字列は読み取らず、このモジュールからは入力もしない。
"""

from __future__ import annotations

import hashlib
import io
import re
import shlex
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator

from .transport import Transport

DUMP_PATH = "/sdcard/jarvis_ui.xml"
PACKAGE_RE = re.compile(r"[a-zA-Z]\w*(\.\w+)+")


def valid_package(name: str) -> bool:
    return bool(PACKAGE_RE.fullmatch(name))

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


class UiError(RuntimeError):
    pass


@dataclass(frozen=True)
class UiNode:
    text: str
    desc: str
    hint: str
    inner: str           # 自身に文言が無い押せるコンテナ用: 子孫で最初に見つかった文言
    resource_id: str
    cls: str
    package: str
    bounds: tuple[int, int, int, int]
    clickable: bool
    enabled: bool
    editable: bool
    password: bool
    checkable: bool = False
    checked: bool = False
    scrollable: bool = False

    @property
    def center(self) -> tuple[int, int]:
        l, t, r, b = self.bounds
        return (l + r) // 2, (t + b) // 2

    @property
    def area(self) -> int:
        l, t, r, b = self.bounds
        return max(0, r - l) * max(0, b - t)

    def label(self) -> str:
        """LLM・ログ向けの短い表記。"""
        name = (self.text or self.desc or self.inner or self.hint
                or self.resource_id.rsplit("/", 1)[-1] or self.cls.rsplit(".", 1)[-1])
        if self.checkable:
            return f"スイッチ「{name}」{'ON' if self.checked else 'OFF'}"
        kind = "入力欄" if self.editable else ("ボタン" if self.clickable else "表示")
        return f"{kind}「{name}」"


def _parse_bounds(s: str) -> tuple[int, int, int, int] | None:
    m = _BOUNDS.fullmatch(s.strip())
    return tuple(int(v) for v in m.groups()) if m else None  # type: ignore[return-value]


def _to_node(a: dict[str, str], inner: str) -> UiNode | None:
    bounds = _parse_bounds(a.get("bounds", ""))
    if bounds is None:
        return None
    password = a.get("password") == "true"
    cls = a.get("class", "")
    return UiNode(
        text="" if password else a.get("text", ""),
        desc=a.get("content-desc", ""),
        hint=a.get("hint", ""),
        inner=inner,
        resource_id=a.get("resource-id", ""),
        cls=cls,
        package=a.get("package", ""),
        bounds=bounds,
        clickable=a.get("clickable") == "true",
        enabled=a.get("enabled", "true") == "true",
        editable=cls.endswith("EditText"),
        password=password,
        checkable=a.get("checkable") == "true",
        checked=a.get("checked") == "true",
        scrollable=a.get("scrollable") == "true",
    )


def iter_nodes(xml: str) -> Iterator[UiNode]:
    try:
        for _, el in ET.iterparse(io.StringIO(xml), events=("end",)):
            if el.tag != "node":
                continue
            # 子は親より先に end になる。子の文言を el.text に載せて親へ渡し、子自体は捨てる
            inner = next((c.text for c in el if c.text), "") or ""
            n = _to_node(el.attrib, inner)
            el.text = (n.text or n.desc or n.inner) if n else inner
            el.attrib.clear()
            for c in list(el):
                el.remove(c)
            if n and n.area > 0:
                yield n
    except ET.ParseError as e:
        raise UiError(f"UI ダンプの XML が壊れています: {e}") from e


def normalize(s: str) -> str:
    """全角・半角、大文字・小文字、空白の違いを吸収する（「Ｗｉ－Ｆｉ」と「wi-fi」を同じに扱う）。"""
    return " ".join(unicodedata.normalize("NFKC", s).casefold().split())


def match_score(n: UiNode, kw: str) -> int | None:
    """小さいほど良い一致。0: 文言と完全一致 / 1: 文言の先頭 / 2: 文言に含む / 3: resource-id に含む。"""
    texts = [normalize(t) for t in (n.text, n.desc, n.inner, n.hint) if t]
    if any(t == kw for t in texts):
        return 0
    if any(t.startswith(kw) for t in texts):
        return 1
    if any(kw in t for t in texts):
        return 2
    if kw in normalize(n.resource_id):
        return 3
    return None


def find(xml: str, keywords: Iterable[str], clickable: bool | None = None,
         editable: bool | None = None, checkable: bool | None = None) -> list[UiNode]:
    """いずれかのキーワードに一致するノードを、一致の良い順（同じなら小さい要素＝具体的な順）に返す。"""
    kws = [normalize(k) for k in keywords if k and normalize(k)]
    if not kws:
        return []
    scored = []
    for n in iter_nodes(xml):
        if clickable is not None and n.clickable != clickable:
            continue
        if editable is not None and n.editable != editable:
            continue
        if checkable is not None and n.checkable != checkable:
            continue
        scores = [s for k in kws if (s := match_score(n, k)) is not None]
        if scores:
            scored.append((min(scores), n.area, n))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [n for _, _, n in scored]


def summarize(xml: str, limit: int = 25) -> tuple[str, str]:
    """操作可能な要素だけを短く列挙（LLM 用）と、画面の同一性判定用ハッシュを返す。"""
    h = hashlib.sha256()
    pkg = ""
    lines: list[str] = []
    scrollable = False
    for n in iter_nodes(xml):
        pkg = pkg or n.package
        # スイッチの ON/OFF も画面の変化として数える（切り替えたのに「変化なし」にならないように）
        h.update(f"{n.text}|{n.desc}|{n.resource_id}|{n.bounds}|{n.checked}".encode())
        scrollable = scrollable or n.scrollable
        if (n.clickable or n.editable or n.checkable) and n.enabled and len(lines) < limit:
            lines.append(f"- {n.label()}")
    head = f"[screen] {pkg}" if pkg else "[screen]"
    if scrollable:
        lines.append("（スクロールすると続きがある一覧あり）")
    return head + "\n" + "\n".join(lines), h.hexdigest()[:16]


def screen_package(xml: str) -> str:
    """前面の画面のパッケージ名（最初に見つかった要素のもの）。"""
    for n in iter_nodes(xml):
        if n.package:
            return n.package
    return ""


# ---------------------------------------------------------------- device I/O

def dump(transport: Transport, timeout: float = 8.0) -> str:
    res = transport.shell(
        f"uiautomator dump {DUMP_PATH} >/dev/null 2>&1 && cat {DUMP_PATH}", timeout=timeout)
    xml = res.stdout
    start = xml.find("<?xml")
    if start < 0:
        start = xml.find("<hierarchy")
    if start < 0:
        raise UiError("uiautomator dump に失敗しました（画面がアニメーション中だと失敗することがあります）")
    return xml[start:]


def tap(transport: Transport, x: int, y: int) -> None:
    transport.shell(f"input tap {int(x)} {int(y)}")


def swipe(transport: Transport, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
    transport.shell(f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(ms)}")


def scroll(transport: Transport, xml: str, direction: str = "down") -> bool:
    """いちばん大きいスクロール可能な一覧の中でスワイプする。一覧が無ければ False。
    down = 続き（下）を見る（指は下から上へ動かす）。"""
    if direction not in ("down", "up"):
        raise UiError(f"不正なスクロール方向: {direction}")
    lists = sorted((n for n in iter_nodes(xml) if n.scrollable), key=lambda n: -n.area)
    if not lists:
        return False
    l, t, r, b = lists[0].bounds
    x = (l + r) // 2
    hi, lo = t + (b - t) * 3 // 4, t + (b - t) // 4
    if direction == "down":
        swipe(transport, x, hi, x, lo)
    else:
        swipe(transport, x, lo, x, hi)
    return True


def find_scrolling(transport: Transport, xml: str, keywords: list[str], max_swipes: int = 4,
                   sleep: Callable[[float], None] = time.sleep, **filters) -> tuple[UiNode | None, str]:
    """画面に無ければ一覧を下へスクロールしながら探す。端まで来たら（画面が変わらなくなったら）やめる。"""
    for i in range(max_swipes + 1):
        hits = [n for n in find(xml, keywords, **filters) if n.enabled]
        if hits:
            return hits[0], xml
        if i == max_swipes:
            break
        before = summarize(xml)[1]
        if not scroll(transport, xml, "down"):
            break
        sleep(0.4)
        xml = dump(transport)
        if summarize(xml)[1] == before:
            break
    return None, xml


def wait_for(transport: Transport, keywords: list[str], timeout: float = 5.0, interval: float = 0.5,
             sleep: Callable[[float], None] = time.sleep) -> tuple[UiNode, str]:
    """要素が現れるまで画面を取り直して待つ（読み込み中の画面など）。
    dump 自体にも時間がかかるので、待ち時間は「interval ごとに最大 timeout/interval 回取り直す」で数える。"""
    polls = max(1, int(timeout / interval))
    for i in range(polls + 1):
        try:
            xml = dump(transport)
            hits = [n for n in find(xml, keywords) if n.enabled]
            if hits:
                return hits[0], xml
        except UiError:
            pass  # アニメーション中の dump 失敗は待てば直る
        if i < polls:
            sleep(interval)
    raise UiError(f"{timeout:g} 秒待っても「{' / '.join(keywords)}」が現れませんでした")


def encode_input_text(text: str) -> str:
    """`input text` 用にエスケープする。input text は ASCII のみ対応で、空白は %s で表す。"""
    if not text.isascii():
        raise UiError("input text は ASCII 以外を入力できません")
    return shlex.quote(text.replace("%", "\\%").replace(" ", "%s"))


def type_text(transport: Transport, node: UiNode, text: str) -> None:
    if node.password:
        raise UiError("パスワード欄への入力はこのエージェントでは行いません")
    if not node.editable:
        raise UiError(f"{node.label()} は入力欄ではありません")
    tap(transport, *node.center)
    transport.shell(f"input text {encode_input_text(text)}")


def key(transport: Transport, keycode: str) -> None:
    if not re.fullmatch(r"KEYCODE_[A-Z0-9_]+", keycode):
        raise UiError(f"不正なキーコード: {keycode}")
    transport.shell(f"input keyevent {keycode}")


def launch(transport: Transport, package: str) -> None:
    if not valid_package(package):
        raise UiError(f"不正なパッケージ名: {package}")
    transport.shell(f"monkey -p {package} -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1")
