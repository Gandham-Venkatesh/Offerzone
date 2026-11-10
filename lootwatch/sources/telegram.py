"""Read public Telegram channels through their web preview (https://t.me/s/<channel>).

No account, bot or API key is involved: this is the same page a browser shows
for any public channel that has previews enabled. It lists the latest ~20 posts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import parse_qs, urlsplit

from bs4 import NavigableString, Tag
from bs4.element import Comment

from . import make_soup

_STRUCK = ("s", "del", "strike")


@dataclass
class TgPost:
    channel: str
    post_id: int
    ts: Optional[float]  # unix time the post was published
    text: str  # what a reader sees
    analysis_text: str  # same, with struck-through amounts marked as MRP
    links: List[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"https://t.me/{self.channel}/{self.post_id}"


@dataclass
class ChannelPage:
    status: str  # "ok" | "empty" | "no_preview" | "unrecognised"
    title: str = ""
    posts: List[TgPost] = field(default_factory=list)


def _render(node: Tag, display: List[str], analysis: List[str]) -> None:
    for child in node.children:
        if isinstance(child, Comment):
            continue
        if isinstance(child, NavigableString):
            display.append(str(child))
            analysis.append(str(child))
        elif isinstance(child, Tag):
            if child.name == "br":
                display.append("\n")
                analysis.append("\n")
            elif child.name in _STRUCK:
                inner_display: List[str] = []
                _render(child, inner_display, [])
                struck = "".join(inner_display)
                display.append(struck)
                # A crossed-out amount is the old price, whatever words surround it.
                analysis.append(f" (MRP {struck}) " if re.search(r"\d", struck) else struck)
            else:
                _render(child, display, analysis)


def _clean(text: str) -> str:
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _unwrap(href: str) -> str:
    """Telegram sometimes wraps outbound links (instant view); return the real target."""
    parts = urlsplit(href)
    if (parts.hostname or "").lower() in ("t.me", "telegram.me") and parts.path.rstrip("/") == "/iv":
        target = parse_qs(parts.query).get("url", [""])[0]
        if target:
            return target
    return href


def _timestamp(message: Tag) -> Optional[float]:
    time_el = message.select_one("a.tgme_widget_message_date time[datetime]") or message.select_one("time[datetime]")
    if not time_el:
        return None
    value = str(time_el.get("datetime", "")).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def parse_channel_page(html: str, channel: str, final_url: str = "") -> ChannelPage:
    """Parse a t.me/s/<channel> page. ``final_url`` (after redirects) reveals a missing preview."""
    if final_url:
        path = urlsplit(final_url).path
        if not path.startswith("/s/"):
            # Telegram redirects /s/<name> to /<name> when there is no public preview.
            return ChannelPage("no_preview")
    soup = make_soup(html)
    title_el = soup.select_one(".tgme_channel_info_header_title")
    page = ChannelPage("ok", title=title_el.get_text(" ", strip=True) if title_el else "")

    for message in soup.select("div.tgme_widget_message[data-post]"):
        ref = str(message.get("data-post", ""))
        match = re.fullmatch(r"([A-Za-z0-9_]+)/(\d+)", ref)
        if not match:
            continue
        text_el = message.select_one(".js-message_text") or message.select_one(".tgme_widget_message_text")
        if text_el is None:
            continue  # sticker, poll, service message...
        display: List[str] = []
        analysis: List[str] = []
        _render(text_el, display, analysis)
        text = _clean("".join(display))
        if not text:
            continue
        links: List[str] = []
        anchors = list(text_el.select("a[href]"))
        anchors += message.select("a.tgme_widget_message_inline_button[href], a.tgme_widget_message_link_preview[href]")
        for anchor in anchors:
            href = _unwrap(str(anchor.get("href", "")).strip())
            if re.match(r"^https?://", href, re.I) and href not in links:
                links.append(href)
        page.posts.append(
            TgPost(
                channel=channel,
                post_id=int(match.group(2)),
                ts=_timestamp(message),
                text=text,
                analysis_text=_clean("".join(analysis)),
                links=links,
            )
        )

    page.posts.sort(key=lambda p: p.post_id)
    if not page.posts:
        if "tgme_channel_info" in html or "tgme_channel_history" in html:
            page.status = "empty"
        elif "tgme_page" in html:
            page.status = "no_preview"
        else:
            page.status = "unrecognised"
    return page
