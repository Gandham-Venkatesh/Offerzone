"""Decide whether something is worth a push, and how loudly.

Tiers map straight onto ntfy priorities.

For a deal post (a channel message or feed entry) the priority follows the discount, and each
rung also needs real money saved, because percent-off alone is cheap talk: on real channels a
₹79 cleaning kit with a ₹999 "MRP" is posted as "92% off" every day.

* URGENT (5) - 95% off or more saving ₹2,000 or more; 90% off or more saving ₹20,000 or more;
               a keyword trap; "price error" wording.
* HIGH (4)   - 90% off or more saving ₹1,000 or more, or at a give-away price (₹10 or less).
* NORMAL (3) - 80% off or more saving ₹2,000 or more; also any deal whose link could not be
               tied to a known store.

A verdict is held at HIGH when the reading itself is unsure: the prices are not labelled, a
second amount stands beside the MRP, the price needs a qualifying purchase, or the post hints
that the deal may be over.

For a product read from a store page the signals are stronger: it appeared under its category
page's price floor, it hit your target, it tripped a trap, it fell far below the price this
monitor has been seeing for it, or it is new on a floor page at a huge discount to its list price.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import islice
from typing import List, Optional, Sequence

from .config import Settings, Trap
from .db import PriceInfo
from .textparse import (SOFT, ParsedText, _line_bounds, fmt_inr, naming_list, naming_words, parse_text, price_near,
                        states_condition)

TIER_URGENT, TIER_HIGH, TIER_NORMAL = 5, 4, 3
TIER_NAMES = {TIER_URGENT: "urgent", TIER_HIGH: "high", TIER_NORMAL: "normal"}
# A list price of at least this many times a page's floor, and at least this many rupees, is not
# what an ordinary accessory carries: at such a price the word test for accessories is set aside.
STRONG_MRP_RATIO = 5
STRONG_MRP_MIN = 10_000.0

# A trap for "nothing phone" must not fire on "Back cover for Nothing Phone" or "Nothing Phone 3a case".
_ACCESSORY_WORDS = (
    r"(?:cases?|covers?|tempered|screen\s*(?:guard|glass|protector)|protectors?|guard|skins?|cables?|chargers?"
    r"|adapters?|adaptors?|straps?|holders?|stands?|pouch(?:es)?|sleeves?|bumpers?|stickers?|lens|back\s*panel"
    r"|controllers?|dualsense|remote|docks?|hubs?|keyboards?|mouse|pencil|stylus|games?|spares?|replacement|parts?"
    r"|power\s*banks?|magsafe|folio|tripods?|airtags?|wallets?|lanyards?|dummy|toys?)"
)
_ACCESSORY = re.compile(r"\b" + _ACCESSORY_WORDS + r"\b", re.I)
_ACCESSORY_FOR = re.compile(r"\b" + _ACCESSORY_WORDS + r"\b[^,|()]{0,30}\bfor\b", re.I)  # "Back Cover Case for iPhone"
# "Spigen case iPhone 16": the accessory noun stands right before the name, nothing in between.
_ACCESSORY_ADJACENT = re.compile(r"\b" + _ACCESSORY_WORDS + r"\s+(?:(?:apple|samsung|oneplus|nothing|cmf)\s+)?$", re.I)
# "... for iPhone 16", "compatible with iPhone, iPad & AirPods", "(works with iPhone)": the keyword
# is what the thing fits. The names may be a list; a price mark or a sentence end closes it.
_LINKED = re.compile(
    r"\b(?P<link>for|compatible(?:\s+(?:with|for))?|works?\s+with|made\s+for|designed\s+for|fits|suitable\s+for"
    r"|supports?)\b[^@₹:;!?\n]{0,70}$",
    re.I,
)
_FOR_WHAT = re.compile(r"\b(?:for|compatible|works?\s+with|fits|suitable)\b", re.I)
# A line about getting hold of the product some other way than buying it at that price.
_NOT_ITS_PRICE = re.compile(
    r"\bpre[\s\-]?(?:reserve|book(?:ing)?)\b|\bwin\b|\blucky\s+draw\b|\bgiveaway\b|\bcontest\b"
    r"|\bspin\s+(?:and|&|to)\s+win\b|\bchance\s+to\s+win\b",
    re.I,
)
# Makers of cases, chargers and cables name their products after the device: "OtterBox React Apple
# iPhone 14 PRO Stardust Clear" has no accessory noun in it at all.
_ACCESSORY_MAKERS = re.compile(
    r"\b(?:otterbox|spigen|esr|ringke|caseology|uag|urban\s+armor|nillkin|supcase|zagg|belkin|anker|baseus|ugreen"
    r"|portronics|ambrane|stuffcool|dbrand|pitaka|torras|benks|raegr|kapaver|tarkan|incipio|speck|casetify|urbn"
    r"|wecool|dailyobjects|macmerise)\b",
    re.I,
)
_DEVICE_BRAND = re.compile(
    r"\b(?:apple|samsung|oneplus|nothing|cmf(?:\s+by\s+nothing)?|google|xiaomi|redmi|realme|vivo|oppo|motorola|sony"
    r"|microsoft|nintendo|asus|hp|dell|lenovo|acer)\s*$",
    re.I,
)
_PHRASE_END = re.compile(r"[^\w\s+'&.]|\.\s")  # punctuation, symbols and emoji end a phrase; "v2.0" does not
# "... with free case", "charger not included": the accessory word is a side note, the product is the real thing.
_SIDE_NOTE = re.compile(r"\b(?:with|without|free|includes?|including|plus|in\s*box|inbox)\b|[+&]", re.I)
_NOT_PART = re.compile(r"^[^,|]{0,12}\b(?:not\s+included|sold\s+separately|not\s+in\s*(?:the\s+)?box|excluded)\b", re.I)
_NOT_NEW = re.compile(
    r"\b(?:refurbished|renewed|pre-?owned|second\s*hand|open\s*box|unboxed|rental|rent)\b|\(\s*used\s*\)", re.I
)
_USED_BEFORE = re.compile(r"\bused\s+(?:(?:apple|samsung|oneplus|nothing)\s+)?$", re.I)  # "Used iPhone 13"
_PRICE_MARK = re.compile(r"@|₹|\brs\b|\bmrp\b|\b(?:at|for)\s+(?:just|only)\b", re.I)
# For whole product titles on a store listing (long, spec-heavy: "... Backlit Keyboard, 1.7 kg"):
# only the first words name the product, so only they are checked for accessory nouns.
_REFURB = re.compile(r"\b(?:refurbished|renewed|pre-?owned|second\s*hand|open\s*box|unboxed)\b|\(used\)", re.I)
_MADE_FOR = re.compile(
    r"\b(?:(?<!sync )compatible\s+(?:with|for)|designed\s+for|suitable\s+for|replacement\s+for)\b", re.I
)


@dataclass
class Verdict:
    tier: int
    reason: str
    price: Optional[float] = None
    mrp: Optional[float] = None
    discount: float = 0.0
    needs_trusted_link: bool = False  # only valid if the post links to a known store
    label: str = ""  # shown first in the notification title: "Price error?", "Trap hit", ...
    flags: List[str] = field(default_factory=list)  # warnings about where the link goes
    notes: List[str] = field(default_factory=list)  # other things worth knowing before acting
    max_tier: int = TIER_URGENT  # no later upgrade (a deal trending across channels) may pass this
    was: Optional[float] = None  # the price this monitor saw before, when that is what it is compared with
    key: str = ""  # what makes this deal the same deal elsewhere, when its link does not (a coupon code)

    @property
    def headline(self) -> str:
        bits: List[str] = []
        if self.discount >= 1:
            shown = self.discount if self.discount >= 100 else min(self.discount, 99.0)  # 99.7% is not "100% off"
            bits.append(f"{shown:.0f}% off")
        if self.price is not None:
            compared = f" (MRP {fmt_inr(self.mrp)})" if self.mrp else f" (was {fmt_inr(self.was)})" if self.was else ""
            bits.append(fmt_inr(self.price) + compared)
        numbers = " · ".join(bits)
        if self.label:
            return f"{self.label}: {numbers}" if numbers else self.label
        return numbers or "Deal alert"


def is_accessory(text: str, keyword_start: int, keyword_end: Optional[int] = None) -> bool:
    """True when the keyword is not the thing on sale: an accessory for it, something that merely
    works with it, or a used / refurbished unit. ``text`` is one post or one product title."""
    keyword_end = keyword_start if keyword_end is None else keyword_end
    seg_start = text.rfind("\n", 0, keyword_start) + 1
    seg_end = text.find("\n", keyword_start)
    seg_end = len(text) if seg_end == -1 else seg_end
    before = text[seg_start:keyword_start]
    if _NOT_NEW.search(text[seg_start:seg_end]) or _USED_BEFORE.search(before):
        return True
    if _ACCESSORY_ADJACENT.search(before) or _ACCESSORY_MAKERS.search(before):
        return True
    # "Charger for iPhone, iPad & AirPods", "Stand compatible with iPhone, iPad": everything the
    # link word introduces, up to the next price or sentence end, is what the thing fits.
    # "Lowest price ever for iPhone 16" is still the phone: nothing is named before its "for".
    linked = _LINKED.search(before)
    if linked:
        lead = before[:linked.start()]
        if linked.group("link").lower() != "for" or naming_words(lead) >= 1 or _ACCESSORY.search(lead):
            return True
    # "<Maker> <Model> Apple iPhone 14 PRO <colour>": two capitalised names run straight into the
    # device's own brand. (Only then: "Haldiram's Nagpur Gulab Jamun" is the sweet itself.)
    phrase = _PHRASE_END.split(before)[-1]
    brand = _DEVICE_BRAND.search(phrase)
    if brand and sum(1 for word in naming_list(phrase[:brand.start()]) if word[0].isupper()) >= 2:
        return True
    # "Mobile Tripod Stand | iPhone & Android | ...": named in a later part of a line whose opening
    # part names another product, with no price of its own, it only says what that product fits.
    if text[seg_start:seg_start + 1] == SOFT and not _PRICE_MARK.search(text[seg_start:seg_end]):
        line_start = seg_start
        while line_start > 0 and text[line_start:line_start + 1] == SOFT:
            line_start = text.rfind("\n", 0, line_start - 1) + 1
        opening = text[line_start:text.find("\n", line_start)]
        if naming_words(opening) >= 2 and (_ACCESSORY.search(opening) or len(text[seg_start:seg_end].split()) <= 4):
            return True
    # "iPhone 15 case @ ₹99": an accessory word right after the name, before any price. What comes
    # after a "for" is what the product itself is for ("Cleaning Kit for Keyboard, AirPods").
    after = _PRICE_MARK.split(text[keyword_end:seg_end][:60], 1)[0]
    after = _FOR_WHAT.split(after, 1)[0]
    hit = _ACCESSORY.search(after)
    return bool(hit and not _SIDE_NOTE.search(after[:hit.start()]) and not _NOT_PART.match(after[hit.end():]))


def _mentions(parsed: ParsedText, traps: Sequence[Trap], in_post: bool):
    """Every place a trap's product is named as the thing on sale: (trap, position)."""
    for trap in traps:
        for match in islice(trap.pattern.finditer(parsed.text), 200):  # a wall of repeats is not worth more time
            # A trap set on a power bank or a charger is about that thing itself: the accessory
            # tests are for traps on the devices such things are named after.
            if not _ACCESSORY.fullmatch(match.group(0).strip()) and is_accessory(parsed.text, match.start(), match.end()):
                continue
            if in_post:
                start, end = _line_bounds(parsed.text, match.start())
                if _NOT_ITS_PRICE.search(parsed.text[start:end]):
                    continue
            yield trap, match.start()


