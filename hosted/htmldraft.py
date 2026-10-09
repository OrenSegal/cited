"""A web page as a draft: its prose and links rewritten as Markdown, so the
same extractor reads a blog post that it reads a .md file. Standard library
only. Parses, never executes.

Kept: headings (so a "References" section is still bibliography), paragraph
and list breaks, table rows, and every <a href> as [text](absolute url).
Dropped: script, style, navigation chrome (nav, header, footer, aside, form),
and anything hidden. Relative links resolve against the page's final URL;
only http(s) links survive. The links are fetched later through safe_fetch
like any other source, so nothing here needs to be trusted.
"""

from __future__ import annotations

import re
import urllib.parse
from html.parser import HTMLParser
from typing import Any

__all__ = ["html_to_markdown"]

SKIP = frozenset({"script", "style", "noscript", "template", "svg", "nav", "header", "footer", "aside", "form",
                  "button", "select", "iframe"})
VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"})
BLOCK = frozenset({"p", "div", "section", "article", "main", "blockquote", "pre", "table", "ul", "ol", "dl", "dd",
                   "dt", "figure", "figcaption", "hr", "br"})
HEADING = {f"h{i}": "#" * i for i in range(1, 7)}


class _Draft(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.out: list[str] = []
        self.title: list[str] = []
        self._skip: list[str] = []  # stack of open skipped tags
        self._in_title = False
        self._link: tuple[str, list[str]] | None = None  # (href, text chunks)

    def _emit(self, text: str) -> None:
        if self._link is not None:
            self._link[1].append(text)
        else:
            self.out.append(text)

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        attr = dict(attrs)
        if self._skip:
            if tag not in VOID:
                self._skip.append(tag)
            return
        hidden = "hidden" in attr or attr.get("aria-hidden") == "true" or \
            re.search(r"display\s*:\s*none", attr.get("style") or "", re.IGNORECASE)
        if tag in SKIP or hidden:
            if tag not in VOID:
                self._skip.append(tag)
            return
        if tag == "title":
            self._in_title = True
        elif tag in HEADING:
            self._emit(f"\n\n{HEADING[tag]} ")
        elif tag in BLOCK:
            self._emit("\n\n")
        elif tag == "li":
            self._emit("\n- ")
        elif tag == "tr":
            self._emit("\n\n")
        elif tag in ("td", "th"):
            self._emit(" ")
        elif tag == "a" and self._link is None:
            href = urllib.parse.urljoin(self.base_url, (attr.get("href") or "").strip())
            self._link = (href, [])

    def handle_endtag(self, tag: str) -> None:
        if self._skip:
            if tag == self._skip[-1]:
                self._skip.pop()
            return
        if tag == "title":
            self._in_title = False
        elif tag in HEADING or tag in BLOCK:
            self._emit("\n\n")
        elif tag in ("td", "th"):
            self._emit(". ")
        elif tag == "a" and self._link is not None:
            href, chunks = self._link
            self._link = None
            text = re.sub(r"\s+", " ", "".join(chunks)).strip().replace("[", "(").replace("]", ")")
            if text and href.lower().startswith(("http://", "https://")):
                self.out.append(f"[{text}]({href.replace(' ', '%20').replace(')', '%29')})")
            else:
                self.out.append(text)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title.append(data)
        elif not self._skip:
            self._emit(re.sub(r"\s+", " ", data))


def html_to_markdown(html: str, base_url: str) -> tuple[str, str]:
    """(title, markdown) for an HTML page."""
    parser = _Draft(base_url)
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 — a hostile page must degrade, not crash the request
        pass
    text = "".join(parser.out)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return re.sub(r"\s+", " ", "".join(parser.title)).strip(), text.strip() + "\n"
