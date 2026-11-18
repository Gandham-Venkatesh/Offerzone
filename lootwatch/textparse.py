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
# "Snitch Shirt at 599": one channel writes every price as a bare number after "at", closing the
# line. Not "sale starts at 12", not "at 12 PM", not "at 50% off".
_P_AT_END = re.compile(
    r"(?<![a-z])(?<!starts )(?<!start )(?<!begins )(?<!live )(?<!sale )(?<!opens )(?<!rated )at\s+(?P<num>" + _NUM
    + r")(?!\d)" + _SUF + r"(?=[^\w\n]*(?:\n|$))",
    re.I,
)
# "Coffee Loot 239 : link": another channel's habit.
_P_LOOT = re.compile(r"(?<![a-z])loot\s+(?P<num>" + _NUM + r")(?!\d)" + _SUF + r"(?=[ \t]*(?::|\n|$))", re.I)
_BARE = (_P_AT_END, _P_LOOT)  # no currency sign at all: only believed from ₹10 up
_PATTERNS = (_P_PREFIX, _P_SUFFIX, _P_LABEL, _P_AT, _P_AT_END, _P_LOOT)  # in priority order

_TRAIL = re.compile(r"[\s\x1f:\-=–—(\[*_~>|•.,]+$")
_CUR_TOKENS = re.compile(r"₹|\brs\.?|\binr\b|\brupees?\b|/-", re.I)
# Links are blanked out before anything is read: "amzn.to/4ab7Rs9" must not become "Rs 9",
# and a product slug inside a URL must not trip a keyword trap.
_URL = re.compile(
    r"(?:https?://|www\.)\S+|\b[a-z0-9][a-z0-9\-]*\.(?:to|in|cc|co|it|ly|me|asia|com|app|link|site|xyz)/\S*", re.I
)

_SLUG_NOISE = frozenset(
    "pvid prid prn itm item items share true false html htm php www com ref utm pid product products buy store "
    "deals deal inr mrp off search category".split()
)


