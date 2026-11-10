"""Pull prices, MRPs and discount claims out of free-form deal text.

Deal channels write things like::

    boAt Airdopes 141 @ ₹899 (MRP ₹4,490) + ₹50 cashback
    Deal Price : 14,999 / MRP : 24,999
    Nothing Phone worth 60k for just 400 - price error!!

Everything here is pure text processing (no network), so it is cheap to test.
The guiding rule: an amount only counts as a price or an MRP when the words
around it say so. Amounts attached to coupons, cashback, bank offers, EMIs,
minimum-order values and the like are excluded, because pairing them with a
real price is the main source of fake "95% off" readings.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

MAX_TEXT = 4000
MAX_AMOUNT = 1e7  # ₹1 crore; anything bigger is a phone number or an order id
# Marks a line break that was a " | " separator in the original text. Channels use the pipe to
# pack several facts, or several products, into one line: the words around an amount describe it
# only within its own part ("segment"), but the parts still belong to one line.
SOFT = "\x1f"

_NUM = r"(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d{1,2})?"
_SUF = r"(?:\s?(?P<suf>k|lakhs?|lacs?)(?![a-z0-9]))?"
_CUR = r"(?:₹|(?<![a-z0-9])rs\.?|(?<![a-z0-9])inr\b|(?<![a-z0-9])rupees?\b)"
# A bare number followed by one of these is a spec or a time - not a price. Only on the same line:
# "@ 400\nOrder now" is a price followed by a sentence.
_UNIT_GUARD = (
    r"(?![ \t]*(?:%|(?:percent|gb|tb|mb|mah|mp|hz|w|watts?|kg|gm?|ml|ltr|l|inch(?:es)?|cm|mm|pcs?|pieces?|pack|x|am|pm"
    r"|years?|yrs?|months?|days?|hrs?|hours?|mins?|minutes?|secs?|off|stars?|st|nd|rd|th)\b))"
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
    r"final|effective|net|best|lowest|new|old)\s+price|price|worth)\s*(?P<conn>is|of|was|now)?\s*[:\-=–—]*\s*"
    r"(?P<num>" + _NUM + r")(?!\d)(?![:/]\d)" + _SUF + _UNIT_GUARD,
    re.I,
)
# "@ 400", "at just 400"
_P_AT = re.compile(
    r"(?:@|(?<![a-z])(?:at|for|in)\s+(?:just|only)\b)\s*(?P<num>" + _NUM + r")(?!\d)(?![:/]\d)" + _SUF + _UNIT_GUARD,
    re.I,
)
_PATTERNS = (_P_PREFIX, _P_SUFFIX, _P_LABEL, _P_AT)  # in priority order

_TRAIL = re.compile(r"[\s\x1f:\-=–—(\[*_~>|•.,]+$")
_CUR_TOKENS = re.compile(r"₹|\brs\.?|\binr\b|\brupees?\b|/-", re.I)
# Links are blanked out before anything is read: "amzn.to/4ab7Rs9" must not become "Rs 9",
# and a product slug inside a URL must not trip a keyword trap.
_URL = re.compile(
    r"(?:https?://|www\.)\S+|\b[a-z0-9][a-z0-9\-]*\.(?:to|in|cc|co|it|ly|me|asia|com|app|link|site|xyz)/\S*", re.I
)

_MRP_BEFORE = re.compile(
    r"(?:\bm\.?r\.?p|\bworth|\b(?:regular|original|actual|market|list|normal|usual|real|retail|earlier|old|"
    r"previous|strike|striked|cut)\s+price|\bwas|\binstead\s+of|\brrp|\bprice\s+was|\busually)"
    r"(?:\s+(?:of|is|was|at|around|approx))?$",
    re.I,
)
# "Regular ₹45,000+": these words alone mark the usual price, but only directly before a currency
# sign. ("original" and "real" are left out: they are variant and brand names too.)
_MRP_BARE = re.compile(r"\b(?:regular|usual|normal|actual)$", re.I)
_MRP_LABELS = re.compile(r"m\.?r\.?p|regular|original|actual|market|list|old|worth", re.I)
_MRP_AFTER = re.compile(r"^\s*\)?\s*(?:mrp|m\.r\.p)\b", re.I)
# The text between two amounts when one "MRP" label sits between them: "₹400 MRP ₹45,999".
_MRP_BETWEEN = re.compile(r"^[\s\W]*(?:(?:mrp|m\.r\.p\.?)[\s\W]*)+$", re.I)

_SELL_BEFORE = re.compile(
    r"(?:\b(?:deal|offer|loot|sale|selling|effective|final|net|best|lowest|new|discounted|special|launch)\s+price"
    r"|\bprice(?:\s+now)?|\bnow|\bbuy\s+(?:at|for|@)|\bgrab\s+(?:it\s+)?(?:at|for|@)|\bget\s+(?:it\s+)?(?:at|for|@)"
    r"|\bavailable\s+(?:at|for|@)|\bonly|\bjust|\bat|@|\bfor|\bin|\bpay)$",
    re.I,
)
_SELL_AFTER = re.compile(r"^\s*(?:only|just)\b", re.I)
# "under ₹999", "starting ₹599", "from ₹1": a range or a budget, not one product's price.
_RANGE_BEFORE = re.compile(
    r"(?:\bunder|\bbelow|\bwithin|\bstarting(?:\s+(?:at|from))?|\bstarts\s+(?:at|from)|\bfrom|\bbudget)$", re.I
)
_TO_AFTER = re.compile(r"^\s*(?:/-)?\s*(?:to|→|➡|➜|-+>|=+>)\s*(?:only|just|now|@)?\s*(?:₹|rs\b|inr\b|\d)", re.I)

_BANK = (
    r"(?:hdfc|sbi|icici|axis|kotak|idfc|rbl|yes|bob|hsbc|amex|onecard|au|indusind|federal|citi|dbs|paytm|phonepe"
    r"|upi|rupay|visa|mastercard)"
)
# Amounts that belong to an offer mechanic rather than to a product price. Each wording is anchored
# directly before or after the amount, so "₹51,999 with bank offer" keeps its price.
_EXCL_BEFORE = re.compile(
    r"(?:\bcoupon(?:\s+(?:of|worth))?|\bcash\s?back(?:\s+(?:of|upto|up\s+to))?"
    r"|\bsav(?:e|ing|ings)(?:\s+(?:upto|up\s+to|extra|flat))?|\bextra|\badditional|\babove|\bover|\bmore\s+than"
    r"|\bmin(?:imum)?\.?\s*(?:order|purchase|cart|bill|spend|txn|transaction|shopping)(?:\s+value)?(?:\s+(?:of|is))?"
    r"|\b(?:orders?|purchase|shopping|spends?|bill|cart)\s+(?:of|above|over|worth|value(?:\s+of)?)"
    r"|\bcart\s+value(?:\s+(?:of|above))?|\bshipping|\bdelivery(?:\s+(?:fee|charge|charges))?"
    r"|\bemi(?:\s+(?:from|of|at|@|for|just|only|starts?(?:\s+at)?|starting(?:\s+(?:at|from))?))*|\bup\s?to"
    r"|\bmax(?:imum)?\.?\s+(?:discount|cashback|off|savings?)(?:\s+of)?"
    r"|%\s*(?:off|discount|cashback)?\s*[,(]?\s*max(?:imum)?\.?|\(\s*max(?:imum)?\.?"
    r"|\bdiscount(?:\s+of)?|\boff\s+of|\bvoucher(?:\s+(?:of|worth))?|\bgift\s?card(?:\s+(?:of|worth))?"
    r"|\bbonus(?:\s+of)?|\brewards?(?:\s+of)?|\brecharge(?:\s+(?:of|with|for))?|\bplan(?:\s+of)?|\bwin|\bearn"
    r"|\b(?:bank|cards?|" + _BANK + r")\s+(?:offer|discount|cashback)s?(?:\s+of)?"
    r"|\b(?:exchange|upgrade)\s+(?:bonus|offer|value|discount)(?:\s+of)?|\bdown\s*payment(?:\s+of)?"
    r"|\b(?:booking|token)(?:\s+amount)?|\binstallation(?:\s+(?:charges?|fee))?(?:\s+(?:for|at|of))?"
    r"|\b(?:dropped|reduced|slashed|cut|down)\s+by|\bprice\s+(?:cut|drop)(?:\s+of)?)$",
    re.I,
)
# "after bank discount ₹51,999", "post coupon ₹899": the amount is what you pay, not the offer.
_AFTER_OFFER = re.compile(r"\b(?:after|post)(?:\s+[a-z]+){0,2}\s*$", re.I)
_EXCL_AFTER = re.compile(
    r"^\s*(?:off\b|cash\s?back|back\b(?!\s+in\b)|coupon|voucher|gv\b|gift\s*(?:card|voucher|coupon)|rewards?\b"
    r"|credits?\b(?!\s+card)"
    r"|(?:" + _BANK + r"\s+)?(?:(?:credit|debit)\s+)?(?:bank|cards?|exchange|upgrade)\s+(?:discount|bonus|off|cashback)s?\b"
    r"|" + _BANK + r"\s+(?:discount|bonus|cashback|off)s?\b"
    r"|(?:instant|extra|additional|flat)\s+(?:off|discount|cashback|savings?|bank)\b|discount\b|bonus\b"
    r"|/\s?mo(?:nth)?\b|(?:a|per)\s+month|p\.?m\.?\b|x\s*\d+|emi\b|(?:on|no\s+cost)\s+emi|plans?\b|recharge"
    r"|saved?\b|savings?\b|wallet|super\s?coins?|coins?\b|points?\b|paytm\s+cash|amazon\s+pay"
    r"|(?:delivery|shipping|handling|platform|convenience)\s*(?:fee|charges?)\b"
    r"|(?:delivery|shipping)\b(?!\s+(?:in|by|within|today|tomorrow)\b)|fee\b|charges?\b)",
    re.I,
)
# "... + ₹4000 Bank Offer", "& ₹50 Amazon Pay cashback": an amount added on with a plus sign and
# named as an offer is the offer's size, whatever bank or wallet stands in between.
_ADDON_BEFORE = re.compile(r"(?:\+|&|\bplus|\band|\bextra)\s*$", re.I)
_ADDON_AFTER = re.compile(r"^\s*(?:[a-z]+\s+){0,3}(?:offers?|discounts?|bonus|cashback|off)\b", re.I)
# Wording that makes a stated price depend on buying something else.
_CONDITION = re.compile(
    r"\b(?:cart\s+value|orders?\s+(?:above|over|of|worth)|min(?:imum)?\.?\s*(?:order|purchase|cart)|on\s+orders"
    r"|(?:1st|first)\s+(?:order|month|purchase)|with\s+(?:\w+\s+){0,3}purchase|lucky\s+winners?|new\s+users?)\b",
    re.I,
)

_ARROW = re.compile(r"^\s*(?:→|➡|➜|➔|➝|⇒|⟶|=+>|-+>|>{2,}|»|\bto\b)\s*(?:only|just|now|@)?\s*$", re.I)

# Only wording that means money off makes a percentage a discount claim. "90% less oil" and
# "95% savings on your bill" describe the product.
_PCT = re.compile(
    r"(?P<pre>(?:up\s?to|flat|min(?:imum)?\.?|at\s?least|extra|additional|addl\.?|save|saving|get)\s+)?"
    r"(?P<pct>\d{1,3}(?:\.\d+)?)\s?%\s*(?P<post>off|discount|cash\s?back|back|price\s?drop)?",
    re.I,
)
_CAPPED = re.compile(r"\s*(?:up\s?to|max(?:imum)?\.?)\s*(?:₹|rs)", re.I)
_SAVE_ON = ("on", "with", "today", "now", "at", "in", "using", "instantly", "when", "this", "extra", "flat")

_ERROR_PHRASE = re.compile(
    r"(?<![a-z])(?:pric(?:e|ing)[\s\-]*(?:error|mistake|glitch|bug)|(?:error|mistake|glitch)[\s\-]*(?:price|deal|loot)"
    r"|wrong\s*price|mis-?priced?|glitch(?:\s*(?:deal|loot|price))?|bug\s*(?:price|deal|loot)"
    r"|(?:amazon|flipkart|myntra|ajio|jiomart|zepto|blinkit|instamart)\s*(?:bug|glitch)|price\s*by\s*mistake)(?![a-z])",
    re.I,
)
# "glitch free audio", "not a price error", "price error orders cancelled" do not announce one.
_ERROR_NEGATED = re.compile(
    r"glitch[\s\-]*free|\bnot?\s+(?:an?\s+)?(?:pric(?:e|ing)\s*)?(?:error|glitch|mistake)"
    r"|(?:error|glitch)\s+orders?\s+(?:(?:are|got|were|being|will\s+be)\s+)?cancell?ed",
    re.I,
)

# Wording that can mean a deal is over. Whether it does depends on the words around it: hot loots
# are full of "will go OOS soon" and "before it sells out", and those are the posts that matter most.
_STATUS = re.compile(
    r"\[\s*(?:over|expired|ended|oos|sold\s*out|closed|finished|out\s+of\s+stock)\s*\]"
    r"|\bexpired\b|\bsold\s+out\b"
    r"|\b(?:deal|loot|offer|sale|stock)\s+(?:is\s+|has\s+|got\s+)?(?:over|ended|gone|closed|finished)\b"
    r"|\bprice\s+(?:has\s+|is\s+|got\s+)?(?:increased|hiked|revised|corrected|back(?:\s+to\s+normal)?)\b"
    r"|\bno\s+longer\s+available\b"
    r"|\b(?:now|currently|is|its|it['’]s|went|gone|got|already|again)\s+(?:oos|out\s+of\s+stock)\b"
    r"|\b(?:oos|out\s+of\s+stock)\b(?=\s+(?:now|again|already)\b|\W*$)"
    r"|^\W*(?:oos|out\s+of\s+stock)\b",
    re.I | re.M,
)
_HEDGE_BEFORE = re.compile(
    r"\b(?:before|until|till|unless|if|in\s+case|almost|nearly|may|might|can|could|will|would|shall|gonna|soon"
    r"|going\s+to|about\s+to|not|never|isn['’]?t|wasn['’]?t|was|were|last\s+time|earlier|previously"
    r"|coupons?|codes?|cards?|vouchers?)\b[^.!?\n]{0,30}$",
    re.I,
)
_HEDGE_AFTER = re.compile(
    r"^\s*(?:\?|(?:very\s+)?soon\b|fast\b|quickly\b|anytime\b|any\s+(?:time|moment)\b|shortly\b|yet\b"
    r"|(?:in|for|at)\s+(?:some|few|many|most|certain|my|your)\b|earlier\b|last\s+time\b|before\b|₹|rs\b|\d)",
    re.I,
)
_WORD = re.compile(r"[A-Za-z]{2,}")

# Combining strike-through marks, variation selectors, zero-width characters and our own marker.
_STRIP_CHARS = dict.fromkeys(map(ord, "\u0335\u0336\u0337\u0338\ufe0f\u200b\u200c\u200d\u2060" + SOFT))
_PIPE = re.compile(r"[ \t]*\|+[ \t]*")
# Words that carry no product. A piece of text made only of these (and amounts) names nothing of
# its own: it is a price line, a hype line or a spec list that belongs to a product named nearby.
_FILLER = re.compile(
    r"^(?:price|pricing|deal|deals|offer|offers|loot|loots|error|glitch|bug|mrp|only|just|for|inr|now|buy|grab|get"
    r"|fast|hurry|link|here|live|again|big|mega|super|hot|new|best|lowest|ever|today|sale|steal|alert|the|and|off"
    r"|was|worth|regular|flat|read|more|click|check|out|with|from|free|delivery|bank|card|cards|coupon|code|apply"
    r"|use|extra|discount|cashback|effective|final|special|launch|limited|time|stock|order|shop|store|amazon"
    r"|flipkart|myntra|ajio|jiomart|meesho|croma|zepto|blinkit|instamart|swiggy|tata|cliq|nykaa|ram|rom|storage"
    r"|ssd|hdd|variant|colour|color|black|white|blue|green|red|grey|gray|silver|gold|pink|purple|pro|max|plus"
    r"|ultra|lite|mini|all|any|per|you|your|this|that|its|are|not|but|can|will|has|have|must|very|too|rupees"
    r"|actual|usual|normal|original|rate|cost|drop|dropped|back|available|unit|units|each"
    # words of a sale, a shop or a sales pitch: they announce a product, they do not name one
    r"|billion|day|days|great|india|indian|festival|festive|republic|diwali|dussehra|season|end|year|sales|savings"
    r"|summer|winter|monsoon|freedom|independence|prime|minutes|exclusive|launched|latest|model|brand|sealed|genuine"
    r"|official|unlocked|edition|version|vijay|reliance|digital|unbelievable|crazy|insane|massive|huge|lowest|loots"
    r"|quick|grab|hurry|missed|case|part|game|changer|remote|chance|try|both|also|then|than|just|about|today)$",
    re.I,
)


def _flatten(text: str) -> str:
    """Stylised Unicode (bold/italic 'fonts', full-width digits) to plain text; pipes to marked breaks."""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.translate(_STRIP_CHARS).replace("\xa0", " ").replace("\r", "")
    return _PIPE.sub("\n" + SOFT, text)[:MAX_TEXT]


def normalize(text: str) -> str:
    """Plain text with one fact per line: fancy characters flattened, " | " turned into line breaks."""
    return _flatten(text).replace(SOFT, "")


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


def naming_words(text: str) -> int:
    """How many words in ``text`` could be naming a product (not prices, hype, specs or filler)."""
    return sum(1 for word in re.findall(r"[A-Za-z]{3,}", _CUR_TOKENS.sub(" ", text)) if not _FILLER.match(word))


@dataclass
class Amount:
    value: float
    start: int  # span of the number inside ParsedText.text
    end: int
    kind: str  # "mrp" | "sell" | "plain" | "range" | "excluded"
    seg: int  # which segment (a line, or a " | " part of one) it sits in
    line: int  # which line of the original text


@dataclass
class ParsedText:
    text: str = ""  # flattened text with links blanked out; every position refers to it
    price: Optional[float] = None
    mrp: Optional[float] = None
    pair_conf: str = ""  # "labelled" | "arrow" | "inferred" | ""
    ambiguous: bool = False  # another amount could equally be the price that goes with the MRP
    conditional: bool = False  # the price depends on buying something else
    pct_claim: Optional[float] = None  # an exact "X% off" style claim
    pct_pos: int = -1
    pct_upto: Optional[float] = None  # an "upto X% off" claim (marketing, weak)
    error_phrase: str = ""
    expired: str = ""  # "over" (the post announces it), "maybe" (it only hints at it) or ""
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

    @property
    def claim_price(self) -> Optional[float]:
        """The price a stated percentage applies to: the one price written beside it ("₹6,294 /
        80% off"), or on a bare price line directly above or below it.

        A price anywhere else in the post belongs to something else. A daily digest reads
        "Jiomart 99% off" on one line and "Atomberg fan - Rs 2700" three lines down.
        """
        if self.pct_pos < 0:
            return None
        usable = [a for a in self.amounts if a.kind in ("sell", "plain")]
        seg = self.text.count("\n", 0, self.pct_pos)
        line = _line_index(self.text, self.pct_pos)
        for group in ([a for a in usable if a.seg == seg], [a for a in usable if a.line == line]):
            values = {a.value for a in group}
            if len(values) == 1:
                return next(iter(values))
            if values:
                return None  # two prices beside the percentage: no telling which one it means
        lines = self.text.replace("\n" + SOFT, " " + SOFT).split("\n")
        for step in (1, -1):
            number = line + step
            while 0 <= number < len(lines) and not re.search(r"\w", lines[number]):
                number += step
            if 0 <= number < len(lines) and naming_words(lines[number]) < 2:
                values = {a.value for a in usable if a.line == number}
                if len(values) == 1:
                    return next(iter(values))
        return None


def _line_index(text: str, position: int) -> int:
    """Index of the original line holding ``position`` (a pipe break does not start a new line)."""
    return text.count("\n", 0, position) - text.count("\n" + SOFT, 0, position)


def _line_bounds(text: str, position: int) -> Tuple[int, int]:
    """Start and end of the original line holding ``position``."""
    start = text.rfind("\n", 0, position)
    while start != -1 and text[start + 1:start + 2] == SOFT:
        start = text.rfind("\n", 0, start)
    end = text.find("\n", position)
    while end != -1 and text[end + 1:end + 2] == SOFT:
        end = text.find("\n", end + 1)
    return start + 1, len(text) if end == -1 else end


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


def _classify(text: str, start: int, end: int, forced: str, prefixed: bool) -> str:
    """Label one amount from the words in its own segment, just before and just after it."""
    seg_start = text.rfind("\n", 0, start) + 1
    seg_end = text.find("\n", end)
    seg_end = len(text) if seg_end == -1 else seg_end
    before = _TRAIL.sub("", text[max(seg_start, start - 60):start])
    after = text[end:min(seg_end, end + 40)]
    if _EXCL_AFTER.match(after) or (_ADDON_BEFORE.search(before) and _ADDON_AFTER.match(after)):
        return "excluded"
    offer = _EXCL_BEFORE.search(before)
    if offer:
        return "sell" if _AFTER_OFFER.search(before[:offer.start()]) else "excluded"
    if forced == "mrp" or _MRP_BEFORE.search(before) or (prefixed and _MRP_BARE.search(before)) or _MRP_AFTER.match(after):
        return "mrp"
    if _RANGE_BEFORE.search(before) and not _TO_AFTER.match(after):
        return "range"
    if forced == "sell" or _SELL_BEFORE.search(before) or _SELL_AFTER.match(after):
        return "sell"
    return "plain"


def extract_amounts(text: str) -> List[Amount]:
    """Find every rupee amount in ``text`` (flattened, links blanked) and label it."""
    found = []  # (priority, num_start, num_end, match_start, match_end, value, forced, prefixed)
    for priority, pattern in enumerate(_PATTERNS):
        for match in pattern.finditer(text):
            value = _to_value(match.group("num"), match.group("suf"))
            if value is None:
                continue
            forced = ""
            if pattern is _P_LABEL:
                usual = _MRP_LABELS.search(match.group("label")) or (match.group("conn") or "").lower() == "was"
                forced = "mrp" if usual else "sell"
            elif pattern is _P_AT:
                forced = "sell"
            num_start = match.start("num")
            num_end = match.end("suf") if match.group("suf") else match.end("num")
            found.append((priority, num_start, num_end, match.start(), match.end(), value, forced, pattern is _P_PREFIX))

    found.sort()
    taken: List[Tuple[int, int]] = []
    amounts: List[Amount] = []
    for _, num_start, num_end, match_start, match_end, value, forced, prefixed in found:
        if any(num_start < t_end and t_start < num_end for t_start, t_end in taken):
            continue  # the same digits were already claimed by a higher-priority pattern
        taken.append((num_start, num_end))
        kind = _classify(text, match_start, match_end, forced, prefixed)
        amounts.append(Amount(value, num_start, num_end, kind, text.count("\n", 0, num_start),
                              _line_index(text, num_start)))
    amounts.sort(key=lambda a: a.start)

    # "₹400 MRP ₹45,999" and "₹34,990 MRP ₹400 only": one MRP label between two amounts belongs
    # to the larger of them, whichever side of it that amount is written on.
    for first, second in zip(amounts, amounts[1:]):
        if first.seg != second.seg or "excluded" in (first.kind, second.kind) or first.value == second.value:
            continue
        if _MRP_BETWEEN.match(_CUR_TOKENS.sub(" ", text[first.end:second.start])):
            larger, smaller = (first, second) if first.value > second.value else (second, first)
            larger.kind = "mrp"
            if smaller.kind in ("mrp", "plain"):
                smaller.kind = "sell"
    return amounts


def _gap(one: Amount, other: Amount) -> int:
    return min(abs(one.start - other.end), abs(other.start - one.end))


def _best_pair(amounts: List[Amount], text: str) -> Tuple[Optional[Amount], Optional[Amount], str, bool]:
    """Choose the (price, mrp) pair the text most clearly states.

    For a labelled MRP, position decides. The price is looked for in the MRP's own segment, then
    on its line, then on the lines next to it; within that, it is the amount written just before
    the MRP, failing that the one just after. Picking simply "the nearest number" turns
    "at ₹62,999 (MRP ₹89,999) + ₹4,000 ..." into a 96% discount. The last value returned says
    whether a second, very different amount stood in the same place and could have been meant.
    """
    usable = [a for a in amounts if a.kind in ("mrp", "sell", "plain")]
    mrps = [a for a in usable if a.kind == "mrp"]
    others = [a for a in usable if a.kind != "mrp"]
    candidates: List[tuple] = []  # (rank, -discount, order, price, mrp, ambiguous)

    def add(rank: int, mrp: Amount, price: Amount, ambiguous: bool = False) -> None:
        if mrp.value > price.value >= 0:
            discount = (mrp.value - price.value) / mrp.value
            candidates.append((rank, -discount, len(candidates), price, mrp, ambiguous))

    for mrp in mrps:
        pool = [o for o in others if o.value < mrp.value]
        near = (
            [o for o in pool if o.seg == mrp.seg]
            or [o for o in pool if o.line == mrp.line]
            or [o for o in pool if abs(o.line - mrp.line) <= 2 and _gap(o, mrp) <= 200]
        )
        if not near:
            continue
        before = [o for o in near if o.start < mrp.start]
        side = before or near
        side = [o for o in side if o.kind == "sell"] or side
        pick = max(side, key=lambda o: o.start) if before else min(side, key=lambda o: o.start)
        rivals = [o for o in side if o is not pick and not 0.5 <= o.value / max(pick.value, 0.01) <= 2.0]
        add(0, mrp, pick, bool(rivals))

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
                add(2, min(pool, key=lambda p: _gap(p, sell)), sell)
        elif not sells:
            by_line: Dict[int, List[Amount]] = {}
            for plain in plains:
                by_line.setdefault(plain.line, []).append(plain)
            for items in by_line.values():
                if len({i.value for i in items}) == 2:
                    add(2, max(items, key=lambda i: i.value), min(items, key=lambda i: i.value))

    if not candidates:
        return None, None, "", False
    rank, _, _, price, mrp, ambiguous = min(candidates)
    return price, mrp, ("labelled", "arrow", "inferred")[rank], ambiguous


def _percent_claims(text: str) -> Tuple[Optional[float], Optional[float], int]:
    exact: Optional[float] = None
    exact_pos = -1
    upto: Optional[float] = None
    for match in _PCT.finditer(text):
        pct = float(match.group("pct"))
        if not 0 < pct <= 100:
            continue
        pre = re.sub(r"\s+", "", (match.group("pre") or "").lower())
        post = re.sub(r"\s+", "", (match.group("post") or "").lower())
        if pre in ("extra", "additional", "addl", "addl.") or post in ("cashback", "back"):
            continue  # stacking coupons and cashback are not the headline discount
        if _CAPPED.match(text[match.end():match.end() + 24]):
            continue  # "80% off up to ₹160" is a capped coupon, not a product at 80% off
        if pre == "upto":
            upto = max(upto or 0.0, pct)
            continue
        if not post:
            if pre not in ("flat", "min", "min.", "minimum", "atleast", "save", "saving"):
                continue
            follower = re.match(r"[ \t]*([a-z]+)", text[match.end():], re.I)
            if pre in ("save", "saving") and follower and follower.group(1).lower() not in _SAVE_ON:
                continue  # "save 90% oil": a feature of the product, not money off
        if exact is None or pct > exact:
            exact, exact_pos = pct, match.start("pct")
    return exact, upto, exact_pos


def _expiry(text: str) -> str:
    """"over" when the post announces that the deal has ended, "maybe" when it only hints at it."""
    live = []
    for match in _STATUS.finditer(text):
        if match.group(0).startswith("["):
            return "over"  # a tag such as [OVER] or [EXPIRED] is never anything else
        seg_start = text.rfind("\n", 0, match.start()) + 1
        seg_end = text.find("\n", match.end())
        seg_end = len(text) if seg_end == -1 else seg_end
        if _HEDGE_BEFORE.search(text[seg_start:match.start()]) or _HEDGE_AFTER.match(text[match.end():seg_end]):
            continue  # "before it's OOS", "sold out soon", "OOS in some pincodes", "coupon expired?"
        live.append(match)
    if not live:
        return ""
    # A status that is all its line says ("Expired", "Deal over ❌", "Sold out in 2 min") is an
    # announcement. Inside a longer sentence it may be about one colour, one pincode, last week.
    for match in live:
        start, end = _line_bounds(text, match.start())
        rest = list(text[start:end])
        for other in live:
            if start <= other.start() < end:
                rest[other.start() - start:other.end() - start] = " " * (other.end() - other.start())
        if len(_WORD.findall("".join(rest))) <= 2:
            return "over"
    return "maybe"


def parse_text(raw: str) -> ParsedText:
    """Analyse one deal post / product blurb."""
    text = _URL.sub(lambda m: " " * len(m.group(0)), _flatten(raw))
    parsed = ParsedText(text=text)
    if not text.strip():
        return parsed
    parsed.amounts = extract_amounts(text)
    price, mrp, parsed.pair_conf, parsed.ambiguous = _best_pair(parsed.amounts, text)
    anchors: List[int] = []
    if price is not None and mrp is not None:
        parsed.price, parsed.mrp = price.value, mrp.value
        anchors = [price.start, mrp.start]
    else:
        sells = [a.value for a in parsed.amounts if a.kind == "sell"]
        if sells:
            parsed.price = min(sells)
    parsed.pct_claim, parsed.pct_upto, parsed.pct_pos = _percent_claims(text)
    if not anchors and parsed.pct_pos >= 0:
        anchors = [parsed.pct_pos]
    for position in anchors:
        start, end = _line_bounds(text, position)
        if _CONDITION.search(text[start:end]):
            parsed.conditional = True
    error = _ERROR_PHRASE.search(text)
    if error and not _ERROR_NEGATED.search(text):
        parsed.error_phrase = re.sub(r"\s+", " ", error.group(0).lower())
    parsed.expired = _expiry(text)
    return parsed


def price_near(parsed: ParsedText, position: int) -> Optional[float]:
    """The price that belongs to the product named at ``position`` (used for keyword traps).

    In order: the price in the keyword's own segment; a bare price in another part of the same
    line; the first priced line below, provided it names no other product ("Deal Price: ₹349
    (MRP ₹2,599)"); a bare price line directly above. A bank discount, a budget heading or the
    price of the next product in a list is never taken for it.
    """
    text = parsed.text
    segments = text.split("\n")
    here = text.count("\n", 0, position)
    by_seg: Dict[int, List[Amount]] = {}
    for amount in parsed.amounts:
        if amount.kind in ("sell", "plain"):
            by_seg.setdefault(amount.seg, []).append(amount)

    def pick(group: List[Amount], after: Optional[int] = None) -> Optional[float]:
        # A labelled or arrowed pair says which amount is the price. A pair merely inferred from
        # two bare numbers does not: one of them may be anything.
        if parsed.pair_conf in ("labelled", "arrow") and any(a.value == parsed.price for a in group):
            return parsed.price
        pool = [a for a in group if after is None or a.start >= after] or group
        sells = [a for a in pool if a.kind == "sell"]
        if sells:
            return sells[0].value
        values = {a.value for a in pool}
        return next(iter(values)) if len(values) == 1 else None

    if here in by_seg:
        return pick(by_seg[here], position)

    lines: List[List[int]] = []  # the segments of each original line
    for index, segment in enumerate(segments):
        if index and segment.startswith(SOFT):
            lines[-1].append(index)
        else:
            lines.append([index])
    mine = next(n for n, members in enumerate(lines) if here in members)

    # Other parts of the keyword's own line: "Nothing Phone (3a) | 8GB/256GB | ₹400".
    for order in (range(here + 1, lines[mine][-1] + 1), range(here - 1, lines[mine][0] - 1, -1)):
        for index in order:
            if index in by_seg:
                if naming_words(segments[index]) < 2:
                    return pick(by_seg[index])
                break  # that part names and prices something else

    def view(number: int) -> Tuple[List[Amount], int, bool]:
        members = lines[number]
        group = [a for index in members for a in by_seg.get(index, [])]
        names = sum(naming_words(segments[index]) for index in members)
        return group, names, any(re.search(r"\w", segments[index]) for index in members)

    wordy = 0
    for number in range(mine + 1, min(mine + 7, len(lines))):
        group, names, _ = view(number)
        if group:
            return pick(group) if names < 2 else None  # a named, priced line is another product
        if names >= 2:
            wordy += 1  # one or two lines of description between a name and its price are normal
            if wordy > 2:
                return None

    above = [number for number in range(mine - 1, -1, -1) if view(number)[2]][:2]
    if above:
        group, names, _ = view(above[0])
        if group and names < 2 and (len(above) < 2 or view(above[1])[1] < 2):
            return pick(group)
    return None
