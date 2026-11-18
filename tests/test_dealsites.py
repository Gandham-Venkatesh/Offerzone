"""Deal sites read as pages (DesiDime, DealsMagnet, the PriceHistory tracker) and how their cards are judged.

The card markup below is cut down from the real pages as they were on 12 Nov 2026.
"""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from lootwatch.config import Settings, load_traps
from lootwatch.db import DB
from lootwatch.fetch import Fetcher
from lootwatch.links import LinkResolver, classify_host
from lootwatch.monitor import Monitor
from lootwatch.notify import Notifier
from lootwatch.scoring import TIER_HIGH, TIER_NORMAL, TIER_URGENT, evaluate_stated, evaluate_tracked
from lootwatch.sources.dealsites import (SiteError, age_seconds, parse_dealsmagnet, parse_desidime,
                                         parse_pricehistory, site_label, site_reader)
from tests.helpers import FakeWeb


def desidime_card(deal_id, title, ago="1 minute ago", price="₹ 1,530", mrp="₹ 9,999", store="Amazon",
                  body="Apply 50% Off Coupon", buy=None):
    slug = f"deals/some-deal-{deal_id}"
    prices = ""
    if price:
        prices += f'<div class="md:text-xll text-lg font-bold text-primary mr-2"> {price} </div>'
    if mrp:
        prices += f'<div class="md:text-xll text-lg line-through text-secondary-text-light">{mrp}</div><div>(85%)</div>'
    buy = buy or f"https://visit.desidime.com/visit/home-new-get-deals-1/{deal_id}"
    return f"""<article class="cursor-pointer group card deal-card" data-gtm-deal-id="{deal_id}" data-gtm-store="{store}"
 data-permalink="{slug}">
<div><div class="flex items-center"><span class="text-xs"> {ago}</span></div>
<div><span class="hotness"> New </span></div></div>
<div class="row"><div class="custom-card-title"><a href="/{slug}">{title}</a></div>
<div class="flex items-center">{prices}<div><span><a href="/stores/amazon-india">{store}</a></span><span>Deals</span></div></div></div>
<p><span>{body}... <span class="continuelink">Read More</span></span></p>
<div><a aria-label="Comment Deal" href="/{slug}#comments"><span>0</span></a>
<a href="{buy}" rel="nofollow">Buy Now</a></div></article>"""


def desidime_page(*cards):
    return "<html><body><div class='deals-list'>" + "".join(cards) + "</div></body></html>"


DEALSMAGNET = """<html><body><div class="row bg-white">
<div class="col-6"><div class="card p-1 shadow-sm hotdeal">
 <div class="card-header"><a href="https://www.dealsmagnet.com/user/antman">@antman</a>
  <div class="col-5 TimeDuration"> 10 min </div></div>
 <a class="MainCardAnchore" href="https://www.dealsmagnet.com/deal/dyson-supersonic-hair-dryer-1cmsNcpJ">
  <div class="card-img"><img alt="dyson..."/><div class="Ribbon"> Use Code : EXCLUXIVEDEAL </div></div></a>
 <div class="card-body"><p class="card-text"><a class="MainCardAnchore"
   href="https://www.dealsmagnet.com/deal/dyson-supersonic-hair-dryer-1cmsNcpJ"> dyson Supersonic Hair Dryer </a></p>
  <div class="row card-text"><div class="card-DiscountPrice"><span class="big"> 38% </span><span>off </span></div>
   <div class="card-DealPrice"><span class="small">₹</span>22900</div><div class="card-OriginalPrice"> ₹36900 </div></div></div>
 <div class="card-footer"><a href="https://www.dealsmagnet.com/offers/myntra"><img alt="myntra"/></a>
  <div class="btn Card-CopyCoupon" onclick="CopyCoupon('BEAUTYEXTRA','1cmsNcpJ',true)">Get</div></div>
</div></div>
<div class="col-6"><div class="card p-1 shadow-sm">
 <div class="card-header"><div class="col-5 TimeDuration"> 2 hr </div></div>
 <a class="MainCardAnchore" href="https://www.dealsmagnet.com/deal/liora-wet-wipes-1dieE1RE"><div class="card-img"><img/></div></a>
 <div class="card-body"><p class="card-text"><a class="MainCardAnchore"
   href="https://www.dealsmagnet.com/deal/liora-wet-wipes-1dieE1RE"> Liora Face Cleansing Wet Wipes - Pack of 500 </a></p>
  <div class="row card-text"><div class="card-DealPrice"><span class="small">₹</span>400</div>
   <div class="card-OriginalPrice"> ₹7500 </div></div></div>
 <div class="card-footer"><a href="https://www.dealsmagnet.com/offers/amazon"><img alt="amazon"/></a></div>
</div></div></div></body></html>"""