def match_trap(parsed: ParsedText, traps: Sequence[Trap], price: Optional[float] = None):
    """First trap whose keyword appears (not as an accessory) at or under its price limit.

    For a post, the price must be the one written for that product: see ``price_near``. A price
    somewhere else in the post is never borrowed, and a line that is about reserving, winning or
    being gifted the product does not state its price.
    """
    for trap, position in _mentions(parsed, traps, in_post=price is None):
        found = price if price is not None else price_near(parsed, position)
        if found is not None and found <= trap.max_price:
            return trap, found
    return None


def evaluate_text(text: str, settings: Settings) -> Optional[Verdict]:
    """Judge a free-text deal post (Telegram message, feed entry)."""
    parsed = parse_text(text)
    if not parsed.text.strip() or parsed.expired == "over":
        return None
    deny = settings.deny_pattern
    if deny and deny.search(parsed.text):
        return None
    return _hedge(_judge(parsed, settings) or _quick_commerce(parsed, settings), parsed)


# ---- quick commerce and food apps ------------------------------------------------------------
# Their prices cannot be read (they depend on the delivery address and live inside the apps), and
# their deals are coupons and one-off loots that the ladder above ignores on purpose: "80% off up
# to ₹160" is ordinary noise for a shopping site. For these apps it is the whole game, so the big
# ones get a rule of their own, always quiet or high, never max.
_QUICK_BRANDS = tuple((re.compile(r"(?<![a-z])(?:" + pattern + r")(?![a-z])", re.I), name) for pattern, name in (
    # the more particular name first: "Swiggy Instamart" is Instamart
    (r"instamart", "Instamart"), (r"flipkart\s+minutes", "Flipkart Minutes"), (r"zepto", "Zepto"),
    (r"blinkit", "Blinkit"), (r"big\s?basket|bb\s?now", "BigBasket"), (r"jio\s?mart", "JioMart"),
    (r"swiggy", "Swiggy"), (r"zomato", "Zomato"), (r"domino'?s", "Domino's"), (r"eatsure", "EatSure"),
    (r"magicpin", "magicpin"),
))
_RUPEES = r"(?:₹|rs\.?)\s?(\d[\d,]*)"
_COUPON_PCT = re.compile(r"(\d{2,3})\s*%\s*(?:off|discount|cashback)", re.I)
_UPTO_BEFORE = re.compile(r"\bup\s?to\s*$", re.I)
_COUPON_CAP = re.compile(r"(?:\bup\s?to|\bmax(?:imum)?\.?(?:\s+discount)?(?:\s+is)?(?:\s+of)?)\s*:?\s*" + _RUPEES, re.I)
_MIN_ORDER = re.compile(
    r"(?:\borders?|\bcart|\bpurchase|\bbill|\bshopping|\bmin(?:imum)?\.?)(?:\s+(?:order|cart|value|amount))*\s*"
    r"(?:above|over|of|worth|from|:|-)?\s*" + _RUPEES, re.I)
