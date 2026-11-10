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
from typing import List, Optional, Sequence

from .config import Settings, Trap
from .db import PriceInfo
from .textparse import SOFT, ParsedText, fmt_inr, naming_words, parse_text, price_near

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
# "... for iPhone 16", "compatible with the iPhone", "(works with iPhone)": the keyword is what it fits.
_LINKED = re.compile(
    r"\b(?P<link>for|compatible(?:\s+(?:with|for))?|works?\s+with|made\s+for|designed\s+for|fits?|suitable\s+for"
    r"|supports?)\s+(?:the\s+)?(?:[\w()+.\-]+\s+){0,3}$",
    re.I,
)
# Makers of cases, chargers and cables name their products after the device: "OtterBox React Apple
# iPhone 14 PRO Stardust Clear" has no accessory noun in it at all.
_ACCESSORY_MAKERS = re.compile(
    r"\b(?:otterbox|spigen|esr|ringke|caseology|uag|urban\s+armor|nillkin|supcase|zagg|belkin|anker|baseus|ugreen"
    r"|portronics|ambrane|stuffcool|dbrand|pitaka|torras|benks|raegr|kapaver|tarkan|incipio|speck|casetify|urbn"
    r"|amazon\s*basics|solimo|duracell|wecool|dailyobjects|macmerise)\b",
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

    @property
    def headline(self) -> str:
        bits: List[str] = []
        if self.discount >= 1:
            shown = self.discount if self.discount >= 100 else min(self.discount, 99.0)  # 99.7% is not "100% off"
            bits.append(f"{shown:.0f}% off")
        if self.price is not None:
            bits.append(fmt_inr(self.price) + (f" (MRP {fmt_inr(self.mrp)})" if self.mrp else ""))
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
    # "<Maker> <model> Apple iPhone 14 PRO <colour>": the keyword closes another product's name.
    # Two or more naming words run straight into it, with nothing but its own brand in between.
    phrase = _DEVICE_BRAND.sub("", _PHRASE_END.split(before)[-1])
    if naming_words(phrase) >= 2:
        return True
    linked = _LINKED.search(before)
    if linked:
        # "Lowest price ever for iPhone 16" is still the phone; "Lanyard for CMF Phone" is not.
        if linked.group("link").lower() != "for" or naming_words(before[:linked.start()]) >= 1:
            return True
    # "Mobile Tripod Stand | iPhone & Android | ...": named in a later part of a line whose opening
    # part names another product, with no price of its own, it only says what that product fits.
    if text[seg_start:seg_start + 1] == SOFT and not _PRICE_MARK.search(text[seg_start:seg_end]):
        line_start = seg_start
        while line_start > 0 and text[line_start:line_start + 1] == SOFT:
            line_start = text.rfind("\n", 0, line_start - 1) + 1
        opening = text[line_start:text.find("\n", line_start)]
        if naming_words(opening) >= 2:
            return True
    # "iPhone 15 case @ ₹99": an accessory word right after the name, before any price.
    after = _PRICE_MARK.split(text[keyword_end:seg_end][:60], 1)[0]
    hit = _ACCESSORY.search(after)
    return bool(hit and not _SIDE_NOTE.search(after[:hit.start()]) and not _NOT_PART.match(after[hit.end():]))


def match_trap(parsed: ParsedText, traps: Sequence[Trap], price: Optional[float] = None):
    """First trap whose keyword appears (not as an accessory) at or under its price limit.

    For a post, the price must be the one written for that product: see ``price_near``. A price
    somewhere else in the post is never borrowed.
    """
    for trap in traps:
        for match in trap.pattern.finditer(parsed.text):
            if is_accessory(parsed.text, match.start(), match.end()):
                continue
            found = price if price is not None else price_near(parsed, match.start())
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
    verdict = _judge(parsed, settings)
    if verdict and parsed.expired:
        # Stock wording inside a sentence ("blue is out of stock now, black available") is not a
        # clear announcement, so the post is not thrown away: it goes out one step quieter.
        if verdict.tier <= TIER_NORMAL:
            return None
        verdict.tier -= 1
        verdict.max_tier = min(verdict.max_tier, TIER_HIGH)
        verdict.notes.append("the post hints that this may already be over")
    return verdict


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

    # 1. A stated price and MRP.
    if parsed.pair_conf:
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
    claim, lone = parsed.pct_claim, parsed.claim_price
    if claim is not None and lone is not None:
        rate = min(claim, 99.5)  # "100% off" beside a price is a rounded 99.5% or more
        saving = lone * rate / (100.0 - rate)
        tier = _ladder(rate, saving, lone, settings)
        if not tier:
            return None
        cap = TIER_HIGH if parsed.conditional else TIER_URGENT
        return Verdict(min(tier, cap), f"stated as {claim:.0f}% off, about {fmt_inr(saving)} saved", lone, None, claim,
                       max_tier=cap)

    # 3. Only a percentage, with no price it can be tied to: worth a look only when it is extreme.
    if claim is not None and claim >= settings.urgent_discount:
        return Verdict(TIER_HIGH, f"claims {claim:.0f}% off", None, None, claim)
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
                               f"{fmt_inr(target)} floor", price, mrp or watched_higher, _pct(mrp or watched_higher, price),
                               label="Under the floor")
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
            return Verdict(tier, f"dropped {drop:.0f}% from its usual {fmt_inr(info.baseline)}", price,
                           info.baseline, drop, label="Price drop")

    # A product this monitor has never met, standing on a cheapest-first page at a huge discount
    # to its list price: a dropped digit (₹74,990 laptop at ₹7,499) lands here, above the floor
    # and with no history to compare against. Ordinary no-name listings on these pages show
    # 50-70% off inflated list prices, so the bar is set well above that.
    returning = (info.quiet_alerted and info.last_age is not None
                 and info.last_age > settings.absence_hours * 3600)  # reported before, then long gone
    if listing and target is not None and (info.first_seen or returning) and mrp and price > target:
        discount = _pct(mrp, price)
        if (discount >= settings.new_listing_discount and mrp >= target * settings.floor_mrp_ratio
                and not is_side_product(title, strong_mrp=_strong_mrp(mrp, target))):
            tier = TIER_URGENT if discount >= settings.urgent_discount else TIER_HIGH
            return Verdict(tier, f"new on this page at {discount:.0f}% off its list price {fmt_inr(mrp)}", price, mrp,
                           discount, label="Far below list price")

    if not listing and info.first_seen:
        discount = _pct(mrp, price)
        if discount >= settings.min_discount:
            return Verdict(TIER_HIGH, f"{discount:.0f}% off MRP on first check", price, mrp, discount)
    return None


def _strong_mrp(mrp: Optional[float], floor: float) -> bool:
    return bool(mrp and mrp >= max(floor * STRONG_MRP_RATIO, STRONG_MRP_MIN))


def _pct(mrp: Optional[float], price: float) -> float:
    return (mrp - price) / mrp * 100 if mrp and mrp > price >= 0 else 0.0
