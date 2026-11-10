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

For a product read from a store page the signals are stronger and all urgent: it appeared under
its category page's price floor, it hit your target, it tripped a trap, or it fell far below the
price this monitor has been seeing for it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from .config import Settings, Trap
from .db import PriceInfo
from .textparse import ParsedText, fmt_inr, parse_text, price_near

TIER_URGENT, TIER_HIGH, TIER_NORMAL = 5, 4, 3
TIER_NAMES = {TIER_URGENT: "urgent", TIER_HIGH: "high", TIER_NORMAL: "normal"}

# A trap for "nothing phone" must not fire on "Back cover for Nothing Phone" or "Nothing Phone 3a case".
_ACCESSORY_WORDS = (
    r"(?:cases?|covers?|tempered|screen\s*(?:guard|glass|protector)|protectors?|guard|skins?|cables?|chargers?"
    r"|adapters?|adaptors?|straps?|holders?|stands?|pouch(?:es)?|sleeves?|bumpers?|stickers?|lens|back\s*panel"
    r"|controllers?|dualsense|remote|docks?|hubs?|keyboards?|mouse|pencil|stylus|games?|spares?|replacement|parts?)"
)
_ACCESSORY = re.compile(r"\b" + _ACCESSORY_WORDS + r"\b", re.I)
_ACCESSORY_FOR = re.compile(r"\b" + _ACCESSORY_WORDS + r"\b[^,|()]{0,30}\bfor\b", re.I)  # "Back Cover Case for iPhone"
_FOR_PREFIX = re.compile(r"\b(?:for|compatible(?:\s+with)?|fits?|suitable\s+for)\s+(?:the\s+)?(?:[\w()+.\-]+\s+){0,3}$", re.I)
# "... with free case", "charger not included": the accessory word is a side note, the product is the real thing.
_SIDE_NOTE = re.compile(r"\b(?:with|without|free|includes?|including|included|plus|in\s*box|inbox)\b|[+&]", re.I)
_NOT_NEW = re.compile(r"\b(?:refurbished|renewed|pre-?owned|used|second\s*hand|open\s*box|unboxed|rental|rent|emi)\b", re.I)
_PRICE_MARK = re.compile(r"@|₹|\brs\b|\bmrp\b|\b(?:at|for)\s+(?:just|only)\b", re.I)
# For whole product titles on a store listing (long, spec-heavy: "... Backlit Keyboard, 1.7 kg"):
# only the first words name the product, so only they are checked for accessory nouns.
_REFURB = re.compile(r"\b(?:refurbished|renewed|pre-?owned|second\s*hand|open\s*box|unboxed)\b|\(used\)", re.I)
_MADE_FOR = re.compile(r"\b(?:compatible\s+(?:with|for)|designed\s+for|suitable\s+for|replacement\s+for)\b", re.I)


@dataclass
class Verdict:
    tier: int
    reason: str
    price: Optional[float] = None
    mrp: Optional[float] = None
    discount: float = 0.0
    needs_trusted_link: bool = False  # only valid if the post links to a known store
    label: str = ""  # shown first in the notification title: "Price error?", "Trap hit", ...
    flags: List[str] = field(default_factory=list)

    @property
    def headline(self) -> str:
        bits: List[str] = []
        if self.discount >= 1:
            bits.append(f"{self.discount:.0f}% off")
        if self.price is not None:
            bits.append(fmt_inr(self.price) + (f" (MRP {fmt_inr(self.mrp)})" if self.mrp else ""))
        numbers = " · ".join(bits)
        if self.label:
            return f"{self.label}: {numbers}" if numbers else self.label
        return numbers or "Deal alert"


def is_accessory(text: str, keyword_start: int, keyword_end: Optional[int] = None) -> bool:
    """True when the keyword is not the thing on sale: an accessory for it, or a used/refurbished unit."""
    line_start = text.rfind("\n", 0, keyword_start) + 1
    line_end = text.find("\n", keyword_start)
    line_end = len(text) if line_end == -1 else line_end
    before = text[line_start:keyword_start]
    if _ACCESSORY.search(before) or _FOR_PREFIX.search(before) or _NOT_NEW.search(text[line_start:line_end]):
        return True
    # "iPhone 15 case @ ₹99": an accessory word right after the name, before any price.
    after = text[keyword_end if keyword_end is not None else keyword_start:line_end][:60]
    after = _PRICE_MARK.split(after, 1)[0]
    hit = _ACCESSORY.search(after)
    return bool(hit and not _SIDE_NOTE.search(after))


def match_trap(parsed: ParsedText, traps: Sequence[Trap], price: Optional[float] = None):
    """First trap whose keyword appears (not as an accessory) at or under its price limit."""
    for trap in traps:
        for match in trap.pattern.finditer(parsed.text):
            if is_accessory(parsed.text, match.start(), match.end()):
                continue
            found = price if price is not None else price_near(parsed, match.start())
            if found is None:
                found = parsed.price
            if found is not None and found <= trap.max_price:
                return trap, found
    return None


