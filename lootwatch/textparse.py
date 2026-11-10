"""Pull prices, MRPs and discount claims out of free-form deal text.

Deal channels write things like::

    boAt Airdopes 141 @ ₹899 (MRP ₹4,490) + ₹50 cashback
    Deal Price : 14,999 / MRP : 24,999
    Nothing Phone worth 60k for just 400 - price error!!

Everything here is pure text processing (no network), so it is cheap to test.
The guiding rule: an amount only counts as a price or an MRP when the words
around it say so. Amounts attached to coupons, cashback, minimum-order values
and the like are excluded, because pairing them with a real price is the main
source of fake "95% off" readings.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

MAX_TEXT = 4000
MAX_AMOUNT = 1e7  # ₹1 crore; anything bigger is a phone number or an order id

_NUM = r"(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d{1,2})?"
_SUF = r"(?:\s?(?P<suf>k|lakhs?|lacs?)(?![a-z0-9]))?"
_CUR = r"(?:₹|(?<![a-z])rs\.?|(?<![a-z])inr\b|(?<![a-z])rupees?\b)"
# A bare number followed by one of these is a spec, a count or a time - not a price.
_UNIT_GUARD = (
    r"(?!\s*(?:%|percent|gb|tb|mb|mah|mp\b|hz|w\b|watts?|kg|gm?\b|ml|ltr|l\b|inch|cm\b|mm\b|"
    r"pcs?\b|pieces?|pack\b|x\b|am\b|pm\b|years?|yrs?|months?|days?|hrs?|hours?|mins?|minutes?|"
    r"secs?\b|off\b|stars?|rated|users?|left|units?|nos?\b|items?|products?|orders?\b|times?|"
    r"st\b|nd\b|rd\b|th\b))"
)

# "₹400", "Rs. 1,49,999", "INR 60k"
_P_PREFIX = re.compile(_CUR + r"\s*[:.]?\s*(?P<num>" + _NUM + r")(?!\d)" + _SUF, re.I)
# "400/-", "400 Rs", "400₹" - but not the "2" in "pack of 2 ₹199"
_P_SUFFIX = re.compile(
    r"(?<![\d.,])(?P<num>" + _NUM + r")(?!\d)" + _SUF
    + r"\s*(?:(?:₹|rs\b\.?|rupees?\b|inr\b)(?!\s*[:.]?\s*\d)|/-)",
    re.I,
)
# "MRP: 60000", "Deal Price - 400", "worth 60k"
_P_LABEL = re.compile(
    r"(?<![a-z])(?P<label>m\.?r\.?p\.?|(?:deal|offer|loot|sale|selling|regular|original|actual|market|list|"
    r"final|effective|net|best|lowest|new|old)\s+price|price|worth)\s*(?:is|of|was|now)?\s*[:\-=–—]*\s*"
    r"(?P<num>" + _NUM + r")(?!\d)(?![:/]\d)" + _SUF + _UNIT_GUARD,
    re.I,
)
# "@ 400", "at just 400"
_P_AT = re.compile(
    r"(?:@|(?<![a-z])(?:at|for|in)\s+(?:just|only)\b)\s*(?P<num>" + _NUM + r")(?!\d)(?![:/]\d)" + _SUF + _UNIT_GUARD,
    re.I,
)
_PATTERNS = (_P_PREFIX, _P_SUFFIX, _P_LABEL, _P_AT)  # in priority order

_TRAIL = re.compile(r"[\s:\-=–—(\[*_~>|•.,]+$")
_CUR_TOKENS = re.compile(r"₹|\brs\.?|\binr\b|\brupees?\b|/-", re.I)

_MRP_BEFORE = re.compile(
    r"(?:\bm\.?r\.?p|\bworth|\b(?:regular|original|actual|market|list|normal|usual|real|retail|earlier|old|"
    r"previous|strike|striked|cut)\s+price|\bwas|\binstead\s+of|\brrp|\bprice\s+was|\busually)"
    r"(?:\s+(?:of|is|was|at|around|approx))?$",
    re.I,
)
_MRP_LABELS = re.compile(r"m\.?r\.?p|regular|original|actual|market|list|old|worth", re.I)
_MRP_AFTER = re.compile(r"^\s*\)?\s*(?:mrp|m\.r\.p)\b", re.I)

_SELL_BEFORE = re.compile(
    r"(?:\b(?:deal|offer|loot|sale|selling|effective|final|net|best|lowest|new|discounted|special|launch)\s+price"
    r"|\bprice(?:\s+now)?|\bnow|\bbuy\s+(?:at|for|@)|\bgrab\s+(?:it\s+)?(?:at|for|@)|\bget\s+(?:it\s+)?(?:at|for|@)"
    r"|\bavailable\s+(?:at|for|@)|\bonly|\bjust|\bat|@|\bfor|\bin|\bstarting(?:\s+(?:at|from))?"
    r"|\bstarts\s+(?:at|from)|\bfrom|\bunder|\bpay)$",
    re.I,
)

_SELL_AFTER = re.compile(r"^\s*(?:only|just)\b", re.I)

# Amounts that belong to an offer mechanic rather than to a product price.
_EXCL_BEFORE = re.compile(
    r"(?:\bcoupon(?:\s+(?:of|worth))?|\bcash\s?back(?:\s+(?:of|upto|up\s+to))?"
    r"|\bsav(?:e|ing|ings)(?:\s+(?:upto|up\s+to|extra|flat))?|\bextra|\badditional|\babove|\bover|\bmore\s+than"
    r"|\bmin(?:imum)?\.?\s*(?:order|purchase|cart|bill|spend|txn|transaction|shopping)(?:\s+value)?(?:\s+(?:of|is))?"
    r"|\b(?:orders?|purchase|shopping|spends?|bill|cart)\s+(?:of|above|over|worth|value(?:\s+of)?)"
    r"|\bcart\s+value(?:\s+(?:of|above))?|\bshipping|\bdelivery(?:\s+(?:fee|charge|charges))?"
    r"|\bemi(?:\s+(?:from|starts?(?:\s+at)?|of))?|\bup\s?to|\bmax(?:imum)?\.?(?:\s+(?:discount|cashback|off))?"
    r"|\bdiscount\s+of|\boff\s+of|\bvoucher(?:\s+(?:of|worth))?|\bgift\s?card(?:\s+(?:of|worth))?"
    r"|\bbonus(?:\s+of)?|\brewards?(?:\s+of)?|\brecharge(?:\s+(?:of|with|for))?|\bplan(?:\s+of)?|\bwin|\bearn)$",
    re.I,
)
_EXCL_AFTER = re.compile(
    r"^\s*(?:off\b|cash\s?back|back\b|discount|coupon|voucher|gv\b|gift|bonus|rewards?|credit"
    r"|/\s?mo(?:nth)?\b|per\s+month|p\.?m\.?\b|emi\b|instant|plans?\b|recharge|extra\b|saved?\b|savings?"
    r"|wallet|super\s?coins?|coins?\b|points?\b|paytm\s+cash|amazon\s+pay|delivery|shipping|fee\b|charges?|handling)",
    re.I,
)

_ARROW = re.compile(r"^\s*(?:→|➡|➜|➔|➝|⇒|⟶|=+>|-+>|>{2,}|»|\bto\b)\s*(?:only|just|@)?\s*$", re.I)

_PCT = re.compile(
    r"(?P<pre>(?:up\s?to|flat|min(?:imum)?\.?|at\s?least|extra|additional|addl\.?|save|saving|get)\s+)?"
    r"(?P<pct>\d{1,3}(?:\.\d+)?)\s?%\s*(?P<post>off|discount|cash\s?back|back|drop|less|savings?|price\s?drop)?",
    re.I,
)

_ERROR_PHRASE = re.compile(
    r"\b(?:pric(?:e|ing)\s*(?:error|mistake|glitch|bug)|error\s*(?:price|deal|loot)|wrong\s*price|mis-?priced"
    r"|glitch(?:\s*(?:deal|loot|price))?|bug\s*(?:price|deal|loot)|price\s*by\s*mistake)\b",
    re.I,
)
# A post announcing that a deal is over. Hot loots are full of "will go OOS soon" and "before it
# sells out", which must not count: those are the posts that matter most.
_EXPIRED = re.compile(
    r"(?<!gets )(?<!get )(?<!be )(?<!before )\bexpired\b"
    r"|(?<!gets )(?<!get )(?<!be )(?<!before )\bsold\s+out\b(?!\s+(?:soon|fast|quickly|anytime))"
    r"|\b(?:deal|loot|offer|sale|stock)\s+(?:is\s+)?(?:over|ended|gone|closed|finished)\b"
    r"|\bprice\s+(?:increased|hiked|is\s+back|back\s+to\s+normal)\b"
    r"|\bno\s+longer\s+available\b"
    r"|\b(?:now|already|currently|is|its|it's|went|gone|got)\s+(?:oos|out\s+of\s+stock)\b"
    r"|\b(?:oos|out\s+of\s+stock)\s+now\b"
    r"|^\W*(?:oos|out\s+of\s+stock)\b(?!\s+(?:soon|anytime|any\s+time|fast|quickly|in\b))",
    re.I | re.M,
)

# Combining strike-through marks, variation selectors and zero-width characters.
_STRIP_CHARS = dict.fromkeys(map(ord, "\u0335\u0336\u0337\u0338\ufe0f\u200b\u200c\u200d\u2060"))
_PIPE = re.compile(r"[ \t]*\|+[ \t]*")


def normalize(text: str) -> str:
    """Flatten stylised Unicode (bold/italic 'fonts', full-width digits) to plain text.

    " | " separators become line breaks: channels use them to pack several facts
    (or several products) into one line, and amounts are only ever related to
    the words of their own segment.
    """
    text = unicodedata.normalize("NFKC", text or "")
    text = text.translate(_STRIP_CHARS).replace("\xa0", " ").replace("\r", "")
    text = _PIPE.sub("\n", text)
    return text[:MAX_TEXT]


def fmt_inr(value: Optional[float]) -> str:
    """Format with Indian digit grouping: 149999 -> ₹1,49,999."""
    if value is None:
        return "?"
    number = int(round(value))
    digits = str(abs(number))
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups: List[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups + [tail])
    return ("-" if number < 0 else "") + "₹" + digits


@dataclass
class Amount:
    value: float
    start: int  # span of the digits inside the normalised text
    end: int
    kind: str  # "mrp" | "sell" | "plain" | "excluded"
    line: int


@dataclass
class ParsedText:
    text: str = ""  # the normalised text all positions refer to
    price: Optional[float] = None
    mrp: Optional[float] = None
    pair_conf: str = ""  # "labelled" | "arrow" | "inferred" | ""
    pct_claim: Optional[float] = None  # an exact "X% off" style claim
    pct_upto: Optional[float] = None  # an "upto X% off" claim (marketing, weak)
    error_phrase: str = ""
    expired: bool = False
    amounts: List[Amount] = field(default_factory=list)

    @property
    def calc_discount(self) -> float:
        if self.price is not None and self.mrp and self.mrp > self.price >= 0:
            return (self.mrp - self.price) / self.mrp * 100.0
        return 0.0

    @property
    def saving(self) -> float:
        if self.price is not None and self.mrp and self.mrp > self.price:
            return self.mrp - self.price
        return 0.0


def _to_value(num: str, suffix: Optional[str]) -> Optional[float]:
    try:
        value = float(num.replace(",", ""))
    except ValueError:
        return None
    suffix = (suffix or "").lower()
    if suffix == "k":
        value *= 1_000
    elif suffix.startswith(("lakh", "lac")):
        value *= 100_000
    return value if value <= MAX_AMOUNT else None


def _classify(text: str, start: int, end: int, forced: str) -> str:
    """Label one amount from the words on its own line, just before and just after it."""
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    line_end = len(text) if line_end == -1 else line_end
    before = _TRAIL.sub("", text[max(line_start, start - 60):start])
    after = text[end:min(line_end, end + 40)]
    if _EXCL_AFTER.match(after) or _EXCL_BEFORE.search(before):
        return "excluded"
    if forced == "mrp" or _MRP_BEFORE.search(before) or _MRP_AFTER.match(after):
        return "mrp"
    if forced == "sell" or _SELL_BEFORE.search(before) or _SELL_AFTER.match(after):
        return "sell"
    return "plain"


def extract_amounts(text: str) -> List[Amount]:
    """Find every rupee amount in ``text`` (already normalised) and label it."""
    found = []  # (priority, num_start, num_end, match_start, match_end, value, forced)
    for priority, pattern in enumerate(_PATTERNS):
        for match in pattern.finditer(text):
            value = _to_value(match.group("num"), match.group("suf"))
            if value is None:
                continue
            forced = ""
            if pattern is _P_LABEL:
                forced = "mrp" if _MRP_LABELS.search(match.group("label")) else "sell"
            elif pattern is _P_AT:
                forced = "sell"
            num_start, num_end = match.span("num")
            found.append((priority, num_start, num_end, match.start(), match.end(), value, forced))

    found.sort()
    taken: List[Tuple[int, int]] = []
    amounts: List[Amount] = []
    for _, num_start, num_end, match_start, match_end, value, forced in found:
        if any(num_start < t_end and t_start < num_end for t_start, t_end in taken):
            continue  # the same digits were already claimed by a higher-priority pattern
        taken.append((num_start, num_end))
        kind = _classify(text, match_start, match_end, forced)
        amounts.append(Amount(value, num_start, num_end, kind, text.count("\n", 0, num_start)))
    amounts.sort(key=lambda a: a.start)
    return amounts


def _distance(a: Amount, b: Amount) -> int:
    return min(abs(a.start - b.end), abs(b.start - a.end))


def _best_pair(amounts: List[Amount], text: str) -> Tuple[Optional[float], Optional[float], str]:
    """Choose the (price, mrp) pair the text most clearly states."""
    usable = [a for a in amounts if a.kind != "excluded"]
    candidates: List[Tuple[int, float, float, float]] = []  # (rank, -discount, price, mrp)

    def add(rank: int, mrp: Amount, price: Amount) -> None:
        if mrp.value > price.value >= 0:
            candidates.append((rank, -(mrp.value - price.value) / mrp.value, price.value, mrp.value))

    mrps = [a for a in usable if a.kind == "mrp"]
    others = [a for a in usable if a.kind in ("sell", "plain")]
    for mrp in mrps:
        pool = [o for o in others if o.value < mrp.value]
        near = [o for o in pool if o.line == mrp.line] or [
            o for o in pool if abs(o.line - mrp.line) <= 2 and _distance(o, mrp) <= 200
        ]
        if near:
            add(0, mrp, min(near, key=lambda o: (_distance(o, mrp), o.kind != "sell")))

    for first, second in zip(usable, usable[1:]):
        if first.line == second.line and _ARROW.match(_CUR_TOKENS.sub(" ", text[first.end:second.start])):
            add(1, first, second)

    if not mrps:
        sells = [a for a in usable if a.kind == "sell"]
        plains = [a for a in usable if a.kind == "plain"]
        if sells and len({s.value for s in sells}) == 1:
            sell = sells[0]
            pool = [p for p in plains if p.value > sell.value and abs(p.line - sell.line) <= 1]
            if pool:
                add(2, min(pool, key=lambda p: _distance(p, sell)), sell)
        elif not sells:
            by_line: Dict[int, List[Amount]] = {}
            for plain in plains:
                by_line.setdefault(plain.line, []).append(plain)
            for items in by_line.values():
                if len({i.value for i in items}) == 2:
                    add(2, max(items, key=lambda i: i.value), min(items, key=lambda i: i.value))

    if not candidates:
        return None, None, ""
    rank, _, price, mrp = min(candidates)
    return price, mrp, ("labelled", "arrow", "inferred")[rank]


def _percent_claims(text: str) -> Tuple[Optional[float], Optional[float]]:
    exact: Optional[float] = None
    upto: Optional[float] = None
    for match in _PCT.finditer(text):
        pct = float(match.group("pct"))
        if not 0 < pct <= 100:
            continue
        pre = re.sub(r"\s+", "", (match.group("pre") or "").lower())
        post = re.sub(r"\s+", "", (match.group("post") or "").lower())
        if pre in ("extra", "additional", "addl", "addl.") or post in ("cashback", "back"):
            continue  # stacking coupons and cashback are not the headline discount
        if pre == "upto":
            upto = max(upto or 0.0, pct)
        elif post or pre in ("flat", "min", "min.", "minimum", "atleast", "save", "saving"):
            exact = max(exact or 0.0, pct)
    return exact, upto


def parse_text(raw: str) -> ParsedText:
    """Analyse one deal post / product blurb."""
    text = normalize(raw)
    parsed = ParsedText(text=text)
    if not text.strip():
        return parsed
    parsed.amounts = extract_amounts(text)
    parsed.price, parsed.mrp, parsed.pair_conf = _best_pair(parsed.amounts, text)
    if parsed.price is None:
        sells = [a.value for a in parsed.amounts if a.kind == "sell"]
        if sells:
            parsed.price = min(sells)
    parsed.pct_claim, parsed.pct_upto = _percent_claims(text)
    error = _ERROR_PHRASE.search(text)
    parsed.error_phrase = error.group(0).lower() if error else ""
    parsed.expired = bool(_EXPIRED.search(text))
    return parsed


def price_near(parsed: ParsedText, position: int) -> Optional[float]:
    """Lowest real price on the line holding ``position``, or on the next lines that have one.

    Used for keyword traps so that, in a post listing several products, a cheap
    cable on another line cannot trip the trap set for a phone.
    """
    line = parsed.text.count("\n", 0, position)
    for offset in range(4):
        values = [a.value for a in parsed.amounts if a.line == line + offset and a.kind in ("sell", "plain")]
        if values:
            return min(values)
    return None