def tracker_card(slug, name, price, text, store="amazon", product="B06XZZWTL6", stamp="2026-11-12T13:43:33+05:30",
                 low="0"):
    host = "amazon.in" if store == "amazon" else "flipkart.com"
    return f"""<li class="phd-card" data-store="{store}" data-low="{low}" data-price="{price}" data-drop="1">
<a class="phd-media" href="https://pricehistory.app/p/{slug}"><img alt="x"/></a>
<p class="phd-found"><time datetime="{stamp}">Dropped 1 min ago</time></p>
<h3 class="phd-name"><a href="https://pricehistory.app/p/{slug}">{name}</a></h3>
<p class="phd-price"><b>₹{price:,}</b> <span class="phd-pct">−71%</span></p>{text}
<a class="phd-buy" data-link="{host}?phasource=pricehistory&amp;phaproduct={product}&amp;phastore={host}"
 href="https://pricehistory.app/p/{slug}">Buy</a></li>"""


def tracker_page(*cards):
    return "<html><body><ol class='phd-grid'>" + "".join(cards) + "</ol></body></html>"


class Readers(unittest.TestCase):
    def test_times_written_as_words(self):
        self.assertEqual(age_seconds("1 minute ago"), 60)
        self.assertEqual(age_seconds(" 7 min "), 420)
        self.assertEqual(age_seconds("an hour ago"), 3600)
        # Rounded wording gets the youngest age it allows: "about 1 hour" is what DesiDime writes from 45 minutes on.
        self.assertEqual(age_seconds("about 1 hour ago"), 45 * 60)
        self.assertEqual(age_seconds("about an hour ago"), 45 * 60)
        self.assertEqual(age_seconds("about 3 hours ago"), 2.5 * 3600)
        self.assertEqual(age_seconds("2 hr"), 7200)
        self.assertEqual(age_seconds("just now"), 0)
        self.assertEqual(age_seconds("yesterday"), 86400)
        self.assertIsNone(age_seconds("Dropped today"))
        self.assertIsNone(age_seconds(""))

    def test_desidime_cards(self):
        now = 1_800_000_000.0
        page = desidime_page(
            desidime_card(2196450, "CableCreation 5-in-1 USB C Hub"),
            desidime_card(2196448, "Kid-Track Pant Under 232", ago="4 minutes ago", price="", mrp="", store="Shopsy",
                          body="Kid-Track Pant Under 232"),
            desidime_card(2196440, "delete", price="₹ 450", mrp="₹ 2,999"),
            desidime_card(2196439, "Myntra: clothing up to 82% off", price="82%", mrp=""),
        )
        items = parse_desidime(page, "https://www.desidime.com/new", now)
        self.assertEqual([i.item_id.split(":")[0] for i in items], ["dd2196450", "dd2196448", "dd2196439"],
                         "the blanked card is skipped")
        self.assertEqual(items[0].item_id, "dd2196450:1530", "the price is part of what was announced")
        hub = items[0]
        self.assertEqual((hub.price, hub.mrp, hub.store), (1530.0, 9999.0, "Amazon"))
        self.assertEqual(hub.ts, now - 60)
        self.assertEqual(hub.url, "https://www.desidime.com/deals/some-deal-2196450")
        self.assertEqual(hub.links, ["https://visit.desidime.com/visit/home-new-get-deals-1/2196450"],
                         "the Buy Now link alone says where the deal leads")
        self.assertEqual(hub.body, "Apply 50% Off Coupon")
        self.assertIn("₹1,530 (list price ₹9,999, 85% off) · Amazon", hub.text)
        pant = items[1]
        self.assertEqual((pant.price, pant.mrp, pant.body), (None, None, ""))
        self.assertEqual(pant.ts, now - 240)
        self.assertEqual((items[2].price, items[2].mrp), (None, None), "a bare percentage is not a price")

    def test_desidime_layout_change_is_an_error_not_an_empty_list(self):
        with self.assertRaises(SiteError):
            parse_desidime("<html><body><p>Just a moment...</p></body></html>", "https://www.desidime.com/new", 0)

    def test_dealsmagnet_cards(self):
        now = 1_800_000_000.0
        items = parse_dealsmagnet(DEALSMAGNET, "https://www.dealsmagnet.com/new", now)
        self.assertEqual([i.item_id for i in items], ["dm1cmsNcpJ:22900", "dm1dieE1RE:400"])
        # The code names a product record that the robot lists again and again: at a new price it is a new item.
        again = parse_dealsmagnet(DEALSMAGNET.replace("</span>22900</div>", "</span>2290</div>"), "https://www.dealsmagnet.com/new", now)
        self.assertEqual([i.item_id for i in again], ["dm1cmsNcpJ:2290", "dm1dieE1RE:400"])
        dyson, wipes = items
        self.assertEqual(dyson.title, "dyson Supersonic Hair Dryer", "the ribbon over the picture is not the title")
        self.assertEqual((dyson.price, dyson.mrp, dyson.store), (22900.0, 36900.0, "Myntra"))
        self.assertEqual(dyson.body, "Use Code : EXCLUXIVEDEAL · Coupon: BEAUTYEXTRA")
        self.assertEqual(dyson.ts, now - 600)
        self.assertEqual((wipes.price, wipes.mrp, wipes.ts), (400.0, 7500.0, now - 7200))

    def test_tracker_cards(self):
        page = tracker_page(
            tracker_card("dove-body-wash-TZ9", "Dove Go Fresh Body Wash", 1984,
                         '<p class="phd-range"><span>Low ₹995</span> <span>High ₹6,891</span></p>'
                         '<p class="phd-save">₹1,089 below its average</p>'),
            tracker_card("pixel-11-AbC", "Google Pixel 11 (Hibiscus, 512 GB)", 94999,
                         '<p class="phd-range"><span>Low ₹94,999</span> <span>High ₹1,04,999</span></p>'
                         '<p class="phd-save">₹50 below its highest</p>', store="flipkart", product="MOBHPHNVBH6BSZ4M", low="1"),
        )
        dove, pixel = parse_pricehistory(page, "https://pricehistory.app/price-drop", 0)
        self.assertTrue(dove.tracked)
        self.assertEqual((dove.price, dove.usual, dove.low, dove.high, dove.new_low), (1984.0, 3073.0, 995.0, 6891.0, False))
        self.assertEqual(dove.links, ["https://www.amazon.in/dp/B06XZZWTL6", "https://pricehistory.app/p/dove-body-wash-TZ9"])
        self.assertEqual(dove.item_id, "phdove-body-wash-TZ9:1984", "the same product at a new price is a new item")
        self.assertAlmostEqual(dove.ts, 1794471213.0, delta=1)
        self.assertIsNone(pixel.usual, "'below its highest' is not an average")
        self.assertTrue(pixel.new_low)
        self.assertEqual(pixel.links[0], "https://www.flipkart.com/product/p/itme?pid=MOBHPHNVBH6BSZ4M")
        self.assertTrue(dove.in_stock)
        self.assertIsNone(dove.mrp)

    def test_tracker_card_notes(self):
        # As printed on the real page: the discount to MRP, a stock note, and a name escaped twice.
        page = tracker_page(tracker_card(
            "levis-711-AbC", "Levi&amp;#39;s Women&amp;#39;s 711 Skinny Jean", 749,
            '<p class="phd-price"><span class="phd-mrp">MRP −38%</span></p>'
            '<p class="phd-range"><span>Low ₹549</span> <span>High ₹6,091</span></p>'
            '<p class="phd-save">₹2,217 below its average</p><p class="phd-stock">Out of stock right now</p>'))
        (item,) = parse_pricehistory(page, "https://pricehistory.app/price-drop", 0)
        self.assertEqual(item.title, "Levi's Women's 711 Skinny Jean")
        self.assertEqual((item.usual, item.mrp, item.in_stock), (2966.0, 1208, False))

    def test_which_addresses_are_read_as_pages(self):
        self.assertIs(site_reader("https://www.desidime.com/new"), parse_desidime)
        self.assertIs(site_reader("https://www.dealsmagnet.com/new"), parse_dealsmagnet)
        self.assertIs(site_reader("https://pricehistory.app/price-drop"), parse_pricehistory)
        self.assertIsNone(site_reader("https://www.desidime.com/posts.atom"), "a feed of the same site stays a feed")
        self.assertIsNone(site_reader("https://www.bigtricks.in/feed"))
        self.assertIsNone(site_reader("https://notdesidime.com/new"))
        self.assertEqual(site_label("https://pricehistory.app/price-drop"), "PriceHistory tracker")

    def test_desidime_buy_links_are_followed_to_the_store(self):
        # ...and trusted only by where they end, like any short link: a member's deal can point anywhere.
        self.assertEqual(classify_host("visit.desidime.com"), "generic")
        self.assertEqual(classify_host("ddime.in"), "generic")
        self.assertEqual(classify_host("www.desidime.com"), "dealsite")
        self.assertEqual(classify_host("link.amazon"), "affiliate")
        self.assertEqual(classify_host("notlink.amazon.example"), "unknown")


