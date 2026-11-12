"""Read prices straight from store pages (the watchlist).

Extraction leans on things that survive redesigns: element ids and data
attributes on Amazon, schema.org JSON-LD and price meta tags everywhere, and
structured JSON that pages embed for their own scripts. Styling class names,
which Flipkart and others regenerate every few weeks, are deliberately not used.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..links import host_of
from . import make_soup

MAX_OFFERS = 80
_RUPEE_ONLY = re.compile(r"^\s*₹\s?[\d,]+(?:\.\d{1,2})?\s*$")
_ASIN = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:[/?]|$)")


@dataclass
class Offer:
    key: str  # stable identity, e.g. "amazon:B0ABCDEFGH"
    store: str
    title: str
    price: float
    mrp: Optional[float]
    url: str


@dataclass
class PageResult:
    kind: str  # "product" | "listing" | "none"
    offers: List[Offer] = field(default_factory=list)
    note: str = ""


def parse_amount(text: Any) -> Optional[float]:
    """First number in a price string: '₹1,49,999.00' -> 149999.0"""
    if isinstance(text, bool) or text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text) if text > 0 else None
    match = re.search(r"\d[\d,]*(?:\.\d+)?", str(text))
    if not match:
        return None
    try:
        value = float(match.group(0).replace(",", ""))
    except ValueError:
        return None
    return value if value > 0 else None


def _first_amount(soup: BeautifulSoup, selectors: Iterable[str]) -> Optional[float]:
    for selector in selectors:
        for element in soup.select(selector)[:3]:
            value = parse_amount(element.get("value") if element.name == "input" else element.get_text(" ", strip=True))
            if value:
                return value
    return None


def _clean_mrp(price: float, mrp: Optional[float]) -> Optional[float]:
    return mrp if mrp and mrp > price else None


def _rupee_values(node: Any) -> List[float]:
    values = []
    for text in node.find_all(string=_RUPEE_ONLY):
        value = parse_amount(text)
        if value:
            values.append(value)
    return values


# ---------------------------------------------------------------- Amazon ----
_AMAZON_PRICE = (
    "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
    "#corePriceDisplay_desktop_feature_div .priceToPay .a-price-whole",
    "#corePriceDisplay_mobile_feature_div .priceToPay .a-offscreen",
    "#corePriceDisplay_mobile_feature_div .priceToPay .a-price-whole",
    "#corePrice_desktop .apexPriceToPay .a-offscreen",
    "#corePrice_feature_div .a-price:not(.a-text-price) .a-offscreen",
    "#apex_desktop .priceToPay .a-offscreen",
    "#apex_desktop .priceToPay .a-price-whole",
    "#priceblock_dealprice",
    "#priceblock_ourprice",
    "#priceblock_saleprice",
    "#price_inside_buybox",
    "#newBuyBoxPrice",
    'input[name="items[0.base][customerVisiblePrice][amount]"]',
    "input#twister-plus-price-data-price",
)
_AMAZON_MRP = (
    "#corePriceDisplay_desktop_feature_div .basisPrice .a-offscreen",
    "#corePriceDisplay_mobile_feature_div .basisPrice .a-offscreen",
    "#corePrice_desktop span.a-price.a-text-price .a-offscreen",
    '#apex_desktop span.a-price.a-text-price[data-a-strike="true"] .a-offscreen',
    "#priceblock_listprice",
    ".priceBlockStrikePriceString",
)


def _amazon_product(soup: BeautifulSoup, url: str) -> PageResult:
    match = _ASIN.search(urlsplit(url).path)
    asin = match.group(1) if match else ""
    if not asin:
        field_el = soup.select_one("input#ASIN[value]")
        asin = str(field_el.get("value", "")) if field_el else ""
    title_el = soup.select_one("#productTitle") or soup.select_one("#title")
    title = title_el.get_text(" ", strip=True) if title_el else ""
    price = _first_amount(soup, _AMAZON_PRICE)
    if not price:
        availability = soup.select_one("#availability")
        state = availability.get_text(" ", strip=True) if availability else ""
        if re.search(r"unavailable|out of stock", state, re.I):
            return PageResult("product", note="currently unavailable (no price shown)")
        return PageResult("none", note="no price found on the Amazon page (layout not recognised)")
    if not asin or not title:
        return PageResult("none", note="price found but no ASIN/title; not treating it as a product page")
    mrp = _clean_mrp(price, _first_amount(soup, _AMAZON_MRP))
    return PageResult("product", [Offer(f"amazon:{asin}", "amazon.in", title, price, mrp, f"https://www.amazon.in/dp/{asin}")])


_ASIN_ONLY = re.compile(r"[A-Z0-9]{10}")


def _amazon_listing(soup: BeautifulSoup) -> PageResult:
    """Search / category results, and other Amazon grids (best sellers, deals) as a fallback.

    Result cards carry the ASIN in data-asin. Best-seller grids instead put it in the element id
    and style prices with generated class names, so there the title comes from the image's alt
    text and the price from the first bare ₹ amount in the card.
    """
    offers: List[Offer] = []
    seen = set()
    cards = soup.select('div[data-component-type="s-search-result"][data-asin]')
    if not cards:
        cards = [el for el in soup.select("div[data-asin], div[id]")
                 if _ASIN_ONLY.fullmatch(str(el.get("data-asin") or el.get("id") or ""))]
    for card in cards:
        asin = str(card.get("data-asin") or card.get("id") or "").strip()
        if not _ASIN_ONLY.fullmatch(asin) or asin in seen:
            continue
        heading = card.select_one("h2")
        image = card.select_one("img[alt]")
        title = ""
        if heading:
            title = str(heading.get("aria-label") or "") or heading.get_text(" ", strip=True)
        if not title and image:
            title = str(image.get("alt", ""))
        title = re.sub(r"^Sponsored Ad\s*[-–—]\s*", "", title).strip()
        price_el = card.select_one("span.a-price:not(.a-text-price) span.a-offscreen")
        price = parse_amount(price_el.get_text()) if price_el else None
        if not price:
            bare = _rupee_values(card)
            price = bare[0] if bare else None
        if not price or not title:
            continue
        mrp_el = card.select_one("span.a-price.a-text-price span.a-offscreen")
        mrp = _clean_mrp(price, parse_amount(mrp_el.get_text()) if mrp_el else None)
        seen.add(asin)
        offers.append(Offer(f"amazon:{asin}", "amazon.in", title, price, mrp, f"https://www.amazon.in/dp/{asin}"))
        if len(offers) >= MAX_OFFERS:
            break
    if not offers:
        return PageResult("none", note="no product cards found on the Amazon listing page")
    return PageResult("listing", offers)


# -------------------------------------------------------------- Flipkart ----
_RUPEE_IN_TEXT = re.compile(r"₹\s*([\d,]+(?:\.\d{1,2})?)")
_PCT_OFF = re.compile(r"(\d{1,3})\s*%\s*off", re.I)
# "Upto ₹15,250 Off on Exchange", "No Cost EMI from ₹1,383/month": amounts of an offer, never the price.
_OFFER_BEFORE = re.compile(r"(?:\bup\s?to|\bextra|\bsave|\bfrom|\bflat)\s*$", re.I)
_OFFER_AFTER = re.compile(r"^\s*(?:off\b|/\s*mo|per\s+month|a\s+month|cash\s?back|emi\b|discount)", re.I)


def _price_and_mrp(text: str) -> tuple:
    """(price, mrp) from the visible text of one product card.

    The first ₹ amount is the selling price. The crossed-out MRP follows it directly; some sites
    (Flipkart) render it as "₹" and the digits in separate text nodes, which is why this works on
    the card's joined text and not on individual nodes. When only "N% off" is shown, the MRP is
    worked back from it. A card with no price of its own (unavailable, coming soon) yields nothing,
    even if it still carries an exchange or EMI line.
    """
    amounts = []
    for match in _RUPEE_IN_TEXT.finditer(text):
        value = parse_amount(match.group(1))
        if not value:
            continue
        if _OFFER_BEFORE.search(text[max(0, match.start() - 14):match.start()]) or _OFFER_AFTER.match(text[match.end():match.end() + 16]):
            continue
        amounts.append((match.start(), match.end(), value))
    if not amounts:
        return None, None
    _, price_end, price = amounts[0]
    mrp = None
    if len(amounts) > 1 and amounts[1][0] - price_end <= 3 and amounts[1][2] > price:
        mrp = amounts[1][2]
    pct = _PCT_OFF.search(text)
    stated = int(pct.group(1)) if pct else 0
    if 0 < stated < 100:
        # The percentage is shown rounded, and near 90%+ one point moves the implied MRP a long way,
        # so accept any crossed-out price consistent with the stated figure give or take 1.5 points.
        # From 98% up there is no upper limit: a ₹74,990 laptop at ₹149 is 99.8% off and still
        # shown as "99% off", and that is exactly the card that must keep its real list price.
        low = price / (1 - max(stated - 1.5, 0.0) / 100.0)
        high = float("inf") if stated >= 98 else price / (1 - (stated + 1.5) / 100.0)
        if mrp is None or not low <= mrp <= high:
            mrp = round(price / (1 - stated / 100.0))  # no usable crossed-out price: work it back
    return price, _clean_mrp(price, mrp)


def _flipkart_listing(soup: BeautifulSoup) -> PageResult:
    offers: List[Offer] = []
    seen = set()
    for card in soup.select("div[data-id]"):
        pid = str(card.get("data-id", "")).strip()
        if not re.fullmatch(r"[A-Z0-9]{10,20}", pid) or pid in seen:
            continue
        link = card.select_one('a[href*="/p/"]')
        price, mrp = _price_and_mrp(card.get_text(" ", strip=True))
        if not link or not price:
            continue
        title_el = card.select_one("a[title]")
        image = card.select_one("img[alt]")
        title = (str(title_el.get("title", "")) if title_el else "") or (str(image.get("alt", "")) if image else "")
        title = title.strip() or link.get_text(" ", strip=True)[:200]
        if not title:
            continue
        href = urljoin("https://www.flipkart.com", str(link.get("href", "")))
        seen.add(pid)
        offers.append(Offer(f"flipkart:{pid}", "flipkart.com", title, price, mrp, href))
        if len(offers) >= MAX_OFFERS:
            break
    if not offers:
        return PageResult("none", note="no product cards found on the Flipkart listing page")
    return PageResult("listing", offers)


# ------------------------------------------------ JSON-LD / meta / JSON -----
def _json_blobs(soup: BeautifulSoup) -> List[Any]:
    blobs: List[Any] = []
    decoder = json.JSONDecoder()
    for script in soup.find_all("script"):
        raw = script.string or script.get_text() or ""
        if not raw or len(raw) > 4_000_000:
            continue
        kind = str(script.get("type", "")).lower()
        if "json" in kind or script.get("id") == "__NEXT_DATA__":
            try:
                blobs.append(json.loads(raw))
            except ValueError:
                start = min([i for i in (raw.find("{"), raw.find("[")) if i >= 0], default=-1)
                if start >= 0:
                    try:
                        blobs.append(decoder.raw_decode(raw[start:])[0])
                    except ValueError:
                        pass
        else:
            # e.g.  window.__myx = {...};   window.__INITIAL_STATE__ = {...}
            for match in list(re.finditer(r"(?:window\.|self\.)?[\w$.]{3,60}\s*=\s*(?=\{\")", raw))[:4]:
                try:
                    blobs.append(decoder.raw_decode(raw[match.end():])[0])
                except ValueError:
                    continue
    return blobs


def _ld_products(blob: Any) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    stack = [blob]
    visited = 0
    while stack and visited < 5000:
        node = stack.pop()
        visited += 1
        if isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            kind = node.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if "Product" in kinds and node.get("offers") is not None:
                found.append(node)
            else:
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return found


def _ld_price(offers: Any) -> tuple:
    """(price, list_price) from a schema.org Offer / AggregateOffer / list of offers."""
    if isinstance(offers, list):
        best = (None, None)
        for entry in offers:
            price, mrp = _ld_price(entry)
            if price and (best[0] is None or price < best[0]):
                best = (price, mrp)
        return best
    if not isinstance(offers, dict):
        return None, None
    price = parse_amount(offers.get("price")) or parse_amount(offers.get("lowPrice"))
    mrp = None
    specs = offers.get("priceSpecification")
    for spec in specs if isinstance(specs, list) else [specs]:
        if not isinstance(spec, dict):
            continue
        value = parse_amount(spec.get("price"))
        if re.search(r"ListPrice|StrikethroughPrice|SRP|MSRP", str(spec.get("priceType", "")), re.I):
            mrp = value
        elif not price:
            price = value
    return price, mrp


_NAME_KEYS = ("name", "title", "productName", "product_name", "displayName", "display_name", "productTitle")
_PRICE_KEYS = (
    "sellingPrice", "selling_price", "discountedSellingPrice", "discountedPrice", "discounted_price",
    "offerPrice", "offer_price", "salePrice", "sale_price", "finalPrice", "final_price", "price", "sp",
)
_MRP_KEYS = (
    "mrp", "markedPrice", "marked_price", "maxRetailPrice", "originalPrice", "original_price", "listPrice",
    "list_price", "strikePrice", "strike_price", "compareAtPrice",
)
_ID_KEYS = ("productId", "product_id", "id", "sku", "skuId", "sku_id", "productVariantId", "pvid", "pid")
_URL_KEYS = ("url", "landingPageUrl", "productUrl", "product_url", "link", "slug")


def _scalar(value: Any) -> Optional[float]:
    if isinstance(value, dict):
        for key in ("value", "amount", "discounted", "selling", "final"):
            if key in value:
                return _scalar(value[key])
        return None
    if isinstance(value, str) and not re.fullmatch(r"\s*₹?\s*[\d,]+(?:\.\d+)?\s*", value):
        return None
    return parse_amount(value)


def _mine(blob: Any, host: str, base_url: str, offers: Dict[str, Offer]) -> None:
    """Collect product-shaped objects (a name plus a selling price below an MRP)."""
    stack = [blob]
    visited = 0
    while stack and visited < 300_000 and len(offers) < MAX_OFFERS:
        node = stack.pop()
        visited += 1
        if isinstance(node, list):
            stack.extend(n for n in node if isinstance(n, (dict, list)))
            continue
        if not isinstance(node, dict):
            continue
        stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        name = next((node[k] for k in _NAME_KEYS if isinstance(node.get(k), str) and 3 <= len(node[k]) <= 200), None)
        if not name:
            continue
        price = mrp = None
        for key in _PRICE_KEYS:
            if key in node:
                nested = node[key]
                price = _scalar(nested)
                if isinstance(nested, dict):  # e.g. "price": {"mrp": 1999, "discounted": 799}
                    mrp = next((_scalar(nested[k]) for k in _MRP_KEYS if k in nested), None)
                if price:
                    break
        if mrp is None:
            mrp = next((_scalar(node[k]) for k in _MRP_KEYS if k in node and _scalar(node[k])), None)
        if not price or not mrp or mrp < price:
            continue
        ident = next((str(node[k]) for k in _ID_KEYS if isinstance(node.get(k), (str, int)) and str(node[k])), "")
        ident = ident or hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]
        link = next((node[k] for k in _URL_KEYS if isinstance(node.get(k), str) and node[k]), "")
        if link and not link.startswith(("http://", "https://", "/")) and "/" in link:
            link = "/" + link  # "some-product/p/123": a path from the site's root, not from this page
        url = urljoin(base_url, link) if link and ("/" in link or link.startswith("http")) else base_url
        key = f"{host}:{ident}"
        offers.setdefault(key, Offer(key, host, name.strip(), price, _clean_mrp(price, mrp), url))


def _meta(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return str(tag["content"]).strip()
    return ""


def _struck_after(soup: BeautifulSoup, price: float) -> Optional[float]:
    """The first larger ₹ amount shown right after the selling price (the crossed-out MRP)."""
    body = soup.body or soup
    text = body.get_text(" ", strip=True)[:60000]
    values = [(m.end(), parse_amount(m.group(1))) for m in _RUPEE_IN_TEXT.finditer(text)][:60]
    for index, (end, value) in enumerate(values):
        if value and abs(value - price) < 0.5:
            for _, later in values[index + 1:index + 4]:
                if later and later > price:
                    return later
            return None
    return None


def _generic(soup: BeautifulSoup, url: str, host: str, store_key: str = "") -> PageResult:
    blobs = _json_blobs(soup)
    products: List[Offer] = []
    for blob in blobs:
        for node in _ld_products(blob):
            price, mrp = _ld_price(node.get("offers"))
            name = node.get("name")
            if not price or not isinstance(name, str) or not name.strip():
                continue
            ident = str(node.get("sku") or node.get("productID") or node.get("mpn") or "")
            link = node.get("url") if isinstance(node.get("url"), str) else ""
            products.append(Offer(
                key=f"{host}:{ident or hashlib.sha1(name.encode('utf-8')).hexdigest()[:12]}",
                store=host, title=name.strip(), price=price, mrp=_clean_mrp(price, mrp),
                url=urljoin(url, link) if link else url,
            ))
    if len(products) == 1:
        offer = products[0]
        if store_key:
            offer.key = store_key
        offer.url = url
        if offer.mrp is None:
            offer.mrp = _clean_mrp(offer.price, _struck_after(soup, offer.price))
        return PageResult("product", [offer])
    if len(products) > 1:
        return PageResult("listing", products[:MAX_OFFERS])

    mined: Dict[str, Offer] = {}
    for blob in blobs:
        _mine(blob, host, url, mined)
    if mined:
        return PageResult("listing", list(mined.values()))

    price = parse_amount(_meta(soup, "product:price:amount", "og:price:amount", "twitter:data1"))
    if not price:
        tag = soup.find(attrs={"itemprop": "price"})
        price = parse_amount(tag.get("content") or tag.get_text()) if tag else None
    title = _meta(soup, "og:title", "twitter:title") or (soup.title.get_text(strip=True) if soup.title else "")
    if price and title:
        key = store_key or f"{host}:{hashlib.sha1(urlsplit(url).path.encode('utf-8')).hexdigest()[:12]}"
        return PageResult("product", [Offer(key, host, title, price, _clean_mrp(price, _struck_after(soup, price)), url)])
    return PageResult("none", note="no product data found in the page (it may need JavaScript, a login or a location)")


# ------------------------------------------------- stores that answer in JSON -----
# Some stores hand their own website a plain list of products. Asking for that list is lighter
# for them and far more exact for us than reading the page built from it.
MAX_CATALOGUE = 250
_JSON_ACCEPT = {"Accept": "application/json, text/plain, */*"}


def request_headers(url: str) -> Optional[Dict[str, str]]:
    """Extra request headers for a watched address: its product-list addresses are asked for JSON."""
    parts = urlsplit(url)
    path = parts.path.lower()
    host = (parts.hostname or "").lower()
    if path.endswith(".json") or "/api/" in path or "-api/" in path or host.startswith("api."):
        return dict(_JSON_ACCEPT)
    return None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    try:
        number = float(str(value).replace(",", "").strip())
    except ValueError:
        return None
    return number if number > 0 else None


def _fynd_items(data: Dict[str, Any], site: str) -> List[Offer]:
    """Reliance Digital (the Fynd / Jio Commerce storefront)."""
    offers: List[Offer] = []
    for item in data.get("items") or []:
        if not isinstance(item, dict) or item.get("sellable") is False:
            continue
        name, slug = item.get("name"), item.get("slug")
        prices = item.get("price") if isinstance(item.get("price"), dict) else {}
        price = _number((prices.get("effective") or {}).get("min"))
        mrp = _number((prices.get("marked") or {}).get("min"))
        if not isinstance(name, str) or not name.strip() or not price:
            continue
        ident = str(item.get("uid") or item.get("item_code") or slug or name)
        link = f"https://www.{site}/product/{slug}" if isinstance(slug, str) and slug else f"https://www.{site}/"
        offers.append(Offer(f"{site}:{ident}", site, name.strip(), price, _clean_mrp(price, mrp), link))
    return offers


def _hybris_products(data: Dict[str, Any], site: str) -> List[Offer]:
    """Croma and Ajio: a "products" list whose entries carry price.value and a relative url."""
    from ..links import product_key

    offers: List[Offer] = []
    for item in data.get("products") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        price = _number((item.get("price") or {}).get("value")) if isinstance(item.get("price"), dict) else None
        if not isinstance(name, str) or not name.strip() or not price:
            continue
        mrp = None
        for key in ("mrp", "wasPriceData"):
            if isinstance(item.get(key), dict):
                mrp = _number(item[key].get("value"))
                break
        brand = (item.get("fnlColorVariantData") or {}).get("brandName") if isinstance(item.get("fnlColorVariantData"), dict) else ""
        title = name.strip() if not brand or name.lower().startswith(str(brand).lower()) else f"{brand} {name.strip()}"
        path = item.get("url") if isinstance(item.get("url"), str) else ""
        link = urljoin(f"https://www.{site}/", path) if path else f"https://www.{site}/"
        ident = str(item.get("code") or path or name)
        offers.append(Offer(product_key(link) or f"{site}:{ident}", site, title, price, _clean_mrp(price, mrp), link))
    return offers


def _shopify_products(data: Dict[str, Any], host: str) -> List[Offer]:
    """A Shopify shop's public catalogue (/products.json): the cheapest variant in stock of each product."""
    offers: List[Offer] = []
    for item in data.get("products") or []:
        if not isinstance(item, dict) or not isinstance(item.get("variants"), list):
            continue
        title, handle = item.get("title"), item.get("handle")
        best = None
        for variant in item["variants"]:
            if not isinstance(variant, dict) or variant.get("available") is False:
                continue
            price = _number(variant.get("price"))
            if price and (best is None or price < best[0]):
                best = (price, _number(variant.get("compare_at_price")))
        if best is None or not isinstance(title, str) or not title.strip() or not isinstance(handle, str):
            continue
        offers.append(Offer(f"{host}:{item.get('id') or handle}", host, title.strip(), best[0],
                            _clean_mrp(best[0], best[1]), f"https://{host}/products/{handle}"))
    return offers


