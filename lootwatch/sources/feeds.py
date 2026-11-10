"""RSS 2.0 and Atom feeds (deal blogs, forums, subreddits)."""

from __future__ import annotations

import email.utils
import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from . import make_soup

MAX_FEED_BYTES = 3_000_000
_ATOM = "{http://www.w3.org/2005/Atom}"
_CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"


@dataclass
class FeedItem:
    item_id: str
    ts: Optional[float]
    title: str
    text: str  # title + plain-text body, ready for the text parser
    url: str
    links: List[str] = field(default_factory=list)


class FeedError(ValueError):
    pass


def _parse_date(value: str) -> Optional[float]:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return email.utils.parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, IndexError):
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _plain(html: str) -> tuple:
    """(text, links) of an HTML fragment from a feed entry."""
    if not html:
        return "", []
    if "<" not in html:
        return html.strip(), []
    soup = make_soup(html)
    for br in soup.find_all("br"):
        br.replace_with("\n")
    links = [str(a.get("href")) for a in soup.find_all("a", href=True) if str(a.get("href", "")).startswith("http")]
    text = re.sub(r"[ \t]+", " ", soup.get_text(" "))
    return re.sub(r"\s*\n\s*", "\n", text).strip(), links


def _text(element: Optional[ET.Element]) -> str:
    return (element.text or "").strip() if element is not None else ""


def parse_feed(xml_text: str) -> List[FeedItem]:
    if len(xml_text) > MAX_FEED_BYTES:
        raise FeedError("feed too large")
    if re.search(r"<!ENTITY", xml_text, re.I):
        raise FeedError("feed declares XML entities; refusing to parse it")
    try:
        root = ET.fromstring(xml_text.lstrip("﻿ \t\r\n").encode("utf-8"))
    except ET.ParseError as exc:
        raise FeedError(f"not valid XML: {exc}") from None

    items: List[FeedItem] = []
    if root.tag == _ATOM + "feed":
        for entry in root.findall(_ATOM + "entry"):
            link = ""
            for candidate in entry.findall(_ATOM + "link"):
                if candidate.get("rel", "alternate") == "alternate" and candidate.get("href"):
                    link = candidate.get("href", "")
                    break
            title = _text(entry.find(_ATOM + "title"))
            body_el = entry.find(_ATOM + "content")
            if body_el is None:
                body_el = entry.find(_ATOM + "summary")
            body, links = _plain(_text(body_el))
            stamp = _parse_date(_text(entry.find(_ATOM + "published")) or _text(entry.find(_ATOM + "updated")))
            items.append(_item(_text(entry.find(_ATOM + "id")), stamp, title, body, link, links))
    else:
        channel = root.find("channel")
        if channel is None:
            raise FeedError("neither an RSS nor an Atom feed")
        for entry in channel.findall("item"):
            title = _text(entry.find("title"))
            link = _text(entry.find("link"))
            body, links = _plain(_text(entry.find(_CONTENT)) or _text(entry.find("description")))
            stamp = _parse_date(_text(entry.find("pubDate")))
            items.append(_item(_text(entry.find("guid")), stamp, title, body, link, links))
    return items


def _item(raw_id: str, ts: Optional[float], title: str, body: str, link: str, links: List[str]) -> FeedItem:
    title, _ = _plain(title)
    basis = raw_id or link or title
    item_id = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:20]
    text = body if (title and body.startswith(title)) else (title + "\n" + body).strip()
    return FeedItem(item_id=item_id, ts=ts, title=title, text=text[:4000], url=link, links=links)