_AMOUNT_OFF = re.compile(
    _RUPEES + r"\s*(?:off|discount|cashback)\s*(?:on|for)\s*(?:a\s+)?(?:min(?:imum)?\.?\s*)?(?:(?:orders?|cart|purchase|bill|shopping)\s*)?"
    r"(?:value\s*)?(?:above|over|of|worth|from)?\s*" + _RUPEES, re.I)
_COUPON_CODE = re.compile(r"(?i:\bcode\b)[\s:)\-–👉]*([A-Z][A-Z0-9]{3,19})\b")
_FLAT_BEFORE = re.compile(r"\bflat\s*$", re.I)
_LOOT_WORD = re.compile(r"\bloot\b", re.I)


def _quick_commerce(parsed: ParsedText, settings: Settings) -> Optional[Verdict]:
    text = parsed.text
    brand = next((name for pattern, name in _QUICK_BRANDS if pattern.search(text)), None)
    if brand is None:
        return None
    lines = text.split("\n")
    best = None  # (effective percent, wording)
    for index, line in enumerate(lines):
        around = " ".join(lines[max(0, index - 1):index + 2])
        flat = _AMOUNT_OFF.search(line)
        if flat:
            off, order = (float(group.replace(",", "")) for group in flat.groups())
            if 0 < off <= order and order >= 49:
                found = (off / order * 100, f"{fmt_inr(off)} off on a {fmt_inr(order)} order")
                best = found if best is None or found[0] > best[0] else best
        for percent in _COUPON_PCT.finditer(line):
            rate = float(percent.group(1))
            if rate > 100 or _UPTO_BEFORE.search(line[:percent.start()]):
                continue  # "up to 80% off" is a banner, not a coupon
            cap = _COUPON_CAP.search(line, percent.end()) or (
                _COUPON_CAP.search(lines[index + 1]) if index + 1 < len(lines) else None)
            least = _MIN_ORDER.search(around)
            if not (cap or least or _FLAT_BEFORE.search(line[:percent.start()])):
                continue  # "restaurants giving 70% off" describes a sale; a coupon states its terms
            wording = f"{rate:.0f}% off"
            effective = rate
            if cap:
                wording += f" up to {fmt_inr(float(cap.group(1).replace(',', '')))}"
            if least:
                wording += f" on orders from {fmt_inr(float(least.group(1).replace(',', '')))}"
            if cap and least and float(least.group(1).replace(",", "")) > 0:
                effective = min(rate, float(cap.group(1).replace(",", "")) / float(least.group(1).replace(",", "")) * 100)
            best = (effective, wording) if best is None or effective > best[0] else best
    if best and settings.quick_coupon_pct > 0 and best[0] >= settings.quick_coupon_pct:
        code = _COUPON_CODE.search(text)
        tier = TIER_HIGH if best[0] >= settings.min_discount else TIER_NORMAL
        return Verdict(tier, f"{brand} coupon: {best[1]}" + (f", code {code.group(1)}" if code else ""), None, None,
                       best[0], label="Coupon", max_tier=tier,
                       key=f"coupon:{brand.lower()}:{code.group(1) if code else int(best[0])}")
    if settings.quick_loots and _LOOT_WORD.search(text):
        price = next((amount.value for amount in parsed.amounts if amount.kind in ("sell", "plain")), None)
        if price is not None:
            return Verdict(TIER_NORMAL, f"{brand} loot post, no list price to check it against", price, None, 0.0,
                           label="Quick loot", max_tier=TIER_NORMAL)
    return None


