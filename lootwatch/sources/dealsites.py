"""Deal sites that list their newest deals on an ordinary web page.

A deal reaches such a page first; the site's own Telegram channel repeats it later (DesiDime's
channel ran about twenty minutes behind its website when this was written). Reading the page
itself removes that wait.

* desidime.com/new        deals as members post them: title, price, list price, store
* dealsmagnet.com/new     the same kind of list, filled by a price robot
* pricehistory.app/price-drop   a price tracker's list of products on Amazon and Flipkart that
                          have just become cheaper, with the price each one usually has

Each card becomes one item. Items of the first two are read like a channel post whose price and
list price are already known. A tracker item carries no claim at all, only measurements: it is
judged by how far the price stands below the product's own average.

The markup these readers lean on is what a redesign is least likely to touch: data attributes,
<time> elements, struck-through prices and the order of things inside a card.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional
from urllib.parse import parse_qs, urljoin, urlsplit

from ..links import host_of
from . import make_soup

MAX_ITEMS = 150


@dataclass
class SiteItem:
    item_id: str
    ts: Optional[float]
    title: str
    text: str  # what the notification shows
    url: str  # the deal's own page on the site
    links: List[str] = field(default_factory=list)
    body: str = ""  # the card's free text (a coupon note, a description)
    price: Optional[float] = None  # the price the card states
    mrp: Optional[float] = None  # the list price the card states
    store: str = ""
    # only on tracker items
    tracked: bool = False
    usual: Optional[float] = None  # the product's average price, as measured by the tracker
    high: Optional[float] = None
    low: Optional[float] = None
    new_low: bool = False


class SiteError(ValueError):
    pass


_AMOUNT = re.compile(r"₹\s*([\d,]+(?:\.\d{1,2})?)")
_AGO = re.compile(
    r"(?<![a-z])(?:(?P<n>\d+|an?|few)\s*)?(?P<unit>sec(?:ond)?s?|min(?:ute)?s?|h(?:ou)?rs?|days?|weeks?|months?|years?)\b",
    re.I,
)
_WITHDRAWN = re.compile(r"^\W*(?:delete[d]?|duplicate|dupe|expired|removed?|test|ignore|na|n/a)\W*$", re.I)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400, "mo": 30 * 86400, "y": 365 * 86400}


def _amount(text: str) -> Optional[float]:
    match = _AMOUNT.search(text or "") or re.search(r"(?<![\d.])([\d,]+(?:\.\d{1,2})?)(?![\d%])", text or "")
    if not match:
        return None
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    return value if value > 0 else None


def age_seconds(text: str) -> Optional[float]:
    """'3 minutes ago', '7 min', 'about an hour ago', 'just now' -> seconds; None when it says nothing."""
    text = (text or "").strip().lower()
    if not text:
        return None
    if "just now" in text or "moment" in text or "less than a minute" in text:
        return 0.0
    if "yesterday" in text:
        return 86400.0
    match = _AGO.search(text)
    if not match:
        return None
    number = match.group("n")
    count = 1.0 if number in (None, "a", "an") else 3.0 if number == "few" else float(number)
    unit = match.group("unit")
    key = "mo" if unit.startswith("mo") else unit[0]
    return count * _UNIT_SECONDS[key]


def _clean(text: str) -> str:
    text = re.sub(r"[﻿​‌‍]", "", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _summary(title: str, price: Optional[float], mrp: Optional[float], store: str, body: str) -> str:
    from ..textparse import fmt_inr

    lines = [title]
    if price is not None:
        priced = fmt_inr(price)
        if mrp and mrp > price:
            priced += f" (list price {fmt_inr(mrp)}, {(mrp - price) / mrp * 100:.0f}% off)"
        lines.append(priced + (f" · {store}" if store else ""))
    elif store:
        lines.append(store)
    if body:
        lines.append(body)
    return "\n".join(lines)


# ---------------------------------------------------------------- DesiDime ---
def parse_desidime(html: str, url: str, now: float) -> List[SiteItem]:
    soup = make_soup(html)
    cards = soup.select("article[data-gtm-deal-id]") or soup.select("article.deal-card")
    items: List[SiteItem] = []
    for card in cards[:MAX_ITEMS]:
        deal_id = str(card.get("data-gtm-deal-id", "")).strip()
        permalink = str(card.get("data-permalink", "")).strip()
        title_link = None
        for anchor in card.find_all("a", href=True):
            href = str(anchor["href"])
            if "/deals/" in href and "#" not in href and anchor.get_text(strip=True):
                title_link = anchor
                break
        if title_link is None or not (deal_id or permalink):
            continue
        title = _clean(title_link.get_text(" ", strip=True))
        if _WITHDRAWN.match(title):
            continue  # members blank a duplicate by renaming it
        deal_url = urljoin("https://www.desidime.com/", permalink or str(title_link["href"]))
        if not deal_id:
            tail = re.search(r"(\d+)/?$", urlsplit(deal_url).path)
            deal_id = tail.group(1) if tail else deal_url
        # The row under the title: selling price, struck list price, "(85%)", store.
        price = mrp = None
        row = title_link.find_parent("div")
        row = row.find_next_sibling("div") if row else None
        if row is not None:
            struck = None
            for node in row.find_all(["div", "span", "s", "del"]):
                if node.name in ("s", "del") or "line-through" in (node.get("class") or []):
                    struck = node
                    break
            amounts = [float(m.group(1).replace(",", "")) for m in _AMOUNT.finditer(row.get_text(" ", strip=True))]
            if amounts:
                price = amounts[0]
                mrp = _amount(struck.get_text(" ", strip=True)) if struck is not None else None
                if mrp is None and len(amounts) > 1 and amounts[1] > price:
                    mrp = amounts[1]
        stamp = None
        for span in card.find_all("span"):
            seconds = age_seconds(span.get_text(" ", strip=True)) if "ago" in span.get_text() or "just now" in span.get_text().lower() else None
            if seconds is not None:
                stamp = now - seconds
                break
        paragraph = card.find("p")
        body = _clean(paragraph.get_text(" ", strip=True)) if paragraph else ""
        body = re.sub(r"\s*\.{3}\s*Read More$|\s*Read More$", "", body).strip()
        if body == title:
            body = ""
        store = _clean(str(card.get("data-gtm-store", "")))
        # The "Buy Now" button: the one link in the card that leaves the site.
        links = []
        for anchor in card.find_all("a", href=True):
            href = str(anchor["href"])
            if href.startswith(("http://", "https://")) and host_of(href) != "desidime.com" and href not in links:
                links.append(href)
        items.append(SiteItem(
            item_id=f"dd{deal_id}", ts=stamp, title=title, text=_summary(title, price, mrp, store, body),
            url=deal_url, links=links + [deal_url], body=body, price=price, mrp=mrp if mrp and price and mrp > price else None,
            store=store,
        ))
    if not items:
        raise SiteError("no deal cards found on the DesiDime page (layout change?)")
    return items


# ------------------------------------------------------------- DealsMagnet ---
def parse_dealsmagnet(html: str, url: str, now: float) -> List[SiteItem]:
    soup = make_soup(html)
    items: List[SiteItem] = []
    seen = set()
    for anchor in soup.select("a.MainCardAnchore[href]"):
        # The same link wraps the picture (with a coupon ribbon) and, further down, the title.
        if anchor.find("img") is not None or not anchor.get_text(strip=True):
            continue
        deal_url = str(anchor["href"])
        card = anchor.find_parent("div", class_="card")
        if card is None or deal_url in seen:
            continue
        seen.add(deal_url)
        title = _clean(anchor.get_text(" ", strip=True))
        price_el = card.select_one(".card-DealPrice")
        mrp_el = card.select_one(".card-OriginalPrice")
        price = _amount(price_el.get_text(" ", strip=True)) if price_el else None
        mrp = _amount(mrp_el.get_text(" ", strip=True)) if mrp_el else None
        time_el = card.select_one(".TimeDuration")
        seconds = age_seconds(time_el.get_text(" ", strip=True)) if time_el else None
        store = ""
        for link in card.find_all("a", href=True):
            match = re.search(r"/offers/([\w\-]+)", str(link["href"]))
            if match:
                store = match.group(1).replace("-", " ").title()
                break
        notes = []
        ribbon = card.select_one(".Ribbon")
        if ribbon is not None and ribbon.get_text(strip=True):
            notes.append(_clean(ribbon.get_text(" ", strip=True)))
        coupon = card.select_one("[onclick*=CopyCoupon]")
        code = re.search(r"CopyCoupon\(\s*'([^']{2,40})'", str(coupon.get("onclick", ""))) if coupon is not None else None
        if code and code.group(1) not in " ".join(notes):
            notes.append(f"Coupon: {code.group(1)}")
        note = " · ".join(notes)
        slug = re.search(r"([\w]+)/?$", urlsplit(deal_url).path)
        items.append(SiteItem(
            item_id=f"dm{slug.group(1) if slug else deal_url}", ts=now - seconds if seconds is not None else None,
            title=title, text=_summary(title, price, mrp, store, note), url=deal_url, links=[deal_url], body=note,
            price=price, mrp=mrp if mrp and price and mrp > price else None, store=store,
        ))
        if len(items) >= MAX_ITEMS:
            break
    if not items:
        raise SiteError("no deal cards found on the DealsMagnet page (layout change?)")
    return items


# ------------------------------------------------- PriceHistory price drops ---
def _store_link(data_link: str) -> str:
    """The product's own page at the store, from the tracker's buy button."""
    parts = urlsplit("https://" + data_link) if data_link and "://" not in data_link else urlsplit(data_link or "")
    host = (parts.hostname or "").lower()
    product = parse_qs(parts.query).get("phaproduct", [""])[0]
    if not product:
        return ""
    if host.endswith("amazon.in") and re.fullmatch(r"[A-Z0-9]{10}", product):
        return f"https://www.amazon.in/dp/{product}"
    if host.endswith("flipkart.com") and re.fullmatch(r"[A-Z0-9]{12,20}", product):
        return f"https://www.flipkart.com/product/p/itme?pid={product}"
    return ""


def parse_pricehistory(html: str, url: str, now: float) -> List[SiteItem]:
    from ..textparse import fmt_inr

    soup = make_soup(html)
    items: List[SiteItem] = []
    for card in soup.select("li[data-price]")[:MAX_ITEMS]:
        name = card.select_one("h3 a[href]") or card.select_one("a[href]")
        price = _amount(str(card.get("data-price", "")))
        if name is None or not price or not name.get_text(strip=True):
            continue
        title = _clean(name.get_text(" ", strip=True))
        page = urljoin(url, str(name["href"]))
        stamp = None
        time_el = card.find("time")
        if time_el is not None and time_el.get("datetime"):
            try:
                stamp = datetime.fromisoformat(str(time_el["datetime"]).replace("Z", "+00:00")).timestamp()
            except ValueError:
                stamp = None
        if stamp is None and time_el is not None:
            seconds = age_seconds(time_el.get_text(" ", strip=True))
            stamp = now - seconds if seconds is not None else None
        text = card.get_text(" | ", strip=True)
        low = re.search(r"\bLow\s*₹\s*([\d,]+)", text)
        high = re.search(r"\bHigh\s*₹\s*([\d,]+)", text)
        below = re.search(r"₹\s*([\d,]+)\s+below\s+its\s+average", text)
        low_v = float(low.group(1).replace(",", "")) if low else None
        high_v = float(high.group(1).replace(",", "")) if high else None
        usual = price + float(below.group(1).replace(",", "")) if below else None
        store = _clean(str(card.get("data-store", ""))).title()
        buy = card.select_one("a[data-link]")
        product_url = _store_link(str(buy.get("data-link", ""))) if buy is not None else ""
        facts = [fmt_inr(price)]
        if usual:
            facts.append(f"usually about {fmt_inr(usual)}")
        if low_v and high_v:
            facts.append(f"seen between {fmt_inr(low_v)} and {fmt_inr(high_v)}")
        slug = urlsplit(page).path.rstrip("/").rsplit("/", 1)[-1]
        items.append(SiteItem(
            item_id=f"ph{slug}:{int(price)}", ts=stamp, title=title,
            text=title + "\n" + " · ".join(facts) + (f" · {store}" if store else ""), url=page,
            links=[link for link in (product_url, page) if link], price=price, store=store, tracked=True,
            usual=usual, high=high_v, low=low_v, new_low=str(card.get("data-low", "")) == "1",
        ))
    if not items:
        raise SiteError("no price-drop cards found on the tracker page (layout change?)")
    return items


_READERS = (
    ("desidime.com", parse_desidime),
    ("dealsmagnet.com", parse_dealsmagnet),
    ("pricehistory.app", parse_pricehistory),
)
_LABELS = {"desidime.com": "DesiDime", "dealsmagnet.com": "DealsMagnet", "pricehistory.app": "PriceHistory tracker"}


def site_reader(url: str) -> Optional[Callable[[str, str, float], List[SiteItem]]]:
    """The reader for this address, or None when it is not one of the deal sites read as a page."""
    host = host_of(url)
    path = urlsplit(url).path.lower()
    if path.endswith((".atom", ".rss", ".xml")) or path.rstrip("/").endswith("/feed"):
        return None  # a feed of that site is read as a feed
    for domain, reader in _READERS:
        if host == domain or host.endswith("." + domain):
            return reader
    return None


def site_label(url: str) -> str:
    host = host_of(url)
    for domain, label in _LABELS.items():
        if host == domain or host.endswith("." + domain):
            return label
    return host