class Judging(unittest.TestCase):
    def setUp(self):
        self.s = Settings()
        folder = tempfile.mkdtemp()
        traps = Path(folder) / "traps.txt"
        traps.write_text("nothing phone | 8000\n", encoding="utf-8")
        self.s.traps = load_traps(traps)

    def test_a_card_is_judged_by_its_own_price_fields(self):
        quiet = evaluate_stated("CableCreation 5-in-1 USB C Hub", "", 1530, 9999, self.s)
        self.assertEqual((quiet.tier, quiet.price, quiet.mrp), (TIER_NORMAL, 1530, 9999))
        high = evaluate_stated("Liora Wet Wipes - Pack of 500", "", 600, 7500, self.s)
        self.assertEqual(high.tier, TIER_HIGH)
        urgent = evaluate_stated("Sony Bravia 55 inch TV", "", 2499, 79990, self.s)
        self.assertEqual(urgent.tier, TIER_URGENT)
        self.assertEqual(urgent.headline, "97% off · ₹2,499 (MRP ₹79,990)")
        self.assertIsNone(evaluate_stated("Mixer Grinder", "", 1403, 3499, self.s), "60% off is an ordinary day")
        self.assertIsNone(evaluate_stated("Gift card", "", 29500, None, self.s), "nothing to compare the price with")

    def test_numbers_in_the_title_do_not_disturb_the_stated_prices(self):
        verdict = evaluate_stated("Trial pack 3 for ₹29,500 - was Rs 99 last week", "", 450, 2999, self.s)
        self.assertEqual((verdict.price, verdict.mrp), (450, 2999))

    def test_card_wording_still_counts(self):
        self.assertIsNone(evaluate_stated("Sony TV 97% off", "Sold out", 2499, 79990, self.s))
        hedged = evaluate_stated("Sony TV", "Sony TV is out of stock on Amazon now", 2499, 79990, self.s)
        self.assertEqual((hedged.tier, hedged.max_tier), (TIER_HIGH, TIER_HIGH))
        self.assertIn("may already be over", hedged.notes[0])
        one_colour = evaluate_stated("Sony TV", "Blue colour is out of stock now, black available", 2499, 79990, self.s)
        self.assertEqual(one_colour.tier, TIER_URGENT)
        error = evaluate_stated("OnePlus buds price error", "", 99, None, self.s)
        self.assertEqual((error.tier, error.label), (TIER_URGENT, "Price error?"))
        self.assertIsNone(evaluate_stated("Hot threads of the week", "", 99, 9999, self.s), "the deny words apply")

    def test_trap_on_a_card_needs_a_list_price_that_fits_the_device(self):
        hit = evaluate_stated("Nothing Phone (3a) Pro 5G", "", 4999, 31999, self.s)
        self.assertEqual((hit.tier, hit.label), (TIER_URGENT, "Trap hit"))
        doubt = evaluate_stated("Nothing Phone (3a) Display Combo", "", 1999, None, self.s)
        self.assertEqual((doubt.tier, doubt.label), (TIER_NORMAL, "Trap match?"))

    def test_a_card_with_a_price_but_no_list_price_is_read_for_what_its_wording_states(self):
        watch = evaluate_stated("Fire-Boltt Rise Smart Watch @ 1099 (92%off) on amazon", "", 1099, None, self.s)
        self.assertEqual((watch.tier, watch.price), (TIER_HIGH, 1099))
        tv = evaluate_stated("Sony Bravia 55 inch 4K TV (MRP ₹79,990)", "", 2499, None, self.s)
        self.assertEqual((tv.tier, tv.price, tv.mrp), (TIER_URGENT, 2499, 79990))
        earbuds = evaluate_stated("Boat earbuds 99% off", "", 49, None, self.s)  # as a post "... 99% off @49" would be
        self.assertEqual((earbuds.tier, earbuds.price), (TIER_URGENT, 49))
        self.assertIsNone(evaluate_stated("[Pre Order] PS5 Grand Theft Auto VI (Standard)", "", 6048, None, self.s))
        self.assertIsNone(evaluate_stated("Trial pack, 3 for 29500, earlier 99", "", 450, None, self.s), "bare numbers are not a list price")

    def test_a_condition_anywhere_on_a_card_holds_it_at_high(self):
        plain = evaluate_stated("Lakme Lipstick Combo", "Free delivery on orders above ₹499", 99, 4490, self.s)
        self.assertEqual(plain.tier, TIER_URGENT, "a delivery threshold is not a condition on the price")
        for note in ("Valid on orders above ₹4,999", "Only for new customers", "On minimum purchase of Rs. 2999"):
            with self.subTest(note=note):
                held = evaluate_stated("Lakme Lipstick Combo", note, 99, 4490, self.s)
                self.assertEqual((held.tier, held.max_tier), (TIER_HIGH, TIER_HIGH))
                self.assertIn("needs a qualifying purchase", held.reason)

    def test_a_card_without_prices_is_read_like_a_post(self):
        verdict = evaluate_stated("Boat earbuds 99% off", "", None, None, self.s)
        self.assertEqual(verdict.tier, TIER_HIGH)
        self.assertIsNone(evaluate_stated("Men Tshirts From 99", "", None, None, self.s))
        labelled = evaluate_stated("Sony Bravia 55 inch TV - Deal Price: ₹2,499 (MRP ₹79,990)", "", None, None, self.s)
        self.assertEqual(labelled.tier, TIER_URGENT)

    def test_an_offer_card_is_not_mistaken_for_a_priced_product(self):
        # Seen on DesiDime on 12 Nov 2026, with its price fields empty: two bare amounts, neither a price.
        title = "Bajaj - Flat Rs 25 on sending Rs 5000 two times ( Single scratch card) - Got Rs 25"
        self.assertIsNone(evaluate_stated(title, "", None, None, self.s))

    def test_tracker_drop_is_measured_against_the_usual_price(self):
        self.assertIsNone(evaluate_tracked("PUMA T-Shirt", 809, 1606, self.s, low=600), "50% below average is routine")
        self.assertIsNone(evaluate_tracked("Lip balm", 272, 702, self.s, low=272), "61% off, but only ₹430 saved")
        quiet = evaluate_tracked("Adidas Predator", 3400, 8765, self.s, low=3400, new_low=True)
        self.assertEqual((quiet.tier, quiet.label, quiet.was), (TIER_NORMAL, "Price drop", 8765))
        self.assertEqual(quiet.headline, "Price drop: 61% off · ₹3,400 (was ₹8,765)")
        high = evaluate_tracked("Samsung 55 inch TV", 12000, 45000, self.s, low=12000)
        self.assertEqual(high.tier, TIER_HIGH)
        urgent = evaluate_tracked("Lenovo IdeaPad Slim 3", 5990, 52000, self.s, low=None)
        self.assertEqual(urgent.tier, TIER_URGENT)
        self.assertIn("88% below the ₹52,000 it usually costs", urgent.reason)

    def test_a_price_it_has_been_under_before_is_one_step_quieter(self):
        seen_lower = evaluate_tracked("Dove Body Wash", 749, 2966, self.s, low=549)
        self.assertEqual(seen_lower.tier, TIER_NORMAL)
        self.assertIn("it has been as low as ₹549", seen_lower.reason)
        self.assertIsNone(evaluate_tracked("Adidas Predator", 3400, 8765, self.s, low=2999))
        self.assertIsNone(evaluate_tracked("No average known", 500, None, self.s))

    def test_tracker_item_out_of_stock_is_not_announced(self):
        self.assertIsNone(evaluate_tracked("Lenovo IdeaPad Slim 3", 5990, 52000, self.s, in_stock=False))

    def test_tracker_average_above_the_list_price_is_not_believed(self):
        # A reseller's fantasy price pulled the average to ₹2,966; the list price is about ₹1,208.
        self.assertIsNone(evaluate_tracked("Dove Gofresh Body Wash", 749, 2966, self.s, low=549, mrp=1208),
                          "38% under its list price is no event")
        tv = evaluate_tracked("Samsung 55 inch TV", 9000, 80000, self.s, mrp=60000)
        self.assertEqual((tv.tier, tv.was), (TIER_URGENT, 60000))
        self.assertIn("85% below its list price of about ₹60,000", tv.reason)

    def test_tracker_trap(self):
        hit = evaluate_tracked("Nothing Phone (3a) Pro 5G (Black, 256 GB)", 4999, 29500, self.s)
        self.assertEqual((hit.tier, hit.label), (TIER_URGENT, "Trap hit"))
        doubt = evaluate_tracked("Nothing Phone (3a) Display Combo", 1999, 2400, self.s)
        self.assertEqual((doubt.tier, doubt.label), (TIER_NORMAL, "Trap match?"))