def _hedge(verdict: Optional[Verdict], parsed: ParsedText) -> Optional[Verdict]:
    if verdict and parsed.expired:
        # Stock wording inside a sentence ("blue is out of stock now, black available") is not a
        # clear announcement, so the post is not thrown away: it goes out one step quieter.
        if verdict.tier <= TIER_NORMAL:
            return None
        verdict.tier -= 1
        verdict.max_tier = min(verdict.max_tier, TIER_HIGH)
        verdict.notes.append("the post hints that this may already be over")
    return verdict


def evaluate_stated(title: str, body: str, price: Optional[float], mrp: Optional[float],
                    settings: Settings) -> Optional[Verdict]:
    """Judge a deal-site card: a title, free text, and a price and list price the site gives in
    fields of their own. Those two need no reading out of the wording; everything else about
    the card (trap names, "price error" wording, sold-out notes, conditions) is read as in a post.
    """
    text = title + ("\n" + body if body else "")
    if price is None:
        # The member left the price fields empty: this is an offer or a coupon ("Flat Rs 25 on
        # sending Rs 5000"), not a priced product. Whatever the wording states outright still
        # counts; two bare numbers guessed to be a price and a list price do not.
        verdict = evaluate_text(text, settings)
        return None if verdict is not None and verdict.needs_trusted_link else verdict
    parsed = parse_text(text)
    if not parsed.text.strip() or parsed.expired == "over":
        return None
    deny = settings.deny_pattern
    if deny and deny.search(parsed.text):
        return None
    mrp = mrp if mrp and mrp > price else None
    discount = _pct(mrp, price)
    hit = match_trap(parse_text(title), settings.traps, price=price)
    if hit:
        trap = hit[0]
        # The same doubt as on a store listing: a spare part is named after the phone too. A
        # list price of at least twice the trap price says it is the device.
        if mrp and mrp >= trap.max_price * 2:
            verdict = Verdict(TIER_URGENT, f"trap '{trap.name}' at or under {fmt_inr(trap.max_price)}", price, mrp,
                              discount, label="Trap hit")
        else:
            verdict = Verdict(TIER_NORMAL, f"named like trap '{trap.name}' at {fmt_inr(price)}, but no list price "
                              "shows it is the device itself", price, mrp, discount, label="Trap match?",
                              max_tier=TIER_NORMAL)
        return _hedge(verdict, parsed)
    if parsed.error_phrase:
        return _hedge(Verdict(TIER_URGENT, f"says '{parsed.error_phrase}'", price, mrp, discount,
                              label="Price error?"), parsed)
    if mrp is None:
        # The list price field was left empty (or repeats the price). The wording may still state
        # it, or a percentage: read the card as a post whose price is known and labelled. As with
        # an empty price field, two bare numbers guessed into a pair do not count.
        verdict = evaluate_text(f"{title}\nDeal price: {fmt_inr(price)}" + ("\n" + body if body else ""), settings)
        return None if verdict is not None and verdict.needs_trusted_link else verdict
    saving = mrp - price
    tier = _ladder(discount, saving, price, settings)
    if not tier:
        return None
    reason = f"{discount:.0f}% off, saves {fmt_inr(saving)}"
    cap = TIER_URGENT
    # The price stands in a field, not on a line of the text, so a condition can be anywhere in it.
    if parsed.conditional or states_condition(parsed.text):
        cap, reason = TIER_HIGH, reason + " (needs a qualifying purchase)"
    return _hedge(Verdict(min(tier, cap), reason, price, mrp, discount, max_tier=cap), parsed)


