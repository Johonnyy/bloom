"""The builder's eyes, and the one thing about them that is a security property.

`read_url` fetches a URL a model chose, from a box that sits beside other services.
The protection is that a refused URL is refused **before an HTTP client exists** —
so these tests assert the client factory was never *called*, not that the returned
message matched. A test that only matched the message would keep passing if someone
moved the check inside the request, which is the exact regression worth catching.

Everything here is offline: no network, no key, no `httpx2` request ever made.
"""

from __future__ import annotations

import asyncio

from app.builder import research
from app.builder.htmltext import extract
from app.config import Settings


def _settings(**over) -> Settings:
    base = {"_env_file": None, "db_path": ":memory:", "search_api_key": "tvly-test"}
    return Settings(**{**base, **over})


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None, chunks=None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}
        self.encoding = "utf-8"
        self._chunks = chunks or []

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeClient:
    """Records every construction, so "was a client built at all" is assertable."""

    constructions = 0

    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[dict] = []

    def factory(self, *a, **kw):
        type(self).constructions += 1
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kw):
        self.calls.append({"url": url, **kw})
        return self.response

    def stream(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        return self.response


# --- the refusal, and when it happens ---------------------------------------


def test_a_refused_url_never_reaches_an_http_client():
    """The property, stated as the test: refused *before* a client is constructed."""
    client = FakeClient(FakeResponse())
    FakeClient.constructions = 0

    for url in (
        "file:///etc/passwd",
        "http://localhost/admin",
        "http://127.0.0.1:8010/admin/agents",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/internal",
        "http://bloom.internal/",
        "ftp://example.com/x",
    ):
        result = asyncio.run(
            research.read_url(url, settings=_settings(), client_factory=client.factory)
        )
        assert result.startswith("Error:"), url

    assert FakeClient.constructions == 0
    assert client.calls == []


def test_a_public_url_is_fetched_and_reduced_to_readable_text():
    markup = (
        b"<html><head><title>Spotify Web API</title></head>"
        b"<body><nav>menu menu</nav><script>var x=1</script>"
        b"<p>Use the /me/player endpoint.</p><footer>legal</footer></body></html>"
    )
    client = FakeClient(
        FakeResponse(headers={"content-type": "text/html; charset=utf-8"}, chunks=[markup])
    )
    result = asyncio.run(
        research.read_url(
            "https://developer.spotify.com/docs",
            settings=_settings(),
            client_factory=client.factory,
        )
    )
    assert "Spotify Web API" in result
    assert "Use the /me/player endpoint." in result
    # Chrome is dropped, which is the whole reason extraction exists.
    assert "menu menu" not in result
    assert "var x=1" not in result
    assert "legal" not in result


def test_a_page_larger_than_the_cap_is_truncated_rather_than_read_whole():
    """A missing or lying Content-Length must not pull an unbounded body into memory."""
    huge = [b"<p>" + b"a" * 5_000 + b"</p>" for _ in range(50)]
    client = FakeClient(FakeResponse(headers={"content-type": "text/html"}, chunks=huge))
    result = asyncio.run(
        research.read_url(
            "https://example.com/big",
            settings=_settings(read_url_max_bytes=10_000, read_url_max_chars=2_000),
            client_factory=client.factory,
        )
    )
    assert "[truncated" in result
    assert len(result) < 4_000


def test_an_unreadable_content_type_is_reported_rather_than_parsed():
    client = FakeClient(FakeResponse(headers={"content-type": "application/pdf"}))
    result = asyncio.run(
        research.read_url(
            "https://example.com/x.pdf", settings=_settings(), client_factory=client.factory
        )
    )
    assert "application/pdf" in result
    assert result.startswith("Error:")


def test_json_is_pretty_printed_rather_than_flattened_by_the_html_parser():
    """API documentation is often served as JSON; an HTML parser makes it unreadable."""
    body = b'{"openapi":"3.0.0","paths":{"/me":{"get":{"summary":"Current user"}}}}'
    client = FakeClient(FakeResponse(headers={"content-type": "application/json"}, chunks=[body]))
    result = asyncio.run(
        research.read_url(
            "https://api.example.com/openapi.json",
            settings=_settings(),
            client_factory=client.factory,
        )
    )
    assert '"summary": "Current user"' in result


# --- search ------------------------------------------------------------------


def test_search_returns_the_answer_first_and_every_snippet_with_its_url():
    """The URLs are what make read_url usable as a second hop."""
    payload = {
        "answer": "Spotify's Web API is documented at developer.spotify.com.",
        "results": [
            {
                "title": "Web API",
                "url": "https://developer.spotify.com/documentation/web-api",
                "content": "Reference for the Spotify Web API.",
            },
            {"title": "No URL", "content": "dropped"},
        ],
    }
    client = FakeClient(FakeResponse(payload=payload))
    result = asyncio.run(
        research.web_search("spotify api", settings=_settings(), client_factory=client.factory)
    )
    assert result.startswith("Spotify's Web API is documented")
    assert "https://developer.spotify.com/documentation/web-api" in result
    # A result with no URL is a dead end for the second hop, so it is dropped.
    assert "dropped" not in result


def test_search_without_a_key_says_so_rather_than_searching():
    client = FakeClient(FakeResponse())
    FakeClient.constructions = 0
    result = asyncio.run(
        research.web_search(
            "anything", settings=_settings(search_api_key=""), client_factory=client.factory
        )
    )
    assert "BLOOM_SEARCH_API_KEY" in result
    assert FakeClient.constructions == 0


def test_a_provider_error_never_echoes_the_body_that_may_quote_the_key():
    client = FakeClient(FakeResponse(status_code=401, payload={"error": "bad key tvly-test"}))
    result = asyncio.run(
        research.web_search("x", settings=_settings(), client_factory=client.factory)
    )
    assert "401" in result
    assert "tvly-test" not in result


# --- the stricter rule applied to a stored URL --------------------------------


def test_a_stored_api_base_must_be_https_and_public():
    """Stricter than read_url by one rule, because a credential gets attached to it."""
    assert research.https_public("http://example.com") == "must be https"
    assert research.https_public("https://127.0.0.1/x") is not None
    assert research.https_public("https://api.spotify.com/v1") is None


# --- extraction, pure ---------------------------------------------------------


def test_nested_chrome_does_not_readmit_content_on_the_first_close_tag():
    title, text = extract(
        "<html><title>T</title><body><nav><div><script>x</script></div>nav text</nav>"
        "<p>real</p></body></html>"
    )
    assert title == "T"
    assert "nav text" not in text
    assert "real" in text


def test_malformed_markup_yields_what_parsed_rather_than_raising():
    """Unclosed tags are normal on the real web; they must degrade, not explode."""
    title, text = extract("<html><title>T</title><body><p>kept<div>also kept")
    assert title == "T"
    assert "kept" in text and "also kept" in text

    # An unclosed <title> genuinely swallows the rest of the document — that is what
    # the parser is told, and guessing otherwise would be worse. It still returns.
    swallowed_title, swallowed_text = extract("<html><title>Half<body><p>lost")
    assert "Half" in swallowed_title
    assert swallowed_text == ""
