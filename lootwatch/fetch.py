"""A polite HTTP client: one steady identity per site, spaced-out requests, back-off when refused.

Staying unblocked on a single IP is mostly about not behaving like a burst:
requests to the same site are serialised and spaced, cookies are kept, and when
a site answers 403/429/503 (or serves a challenge page) the monitor leaves that
site alone for a growing cool-down instead of hammering it. Nothing here tries
to defeat CAPTCHAs or rotate identities.
"""

from __future__ import annotations

import email.utils
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, replace
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit

import requests

log = logging.getLogger("lootwatch")

try:  # optional: makes store requests look like the Chrome they claim to be at the TLS level
    from curl_cffi import requests as cffi_requests  # type: ignore

    HAS_CURL_CFFI = True
except Exception:  # pragma: no cover - depends on the platform
    cffi_requests = None
    HAS_CURL_CFFI = False

MAX_BYTES = 6_000_000

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}

# An anti-bot interstitial served with HTTP 200. "Strong" markers only ever appear on such pages;
# "weak" ones (a captcha script tag, a WAF token) also appear on normal pages, so they only count
# when the whole response is tiny.
_CHALLENGE_STRONG = re.compile(
    r"/errors/validateCaptcha|Enter the characters you see below|Pardon Our Interruption|bm-verify"
    r"|<title>\s*(?:Robot Check|Just a moment|Access Denied|Attention Required|Are you a human)",
    re.I,
)
_CHALLENGE_WEAK = re.compile(
    r"api-services-support@amazon\.com|cf-chl-|errors\.edgesuite\.net|px-captcha|awswaf\.com|gokuProps"
    r"|g-recaptcha|hcaptcha\.com/1/api",
    re.I,
)


def challenge_marker(html: str) -> str:
    """The tell-tale text of an anti-bot page, or '' for an ordinary page."""
    head = html[:200_000]
    strong = _CHALLENGE_STRONG.search(head) if len(html) < 400_000 else None
    if strong:
        return re.sub(r"<title>\s*", "", strong.group(0), flags=re.I)[:40]
    weak = _CHALLENGE_WEAK.search(head) if len(html) < 20_000 else None
    return weak.group(0)[:40] if weak else ""


def looks_like_challenge(html: str) -> bool:
    return bool(challenge_marker(html))


@dataclass
class Policy:
    min_gap: float
    max_gap: float
    base_cooldown: float
    max_cooldown: float
    impersonate: bool = False  # use the Chrome-grade TLS client when it is installed
    sniff_challenge: bool = True  # look for CAPTCHA / challenge pages behind an HTTP 200


_POLICIES: Tuple[Tuple[str, Policy], ...] = (
    # Telegram previews are made of other people's words (even the <title> is the channel's name),
    # so their content must never be able to look like a block. Telegram refuses with a status code.
    ("t.me", Policy(0.5, 1.0, 60, 900, sniff_challenge=False)),
    ("amazon.in", Policy(6.0, 10.0, 300, 7200, True)),
    ("flipkart.com", Policy(3.0, 5.0, 300, 7200, True)),
    ("myntra.com", Policy(6.0, 10.0, 300, 7200, True)),
    ("blinkit.com", Policy(6.0, 10.0, 300, 7200, True)),
    ("zepto.com", Policy(6.0, 10.0, 300, 7200, True)),
    ("zeptonow.com", Policy(6.0, 10.0, 300, 7200, True)),
    ("swiggy.com", Policy(6.0, 10.0, 300, 7200, True)),
)
_DEFAULT_POLICY = Policy(3.0, 5.0, 120, 3600, True)
_LOCAL_POLICY = Policy(0.0, 0.0, 1, 5)  # localhost: tests and self-hosted services


_gap_overrides: Dict[str, Tuple[float, float]] = {}


