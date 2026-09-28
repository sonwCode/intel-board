from __future__ import annotations

from email.utils import format_datetime
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from app.collectors import rss


RSS_FIXTURE = b"""
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Example</title>
    <item>
      <guid>entry-1</guid>
      <title>First item</title>
      <link>https://example.com/posts/1#fragment</link>
      <description><![CDATA[<p>Short summary</p>]]></description>
      <content:encoded><![CDATA[<p>Full <strong>evidence</strong></p>]]></content:encoded>
      <pubDate>Tue, 28 Sep 2026 08:30:00 +0800</pubDate>
    </item>
  </channel>
</rss>
"""

ATOM_FIXTURE = b"""
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Atom</title>
  <entry>
    <id>tag:example.com,2026:2</id>
    <title>Atom item</title>
    <link rel="alternate" href="/posts/2" />
    <summary>Atom summary</summary>
    <content type="html"><![CDATA[<p>Atom body</p>]]></content>
    <updated>2026-09-28T01:00:00Z</updated>
  </entry>
</feed>
"""


def test_parse_rss_and_normalize_fields() -> None:
    entries = rss.parse_feed(RSS_FIXTURE, "https://example.com/feed.xml")

    assert len(entries) == 1
    entry = entries[0]
    assert entry.external_id == "entry-1"
    assert entry.link == "https://example.com/posts/1"
    assert entry.summary == "Short summary"
    assert entry.content == "Full evidence"
    assert entry.published_at == "2026-09-28T00:30:00Z"


def test_parse_atom_and_fingerprint_is_stable() -> None:
    entries = rss.parse_feed(ATOM_FIXTURE, "https://example.com/feed.xml")

    assert entries[0].link == "https://example.com/posts/2"
    assert entries[0].published_at == "2026-09-28T01:00:00Z"
    assert entries[0].fingerprint == rss.fingerprint_for_entry(
        entries[0].link, entries[0].title, entries[0].published_at, entries[0].content
    )
    assert rss.fingerprint_for_entry("https://example.com/posts/2#x", "ignored", None, "ignored") == entries[0].fingerprint


def test_parse_datetime_accepts_iso_and_rfc822() -> None:
    value = datetime(2026, 9, 28, 0, 0, tzinfo=timezone.utc)
    assert rss.parse_datetime("2026-09-28T00:00:00Z") == "2026-09-28T00:00:00Z"
    assert rss.parse_datetime(format_datetime(value)) == "2026-09-28T00:00:00Z"
    assert rss.parse_datetime("not a date") is None


def test_private_and_credential_urls_are_rejected() -> None:
    with pytest.raises(rss.FeedError):
        rss.validate_feed_url("http://127.0.0.1/feed", resolve_dns=True)
    with pytest.raises(rss.FeedError):
        rss.validate_feed_url("https://user:password@example.com/feed")


class _FakeResponse:
    def __init__(self, body: bytes, headers: dict[str, str]):
        self.body = body
        self.headers = headers
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if not self.body:
            return b""
        result, self.body = self.body[:size], self.body[size:]
        return result

    def close(self) -> None:
        self.closed = True


class _FakeOpener:
    def __init__(self, response: _FakeResponse | Exception):
        self.response = response
        self.request: Request | None = None

    def open(self, request: Request, timeout: int):
        self.request = request
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_fetch_feed_sends_conditional_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    opener = _FakeOpener(_FakeResponse(RSS_FIXTURE, {"ETag": '"v1"', "Last-Modified": "yesterday"}))
    monkeypatch.setattr(rss, "build_opener", lambda *_args: opener)

    result = rss.fetch_feed(
        "http://93.184.216.34/feed.xml",
        etag='"old"',
        last_modified="before",
    )

    assert result.not_modified is False
    assert result.etag == '"v1"'
    assert opener.request is not None
    assert opener.request.get_header("If-none-match") == '"old"'
    assert opener.request.get_header("If-modified-since") == "before"


def test_fetch_feed_handles_304(monkeypatch: pytest.MonkeyPatch) -> None:
    error = HTTPError("http://93.184.216.34/feed.xml", 304, "Not Modified", {}, None)
    opener = _FakeOpener(error)
    monkeypatch.setattr(rss, "build_opener", lambda *_args: opener)

    result = rss.fetch_feed("http://93.184.216.34/feed.xml", etag='"v1"')

    assert result.not_modified is True
    assert result.entries == []
    assert result.etag == '"v1"'


def test_fetch_feed_with_retry_retries_only_transient_errors() -> None:
    calls: list[int] = []
    delays: list[float] = []
    result = rss.FeedFetchResult([], '"v2"', None, False, 0)

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise rss.FeedError("temporary outage", retryable=True)
        return result

    actual = rss.fetch_feed_with_retry(
        "http://93.184.216.34/feed.xml",
        max_attempts=3,
        backoff_seconds=0.25,
        sleep=delays.append,
        fetcher=flaky,
    )

    assert actual is result
    assert len(calls) == 2
    assert delays == [0.25]


def test_fetch_feed_with_retry_does_not_repeat_parse_errors() -> None:
    calls: list[int] = []

    def invalid(*args, **kwargs):
        calls.append(1)
        raise rss.FeedError("invalid XML")

    with pytest.raises(rss.FeedError):
        rss.fetch_feed_with_retry(
            "http://93.184.216.34/feed.xml",
            max_attempts=3,
            sleep=lambda _: None,
            fetcher=invalid,
        )
    assert calls == [1]