def _link_words(match: "re.Match[str]") -> str:
    """What a link's address says about the product: the plain words of its path.

    Quick-commerce posts often name the product nowhere else ("Zepto Loot / @ 24 /
    zepto.com/pn/mccain-chilli-garlic-potato-bites/pvid/..."). Codes and long numbers are
    dropped, so nothing in an address can be read as an amount, and the words are fenced off
    from the text around them.
    """
    path = re.sub(r"^(?:[a-z]+://)?[^/]*", "", match.group(0), flags=re.I).split("?", 1)[0].split("#", 1)[0]
    words = []
    for part in re.split(r"[^A-Za-z0-9]+", path):
        low = part.lower()
        if re.fullmatch(r"[a-z]{3,}", low):
            if low not in _SLUG_NOISE:
                words.append(part)
        elif re.fullmatch(r"(?=[a-z]*\d)[a-z0-9]{1,4}", low) and not re.match(r"(?:rs|inr|mrp)\d", low):
            words.append(part)  # a model number: "16", "3a", "s25" (never something like "Rs9")
    return " · " + " ".join(words) + " · " if words else " "


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
    r"^\s*(?:off\b|cash\s?back|back\b(?!\s+(?:in|again|now|live)\b)|coupon|voucher|gv\b|gift\s*(?:card|voucher|coupon)"
    r"|rewards?\b|price\s+(?:cut|drop)\b"
    r"|credits?\b(?!\s+card)"
    r"|(?:" + _BANK + r"\s+)?(?:(?:credit|debit)\s+)?(?:bank|cards?|exchange|upgrade)\s+(?:discount|bonus|off|cashback)s?\b"
    r"|" + _BANK + r"\s+(?:discount|bonus|cashback|off)s?\b"
    r"|(?:instant|extra|additional|flat)\s+(?:off|discount|cashback|savings?|bank)\b|discount\b|bonus\b"
    r"|/\s?mo(?:nth)?\b|(?:a|per)\s+month|p\.?m\.?\b|x\s*\d+|(?:(?:on|no\s+cost)\s+)?emi\b(?!\s+(?:available|option|also|too)\b)"
    r"|plans?\b|recharge|saved\b|savings?\b|save\b(?!\s*(?:₹|rs\b|\d|upto|up\s+to|extra|flat|big|more)\b)"
    r"|wallet|super\s?coins?|coins?\b|points?\b|paytm\s+cash|amazon\s+pay"
    r"|(?:delivery|shipping|handling|platform|convenience)\s*(?:fee|charges?)\b"
    r"|(?:delivery|shipping)\b(?!\s+(?:in|by|within|today|tomorrow)\b)|fee\b|charges?\b)",
    re.I,
)
# "... + ₹4000 Bank Offer", "& ₹50 Amazon Pay cashback": an amount added on with a plus sign and
# named as an offer is the offer's size, whatever bank or wallet stands in between.
_ADDON_BEFORE = re.compile(r"(?:\+|&|\bplus|\band|\bextra)\s*$", re.I)
_ADDON_AFTER = re.compile(r"^\s*(?:[a-z]+\s+){0,3}(?:offers?|discounts?|bonus|cashback|off)\b", re.I)
# "₹4000 Bank Offer" with nothing in front of it on its line is the size of the offer.
_BANK_OFFER_AFTER = re.compile(r"^\s*(?:" + _BANK + r"\s+)?(?:(?:credit|debit)\s+)?(?:bank|cards?)\s+offers?\b", re.I)
# "Available @ 560001": a pincode, not a price.
_PIN_BEFORE = re.compile(
    r"(?:\bavailable|\bpin\s?codes?|\bpin|\blocation|\bdeliver(?:y|able|ing)|\bserviceable)(?:\s+(?:at|in|for|to))?$", re.I
)
# Wording that makes a stated price depend on buying something else.
_CONDITION = re.compile(
    r"\b(?:cart\s+value|orders?\s+(?:above|over|of|worth)|min(?:imum)?\.?\s*(?:order|purchase|cart)|on\s+orders"
    r"|(?:1st|first)\s+(?:order|month|purchase|time)|with\s+(?:\w+\s+){0,3}purchase|lucky\s+winners?"
    r"|new\s+(?:users?|customers?)|on\s+(?:shopping|purchase|spends?|spending)\s+of|valid\s+on"
    r"|for\s+(?:plus|prime|gold|black|one)\s+members?)\b",
    re.I,
)
# A free-delivery threshold is not a condition on the price.
_FREE_DELIVERY = re.compile(
    r"(?:free\s+)?(?:delivery|shipping)\b[^.\n|]{0,25}?\b(?:orders?|purchases?|cart)\s+(?:above|over|of|worth)\s*(?:₹|rs\.?)?\s*[\d,]+",
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
# Words a line may hold and still be nothing but a discount claim ("Flat 99% Off 😱", "Now 90% off").
_CLAIM_WORD = re.compile(
    r"^(?:off|discount|flat|price|drop|now|only|just|loot|deal|hurry|fast|grab|big|mega|steal|alert|live|again|today"
    r"|min|minimum|upto|up|to|save|saving|at|on|the|is|its|it|get|and|wow|omg)$",
    re.I,
)
_ON_THE_NEXT_ONE = re.compile(r"\s*on\s+(?:the\s+|your\s+)?(?:2nd|second|3rd|third|next)\b", re.I)  # "100% off on 2nd unit"
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
    r"|\bprice\s+(?:has\s+|is\s+|got\s+)?(?:increased|hiked|revised|corrected|back\s+to\s+(?:normal|regular|original|mrp))\b"
    r"|\bno\s+longer\s+available\b"
    r"|\b(?:now|currently|is|its|it['’]s|went|gone|got|already|again)\s+(?:oos|out\s+of\s+stock)\b"
    r"|\b(?:oos|out\s+of\s+stock)\b(?=\s+(?:now|again|already)\b|\W*$)"
    r"|^\W*(?:oos|out\s+of\s+stock)\b",
    re.I | re.M,
)
# Where a clause ends: punctuation, emoji and symbols, a dash standing alone.
_CLAUSE_BREAK = re.compile(r"[^\w\s'’₹%@&+/\-]|\s-\s|\n")
# In the clause before the status: it has not happened ("before it's OOS", "almost sold out"),
# or it is about something else ("coupon expired", "some pincodes OOS", "last time expired in 5 min").
_HEDGE_LEAD = re.compile(
    r"\b(?:before|until|till|unless|if|in\s+case|almost|nearly|may|might|can|could|will|would|shall|gonna|soon"
    r"|going\s+to|about\s+to|not|never|isn['’]?t|wasn['’]?t|last\s+time|earlier|previously"
    r"|coupons?|codes?|cards?|vouchers?|some|few|many|most|certain|others?)\b",
    re.I,
)
# One colour or size gone is not the deal gone.
_VARIANT = re.compile(
    r"\b(?:black|white|blue|green|red|grey|gray|silver|gold|golden|pink|purple|yellow|orange|titanium|colou?rs?"
    r"|variants?|sizes?|\d+\s?gb|small|medium|large|xl|xxl)\b",
    re.I,
)
_HEDGE_AFTER = re.compile(
    r"^\s*(?:\?|(?:very\s+)?soon\b|fast\b|quickly\b|anytime\b|any\s+(?:time|moment)\b|shortly\b|yet\b"
    r"|(?:in|for|at)\s+(?:some|few|many|most|certain|my|your)\b|earlier\b|last\s+time\b|before\b|₹|rs\b|\d)",
    re.I,
)
# Words that may stand before the status in its clause and leave it a plain announcement.
_STATUS_SUPPORT = frozenset(
    "now currently is its it it's it’s went gone got already again all deal deals loot loots offer offers sale stock "
    "price prices error glitch bug totally completely fully sorry guys update edit this the has have been and also "
    "both finally sadly unfortunately".split()
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
    r"|flipkart|myntra|ajio|jiomart|meesho|croma|zepto|blinkit|instamart|swiggy|tata|cliq|nykaa|bigbasket|dmart"
    r"|snapdeal|shopsy|paytm|firstcry|pepperfry|purplle|zomato|licious|ram|rom|storage"
    r"|ssd|hdd|variant|colour|color|black|white|blue|green|red|grey|gray|silver|gold|pink|purple|pro|max|plus"
    r"|ultra|lite|mini|all|any|per|you|your|this|that|its|are|not|but|can|will|has|have|must|very|too|rupees"
    r"|actual|usual|normal|original|rate|cost|drop|dropped|back|available|unit|units|each"
    # words of a sale, a shop or a sales pitch: they announce a product, they do not name one
    r"|billion|day|days|great|india|indian|festival|festive|republic|diwali|dussehra|season|end|year|sales|savings"
    r"|summer|winter|monsoon|freedom|independence|prime|minutes|exclusive|launched|latest|model|brand|sealed|genuine"
    r"|official|unlocked|edition|version|vijay|reliance|digital|unbelievable|crazy|insane|massive|huge|lowest|loots"
    r"|quick|grab|hurry|missed|changer|chance|both|also|then|than|just|about|today"
    r"|exchange|upgrade|bonus|emi|member|members|users|customers|students|early|access|search|filter|high|low"
    r"|instant|hdfc|sbi|icici|axis|kotak|credit|debit|upi|pincode|pincodes|location|specific|valid|till|tonight"
    r"|midnight|account|before|after|lasts|soon|anytime)$",
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


def naming_list(text: str) -> List[str]:
    """The words in ``text`` that could be naming a product (not prices, hype, specs or filler)."""
    return [word for word in re.findall(r"[A-Za-z]{3,}", _CUR_TOKENS.sub(" ", text)) if not _FILLER.match(word)]


def naming_words(text: str) -> int:
    return len(naming_list(text))


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
    text: str = ""  # flattened text, links reduced to the words of their address; positions refer to it
    price: Optional[float] = None
    mrp: Optional[float] = None
    pair_conf: str = ""  # "labelled" | "arrow" | "inferred" | ""
    ambiguous: bool = False  # another amount could equally be the price that goes with the MRP
    conditional: bool = False  # the price depends on buying something else
    pct_claim: Optional[float] = None  # the largest exact "X% off" style claim
    pct_pos: int = -1  # where it is first made
    pct_spots: List[int] = field(default_factory=list)  # every place it is made
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
        80% off"), or on the line next to it.

        A price anywhere else in the post belongs to something else. A daily digest reads
        "Jiomart 99% off" on one line and "Atomberg fan - Rs 2700" three lines down. Where the
        percentage is stated more than once (a headline, then the price line), each place is tried.
        """
        for spot in self.pct_spots:
            price = self._price_beside(spot)
            if price is not None:
                return price
        return None

    def _price_beside(self, spot: int) -> Optional[float]:
        usable = [a for a in self.amounts if a.kind in ("sell", "plain")]
        seg = self.text.count("\n", 0, spot)
        line = _line_index(self.text, spot)
        for group in ([a for a in usable if a.seg == seg], [a for a in usable if a.line == line]):
            values = {a.value for a in group}
            if len(values) == 1:
                return next(iter(values))
            if values:
                return None  # two prices beside the percentage: no telling which one it means
        lines = self.text.replace("\n" + SOFT, " " + SOFT).split("\n")
        # "99% Off 😱" on a line of its own speaks for the line next to it, whatever that line
        # names. A line that says more ("Jiomart 99% off") only takes a bare price line.
        bare = all(_CLAIM_WORD.match(word) for word in re.findall(r"[A-Za-z]{2,}", lines[line]))
        for step in (1, -1):
            number = line + step
            while 0 <= number < len(lines) and not re.search(r"\w", lines[number]):
                number += step
            if 0 <= number < len(lines) and (bare or naming_words(lines[number]) == 0):
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


def _classify(text: str, start: int, end: int, forced: str, prefixed: bool, digits: str) -> str:
    """Label one amount from the words in its own segment, just before and just after it."""
    seg_start = text.rfind("\n", 0, start) + 1
    seg_end = text.find("\n", end)
    seg_end = len(text) if seg_end == -1 else seg_end
    before = _TRAIL.sub("", text[max(seg_start, start - 60):start])
    after = text[end:min(seg_end, end + 40)]
    if _EXCL_AFTER.match(after) or (_ADDON_BEFORE.search(before) and _ADDON_AFTER.match(after)):
        return "excluded"
    if not prefixed and re.fullmatch(r"[1-9]\d{5}", digits) and _PIN_BEFORE.search(before):
        return "excluded"
    if _BANK_OFFER_AFTER.match(after) and not _SELL_BEFORE.search(before) and not naming_words(text[seg_start:start]):
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
    """Find every rupee amount in ``text`` (flattened, links reduced) and label it."""
    found = []  # (priority, num_start, num_end, match_start, match_end, value, forced, prefixed)
    for priority, pattern in enumerate(_PATTERNS):
        for match in pattern.finditer(text):
            value = _to_value(match.group("num"), match.group("suf"))
            if value is None:
                continue
            if pattern in _BARE and value < 10:
                # No currency sign and a single digit: only when the line names a product in
                # front of it ("Gulab Jamun 1kg at 1"), never "sale today at 9" or "Loot 5 :".
                line_start = text.rfind("\n", 0, match.start()) + 1
                if pattern is _P_LOOT or not naming_words(text[line_start:match.start()]):
                    continue
            forced = ""
            if pattern is _P_LABEL:
                usual = _MRP_LABELS.search(match.group("label")) or (match.group("conn") or "").lower() == "was"
                forced = "mrp" if usual else "sell"
            elif pattern is not _P_PREFIX and pattern is not _P_SUFFIX:
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
        kind = _classify(text, match_start, match_end, forced, prefixed, text[num_start:num_end])
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

    For a labelled MRP, position decides, never mere nearness: picking "the nearest number"
    turns "at ₹62,999 (MRP ₹89,999) + ₹4,000 ..." into a 96% discount.

    * On the MRP's own line the price is the amount written before it (a marked price such as
      "at ₹X" if there is one, else the nearest); if nothing stands before it, the first amount
      after it ("(MRP ₹74,990) ₹52,990 + bag at ₹1").
    * With the MRP alone on its line, the price is on a neighbouring line: the one after it when
      the MRP's line names the product ("Sony Bravia TV MRP ₹99,900" / "Deal Price ₹54,990"),
      otherwise the one before it ("Deal Price ₹14,999" / "MRP ₹24,999").

    The last value returned says whether a second, very different amount stood in the same place
    and could have been meant.
    """
    usable = [a for a in amounts if a.kind in ("mrp", "sell", "plain")]
    mrps = [a for a in usable if a.kind == "mrp"]
    others = [a for a in usable if a.kind != "mrp"]
    candidates: List[tuple] = []  # (rank, -discount, order, price, mrp, ambiguous)

    def add(rank: int, mrp: Amount, price: Amount, ambiguous: bool = False) -> None:
        if mrp.value > price.value >= 0:
            discount = (mrp.value - price.value) / mrp.value
            candidates.append((rank, -discount, len(candidates), price, mrp, ambiguous))

    def far(pick: Amount, rest: List[Amount]) -> bool:
        return any(not 0.5 <= o.value / max(pick.value, 0.01) <= 2.0 for o in rest if o is not pick)

    for mrp in mrps:
        pool = [o for o in others if o.value < mrp.value]
        same = [o for o in pool if o.seg == mrp.seg] or [o for o in pool if o.line == mrp.line]
        if same:
            before = [o for o in same if o.start < mrp.start]
            after = [o for o in same if o.start > mrp.start]
            after_first = False
        else:
            near = [o for o in pool if abs(o.line - mrp.line) <= 5 and _gap(o, mrp) <= 400]
            before = [o for o in near if o.line < mrp.line]
            after = [o for o in near if o.line > mrp.line]
            start, end = _line_bounds(text, mrp.start)
            after_first = naming_words(text[start:end]) >= 2
        for side in (("after", "before") if after_first else ("before", "after")):
            if side == "before" and before:
                marked = [o for o in before if o.kind == "sell"] or before
                pick = max(marked, key=lambda o: o.start)
                add(0, mrp, pick, far(pick, marked))
                break
            if side == "after" and after:
                pick = min(after, key=lambda o: o.start)
                add(0, mrp, pick, far(pick, after))
                break

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


def _percent_claims(text: str) -> Tuple[List[Tuple[float, int]], Optional[float]]:
    """Every exact "X% off" style claim as (percent, position), and the largest "upto" claim."""
    claims: List[Tuple[float, int]] = []
    upto: Optional[float] = None
    for match in _PCT.finditer(text):
        pct = float(match.group("pct"))
        if not 0 < pct <= 100:
            continue
        pre = re.sub(r"\s+", "", (match.group("pre") or "").lower())
        post = re.sub(r"\s+", "", (match.group("post") or "").lower())
        if pre in ("extra", "additional", "addl", "addl.") or post in ("cashback", "back"):
            continue  # stacking coupons and cashback are not the headline discount
        rest = text[match.end():match.end() + 30]
        if _CAPPED.match(rest) or _ON_THE_NEXT_ONE.match(rest):
            continue  # "80% off up to ₹160" is a capped coupon; "100% off on 2nd unit" is buy one get one
        if pre == "upto":
            upto = max(upto or 0.0, pct)
            continue
        if not post:
            if pre not in ("flat", "min", "min.", "minimum", "atleast", "save", "saving"):
                continue
            follower = re.match(r"[ \t]*([a-z]+)", text[match.end():], re.I)
            if pre in ("save", "saving") and follower and follower.group(1).lower() not in _SAVE_ON:
                continue  # "save 90% oil": a feature of the product, not money off
        claims.append((pct, match.start("pct")))
    return claims, upto


def _clause(text: str, start: int, end: int) -> Tuple[str, str]:
    """The clause around text[start:end]: the words leading up to it and the words that follow."""
    seg_start = text.rfind("\n", 0, start) + 1
    seg_end = text.find("\n", end)
    seg_end = len(text) if seg_end == -1 else seg_end
    lead = text[seg_start:start]
    breaks = list(_CLAUSE_BREAK.finditer(lead))
    if breaks:
        lead = lead[breaks[-1].end():]
    tail = text[end:seg_end]
    stop = _CLAUSE_BREAK.search(tail)
    return lead, tail[:stop.start()] if stop else tail


def _expiry(text: str) -> str:
    """"over" when the post announces that the deal has ended, "maybe" when it only hints at it.

    An announcement is a status standing as a clause of its own: "Expired", "Deal over ❌",
    "..., now sold out", "Sold out in 2 min". The same words governed by "before", "almost",
    "if" and the like have not happened yet; about a coupon, a colour or "some pincodes" they
    are about something else. Wrapped in a longer sentence ("Sony XM5 is out of stock on
    Amazon") they are a hint: the post is not thrown away, it goes out one step quieter.
    """
    hint = False
    for match in _STATUS.finditer(text):
        if match.group(0).startswith("["):
            return "over"  # a tag such as [OVER] or [EXPIRED] is never anything else
        seg_end = text.find("\n", match.end())
        if _HEDGE_AFTER.match(text[match.end():len(text) if seg_end == -1 else seg_end]):
            continue  # "sold out soon", "OOS in some pincodes", "coupon expired?", "sale over ₹999"
        lead, tail = _clause(text, match.start(), match.end())
        if _HEDGE_LEAD.search(lead) or _VARIANT.search(lead):
            continue
        foreign = [word for word in re.findall(r"[A-Za-z’']{2,}", lead) if word.lower() not in _STATUS_SUPPORT]
        if not foreign and len(_WORD.findall(tail)) <= 2:
            return "over"
        hint = True
    return "maybe" if hint else ""


def states_condition(text: str) -> bool:
    """Does any line of this text tie an offer to buying something else, or to who is buying?

    For text whose price stands somewhere else (a deal-site card keeps it in a field of its own),
    so there is no price line to look beside.
    """
    return any(_CONDITION.search(_FREE_DELIVERY.sub(" ", line)) for line in text.split("\n"))


def _conditional(text: str, anchors: List[int]) -> bool:
    """Does the line a price stands on, or the line right after it, tie that price to buying
    something else ("on orders above ₹999", "for new customers")?"""
    lines = text.replace("\n" + SOFT, " " + SOFT).split("\n")
    wanted = set()
    for position in anchors:
        number = _line_index(text, position)
        wanted.add(number)
        following = number + 1
        while following < len(lines) and not re.search(r"\w", lines[following]):
            following += 1
        if following < len(lines):
            wanted.add(following)
    return any(_CONDITION.search(_FREE_DELIVERY.sub(" ", lines[number])) for number in wanted)


def parse_text(raw: str) -> ParsedText:
    """Analyse one deal post / product blurb."""
    text = _URL.sub(_link_words, _flatten(raw))
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
    claims, parsed.pct_upto = _percent_claims(text)
    if claims:
        parsed.pct_claim = max(pct for pct, _ in claims)
        parsed.pct_spots = [position for pct, position in claims if pct == parsed.pct_claim]
        parsed.pct_pos = parsed.pct_spots[0]
    parsed.conditional = _conditional(text, anchors or parsed.pct_spots)
    error = _ERROR_PHRASE.search(text)
    if error and not _ERROR_NEGATED.search(text):
        parsed.error_phrase = re.sub(r"\s+", " ", error.group(0).lower())
    parsed.expired = _expiry(text)
    return parsed


def price_near(parsed: ParsedText, position: int) -> Optional[float]:
    """The price that belongs to the product named at ``position`` (used for keyword traps).

    In order:
    1. the price in the keyword's own part of the line;
    2. the price elsewhere on its line: if the line holds one price it is that product's (store
       titles are full of pipes: "OnePlus 13 | Smarter with OnePlus AI (12GB RAM) @ 699"), if it
       holds several, only a part that is nothing but a price;
    3. a bare price line directly below ("Deal Price: ₹349 (MRP ₹2,599)");
    4. a bare price line directly above, when the line above that names nothing else
       ("Blinkit LOOT @₹7" / "Search : Phillips Bulb");
    5. the first bare price line further down, past at most two lines of description.

    A bank discount, a budget heading, a pincode or the price of the next product in a list is
    never taken for it. A product whose own line gives a range ("iPhone 16 from ₹64,900") has no
    price to take.
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
    if any(a.seg == here and a.kind == "range" for a in parsed.amounts):
        return None

    lines: List[List[int]] = []  # the segments of each original line
    for index, segment in enumerate(segments):
        if index and segment.startswith(SOFT):
            lines[-1].append(index)
        else:
            lines.append([index])
    mine = next(n for n, members in enumerate(lines) if here in members)

    priced = [index for index in lines[mine] if index in by_seg]
    if len(priced) == 1:
        return pick(by_seg[priced[0]])
    for order in (range(here + 1, lines[mine][-1] + 1), range(here - 1, lines[mine][0] - 1, -1)):
        for index in order:
            if index in by_seg:
                if naming_words(segments[index]) < 2:
                    return pick(by_seg[index])
                break  # that part names and prices something else

    def view(number: int) -> Tuple[List[Amount], int]:
        members = lines[number]
        return ([a for index in members for a in by_seg.get(index, [])],
                sum(naming_words(segments[index]) for index in members))

    def written(number: int) -> bool:
        return any(re.search(r"\w", segments[index]) for index in lines[number])

    below = [number for number in range(mine + 1, min(mine + 8, len(lines))) if written(number)]
    above = [number for number in range(mine - 1, -1, -1) if written(number)][:4]
    if below:
        group, names = view(below[0])
        if group and not names:
            return pick(group)
    for place, number in enumerate(above[:3]):
        group, names = view(number)
        if group:
            # a bare price line; and whatever stands above it must not be the product it belongs to
            if not names and (place + 1 >= len(above) or view(above[place + 1])[1] < 2):
                return pick(group)
            break
        if names >= 2:
            break  # a line that names something else stands between
    wordy = 0
    for number in below:
        group, names = view(number)
        if group:
            return pick(group) if not names else None  # a named, priced line is another product
        if names >= 2:
            wordy += 1  # one or two lines of description between a name and its price are normal
            if wordy > 2:
                return None
    return None