def evaluate_tracked(title: str, price: float, usual: Optional[float], settings: Settings,
                     low: Optional[float] = None, new_low: bool = False, mrp: Optional[float] = None,
                     in_stock: bool = True) -> Optional[Verdict]:
    """Judge a product from a price tracker's list of fresh price drops.

    Nothing here is a claim: ``usual`` is the average price the tracker measured for this very
    product and ``low`` the lowest it ever saw. So the bar is how far the price stands below its
    own average, and it sits lower than for a "% off MRP" claim. A price the product has already
    been under before is one step less remarkable.

    On marketplace listings a reseller's passing fantasy price can pull the average above the
    product's list price; the price is then measured against the list price (``mrp``) instead.
    A product the tracker shows as out of stock is not announced at all.
    """
    if not in_stock:
        return None
    reference, called = usual, "it usually costs"
    if usual and mrp and mrp < usual:
        reference, called = mrp, "list price"
    hit = match_trap(parse_text(title), settings.traps, price=price)
    if hit:
        trap = hit[0]
        if reference and reference >= trap.max_price * 2:
            return Verdict(TIER_URGENT, f"trap '{trap.name}' at or under {fmt_inr(trap.max_price)}", price, None,
                           _pct(reference, price), label="Trap hit", was=reference)
        return Verdict(TIER_NORMAL, f"named like trap '{trap.name}' at {fmt_inr(price)}, but its usual price does "
                       "not show it is the device itself", price, None, _pct(reference, price), label="Trap match?",
                       max_tier=TIER_NORMAL, was=reference)
    if not reference or reference <= price:
        return None
    drop = (reference - price) / reference * 100
    saving = reference - price
    if drop < settings.tracker_drop_pct or saving < settings.urgent_min_saving:
        return None
    middle = (settings.tracker_drop_pct + settings.urgent_drop_pct) / 2
    tier = TIER_URGENT if drop >= settings.urgent_drop_pct else TIER_HIGH if drop >= middle else TIER_NORMAL
    reason = (f"{drop:.0f}% below the {fmt_inr(reference)} it usually costs" if called == "it usually costs"
              else f"{drop:.0f}% below its list price of about {fmt_inr(reference)}")
    at_low = new_low or low is None or price <= low * 1.03
    if at_low:
        reason += ", its lowest price so far"
    else:
        tier -= 1
        reason += f" (it has been as low as {fmt_inr(low)})"
        if tier < TIER_NORMAL:
            return None
    return Verdict(tier, reason, price, None, drop, label="Price drop", was=reference, max_tier=tier)


