"""Small, dependency-free RSS and Atom collector.

The parser is deliberately separate from the database layer so feed fixtures
can be tested without making network requests. Network fetching has conservative
limits because source URLs are user-controlled configuration.
"""
from __future__ import annotations

import hashlib
import html
import ipaddress
import os
import re
import socket
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
USER_AGENT = "intel-board/0.2 (+https://github.com/sonwCode/intel-board)"
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


class FeedError(ValueError):
    """A feed could not be fetched or parsed safely."""


@dataclass(frozen=True)
class FeedEntry:
    external_id: str
    title: str
    link: Optional[str]
    summary: str
    content: str
    published_at: Optional[str]
    fingerprint: str


@dataclass(frozen=True)
class FeedFetchResult:
    entries: list[FeedEntry]
    etag: Optional[str]
    last_modified: Optional[str]
    not_modified: bool
    bytes_read: int


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _text(value: Optional[str]) -> str:
    if not value:
        return ""
    value = html.unescape(value)
    value = TAG_RE.sub(" ", value)
    return SPACE_RE.sub(" ", value).strip()


def _child_text(element: ET.Element, names: set[str]) -> str:
    for child in element.iter():
        if child is element:
            continue
        if _local_name(child.tag) in names:
            return _text("".join(child.itertext()))
    return ""


def _atom_link(element: ET.Element, base_url: str) -> Optional[str]:
    candidates: list[tuple[str, str]] = []
    for child in element:
        if _local_name(child.tag) != "link":
            continue
        href = (child.attrib.get("href") or "").strip()
        value = (child.text or "").strip()
        link = href or value
        if link:
            candidates.append((child.attrib.get("rel", "alternate"), link))
    for rel, link in candidates:
        if rel == "alternate":
            return canonicalize_url(urljoin(base_url, link))
    return canonicalize_url(urljoin(base_url, candidates[0][1])) if candidates else None


def parse_datetime(value: Optional[str]) -> Optional[str]:
    """Normalize common RSS/Atom date formats to UTC ISO-8601."""
    if not value:
        return None
    raw = value.strip()
    parsed: Optional[datetime] = None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonicalize_url(url: str) -> str:
    """Normalize a public HTTP(S) URL for matching and storage."""
    raw = (url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return ""
    if parts.username is not None or parts.password is not None:
        return ""
    host = parts.hostname.lower()
    try:
        port = parts.port
    except ValueError:
        return ""
    # IPv6 literals must stay bracketed when rebuilt as a netloc.
    netloc_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    netloc = netloc_host
    if port and not ((parts.scheme.lower() == "http" and port == 80) or (parts.scheme.lower() == "https" and port == 443)):
        netloc = f"{netloc_host}:{port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))


def fingerprint_for_entry(link: Optional[str], title: str, published_at: Optional[str], content: str) -> str:
    canonical_link = canonicalize_url(link or "")
    identity = f"url:{canonical_link}" if canonical_link else f"text:{title.strip().lower()}|{published_at or ''}|{content.strip()}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def parse_feed(xml: bytes | str, feed_url: str) -> list[FeedEntry]:
    """Parse RSS 2.x or Atom into a common entry shape."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise FeedError(f"invalid XML feed: {exc}") from exc

    entries: list[FeedEntry] = []
    entry_elements = [element for element in root.iter() if _local_name(element.tag) in {"item", "entry"}]
    for element in entry_elements:
        is_atom = _local_name(element.tag) == "entry"
        title = _child_text(element, {"title"})
        link = _atom_link(element, feed_url) if is_atom else canonicalize_url(_child_text(element, {"link"}))
        external_id = _child_text(element, {"id" if is_atom else "guid"}) or link or title
        summary = _child_text(element, {"summary", "description"})
        # RSS feeds often expose a short description plus a richer
        # content:encoded element. Prefer the rich body when present.
        content = _child_text(element, {"content", "encoded"}) or summary
        published_raw = _child_text(element, {"published", "updated", "pubdate", "date"})
        published_at = parse_datetime(published_raw)
        if not title:
            title = link or external_id or "未命名条目"
        if not external_id:
            external_id = title
        entries.append(
            FeedEntry(
                external_id=external_id.strip(),
                title=title[:240],
                link=link or None,
                summary=(summary or content)[:1000],
                content=(content or summary)[:10000],
                published_at=published_at,
                fingerprint=fingerprint_for_entry(link, title, published_at, content or summary),
            )
        )
    if not entries:
        raise FeedError("feed contains no RSS items or Atom entries")
    return entries


def _allowed_hosts() -> set[str]:
    return {host.strip().lower() for host in os.getenv("INTEL_SOURCE_ALLOWLIST", "").split(",") if host.strip()}


def validate_feed_url(url: str, *, resolve_dns: bool = False) -> str:
    """Validate a feed URL and reject credentials/private destinations."""
    canonical = canonicalize_url(url)
    if not canonical:
        raise FeedError("feed URL must be an http(s) URL without credentials")
    parts = urlsplit(canonical)
    host = (parts.hostname or "").lower()
    allowlist = _allowed_hosts()
    if allowlist and not any(host == allowed or host.endswith(f".{allowed}") for allowed in allowlist):
        raise FeedError(f"feed host is not in INTEL_SOURCE_ALLOWLIST: {host}")

    addresses: set[str] = set()
    try:
        literal = ipaddress.ip_address(host)
        addresses.add(str(literal))
    except ValueError:
        if resolve_dns:
            try:
                addresses.update(info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM))
            except OSError as exc:
                raise FeedError(f"could not resolve feed host: {host}") from exc
    for address in addresses:
        parsed = ipaddress.ip_address(address)
        if not parsed.is_global:
            raise FeedError(f"feed host resolves to a non-public address: {address}")
    return canonical


class _SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        validate_feed_url(newurl, resolve_dns=True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_feed(
    url: str,
    *,
    etag: Optional[str] = None,
    last_modified: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> FeedFetchResult:
    """Fetch and parse one feed with conditional requests and size limits."""
    canonical = validate_feed_url(url, resolve_dns=True)
    headers = {
        "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8",
        "Accept-Encoding": "identity",
        "User-Agent": USER_AGENT,
    }
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    request = Request(canonical, headers=headers)
    opener = build_opener(_SafeRedirectHandler())
    try:
        response = opener.open(request, timeout=timeout)
    except HTTPError as exc:
        if exc.code == 304:
            return FeedFetchResult([], etag, last_modified, True, 0)
        raise FeedError(f"feed returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise FeedError(f"feed request failed: {exc}") from exc

    try:
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                declared_length = int(content_length)
            except ValueError as exc:
                raise FeedError("feed response has an invalid Content-Length") from exc
            if declared_length > max_bytes:
                raise FeedError(f"feed response exceeds {max_bytes} bytes")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(min(64 * 1024, max_bytes - total + 1))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise FeedError(f"feed response exceeds {max_bytes} bytes")
        body = b"".join(chunks)
    finally:
        close = getattr(response, "close", None)
        if close:
            close()
    return FeedFetchResult(
        parse_feed(body, canonical),
        response.headers.get("ETag"),
        response.headers.get("Last-Modified"),
        False,
        len(body),
    )