def _json_page(url: str, text: str) -> PageResult:
    host = host_of(url)
    try:
        data = json.loads(text)
    except ValueError:
        return PageResult("none", note="the address answered with something that is neither a page nor valid JSON")
    offers: List[Offer] = []
    if isinstance(data, dict):
        products = data.get("products")
        first = products[0] if isinstance(products, list) and products and isinstance(products[0], dict) else {}
        if isinstance(data.get("items"), list) and host.endswith("reliancedigital.in"):
            offers = _fynd_items(data, "reliancedigital.in")
        elif "variants" in first and "handle" in first:
            offers = _shopify_products(data, host)
        elif isinstance(first.get("price"), dict):
            site = "croma.com" if host.endswith("croma.com") else "ajio.com" if host.endswith("ajio.com") else host
            offers = _hybris_products(data, site)
    if not offers:
        mined: Dict[str, Offer] = {}
        _mine(data, host, url, mined)
        offers = list(mined.values())
    if not offers:
        return PageResult("none", note="no products found in the JSON this address returned")
    return PageResult("listing", offers[:MAX_CATALOGUE])


def _vijaysales_listing(soup: BeautifulSoup) -> PageResult:
    offers: List[Offer] = []
    for card in soup.select("div.product-card[data-child-sku]"):
        sku = str(card.get("data-child-sku", "")).strip()
        name = card.select_one(".product-name")
        link = card.select_one("a[href]")
        price_el = card.select_one("[data-price]")
        price = _number(price_el.get("data-price")) if price_el is not None else None
        if not sku or name is None or not price or not name.get_text(strip=True):
            continue
        mrp_el = card.select_one(".originalPrice")
        mrp = parse_amount(mrp_el.get_text(" ", strip=True)) if mrp_el is not None else None
        href = urljoin("https://www.vijaysales.com/", str(link.get("href", ""))) if link is not None else "https://www.vijaysales.com/"
        offers.append(Offer(f"vijaysales.com:{sku}", "vijaysales.com", name.get_text(" ", strip=True), price,
                            _clean_mrp(price, mrp), href))
        if len(offers) >= MAX_OFFERS:
            break
    if not offers:
        return PageResult("none", note="no product cards found on the Vijay Sales page")
    return PageResult("listing", offers)


def extract_page(url: str, html: str) -> PageResult:
    """Offers found on a watched page, whatever store it belongs to."""
    host = host_of(url)
    path = urlsplit(url).path
    if html.lstrip()[:1] in ("{", "["):
        return _json_page(url, html)
    soup = make_soup(html)
    if host.endswith("vijaysales.com") and "/p/" not in path:
        return _vijaysales_listing(soup)
    if host.endswith("amazon.in"):
        if _ASIN.search(path):
            return _amazon_product(soup, url)
        return _amazon_listing(soup)
    if host.endswith("flipkart.com"):
        if "/p/" in path:
            from ..links import product_key

            return _generic(soup, url, "flipkart.com", store_key=product_key(url) or "")
        return _flipkart_listing(soup)
    return _generic(soup, url, host)