def _judge(parsed: ParsedText, settings: Settings) -> Optional[Verdict]:
    hit = match_trap(parsed, settings.traps)
    if hit:
        trap, price = hit
        mrp = parsed.mrp if parsed.pair_conf and parsed.price == price else None
        return Verdict(TIER_URGENT, f"trap '{trap.name}' at or under {fmt_inr(trap.max_price)}", price, mrp,
                       _pct(mrp, price), label="Trap hit")

    if parsed.error_phrase:
        price = parsed.price
        mrp = parsed.mrp if parsed.pair_conf else None
        return Verdict(TIER_URGENT, f"says '{parsed.error_phrase}'", price, mrp,
                       parsed.calc_discount if parsed.pair_conf else 0.0, label="Price error?")

    claim, lone = parsed.pct_claim, parsed.claim_price
    stated = claim is not None and lone is not None

    # 1. A stated price and MRP. A pair that was only inferred from two bare numbers gives way to
    #    a percentage written beside a price: that is the post's own word on the discount.
    if parsed.pair_conf and (parsed.pair_conf != "inferred" or not stated):
        confident = parsed.pair_conf in ("labelled", "arrow")
        discount = parsed.calc_discount
        tier = _ladder(discount, parsed.saving, parsed.price, settings)
        if not tier:
            return None
        reason = f"{discount:.0f}% off, saves {fmt_inr(parsed.saving)}"
        cap = TIER_URGENT
        if not confident:
            cap, reason = TIER_HIGH, reason + " (prices not labelled)"
        elif parsed.ambiguous:
            cap, reason = TIER_HIGH, reason + " (another amount stands beside the MRP)"
        if parsed.conditional and cap > TIER_HIGH:
            cap, reason = TIER_HIGH, reason + " (needs a qualifying purchase)"
        return Verdict(min(tier, cap), reason, parsed.price, parsed.mrp, discount,
                       needs_trusted_link=not confident, max_tier=cap)

    # 2. A stated price and a stated percentage ("₹6,294 / 80% off"): the saving follows from the two.
    if stated:
        rate = min(claim, 99.5)  # "100% off" beside a price is a rounded 99.5% or more
        saving = lone * rate / (100.0 - rate)
        tier = _ladder(rate, saving, lone, settings)
        if not tier:
            return None
        cap = TIER_HIGH if parsed.conditional else TIER_URGENT
        return Verdict(min(tier, cap), f"stated as {claim:.0f}% off, about {fmt_inr(saving)} saved", lone, None, claim,
                       max_tier=cap)

    # 3. Only a percentage, with no price it can be tied to: worth a look only when it is extreme,
    #    or when what it is claimed for is a product you set a trap on ("91% Off - Apple MacBook Air").
    if claim is not None and claim >= settings.urgent_discount:
        return Verdict(TIER_HIGH, f"claims {claim:.0f}% off", None, None, claim)
    if claim is not None and claim >= settings.high_discount and not parsed.amounts:
        trap = next((trap for trap, _ in _mentions(parsed, settings.traps, in_post=True)), None)
        if trap:
            return Verdict(TIER_HIGH, f"claims {claim:.0f}% off '{trap.name}', no price given", None, None, claim,
                           label="Trap product")
    return None


