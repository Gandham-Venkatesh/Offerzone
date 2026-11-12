"""Deal links: which sites they point to, which product they are, and where short links end up.

Loot channels are also where scam posts appear ("OnePlus at ₹99" leading to a
look-alike shop). So a link only counts as trusted when it is, or resolves to,
a store we recognise; anything else is shown with a warning and never rings as
urgent.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
import threading
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, parse_qsl, unquote, urljoin, urlsplit

import requests

# Real shops. A link that lands here is a real product page.
STORE_DOMAINS = (
    "amazon.in", "flipkart.com", "shopsy.in", "myntra.com", "ajio.com", "meesho.com", "tatacliq.com",
    "croma.com", "reliancedigital.in", "jiomart.com", "vijaysales.com", "nykaa.com", "nykaafashion.com",
    "purplle.com", "firstcry.com", "snapdeal.com", "blinkit.com", "zepto.com", "zeptonow.com", "swiggy.com",
    "bigbasket.com", "zomato.com", "pharmeasy.in", "1mg.com", "netmeds.com", "apollopharmacy.in",
    "samsung.com", "mi.com", "oneplus.in", "nothing.tech", "apple.com", "boat-lifestyle.com", "lenskart.com",
    "pepperfry.com", "decathlon.in", "bewakoof.com", "thesouledstore.com", "paytm.com", "paytmmall.com",
)
# Short links owned by a store or an affiliate network: they can only lead to partner shops.
TRUSTED_SHORTENERS = (
    "amzn.to", "amzn.in", "a.co", "fkrt.it", "fkrt.cc", "fkrt.co", "fkrt.to", "fkrt.site", "myntr.it", "myntr.in",
    "ajiio.in", "ajiio.co", "ekaro.in", "extp.in", "clnk.in", "linkredirect.in", "inr.deals", "wishlink.com",
    "onelink.me", "app.link",
    "link.amazon",  # Amazon's own short links (only Amazon can own a .amazon name)
    # DesiDime's "Buy Now" links. They are followed like any short link, because the store page
    # they end at is what identifies the product: the same deal then arrives only once, whether
    # it is read from the website, from DesiDime's channel or from somebody else's.
    "ddime.in", "visit.desidime.com",
)
# Deal communities and blogs: not shops, but a link to their own deal page is not a scam shop either.
DEAL_SITES = (
    "desidime.com", "indiafreestuff.in", "freekaamaal.com", "bigtricks.in", "flipshope.com", "grabon.in",
    "dealsmagnet.com", "pricehistoryapp.com", "pricehistory.app", "buyhatke.com", "smartprix.com", "reddit.com",
)
# Anyone can point these anywhere, so they are only trusted once they resolve to a store.
GENERIC_SHORTENERS = ("bit.ly", "bitli.in", "tinyurl.com", "cutt.ly", "t.co", "is.gd", "rb.gy", "page.link", "shorturl.at")

# Query parameters that track the click rather than identify the page.
_TRACKING_PARAMS = re.compile(
    r"^(?:tag|ref_?|linkcode|linkid|th|psc|affid|affextparam\d|utm_\w+|cmpid|_refid|_appid|source|camp|af_\w+"
    r"|smid|m|ascsubtag|creative|creativeasin|qid|sr|sprefix|crid|dib|dib_tag|otracker\w*"
    r"|ssid|lid|srno|iid|ppt|ppn|fm|spotlighttagid|as|as-\w+|requestid|suggestionid)$",
    re.I,
)
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
_META_REFRESH = re.compile(
    r"""<meta[^>]+http-equiv=["']?refresh["']?[^>]*content=["']?\s*\d+\s*;\s*url=([^"'>\s]+)""", re.I
)
_JS_REDIRECT = re.compile(
    r"""(?:window\.|document\.)?location(?:\.href)?\s*=\s*["'](https?://[^"']+)["']"""
    r"""|location\.replace\(\s*["'](https?://[^"']+)["']""",
    re.I,
)


def host_of(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _in(host: str, domains: Iterable[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


TRUSTED_KINDS = ("store", "affiliate", "dealsite")
_extra_stores: Tuple[str, ...] = ()


def set_extra_store_domains(csv: str) -> None:
    global _extra_stores
    _extra_stores = tuple(d.strip().lower().lstrip(".") for d in csv.split(",") if d.strip())


def classify_host(host: str) -> str:
    """'store' | 'affiliate' | 'dealsite' | 'generic' | 'telegram' | 'unknown'"""
    if not host:
        return "unknown"
    if host in ("t.me", "telegram.me", "telegram.org", "telegram.dog") or host.endswith(".t.me"):
        return "telegram"
    if _in(host, STORE_DOMAINS) or _in(host, _extra_stores):
        return "store"
    if _in(host, TRUSTED_SHORTENERS):
        return "affiliate"
    if _in(host, DEAL_SITES):
        return "dealsite"
    if _in(host, GENERIC_SHORTENERS):
        return "generic"
    return "unknown"


def product_key(url: str) -> Optional[str]:
    """A stable identity for the product a store URL points at (None for non-store URLs)."""
    host = host_of(url)
    if classify_host(host) != "store":
        return None
    parts = urlsplit(url)
    path = unquote(parts.path)
    query = parse_qs(parts.query)
    if host.endswith("amazon.in"):
        match = re.search(r"/(?:dp|gp/product|gp/aw/d|gp/offer-listing|product-reviews)/([A-Z0-9]{10})(?:[/?]|$)", path)
        if match:
            return "amazon:" + match.group(1)
    if host.endswith("flipkart.com") or host.endswith("shopsy.in"):
        pid = query.get("pid", [""])[0]
        if re.fullmatch(r"[A-Z0-9]{12,20}", pid):
            return "flipkart:" + pid
        match = re.search(r"/p/(itm[a-z0-9]+)", path)
        if match:
            return "flipkart:" + match.group(1)
    if host.endswith("myntra.com"):
        match = re.search(r"/(\d{5,10})(?:/buy)?/?$", path)
        if match:
            return "myntra:" + match.group(1)
    if host.endswith("ajio.com"):
        match = re.search(r"/p/([\w]+)", path)
        if match:
            return "ajio:" + match.group(1)
    kept = sorted((k, v) for k, v in parse_qsl(parts.query) if not _TRACKING_PARAMS.match(k))
    path = path.rstrip("/")
    if not path and not kept:
        return None  # a bare home page identifies nothing
    digest = hashlib.sha1(repr((path.lower(), kept)).encode("utf-8")).hexdigest()[:16]
    return f"{host}:{digest}"


_FILLER_WORDS = {
    "loot", "deal", "deals", "fast", "price", "error", "buy", "now", "grab", "at", "rs", "inr", "mrp", "off",
    "the", "a", "an", "on", "for", "just", "only", "link", "http", "https", "www", "com", "in", "to", "and",
    "with", "offer", "big", "hurry", "steal",
}


def name_words(name: str, limit: int = 6) -> List[str]:
    """The first few words of a product name that say what it is (no hype, no single letters)."""
    found: List[str] = []
    for word in re.findall(r"[a-z0-9]+", name.lower()):
        if word in _FILLER_WORDS or word in ("lowest", "new", "upto", "up", "flat") or len(word) < 2:
            continue
        if word not in found:
            found.append(word)
        if len(found) >= limit:
            break
    return found


def same_product_name(first: str, second: str) -> bool:
    """Do two product names, written by different people, name the same thing?

    Used only between two deals at the same price, for posts whose links cannot be followed to a
    store. All but one of the shorter name's leading words must be in the other name (all of
    them when it has three or fewer), and every number in it must be: "Galaxy M35" and "Galaxy
    A06", or two watches of one brand, share a price often enough.
    """
    a, b = name_words(first), name_words(second)
    (short, long_, long_text) = (a, b, second) if len(a) <= len(b) else (b, a, first)
    if len(short) < 2:
        return False
    # "128gb" in one name is "128 GB" in the other: numbers are looked for in the name run together.
    run_together = re.sub(r"[^a-z0-9]", "", long_text.lower())
    numbered = [word for word in short if any(ch.isdigit() for ch in word)]
    if any(word not in run_together for word in numbered):
        return False
    shared = sum(1 for word in short if word in long_ or (word in numbered and word in run_together))
    return shared >= (len(short) if len(short) <= 3 else len(short) - 1)


def text_fingerprint(text: str, price: Optional[float]) -> str:
    """Fallback identity when a post has no resolvable product link."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    stop = _FILLER_WORDS
    significant = [w for w in words if w not in stop and not w.isdigit() and len(w) > 1][:10]
    basis = " ".join(sorted(set(significant))) + f"|{int(price) if price is not None else ''}"
    return "text:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def _is_public_host(host: str) -> bool:
    """Refuse to follow links into private / loopback / link-local address space."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            if not ipaddress.ip_address(info[4][0]).is_global:
                return False
        except ValueError:
            return False
    return bool(infos)


@dataclass
class Resolved:
    original: str
    final: str
    host: str
    kind: str  # classify_host() of the final host
    hops: int = 0
    error: str = ""

    @property
    def trusted(self) -> bool:
        return self.kind in TRUSTED_KINDS


class LinkResolver:
    """Follow redirects of a short link until it reaches a store (or the time budget ends)."""

    def __init__(self, budget: float = 5.0, max_hops: int = 6, allow_private: bool = False) -> None:
        self.budget = budget
        self.max_hops = max_hops
        self.allow_private = allow_private
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _UA, "Accept": "text/html,*/*;q=0.8", "Accept-Language": "en-IN,en;q=0.9"})
        self._cache: Dict[str, Tuple[float, Resolved]] = {}
        self._lock = threading.Lock()

    def resolve(self, url: str) -> Resolved:
        with self._lock:
            cached = self._cache.get(url)
            if cached and time.time() - cached[0] < 3600:
                return cached[1]
        result = self._follow(url)
        with self._lock:
            if len(self._cache) > 2000:
                self._cache.clear()
            self._cache[url] = (time.time(), result)
        return result

    def _follow(self, url: str) -> Resolved:
        deadline = time.time() + self.budget
        current = url
        hops = 0
        error = ""
        while hops < self.max_hops:
            host = host_of(current)
            if classify_host(host) in ("store", "telegram", "dealsite"):
                break
            remaining = deadline - time.time()
            if remaining <= 0.2:
                error = "timeout"
                break
            if urlsplit(current).scheme not in ("http", "https"):
                error = "not-http"
                break
            if not self.allow_private and not _is_public_host(host):
                error = "blocked-address"
                break
            try:
                resp = self._session.get(
                    current, allow_redirects=False, stream=True, timeout=(min(3.0, remaining), min(4.0, remaining))
                )
            except requests.RequestException as exc:
                error = type(exc).__name__
                break
            try:
                target = ""
                if resp.status_code in (301, 302, 303, 307, 308):
                    target = resp.headers.get("Location", "")
                elif resp.status_code == 200:
                    # Some affiliate pages redirect from the page body instead of a header.
                    body = resp.raw.read(30000, decode_content=True).decode("utf-8", "replace")
                    match = _META_REFRESH.search(body)
                    js = _JS_REDIRECT.search(body)
                    target = (match.group(1) if match else "") or (js.group(1) or js.group(2) if js else "")
                else:
                    error = f"http-{resp.status_code}"
            except Exception as exc:  # truncated body, bad encoding, ...
                error = type(exc).__name__
                target = ""
            finally:
                resp.close()
            if not target:
                break
            nxt = urljoin(current, target.strip().replace("&amp;", "&"))
            if urlsplit(nxt).scheme not in ("http", "https") or nxt == current:
                break  # app deep link (intent://, flipkart://) or a loop: keep the last web URL
            current = nxt
            hops += 1
        host = host_of(current)
        return Resolved(original=url, final=current, host=host, kind=classify_host(host), hops=hops, error=error)


def deal_links(links: List[str]) -> List[str]:
    """Outbound http(s) links of a post, without Telegram-internal ones, in order, de-duplicated."""
    out: List[str] = []
    for link in links:
        link = link.strip()
        if not re.match(r"^https?://", link, re.I):
            continue
        if classify_host(host_of(link)) == "telegram":
            continue
        if link not in out:
            out.append(link)
    return out
