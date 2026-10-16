"""APK のメタ情報を読む（aapt 不要）。

APK は zip で、中の AndroidManifest.xml はバイナリ XML（AXML）。必要なのは
<manifest package versionCode versionCodeMajor versionName> と <uses-sdk minSdkVersion> だけなので、
文字列プールと開始タグだけを読む最小限のパーサーで済ませる。
属性名は難読化で空になっていることがあるため、リソース ID（android:versionCode = 0x0101021b 等）でも判定する。
"""

from __future__ import annotations

import hashlib
import struct
import zipfile
from dataclasses import dataclass

_RES_XML = 0x0003
_STRING_POOL = 0x0001
_RESOURCE_MAP = 0x0180
_START_ELEMENT = 0x0102
_UTF8_FLAG = 0x100

_TYPE_STRING = 0x03
_TYPE_INT_DEC = 0x10
_TYPE_INT_HEX = 0x11

_ATTR_IDS = {
    0x0101021B: "versionCode",
    0x0101021C: "versionName",
    0x0101020C: "minSdkVersion",
    0x01010576: "versionCodeMajor",
}


# 実際の manifest は数十 KB。展開後に巨大化する細工された APK を読まないための上限
_MAX_MANIFEST = 4 * 1024 * 1024


class ApkError(ValueError):
    pass


@dataclass(frozen=True)
class ApkInfo:
    package: str
    version_code: int        # versionCodeMajor << 32 | versionCode（dumpsys の versionCode と同じ尺度）
    version_name: str
    min_sdk: int | None
    split: str = ""          # 分割 APK の一部なら split 名（config.arm64_v8a など）。単体ではインストールできない


def _decode_pool(buf: bytes, start: int) -> list[str]:
    _, header_size, _ = struct.unpack_from("<HHI", buf, start)
    count, _styles, flags, strings_start, _ = struct.unpack_from("<IIIII", buf, start + 8)
    offsets = struct.unpack_from(f"<{count}I", buf, start + header_size)
    base = start + strings_start
    utf8 = bool(flags & _UTF8_FLAG)
    out = []
    for off in offsets:
        p = base + off
        if utf8:
            # UTF-16 での長さ（読み飛ばす）→ UTF-8 のバイト長。どちらも上位ビットが立てば 2 バイト
            p += 2 if buf[p] & 0x80 else 1
            n = buf[p]
            if n & 0x80:
                n = ((n & 0x7F) << 8) | buf[p + 1]
                p += 2
            else:
                p += 1
            out.append(buf[p:p + n].decode("utf-8", "replace"))
        else:
            (n,) = struct.unpack_from("<H", buf, p)
            p += 2
            if n & 0x8000:
                (lo,) = struct.unpack_from("<H", buf, p)
                n = ((n & 0x7FFF) << 16) | lo
                p += 2
            out.append(buf[p:p + n * 2].decode("utf-16-le", "replace"))
    return out


def parse_manifest(buf: bytes) -> dict[str, dict[str, object]]:
    """AXML から manifest / uses-sdk 要素の属性を {要素名: {属性名: 値}} で返す。"""
    try:
        typ, header_size, total = struct.unpack_from("<HHI", buf, 0)
    except struct.error as e:
        raise ApkError("AndroidManifest.xml が短すぎます") from e
    if typ != _RES_XML:
        raise ApkError("バイナリ XML ではありません")
    strings: list[str] = []
    res_ids: list[int] = []
    found: dict[str, dict[str, object]] = {}
    pos = header_size
    end = min(total, len(buf))
    while pos + 8 <= end:
        ctype, chs, csize = struct.unpack_from("<HHI", buf, pos)
        if csize < 8:
            raise ApkError("チャンクが壊れています")
        if ctype == _STRING_POOL:
            strings = _decode_pool(buf, pos)
        elif ctype == _RESOURCE_MAP:
            n = (csize - chs) // 4
            res_ids = list(struct.unpack_from(f"<{n}I", buf, pos + chs))
        elif ctype == _START_ELEMENT:
            ext = pos + chs
            _ns, name_i, attr_start, attr_size, attr_count = struct.unpack_from("<IIHHH", buf, ext)
            tag = strings[name_i] if name_i < len(strings) else ""
            if tag in ("manifest", "uses-sdk"):
                attrs: dict[str, object] = {}
                for i in range(attr_count):
                    a = ext + attr_start + i * attr_size
                    _ans, aname, raw, _vsize, _res0, dtype, data = struct.unpack_from("<IIIHBBI", buf, a)
                    key = _ATTR_IDS.get(res_ids[aname]) if aname < len(res_ids) else None
                    key = key or (strings[aname] if aname < len(strings) else "")
                    if dtype == _TYPE_STRING:
                        idx = raw if raw != 0xFFFFFFFF else data
                        attrs[key] = strings[idx] if idx < len(strings) else ""
                    elif dtype in (_TYPE_INT_DEC, _TYPE_INT_HEX):
                        attrs[key] = data
                    # それ以外（@string/... などの参照）は resources.arsc が無いと解決できないので捨てる
                found.setdefault(tag, attrs)
                if len(found) == 2:
                    break
        pos += csize
    return found


def read_apk(path: str) -> ApkInfo:
    try:
        with zipfile.ZipFile(path) as z:
            if z.getinfo("AndroidManifest.xml").file_size > _MAX_MANIFEST:
                raise ApkError("AndroidManifest.xml が大きすぎます")
            buf = z.read("AndroidManifest.xml")
    except (zipfile.BadZipFile, KeyError, OSError) as e:
        raise ApkError(f"APK として読めません: {e}") from e
    try:
        found = parse_manifest(buf)
    except (struct.error, IndexError) as e:  # 途中で切れた・壊れた AXML
        raise ApkError(f"AndroidManifest.xml が壊れています: {e}") from e
    m = found.get("manifest", {})
    pkg = m.get("package")
    code = m.get("versionCode")
    if not isinstance(pkg, str) or not pkg or not isinstance(code, int):
        raise ApkError("package / versionCode がありません")
    major = m.get("versionCodeMajor", 0)
    sdk = found.get("uses-sdk", {}).get("minSdkVersion")
    name = m.get("versionName")
    split = m.get("split")
    return ApkInfo(pkg, ((major if isinstance(major, int) else 0) << 32) | code,
                   name if isinstance(name, str) else "", sdk if isinstance(sdk, int) else None,
                   split if isinstance(split, str) else "")


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