def _ladder(discount: float, saving: float, price: Optional[float], settings: Settings) -> Optional[int]:
    """Priority for a stated discount. Percent-off alone is cheap talk in India (a ₹79 gadget with a
    ₹999 "MRP" is "92% off" every day), so each rung also needs real rupees saved, or a give-away price."""
    loot = price is not None and price <= settings.loot_price
    if discount >= settings.urgent_discount:
        if saving >= settings.urgent_min_saving:
            return TIER_URGENT
        return TIER_HIGH if (saving >= settings.min_saving or loot) else None
    if discount >= settings.high_discount:
        if saving >= settings.big_saving:
            return TIER_URGENT  # an iPhone at ₹6,999: "only" 91% off, but ₹72,000 is not a rounding error
        return TIER_HIGH if (saving >= settings.min_saving or loot) else None
    if discount >= settings.min_discount:
        return TIER_NORMAL if saving >= settings.urgent_min_saving else None
    return None


def is_side_product(title: str, strong_mrp: bool = False) -> bool:
    """A store listing entry that is an accessory, a spare or a used unit, not the product itself.

    Store titles are long and full of features ("..., Backlit Keyboard, Fast Charge"), so an
    accessory noun only counts where accessory titles actually put it: in the first few words
    ("Laptop Skin ...", "Wireless Mouse ...") or as "<accessory> for <device>" near the start.
    ``strong_mrp`` says the card's list price is several times the page's floor. Accessories do
    not carry such list prices, so it overrules the word test ("Motorola Edge 60 Stylus" is a
    phone), but never the used / made-for wording.
    """
    if _REFURB.search(title) or _MADE_FOR.search(title):
        return True
    if strong_mrp:
        return False
    words = title.split()
    return bool(_ACCESSORY.search(" ".join(words[:4])) or _ACCESSORY_FOR.search(" ".join(words[:9])))


def _entered(price: float, limit: float, info: PriceInfo, settings: Settings) -> bool:
    """True when this reading is news: the product is at or under ``limit`` and that has not
    already been reported, or accepted as the normal state of its page.

    Without this, the permanently cheap items on a page would alert for ever. The door reopens
    when a product that was reported comes back after a long absence (the stored price is then
    the old error price, not what it costs today) and when a price falls clearly further.
    """
    if price > limit:
        return False
    if info.last_price is None or info.last_price > limit:
        return True  # first sighting, or it has just crossed the line
    if info.quiet_price is None:
        return True  # it was this cheap before, but nothing was sent and it was not part of a baseline
    if not info.quiet_alerted:
        # Part of its page's normal picture (a phone that always costs a little under a trap
        # price): a small dip is not news, a real fall from that normal price is.
        return price <= info.quiet_price * (1 - settings.watch_drop_pct / 100.0)
    if info.last_age is not None and info.last_age > settings.absence_hours * 3600:
        return True  # reported once, then gone for a long time: the same mistake made again
    return price <= info.quiet_price * 0.9  # reported already: only a clearly deeper fall is news


