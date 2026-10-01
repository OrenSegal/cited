"""Turns a fetched body into the text a claim is checked against: content
classification, charset decoding and HTML text extraction. Parses, never
executes: HTML goes through html.parser and JSON-LD through json.loads."""

from __future__ import annotations

import codecs
import json
import re
from html.parser import HTMLParser
from typing import Any

__all__ = ["UNREADABLE_KINDS", "body_text", "classify", "decode_body", "extract_html_text"]

UNREADABLE_KINDS = frozenset({"pdf", "binary"})


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def classify(mime: str, body: bytes) -> str:
    """"html", "text", "pdf" or "binary", from the MIME type, or the body when there is none."""
    head = body[:1024].lstrip().lower()
    if body.startswith(b"%PDF-") or mime == "application/pdf":
        return "pdf"
    if mime in ("text/html", "application/xhtml+xml", "text/xml", "application/xml") or mime.endswith("+xml"):
        return "html"
    if mime.startswith("text/") or mime in ("application/json", "application/ld+json") or mime.endswith("+json"):
        return "text"
    if not mime:
        if head.startswith((b"<!doctype html", b"<html")) or b"<body" in head:
            return "html"
        return "binary" if b"\x00" in body[:4096] else "text"
    return "binary"


_BOMS = ((codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"))
_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.IGNORECASE)


def _text_codec(name: str | None) -> str | None:
    if not name:
        return None
    try:
        info = codecs.lookup(name.strip().strip("\"'"))
    except LookupError:
        return None
    return info.name if getattr(info, "_is_text_encoding", True) else None


def decode_body(body: bytes, header_charset: str | None) -> str:
    """Decode with BOM > Content-Type charset > <meta charset> > UTF-8 >
    windows-1252. Unknown or non-text codec names are skipped, never fatal."""
    for bom, codec in _BOMS:
        if body.startswith(bom):
            return body.decode(codec, errors="replace")
    meta = _META_CHARSET.search(body[:4096])
    for candidate in (header_charset, meta.group(1).decode("ascii", "ignore") if meta else None):
        codec = _text_codec(candidate)
        if codec:
            return body.decode(codec, errors="replace")
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("cp1252", errors="replace")


_META_KEYS = frozenset({
    "description", "og:description", "og:title", "twitter:description", "twitter:title",
})


def _ldjson_strings(raw: str) -> str:
    """Every string value in a JSON-LD block, joined. Iterative, so a hostile
    deeply-nested document cannot exhaust the recursion limit."""
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError):
        return raw
    found: list[str] = []
    stack: list[Any] = [parsed]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            found.append(node)
        elif isinstance(node, dict):
            stack.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            stack.extend(reversed(node))
    return " ".join(found)


class _TextExtractor(HTMLParser):
    """Visible text, plus meta descriptions and JSON-LD string values: the
    latter two are what a JavaScript-rendered page still exposes to a plain
    fetch."""

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip = False
        self._in_ldjson = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "script":
            attr_map = dict(attrs)
            self._in_ldjson = (attr_map.get("type") or "").strip().lower() == "application/ld+json"
            self._skip = not self._in_ldjson
        elif tag == "style":
            self._skip = True
        elif tag == "meta":
            attr_map = dict(attrs)
            key = (attr_map.get("name") or attr_map.get("property") or "").strip().lower()
            content = attr_map.get("content")
            if key in _META_KEYS and content:
                self._chunks.append(f" {content} ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = False
            self._in_ldjson = False

    def handle_data(self, data: str) -> None:
        if self._in_ldjson:
            self._chunks.append(f" {_ldjson_strings(data)} ")
        elif not self._skip:
            self._chunks.append(data)

    def text(self) -> str:
        return _squash("".join(self._chunks))


def extract_html_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 — a hostile page must degrade, not crash the run
        stripped = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", " ", html)
        return _squash(re.sub(r"<[^>]+>", " ", stripped))
    return parser.text()


def body_text(kind: str, body: bytes, header_charset: str | None) -> str:
    """The checkable text of a body already classified as `kind`; empty for pdf and binary."""
    if kind in UNREADABLE_KINDS:
        return ""
    decoded = decode_body(body, header_charset)
    return extract_html_text(decoded) if kind == "html" else _squash(decoded)