def evaluate_text(text: str, settings: Settings) -> Optional[Verdict]:
    """Judge a free-text deal post (Telegram message, feed entry)."""
    parsed = parse_text(text)
    if not parsed.text.strip() or parsed.expired:
        return None
    deny = settings.deny_pattern
    if deny and deny.search(parsed.text):
        return None

    hit = match_trap(parsed, settings.traps)
    if hit:
        trap, price = hit
        mrp = parsed.mrp if parsed.mrp and parsed.mrp > price else None
        discount = (mrp - price) / mrp * 100 if mrp else 0.0
        return Verdict(TIER_URGENT, f"trap '{trap.name}' at or under {fmt_inr(trap.max_price)}", price, mrp, discount,
                       label="Trap hit")

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
        if tier:
            reason = f"{discount:.0f}% off, saves {fmt_inr(parsed.saving)}"
            if not confident:
                tier = min(tier, TIER_HIGH)
                reason += " (prices not labelled)"
            return Verdict(tier, reason, parsed.price, parsed.mrp, discount, needs_trusted_link=not confident)
        return None

    # 2. A stated price and a stated percentage ("₹6,294 / 80% off"): the saving follows from the two.
    claim, lone = parsed.pct_claim, parsed.lone_price
    if claim is not None and lone is not None and 0 < claim < 100:
        saving = lone * claim / (100.0 - claim)
        tier = _ladder(claim, saving, lone, settings)
        if tier:
            return Verdict(tier, f"stated as {claim:.0f}% off, about {fmt_inr(saving)} saved", lone, None, claim)
        return None

    # 3. Only a percentage, no price at all: worth a look only when it is extreme.
    if claim is not None and claim >= settings.urgent_discount and lone is None:
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


def is_side_product(title: str) -> bool:
    """A store listing entry that is an accessory, a spare or a used unit, not the product itself.

    Store titles are long and full of features ("..., Backlit Keyboard, Fast Charge"), so an
    accessory noun only counts where accessory titles actually put it: in the first few words
    ("Laptop Skin ...", "Wireless Mouse ...") or as "<accessory> for <device>" near the start.
    """
    if _REFURB.search(title) or _MADE_FOR.search(title):
        return True
    words = title.split()
    return bool(_ACCESSORY.search(" ".join(words[:4])) or _ACCESSORY_FOR.search(" ".join(words[:9])))


def _entered(price: float, limit: float, info: PriceInfo) -> bool:
    """True when this reading is the first we know of that puts the product at or under ``limit``.

    Something that was already that cheap the last time we looked is the normal state of that
    page, not news: without this, the permanently cheap items on a page would alert for ever.
    """
    return price <= limit and (info.last_price is None or info.last_price > limit)


def evaluate_offer(title: str, price: float, mrp: Optional[float], info: PriceInfo, settings: Settings,
                   target: Optional[float] = None, listing: bool = False, baseline: bool = False) -> Optional[Verdict]:
    """Judge a product read straight from a store page on the watchlist.

    ``target`` is the price you set for the page: a target for a product page, a floor for a
    search/category page ("nothing here should cost this little"). ``baseline`` marks the very
    first read of a listing, which only records what is normal there.
    """
    if listing and baseline:
        return None

    if target is not None and _entered(price, target, info):
        if not listing:
            return Verdict(TIER_URGENT, f"at or under your target {fmt_inr(target)}", price, mrp,
                           _pct(mrp, price), label="Target hit")
        # Store listings are noisy: cheap odds and ends sit in every category and the same search
        # returns a different mix each time. So being under the floor is not enough. There must
        # also be a reason to believe the thing should cost far more: we watched it cost at least
        # twice the floor ourselves, or its list price is several times the floor.
        watched_higher = max(info.baseline or 0.0, info.last_price or 0.0)
        if not is_side_product(title):
            if watched_higher >= target * 2:
                return Verdict(TIER_URGENT, f"fell from {fmt_inr(watched_higher)} to under this page's "
                               f"{fmt_inr(target)} floor", price, mrp or watched_higher, _pct(mrp or watched_higher, price),
                               label="Under the floor")
            if mrp and mrp >= target * settings.floor_mrp_ratio:
                return Verdict(TIER_URGENT, f"new under this page's {fmt_inr(target)} floor, list price {fmt_inr(mrp)}",
                               price, mrp, _pct(mrp, price), label="Under the floor")

    hit = match_trap(parse_text(title), settings.traps, price=price)
    if hit and _entered(price, hit[0].max_price, info):
        trap, _ = hit
        return Verdict(TIER_URGENT, f"trap '{trap.name}' at or under {fmt_inr(trap.max_price)}", price, mrp,
                       _pct(mrp, price), label="Trap hit")

    if info.baseline and info.observations >= 1 and price < info.baseline:
        drop = (info.baseline - price) / info.baseline * 100
        was_already_low = (
            info.last_price is not None
            and (info.baseline - info.last_price) / info.baseline * 100 >= settings.watch_drop_pct
        )
        if drop >= settings.watch_drop_pct and not was_already_low:
            saving = info.baseline - price
            tier = TIER_URGENT if saving >= settings.urgent_min_saving else TIER_HIGH
            return Verdict(tier, f"dropped {drop:.0f}% from its usual {fmt_inr(info.baseline)}", price,
                           info.baseline, drop, label="Price drop")

    if not listing and info.first_seen:
        discount = _pct(mrp, price)
        if discount >= settings.min_discount:
            return Verdict(TIER_HIGH, f"{discount:.0f}% off MRP on first check", price, mrp, discount)
    return None


def _pct(mrp: Optional[float], price: float) -> float:
    return (mrp - price) / mrp * 100 if mrp and mrp > price >= 0 else 0.0
