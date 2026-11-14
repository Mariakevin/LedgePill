"""RSS and JSON feed reader for user configured information sources."""

from __future__ import annotations

import html
import http.client
import json
import re
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from typing import ClassVar
from urllib.parse import urlparse

from gi.repository import GLib, GObject

MAX_BYTES = 1_000_000
MAX_ITEMS_PER_FEED = 20
REFRESH_SECONDS = 300
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _clean_text(raw: str) -> str:
    stripped = _HTML_TAG_RE.sub(" ", html.unescape(raw or ""))
    return " ".join(stripped.split())


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if parsed.scheme not in ("http", "https"):
            raise urllib.error.URLError("blocked non-http redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SafeRedirectHandler)


class FeedService(GObject.Object):
    """Fetch configured RSS/Atom and JSON feeds without blocking GTK."""

    __gsignals__: ClassVar[dict] = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, feeds: list[dict] | None = None):
        super().__init__()
        self.feeds = self._normalize_feeds(feeds or [])
        self.items: list[dict] = []
        self.error_count = 0
        self._timer = 0
        self._busy = False
        self._generation = 0
        self._last_fetch = 0.0

    @staticmethod
    def _normalize_feeds(feeds: list[dict]) -> list[dict]:
        normalized = []
        for item in feeds[:20]:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            kind = item.get("type", "rss")
            parsed = urlparse(url if isinstance(url, str) else "")
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                continue
            if kind not in ("rss", "json"):
                continue
            normalized.append(
                {
                    "name": str(item.get("name") or parsed.hostname),
                    "url": url,
                    "type": kind,
                }
            )
        return normalized

    def start(self):
        if not self._timer:
            if (
                not self._last_fetch
                or time.monotonic() - self._last_fetch >= REFRESH_SECONDS
            ):
                self.refresh_now()
            self._timer = GLib.timeout_add_seconds(REFRESH_SECONDS, self._tick)

    def stop(self):
        self._generation += 1
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0

    def _tick(self):
        self.refresh_now()
        return True

    def refresh_now(self):
        if self._busy or not self.feeds:
            return
        self._busy = True
        generation = self._generation
        threading.Thread(target=self._fetch, args=(generation,), daemon=True).start()

    @staticmethod
    def _read(url: str) -> bytes:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "LostIsland/1.0 (+RSS and JSON feed reader)"},
        )
        with _OPENER.open(req, timeout=5) as response:
            data = response.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("feed is larger than 1 MB")
        return data

    @staticmethod
    def _rss_items(body: bytes, source: str) -> list[dict]:
        def node_text(parent: ET.Element, *tags: str) -> str:
            for tag in tags:
                node = parent.find(tag)
                if node is not None:
                    txt = "".join(node.itertext())
                    if txt.strip():
                        return txt
            return ""

        if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            return []
        root = ET.fromstring(body)
        entries = root.findall(".//item")
        if not entries:
            entries = root.findall(".//{*}entry")
        output = []
        for entry in entries[:MAX_ITEMS_PER_FEED]:
            title = node_text(entry, "title", "{*}title") or "Untitled"
            summary = node_text(entry, "description", "{*}summary", "{*}content")
            link = node_text(entry, "link")
            if not link:
                link_node = entry.find("{*}link")
                if link_node is not None:
                    link = link_node.attrib.get("href", "")
            output.append(
                {
                    "title": _clean_text(title) or "Untitled",
                    "summary": _clean_text(summary),
                    "url": link.strip(),
                    "source": source,
                }
            )
        return output

    @staticmethod
    def _json_items(body: bytes, source: str) -> list[dict]:
        data = json.loads(body)
        rows = data if isinstance(data, list) else None
        if isinstance(data, dict):
            for key in ("items", "entries", "results", "data"):
                if isinstance(data.get(key), list):
                    rows = data[key]
                    break
            if rows is None:
                rows = [data]
        output = []
        for row in (rows or [])[:MAX_ITEMS_PER_FEED]:
            if not isinstance(row, dict):
                continue
            title = row.get("title") or row.get("name") or row.get("text")
            if not title:
                continue
            summary = (
                row.get("summary") or row.get("description") or row.get("body") or ""
            )
            url = row.get("url") or row.get("link") or ""
            output.append(
                {
                    "title": _clean_text(str(title)),
                    "summary": _clean_text(str(summary)),
                    "url": str(url).strip(),
                    "source": source,
                }
            )
        return output

    def _fetch(self, generation: int):
        gathered = []
        failures = 0
        for feed in self.feeds:
            try:
                body = self._read(feed["url"])
                parser = self._rss_items if feed["type"] == "rss" else self._json_items
                gathered.extend(parser(body, feed["name"]))
            except (
                http.client.HTTPException,
                OSError,
                TimeoutError,
                ValueError,
                ET.ParseError,
                json.JSONDecodeError,
                urllib.error.URLError,
            ):
                failures += 1
        GLib.idle_add(self._apply, gathered[:100], failures, generation)

    def _apply(self, items: list[dict], failures: int, generation: int):
        self._busy = False
        if generation != self._generation:
            return False
        self.items = items
        self.error_count = failures
        self._last_fetch = time.monotonic()
        self.emit("changed", items)
        return False