class SitePagesInTheMonitor(unittest.TestCase):
    def setUp(self):
        self.web = FakeWeb().start()
        self.folder = tempfile.mkdtemp()
        self.settings = Settings(
            ntfy_server=self.web.base, ntfy_topic="test-topic", telegram_base_url=self.web.base + "/s",
            interval=15, heartbeat_hour=-1, startup_note=False, db_path=os.path.join(self.folder, "state.db"),
        )
        self.url = self.web.base + "/new"
        self.settings.feeds = [self.url]
        self.monitors = []

    def tearDown(self):
        for monitor in self.monitors:
            monitor.db.close()
        self.web.stop()

    def monitor(self) -> Monitor:
        notifier = Notifier(self.settings.ntfy_server, self.settings.ntfy_topic, "", "")
        monitor = Monitor(self.settings, DB(self.settings.db_path), Fetcher("off"), notifier, LinkResolver(allow_private=True))
        self.monitors.append(monitor)
        return monitor

    def deals(self):
        return [p for p in self.web.pushes if "monitor" not in p["title"].lower()]

    def test_a_new_card_is_pushed_once_and_resolved_to_its_store(self):
        buy = self.web.base + "/visit/1"
        self.web.redirects["/visit/1"] = "https://www.amazon.in/dp/B09G28SW8W?tag=desidime01-21"
        self.web.pages["/new"] = desidime_page(desidime_card(7, "Old deal", ago="3 hours ago", price="₹ 99", mrp="₹ 9,999"))
        with mock.patch("lootwatch.monitor.site_reader", return_value=parse_desidime), \
                mock.patch("lootwatch.monitor.site_label", return_value="DesiDime"):
            monitor = self.monitor()
            monitor.cycle()
            self.assertEqual(self.deals(), [], "what was already on the page hours ago is not news")
            self.web.pages["/new"] = desidime_page(
                desidime_card(8, "Sony Bravia 55 inch 4K TV", price="₹ 2,499", mrp="₹ 79,990", buy=buy),
                desidime_card(7, "Old deal", ago="3 hours ago", price="₹ 99", mrp="₹ 9,999"))
            monitor.cycle()
            monitor.cycle()
        pushes = self.deals()
        self.assertEqual(len(pushes), 1)
        push = pushes[0]
        self.assertEqual(push["priority"], 5)
        self.assertEqual(push["title"], "97% off · ₹2,499 (MRP ₹79,990) — Sony Bravia 55 inch 4K TV")
        self.assertEqual(push["click"], "https://www.amazon.in/dp/B09G28SW8W?tag=desidime01-21")
        self.assertIn("DesiDime", push["message"])
        self.assertIn("posted", push["message"])
        self.assertEqual([(a["label"], a["url"]) for a in push["actions"]],
                         [("Open deal", "https://www.amazon.in/dp/B09G28SW8W?tag=desidime01-21"),
                          ("Open post", "https://www.desidime.com/deals/some-deal-8")])

    def test_the_same_deal_from_a_channel_later_is_not_pushed_again(self):
        self.settings.channels = ["alpha"]
        self.web.channels["alpha"] = [{"id": 0, "html": "Welcome", "ts": time.time() - 86400, "buttons": []}]
        self.web.redirects["/visit/1"] = "https://www.amazon.in/dp/B09G28SW8W?tag=desidime01-21"
        self.web.redirects["/r/x"] = "https://www.amazon.in/dp/B09G28SW8W?tag=other-21"
        self.web.pages["/new"] = desidime_page(desidime_card(7, "Old deal", ago="3 hours ago"))
        with mock.patch("lootwatch.monitor.site_reader", side_effect=lambda u: parse_desidime if u.endswith("/new") else None), \
                mock.patch("lootwatch.monitor.site_label", return_value="DesiDime"):
            monitor = self.monitor()
            monitor.cycle()
            self.web.pages["/new"] = desidime_page(
                desidime_card(8, "Sony Bravia 55 inch 4K TV", price="₹ 2,499", mrp="₹ 79,990", buy=self.web.base + "/visit/1"))
            monitor.cycle()
            self.web.post("alpha", 5, f'Sony Bravia TV loot ₹2,499 (MRP ₹79,990) <a href="{self.web.base}/r/x">buy</a>',
                          time.time() - 5)
            monitor.cycle()
        self.assertEqual(len(self.deals()), 1)

    def run_card(self, card_html):
        self.web.pages["/new"] = desidime_page(desidime_card(7, "Old deal", ago="3 hours ago"))
        with mock.patch("lootwatch.monitor.site_reader", return_value=parse_desidime), \
                mock.patch("lootwatch.monitor.site_label", return_value="DesiDime"):
            monitor = self.monitor()
            monitor.cycle()
            self.web.pages["/new"] = desidime_page(card_html)
            monitor.cycle()
            monitor.cycle()
        return self.deals()

    def test_a_buy_link_that_ends_on_an_unknown_shop_is_quiet_and_says_so(self):
        self.web.pages["/shop"] = "<html><body>Some shop nobody knows</body></html>"
        self.web.redirects["/visit/9"] = self.web.base + "/shop"
        (push,) = self.run_card(desidime_card(9, "Nude Matte Liquid Lipstick", price="₹ 149", mrp="₹ 8,999",
                                              store="Hamara Mall", buy=self.web.base + "/visit/9"))
        self.assertEqual(push["priority"], 3)
        self.assertIn("not a known store", push["message"])

    def test_a_buy_link_that_cannot_be_followed_is_held_at_high(self):
        self.web.status_for["/visit/10"] = 500
        (push,) = self.run_card(desidime_card(10, "Sony Bravia 55 inch 4K TV", price="₹ 2,499", mrp="₹ 79,990",
                                              buy=self.web.base + "/visit/10"))
        self.assertEqual(push["priority"], 4)
        self.assertIn("could not check where the link goes", push["message"])

    def test_a_card_listed_again_at_a_new_price_is_judged_again(self):
        self.web.redirects["/visit/11"] = "https://www.amazon.in/dp/B0RELISTED1"
        buy = self.web.base + "/visit/11"
        first = self.run_card(desidime_card(11, "Dyson Supersonic Hair Dryer", price="₹ 22,900", mrp="₹ 36,900", buy=buy))
        self.assertEqual(first, [], "38% off is an ordinary day")
        with mock.patch("lootwatch.monitor.site_reader", return_value=parse_desidime), \
                mock.patch("lootwatch.monitor.site_label", return_value="DesiDime"):
            self.web.pages["/new"] = desidime_page(
                desidime_card(11, "Dyson Supersonic Hair Dryer", price="₹ 2,290", mrp="₹ 36,900", buy=buy))
            self.monitors[-1].cycle()
        (push,) = self.deals()
        self.assertTrue(push["title"].startswith("94% off · ₹2,290 (MRP ₹36,900)"), push["title"])

    def test_a_deal_with_no_followable_link_is_matched_by_name_and_price(self):
        """DealsMagnet's cards link only to DealsMagnet. The same deal from a channel must not ring again."""
        self.settings.channels = ["alpha"]
        self.web.channels["alpha"] = [{"id": 0, "html": "Welcome", "ts": time.time() - 86400, "buttons": []}]
        self.web.pages["/new"] = desidime_page(desidime_card(7, "Old deal", ago="3 hours ago"))
        site = self.web.base + "/elsewhere"  # a link that leads to no store
        self.web.pages["/elsewhere"] = "<html><body>a deal page</body></html>"
        with mock.patch("lootwatch.monitor.site_reader", side_effect=lambda u: parse_desidime if u.endswith("/new") else None), \
                mock.patch("lootwatch.monitor.site_label", return_value="DealsMagnet"):
            monitor = self.monitor()
            monitor.cycle()
            self.web.pages["/new"] = desidime_page(
                desidime_card(8, "Liora Face Cleansing Wet Wipes - Pack of 500", price="₹ 400", mrp="₹ 7,500", buy=site))
            monitor.cycle()
            first = len(self.deals())
            # Twenty minutes later a channel has it, a rupee apart, through a link of its own.
            self.web.post("alpha", 5, f'Liora Face Cleansing Wet Wipes - Pack of 500 | Gentle and Refreshing<br/>'
                                      f'Deal Price: ₹399 (MRP ₹7,500) <a href="{site}?x=1">buy</a>', time.time() - 5)
            monitor.cycle()
            # A different product at the same price is its own deal.
            self.web.post("alpha", 6, f'Himalaya Baby Wipes - Pack of 12 combo<br/>Deal Price: ₹400 (MRP ₹7,500) '
                                      f'<a href="{site}?x=2">buy</a>', time.time() - 5)
            monitor.cycle()
        self.assertEqual(first, 1)
        titles = [p["title"] for p in self.deals()]
        self.assertEqual(len(titles), 2, titles)
        self.assertIn("Himalaya Baby Wipes", titles[1])

    def test_tracker_items_are_judged_by_their_drop(self):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 60))
        old = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 7200))
        average = '<p class="phd-range"><span>Low ₹5,990</span> <span>High ₹58,000</span></p>' \
                  '<p class="phd-save">₹46,010 below its average</p>'
        small = '<p class="phd-range"><span>Low ₹800</span> <span>High ₹1,500</span></p><p class="phd-save">₹200 below its average</p>'
        self.web.pages["/new"] = tracker_page(tracker_card("old-one", "Old one", 5990, average, stamp=old))
        with mock.patch("lootwatch.monitor.site_reader", return_value=parse_pricehistory), \
                mock.patch("lootwatch.monitor.site_label", return_value="PriceHistory tracker"):
            monitor = self.monitor()
            monitor.cycle()
            self.web.pages["/new"] = tracker_page(
                tracker_card("ideapad-x", "Lenovo IdeaPad Slim 3", 5990, average, stamp=stamp, product="B0IDEAPAD1", low="1"),
                tracker_card("tee-y", "PUMA T-Shirt", 800, small, stamp=stamp, product="B0PUMATEE1"),
                tracker_card("old-one", "Old one", 5990, average, stamp=old))
            monitor.cycle()
            monitor.cycle()
        pushes = self.deals()
        self.assertEqual(len(pushes), 1)
        self.assertEqual(pushes[0]["priority"], 5)
        self.assertEqual(pushes[0]["title"], "Price drop: 88% off · ₹5,990 (was ₹52,000) — Lenovo IdeaPad Slim 3")
        self.assertEqual(pushes[0]["click"], "https://www.amazon.in/dp/B0IDEAPAD1")
        self.assertIn("PriceHistory tracker", pushes[0]["message"])

    def test_a_page_that_stops_showing_cards_is_reported_as_failing(self):
        self.web.pages["/new"] = "<html><body>Just a moment...</body></html>"
        with mock.patch("lootwatch.monitor.site_reader", return_value=parse_desidime):
            monitor = self.monitor()
            monitor.cycle()
        rows = {row[0]: row for row in monitor.db.health_rows()}
        (row,) = rows.values()
        self.assertEqual(row[3], 1, "one failure in a row")
        self.assertIn("no deal cards", row[6])


if __name__ == "__main__":
    unittest.main()