def set_site_gaps(spec: str) -> None:
    """Change how long to wait between two requests to a site: "flipkart.com=3-5, t.me=0.5-1".

    Everything else about the site's treatment stays (the back-off after a refusal above all).
    Entries that make no sense are ignored with a warning; a gap is never allowed under 0.3 s.
    """
    _gap_overrides.clear()
    for entry in (spec or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        match = re.fullmatch(r"([a-z0-9.\-]+)\s*=\s*(\d+(?:\.\d+)?)\s*(?:-\s*(\d+(?:\.\d+)?))?", entry, re.I)
        if not match:
            log.warning("SITE_GAPS: cannot read %r (write it like flipkart.com=3-5)", entry)
            continue
        low = max(0.3, float(match.group(2)))
        high = max(low, float(match.group(3) or match.group(2)))
        _gap_overrides[match.group(1).lower().lstrip(".")] = (low, high)


def policy_for(host: str) -> Policy:
    if host in ("localhost", "127.0.0.1", "::1"):
        return _LOCAL_POLICY
    policy = _DEFAULT_POLICY
    for domain, candidate in _POLICIES:
        if host == domain or host.endswith("." + domain):
            policy = candidate
            break
    for domain, (low, high) in _gap_overrides.items():
        if host == domain or host.endswith("." + domain):
            return replace(policy, min_gap=low, max_gap=high)
    return policy


@dataclass
class FetchResult:
    url: str
    status: int = 0
    text: str = ""
    final_url: str = ""
    elapsed: float = 0.0
    error: str = ""  # network failure, or "cooldown" when the site is being left alone
    blocked: str = ""  # why we think the site refused us
    skipped: bool = False
    retry_after: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == 200 and not self.error and not self.blocked

    @property
    def problem(self) -> str:
        if self.ok:
            return ""
        return self.blocked or self.error or f"HTTP {self.status}"


class _Site:
    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self.lock = threading.Lock()
        self.session: Optional[object] = None
        self.next_ok = 0.0
        self.cooldown_until = 0.0
        self.strikes = 0
        self.last_refusal = ""


def _retry_after(value: Optional[str]) -> float:
    if not value:
        return 0.0
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        return max(0.0, parsed.timestamp() - time.time())
    except (TypeError, ValueError):
        return 0.0


def _decode(content: bytes, content_type: str) -> str:
    match = re.search(r"charset=([\w\-]+)", content_type or "", re.I)
    for encoding in ([match.group(1)] if match else []) + ["utf-8"]:
        try:
            return content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", "replace")


class Fetcher:
    def __init__(self, use_curl_cffi: str = "auto", timeout: float = 15.0, profile: str = "chrome124") -> None:
        self.timeout = timeout
        self.profile = profile or "chrome124"
        self.impersonation = HAS_CURL_CFFI and use_curl_cffi != "off"
        if use_curl_cffi == "on" and not HAS_CURL_CFFI:
            log.warning("USE_CURL_CFFI=on but curl_cffi is not installed; using plain requests")
        self._sites: Dict[str, _Site] = {}
        self._lock = threading.Lock()

    def _site(self, host: str) -> _Site:
        with self._lock:
            site = self._sites.get(host)
            if site is None:
                site = self._sites[host] = _Site(policy_for(host))
            return site

    def _session(self, site: _Site):
        if site.session is None:
            if self.impersonation and site.policy.impersonate:
                # curl_cffi sends that browser's own header set and TLS handshake; do not override them.
                try:
                    site.session = cffi_requests.Session(impersonate=self.profile)
                except Exception:  # a profile name this curl_cffi version does not know
                    log.warning("curl_cffi has no browser profile %r; using its default Chrome profile", self.profile)
                    self.profile = "chrome"
                    site.session = cffi_requests.Session(impersonate="chrome")
                site.session.headers.update({"Accept-Language": "en-IN,en;q=0.9"})
            else:
                session = requests.Session()
                session.headers.update(_BROWSER_HEADERS)
                site.session = session
        return site.session

    def cooling_down(self, url: str) -> float:
        """Seconds this URL's site is still being left alone (0 when it may be fetched)."""
        host = (urlsplit(url).hostname or "").lower()
        return max(0.0, self._site(host).cooldown_until - time.time())

    def get(self, url: str, headers: Optional[Dict[str, str]] = None, deadline: Optional[float] = None) -> FetchResult:
        host = (urlsplit(url).hostname or "").lower()
        if not host:
            return FetchResult(url, error="bad url")
        site = self._site(host)
        with site.lock:
            now = time.time()
            if now < site.cooldown_until:
                return FetchResult(
                    url, skipped=True,
                    error=f"cooldown {int(site.cooldown_until - now)}s after {site.last_refusal or 'a refusal'}",
                )
            wait = site.next_ok - now
            if deadline is not None and now + max(0.0, wait) > deadline:
                return FetchResult(url, error="skipped: cycle time budget used up", skipped=True)
            if wait > 0:
                time.sleep(wait)
            started = time.time()
            result = self._request(site, url, headers)
            result.elapsed = time.time() - started
            site.next_ok = time.time() + random.uniform(site.policy.min_gap, site.policy.max_gap)
            if result.blocked or result.status in (403, 429, 503):
                site.strikes += 1
                cooldown = min(site.policy.max_cooldown, site.policy.base_cooldown * 2 ** (site.strikes - 1))
                cooldown = max(cooldown * random.uniform(0.8, 1.2), min(result.retry_after, site.policy.max_cooldown))
                site.cooldown_until = time.time() + cooldown
                if not result.blocked:
                    result.blocked = f"HTTP {result.status}"
                site.last_refusal = result.blocked
                log.warning("%s refused us (%s); leaving it alone for %ds", host, result.blocked, int(cooldown))
            elif result.ok:
                site.strikes = 0
            return result

    def _request(self, site: _Site, url: str, headers: Optional[Dict[str, str]]) -> FetchResult:
        result = FetchResult(url)
        try:
            session = self._session(site)
            if self.impersonation and site.policy.impersonate:
                resp = session.get(url, headers=headers, timeout=self.timeout, allow_redirects=True)
                content = resp.content[:MAX_BYTES]
            else:
                resp = session.get(url, headers=headers, timeout=(6.0, self.timeout), allow_redirects=True, stream=True)
                try:
                    content = resp.raw.read(MAX_BYTES, decode_content=True)
                finally:
                    resp.close()
            result.status = int(resp.status_code)
            result.final_url = str(resp.url)
            result.text = _decode(content, resp.headers.get("Content-Type", ""))
            result.retry_after = _retry_after(resp.headers.get("Retry-After"))
            if resp.headers.get("x-amzn-waf-action"):
                result.blocked = "AWS WAF challenge"  # Amazon answers 202/403 with this header instead of a page
            elif result.status == 200 and site.policy.sniff_challenge and looks_like_challenge(result.text):
                result.blocked = f"challenge page ({challenge_marker(result.text)})"
        except Exception as exc:  # requests / curl errors, decoding problems
            result.error = _describe(exc)
        return result


def _describe(exc: Exception) -> str:
    """A network failure in a few plain words, keeping the exception name for debugging."""
    name = type(exc).__name__
    if "Timeout" in name:
        reason = "timed out"
    elif "Proxy" in name:
        reason = "refused by a proxy between this machine and the site"
    elif "SSL" in name or "Certificate" in name:
        reason = "TLS / certificate problem"
    elif "Connection" in name or "DNS" in name or "Resolve" in name:
        reason = "could not connect"
    else:
        return f"{name}: {str(exc)[:80]}"
    return f"{reason} ({name})"