def evaluate_offer(title: str, price: float, mrp: Optional[float], info: PriceInfo, settings: Settings,
                   target: Optional[float] = None, listing: bool = False, baseline: bool = False) -> Optional[Verdict]:
    """Judge a product read straight from a store page on the watchlist.

    ``target`` is the price you set for the page: a target for a product page, a floor for a
    search/category page ("nothing here should cost this little"). ``baseline`` marks the very
    first read of a listing, which only records what is normal there.
    """
    if listing and baseline:
        return None

    if target is not None and _entered(price, target, info, settings):
        if not listing:
            return Verdict(TIER_URGENT, f"at or under your target {fmt_inr(target)}", price, mrp,
                           _pct(mrp, price), label="Target hit")
        # Store listings are noisy: cheap odds and ends sit in every category and the same search
        # returns a different mix each time. So being under the floor is not enough. There must
        # also be a reason to believe the thing should cost far more: we watched it cost at least
        # twice the floor ourselves, or its list price is several times the floor.
        watched_higher = max(info.baseline or 0.0, info.last_price or 0.0)
        if not is_side_product(title, strong_mrp=_strong_mrp(mrp, target)):
            if watched_higher >= target * 2:
                return Verdict(TIER_URGENT, f"fell from {fmt_inr(watched_higher)} to under this page's "
                               f"{fmt_inr(target)} floor", price, mrp, _pct(mrp or watched_higher, price),
                               label="Under the floor", was=watched_higher)
            if mrp and mrp >= target * settings.floor_mrp_ratio:
                return Verdict(TIER_URGENT, f"new under this page's {fmt_inr(target)} floor, list price {fmt_inr(mrp)}",
                               price, mrp, _pct(mrp, price), label="Under the floor")

    hit = match_trap(parse_text(title), settings.traps, price=price)
    if hit and _entered(price, hit[0].max_price, info, settings):
        trap = hit[0]
        # On a listing the title alone is weak proof: spare parts are named after the phone too
        # ("Nothing Phone (2a) Display Combo"). A list price, or a price we watched ourselves, of
        # at least twice the trap price says it is the device.
        proof = max(mrp or 0.0, info.baseline or 0.0, info.last_price or 0.0)
        if not listing or proof >= trap.max_price * 2:
            return Verdict(TIER_URGENT, f"trap '{trap.name}' at or under {fmt_inr(trap.max_price)}", price, mrp,
                           _pct(mrp, price), label="Trap hit")
        return Verdict(TIER_NORMAL, f"named like trap '{trap.name}' at {fmt_inr(price)}, but no list price shows it "
                       "is the device itself", price, mrp, _pct(mrp, price), label="Trap match?", max_tier=TIER_NORMAL)

    if info.baseline and info.observations >= 1 and price < info.baseline:
        drop = (info.baseline - price) / info.baseline * 100
        line = info.baseline * (1 - settings.watch_drop_pct / 100.0)
        if drop >= settings.watch_drop_pct and _entered(price, line, info, settings):
            saving = info.baseline - price
            tier = TIER_URGENT if saving >= settings.urgent_min_saving else TIER_HIGH
            if listing:
                # On a category page nobody chose this product, and "its usual price" may rest on
                # a few readings of a seller's passing high price. So the size of the fall sets
                # the volume: only a fall to a fraction of the price is treated as an error.
                # (A fall to under the page's floor has already rung above.)
                # And with thousands of products followed, a no-name gadget going from ₹449 to
                # ₹199 is not worth anyone's attention: the fall has to save real money.
                if saving < settings.min_saving:
                    return None
                middle = (settings.watch_drop_pct + settings.urgent_drop_pct) / 2
                tier = min(tier, TIER_URGENT if drop >= settings.urgent_drop_pct else TIER_HIGH if drop >= middle
                           else TIER_NORMAL)
                if mrp and info.baseline >= mrp * 0.98 and drop < settings.urgent_drop_pct:
                    # It was seen at its full list price and is back at the discount its card
                    # always shows: the odd reading was the earlier one. Not news.
                    return None
            return Verdict(tier, f"dropped {drop:.0f}% from the {fmt_inr(info.baseline)} it was seen at", price,
                           None, drop, label="Price drop", was=info.baseline)

    # A product this monitor has never met, standing on a cheapest-first page at a huge discount
    # to its list price: a dropped digit (₹74,990 laptop at ₹7,499) lands here, above the floor
    # and with no history to compare against. Ordinary no-name listings on these pages show
    # 50-70% off inflated list prices, so the bar is set well above that.
    returning = (info.quiet_alerted and info.last_age is not None
                 and info.last_age > settings.absence_hours * 3600)  # reported before, then long gone
    if listing and target is not None and (info.first_seen or returning) and mrp and price > target:
        discount = _pct(mrp, price)
        if (discount >= settings.new_listing_discount and mrp >= target * settings.floor_mrp_ratio
                and mrp - price >= settings.urgent_min_saving
                and not is_side_product(title, strong_mrp=_strong_mrp(mrp, target) if target else False)):
            # A floor of 0 marks a page sorted by discount. The top of such a page is always at
            # 95% off and more (clearance stock), so a newcomer there is worth a look, not a siren.
            tier = TIER_URGENT if discount >= settings.urgent_discount and target > 0 else TIER_HIGH
            return Verdict(tier, f"new on this page at {discount:.0f}% off its list price {fmt_inr(mrp)}", price, mrp,
                           discount, label="Far below list price", max_tier=tier)

    if not listing and info.first_seen:
        discount = _pct(mrp, price)
        if discount >= settings.min_discount:
            return Verdict(TIER_HIGH, f"{discount:.0f}% off MRP on first check", price, mrp, discount)
    return None


def _strong_mrp(mrp: Optional[float], floor: float) -> bool:
    return bool(mrp and mrp >= max(floor * STRONG_MRP_RATIO, STRONG_MRP_MIN))


def _pct(mrp: Optional[float], price: float) -> float:
    return (mrp - price) / mrp * 100 if mrp and mrp > price >= 0 else 0.0
