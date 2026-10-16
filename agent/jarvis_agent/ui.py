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
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterable, Iterator

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


def _haystack(a: UiNode) -> str:
    return " ".join((a.text, a.desc, a.inner, a.hint, a.resource_id)).lower()


def find(xml: str, keywords: Iterable[str], clickable: bool | None = None,
         editable: bool | None = None) -> list[UiNode]:
    """いずれかのキーワードを含むノードを返す（小さい要素ほど具体的なので面積の昇順）。"""
    kws = [k.lower() for k in keywords if k]
    if not kws:
        return []
    hits = []
    for n in iter_nodes(xml):
        if clickable is not None and n.clickable != clickable:
            continue
        if editable is not None and n.editable != editable:
            continue
        hay = _haystack(n)
        if any(k in hay for k in kws):
            hits.append(n)
    hits.sort(key=lambda n: n.area)
    return hits


def summarize(xml: str, limit: int = 25) -> tuple[str, str]:
    """操作可能な要素だけを短く列挙（LLM 用）と、画面の同一性判定用ハッシュを返す。"""
    h = hashlib.sha256()
    pkg = ""
    lines: list[str] = []
    for n in iter_nodes(xml):
        pkg = pkg or n.package
        h.update(f"{n.text}|{n.desc}|{n.resource_id}|{n.bounds}".encode())
        if (n.clickable or n.editable) and n.enabled and len(lines) < limit:
            lines.append(f"- {n.label()}")
    head = f"[screen] {pkg}" if pkg else "[screen]"
    return head + "\n" + "\n".join(lines), h.hexdigest()[:16]


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
