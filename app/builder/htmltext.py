"""Turn an HTML page into the readable text underneath it.

Stdlib only, deliberately. Bloom runs on a handful of dependencies and a readability
library is a large one to add for a single tool; `html.parser` gets most of the way
there, and everything here is pure and offline so it can be tested exhaustively
without a network. If extraction quality turns out to be what limits `read_url` in
practice, `trafilatura` is the upgrade — but that should be decided against real
pages, not assumed up front.

**This is a copy of ``amber_v2/app/tools/htmltext.py``, and that is deliberate.**
It is forty lines of pure stdlib with no configuration and no state; the shared
libraries in this ecosystem are the agentic loop (`agent_runtime`) and the MCP
conventions (`agent_mcp`), and a text-extraction helper belongs to neither. Adding a
third shared package, or a dependency from Bloom to Amber, would cost more than the
duplication does — the same trade Amber's own `sentence_splitter` documents. If the
two ever diverge, they diverge harmlessly: each is used only by its own fetcher.

What it does: drops the subtrees that are never the content (scripts, styles, nav,
headers, footers, forms), keeps the ``<title>``, turns block-level boundaries into
newlines so paragraphs survive, unescapes entities, and collapses the whitespace
that HTML formatting leaves behind.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# Everything inside one of these is chrome, not content.
_SKIP = frozenset(
    {
        "script",
        "style",
        "noscript",
        "svg",
        "canvas",
        "nav",
        "header",
        "footer",
        "aside",
        "form",
        "iframe",
        "template",
    }
)

# Tags that end a line of text.
_BLOCK = frozenset(
    {
        "p", "div", "br", "li", "tr", "section", "article", "blockquote",
        "h1", "h2", "h3", "h4", "h5", "h6", "pre", "figcaption", "td", "th",
    }
)  # fmt: skip

_BLANK_LINES_RE = re.compile(r"\n{3,}")
_WHITESPACE_RE = re.compile(r"\s+")
_SPACES_RE = re.compile(r"[ \t ]+")


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._chunks: list[str] = []
        # A counter, not a flag: <nav> can contain <div> can contain <script>, and
        # leaving the skip on the first close tag would readmit the rest.
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        elif tag in _BLOCK:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        elif tag in _BLOCK:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        # Newlines inside a text run are just source formatting — HTML renders them
        # as spaces. Only block boundaries make a line break, so they are collapsed
        # here and emitted there. (This does flatten <pre>, which is the right trade
        # for readable prose and wrong for code listings.)
        data = _WHITESPACE_RE.sub(" ", data)
        if self._in_title:
            self.title += data
        else:
            self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def _tidy(text: str) -> str:
    lines = [_SPACES_RE.sub(" ", line).strip() for line in text.splitlines()]
    return _BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip()


def extract(markup: str) -> tuple[str, str]:
    """``(title, text)`` for an HTML document. Never raises on malformed markup."""
    parser = _Extractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # noqa: BLE001 — a broken page yields what parsed so far
        pass
    return _SPACES_RE.sub(" ", parser.title).strip(), _tidy(parser.text())
