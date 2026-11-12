"""Parsers for Telegram previews, feeds and store pages, run on hand-built pages.

These fixtures reproduce the markup the parsers rely on (ids, data attributes,
JSON-LD). They prove the logic; `--doctor` is what confirms the live sites still
serve that markup.
"""

import json
import time
import unittest

from lootwatch.sources.feeds import FeedError, parse_feed
from lootwatch.sources.stores import extract_page, parse_amount, request_headers
from lootwatch.sources.telegram import parse_channel_page
from lootwatch.textparse import parse_text
from tests.helpers import _LANDING, channel_html


class TelegramPages(unittest.TestCase):
    def test_posts_text_links_and_time(self):
        now = int(time.time())
        html = channel_html("LootChan", [
            {"id": 101, "ts": now - 60, "html":
                '<b>Price Error</b> <i class="emoji" style="background-image:url(x)"><b>😱</b></i><br/>'
                'Nothing Phone (3a) Pro <s>₹60,000</s> ₹400 only<br/><br/>'
                '<a href="https://fkrt.cc/abc" target="_blank" rel="noopener">https://fkrt.cc/abc</a>'},
            {"id": 102, "ts": now - 5, "html": 'boAt 141 @ ₹899<br/>MRP ₹4,490', "buttons": ["https://amzn.to/xyz"]},
        ])
        page = parse_channel_page(html, "LootChan", "https://t.me/s/LootChan")
        self.assertEqual(page.status, "ok")
        self.assertEqual(page.title, "LootChan Deals")
        self.assertEqual([p.post_id for p in page.posts], [101, 102])
        first, second = page.posts
        self.assertEqual(first.text.splitlines()[0], "Price Error 😱")
        self.assertIn("₹60,000 ₹400 only", first.text)
        self.assertEqual(first.links, ["https://fkrt.cc/abc"])
        self.assertEqual(first.url, "https://t.me/LootChan/101")
        self.assertAlmostEqual(first.ts, now - 60, delta=1)
        # The crossed-out amount reaches the text parser as the MRP.
        parsed = parse_text(first.analysis_text)
        self.assertEqual((parsed.price, parsed.mrp), (400, 60000))
        # Line breaks survive, and inline "Buy Now" buttons count as links.
        self.assertEqual(second.text, "boAt 141 @ ₹899\nMRP ₹4,490")
        self.assertEqual(second.links, ["https://amzn.to/xyz"])

    def test_channel_without_public_preview(self):
        page = parse_channel_page(_LANDING.format(name="ghost"), "ghost", "https://t.me/ghost")
        self.assertEqual(page.status, "no_preview")
        page = parse_channel_page(_LANDING.format(name="ghost"), "ghost")  # redirect not visible
        self.assertEqual(page.status, "no_preview")

    def test_empty_and_unrecognised_pages(self):
        self.assertEqual(parse_channel_page(channel_html("quiet", []), "quiet").status, "empty")
        self.assertEqual(parse_channel_page("<html><body>maintenance</body></html>", "x").status, "unrecognised")

    def test_instant_view_links_are_unwrapped(self):
        html = channel_html("c", [{"id": 1, "ts": time.time(), "html":
            'Deal <a href="https://t.me/iv?url=https%3A%2F%2Fwww.amazon.in%2Fdp%2FB0ABCDEFGH&rhash=1">link</a> '
            '<a href="?q=%23loot">#loot</a>'}])
        self.assertEqual(parse_channel_page(html, "c").posts[0].links, ["https://www.amazon.in/dp/B0ABCDEFGH"])


RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><title>Deals</title>
<item><title>boAt Airdopes 141 @ ₹899 (MRP ₹4,490)</title><link>https://example.in/deal/1</link>
<guid>deal-1</guid><pubDate>Tue, 10 Nov 2026 09:30:00 +0530</pubDate>
<description><![CDATA[<p>Grab it <a href="https://amzn.to/abc">here</a><br>Limited stock</p>]]></description></item>
<item><title>Second</title><link>https://example.in/deal/2</link></item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>r/deals</title>
<entry><id>t3_abc</id><title>[Amazon] Sony XM5 for ₹400 (MRP ₹34,990) price error</title>
<link href="https://www.reddit.com/r/x/comments/abc/"/><updated>2026-11-10T04:00:00+00:00</updated>
<content type="html">&lt;a href="https://www.amazon.in/dp/B0ABCDEFGH"&gt;link&lt;/a&gt;</content></entry>
</feed>"""


class Feeds(unittest.TestCase):
    def test_rss(self):
        items = parse_feed(RSS)
        self.assertEqual(len(items), 2)
        first = items[0]
        self.assertEqual(first.title, "boAt Airdopes 141 @ ₹899 (MRP ₹4,490)")
        self.assertEqual(first.url, "https://example.in/deal/1")
        self.assertEqual(first.links, ["https://amzn.to/abc"])
        self.assertIn("Limited stock", first.text)
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M", time.gmtime(first.ts)), "2026-11-10 04:00")
        self.assertIsNone(items[1].ts)
        self.assertNotEqual(items[0].item_id, items[1].item_id)

    def test_atom(self):
        item = parse_feed(ATOM)[0]
        self.assertIn("Sony XM5", item.title)
        self.assertEqual(item.url, "https://www.reddit.com/r/x/comments/abc/")
        self.assertEqual(item.links, ["https://www.amazon.in/dp/B0ABCDEFGH"])
        self.assertEqual(time.strftime("%H:%M", time.gmtime(item.ts)), "04:00")

    def test_rejects_non_feeds_and_entity_tricks(self):
        with self.assertRaises(FeedError):
            parse_feed("<html><body>Just a moment...</body></html>")
        with self.assertRaises(FeedError):
            parse_feed('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><rss><channel/></rss>')
        with self.assertRaises(FeedError):
            parse_feed("not xml at all")


AMAZON_PRODUCT = """<html><head><title>Nothing Phone (3a) : Amazon.in</title></head><body>
<span id="productTitle" class="a-size-large">  Nothing Phone (3a) 5G (Black, 8GB RAM, 128GB Storage)  </span>
<div id="corePriceDisplay_desktop_feature_div">
 <span class="a-price aok-align-center reinventPricePriceToPayMargin priceToPay">
  <span class="a-offscreen"> </span><span aria-hidden="true"><span class="a-price-symbol">₹</span>
  <span class="a-price-whole">24,999<span class="a-price-decimal">.</span></span></span></span>
 <span class="a-size-small aok-offscreen">M.R.P.: ₹27,999</span>
 <span class="a-price a-text-price basisPrice" data-a-strike="true"><span class="a-offscreen">₹27,999</span></span>
</div><div id="availability"><span>In stock</span></div>
<div id="similar"><span class="a-price"><span class="a-offscreen">₹199</span></span></div>
</body></html>"""

AMAZON_UNAVAILABLE = """<html><body><span id="productTitle">Old Phone</span>
<div id="availability"><span class="a-color-price">Currently unavailable.</span></div></body></html>"""

AMAZON_SEARCH = """<html><body><div class="s-main-slot">
<div data-component-type="s-search-result" data-asin="B0AAAAAAA1" class="s-result-item">
 <h2 aria-label="Sponsored Ad – Nothing Phone (3a) 5G (Black, 128GB)"><a href="/sspa/click?x=1"><span>Nothing Phone (3a) 5G</span></a></h2>
 <span class="a-price" data-a-color="base"><span class="a-offscreen">₹24,999</span></span>
 <span class="a-price a-text-price" data-a-strike="true"><span class="a-offscreen">₹27,999</span></span></div>
<div data-component-type="s-search-result" data-asin="B0AAAAAAA2" class="s-result-item">
 <h2><a href="/dp/B0AAAAAAA2"><span>Back Cover for Nothing Phone 3a</span></a></h2>
 <span class="a-price"><span class="a-offscreen">₹149</span></span>
 <span class="a-price a-text-price"><span class="a-offscreen">₹999</span></span></div>
<div data-component-type="s-search-result" data-asin="" class="s-result-item"><h2>widget</h2></div>
<div data-component-type="s-search-result" data-asin="B0AAAAAAA3"><h2><span>No price shown</span></h2></div>
</div></body></html>"""

FLIPKART_SEARCH = """<html><body><div class="DOjaWF">
<div data-id="MOBH4DQFG8NKFRDY"><div class="xQ7z"><a class="k7wcnx" target="_blank"
  href="/nothing-phone-3a/p/itm0123abcd?pid=MOBH4DQFG8NKFRDY&amp;lid=LSTX"><img alt="Nothing Phone (3a) (Black, 128 GB)" src="x.jpg"/>
  <div class="zZ1a">Nothing Phone (3a) (Black, 128 GB)</div><div class="qq9"><div class="Aa1">₹24,999</div>
  <div class="Bb2">₹27,999</div><div class="Cc3"><span>10% off</span></div></div></a></div></div>
<div data-id="ACCGZZZZZZZZZZZZ"><a title="Flip Cover for Nothing Phone 3a" href="/cover/p/itmcover?pid=ACCGZZZZZZZZZZZZ">Flip Cover</a>
  <div>₹199</div></div>
<div data-id="not-a-product"><div>₹5</div></div>
</div></body></html>"""

JSON_LD_PRODUCT = """<html><head><title>x</title>
<script id="jsonLD" type="application/ld+json">[{"@context":"http://schema.org","@type":"Product",
"name":"Nothing Phone (3a) (Black, 128 GB)","offers":{"@type":"Offer","priceCurrency":"INR","price":24999}},
{"@type":"BreadcrumbList","itemListElement":[]}]</script></head>
<body><div class="p1">₹24,999</div><div class="p2">₹27,999</div><div>10% off</div></body></html>"""


class StorePages(unittest.TestCase):
    def test_parse_amount(self):
        self.assertEqual(parse_amount("₹1,49,999.00"), 149999.0)
        self.assertEqual(parse_amount("M.R.P.: ₹27,999"), 27999.0)
        self.assertEqual(parse_amount(16999), 16999.0)
        self.assertIsNone(parse_amount("Currently unavailable"))
        self.assertIsNone(parse_amount(None))
        self.assertIsNone(parse_amount(0))

    def test_amazon_product_page(self):
        result = extract_page("https://www.amazon.in/Nothing-Phone/dp/B0ABCDEFGH?th=1", AMAZON_PRODUCT)
        self.assertEqual(result.kind, "product")
        offer = result.offers[0]
        self.assertEqual(offer.key, "amazon:B0ABCDEFGH")
        self.assertEqual(offer.title, "Nothing Phone (3a) 5G (Black, 8GB RAM, 128GB Storage)")
        self.assertEqual((offer.price, offer.mrp), (24999.0, 27999.0))
        self.assertEqual(offer.url, "https://www.amazon.in/dp/B0ABCDEFGH")

    def test_amazon_unavailable_and_unrecognised(self):
        result = extract_page("https://www.amazon.in/dp/B0ABCDEFGH", AMAZON_UNAVAILABLE)
        self.assertEqual((result.kind, result.offers), ("product", []))
        result = extract_page("https://www.amazon.in/dp/B0ABCDEFGH", "<html><body>hello</body></html>")
        self.assertEqual(result.kind, "none")

    def test_amazon_search_page(self):
        result = extract_page("https://www.amazon.in/s?k=nothing+phone", AMAZON_SEARCH)
        self.assertEqual(result.kind, "listing")
        self.assertEqual([o.key for o in result.offers], ["amazon:B0AAAAAAA1", "amazon:B0AAAAAAA2"])
        first = result.offers[0]
        self.assertEqual(first.title, "Nothing Phone (3a) 5G (Black, 128GB)")
        self.assertEqual((first.price, first.mrp), (24999.0, 27999.0))
        self.assertEqual(first.url, "https://www.amazon.in/dp/B0AAAAAAA1")

    def test_amazon_best_seller_grid(self):
        # Best-seller grids carry the ASIN in the element id and style prices with generated class names.
        html = """<html><body><div id="zg-right-col"><div id="gridItemRoot" class="a-column">
          <div id="B0AAAAAAA1" class="p13n-sc-uncoverable-faceout"><a href="/x/dp/B0AAAAAAA1"><img alt="Apple iPhone 16 (128 GB) - Black" src="i.jpg"/></a>
            <span class="_cDEzb_p13n-sc-price_3mJ9Z">₹76,999.00</span></div></div>
          <div id="gridItemRoot" class="a-column"><div id="B0AAAAAAA2" class="p13n-sc-uncoverable-faceout">
            <img alt="Samsung Galaxy M35 5G" src="j.jpg"/><span class="_cDEzb_p13n-sc-price_3mJ9Z">₹14,999.00</span></div></div>
          <div id="B0NOPRICE1"><img alt="Currently unavailable item" src="k.jpg"/></div>
        </div></body></html>"""
        result = extract_page("https://www.amazon.in/gp/bestsellers/electronics/1805560031", html)
        self.assertEqual(result.kind, "listing")
        self.assertEqual([(o.key, o.title, o.price) for o in result.offers],
                         [("amazon:B0AAAAAAA1", "Apple iPhone 16 (128 GB) - Black", 76999.0),
                          ("amazon:B0AAAAAAA2", "Samsung Galaxy M35 5G", 14999.0)])

    def test_flipkart_search_page_without_relying_on_class_names(self):
        result = extract_page("https://www.flipkart.com/search?q=nothing+phone", FLIPKART_SEARCH)
        self.assertEqual(result.kind, "listing")
        self.assertEqual([o.key for o in result.offers], ["flipkart:MOBH4DQFG8NKFRDY", "flipkart:ACCGZZZZZZZZZZZZ"])
        phone, cover = result.offers
        self.assertEqual(phone.title, "Nothing Phone (3a) (Black, 128 GB)")
        self.assertEqual((phone.price, phone.mrp), (24999.0, 27999.0))
        self.assertTrue(phone.url.startswith("https://www.flipkart.com/nothing-phone-3a/p/itm0123abcd?pid=MOBH4DQFG8NKFRDY"))
        self.assertEqual((cover.title, cover.price, cover.mrp), ("Flip Cover for Nothing Phone 3a", 199.0, None))

    def test_flipkart_mrp_split_across_text_nodes(self):
        # On the live site the crossed-out MRP is "₹", an HTML comment, then the digits: three nodes.
        html = """<html><body>
        <div data-id="MOBAAAAAAAAAAAA1"><a href="/lava-bold/p/itm1?pid=MOBAAAAAAAAAAAA1"><img alt="LAVA Bold N4 Lite (Gold, 32 GB)"/>
          <div class="x1">₹600</div><div class="x2">₹<!-- -->8,299</div><div class="x3"><span>92% off</span></div>
          <div>Upto ₹5,750 Off on Exchange</div></a></div>
        <div data-id="MOBAAAAAAAAAAAA2"><a href="/itel/p/itm2?pid=MOBAAAAAAAAAAAA2"><img alt="itel A100C (Black, 64 GB)"/>
          <div class="x1">₹9,999</div><div>Upto ₹12,000 Off on Exchange</div><div>No Cost EMI from ₹1,667/month</div></a></div>
        <div data-id="MOBAAAAAAAAAAAA3"><a href="/ai/p/itm3?pid=MOBAAAAAAAAAAAA3"><img alt="Ai+ Pulse 2 (Green, 64 GB)"/>
          <div class="x1">₹8,999</div><div><span>25% off</span></div></a></div>
        </body></html>"""
        offers = {o.key[-1]: o for o in extract_page("https://www.flipkart.com/mobiles/pr?sid=tyy,4io", html).offers}
        self.assertEqual((offers["1"].price, offers["1"].mrp), (600.0, 8299.0))
        self.assertEqual((offers["2"].price, offers["2"].mrp), (9999.0, None))   # exchange / EMI amounts are not an MRP
        self.assertEqual((offers["3"].price, offers["3"].mrp), (8999.0, 11999.0))  # worked back from "25% off"

    def test_flipkart_deepest_errors_keep_their_real_list_price(self):
        # A ₹74,990 laptop at ₹149 is 99.8% off and shown as "99% off". The crossed-out price must
        # survive: working it back from "99%" would give ₹14,900 and hide the error from the floor rule.
        html = """<html><body>
        <div data-id="COMAAAAAAAAAAAA1"><a href="/hp/p/itm1?pid=COMAAAAAAAAAAAA1"><img alt="HP Pavilion 15 Core i5"/>
          <div>₹149</div><div>₹<!-- -->74,990</div><div><span>99% off</span></div></a></div>
        <div data-id="MOBAAAAAAAAAAAA2"><a href="/s/p/itm2?pid=MOBAAAAAAAAAAAA2"><img alt="Samsung Galaxy A56 5G"/>
          <div>₹1</div><div>₹<!-- -->45,999</div><div><span>99% off</span></div></a></div>
        <div data-id="MOBAAAAAAAAAAAA3"><a href="/l/p/itm3?pid=MOBAAAAAAAAAAAA3"><img alt="LAVA Bold N4 Lite"/>
          <div>₹8,299</div><div>₹<!-- -->9,999</div><div><span>17% off</span></div></a></div>
        <div data-id="MOBAAAAAAAAAAAA4"><a href="/x/p/itm4?pid=MOBAAAAAAAAAAAA4"><img alt="Pixel 9 (Obsidian, 256 GB)"/>
          <div>₹400</div><div>₹<!-- -->79,999</div><div><span>100% off</span></div></a></div>
        <div data-id="MOBAAAAAAAAAAAA5"><a href="/y/p/itm5?pid=MOBAAAAAAAAAAAA5"><img alt="No-name phone"/>
          <div>₹2,399</div><div>₹<!-- -->4,000</div><div><span>55% off</span></div></a></div>
        </body></html>"""
        offers = {o.key[-1]: o for o in extract_page("https://www.flipkart.com/mobiles/pr?sid=tyy,4io", html).offers}
        self.assertEqual((offers["1"].price, offers["1"].mrp), (149.0, 74990.0))
        self.assertEqual((offers["2"].price, offers["2"].mrp), (1.0, 45999.0))
        self.assertEqual((offers["3"].price, offers["3"].mrp), (8299.0, 9999.0))
        self.assertEqual((offers["4"].price, offers["4"].mrp), (400.0, 79999.0))
        # Below 98% the stated figure still wins over an amount that does not fit it.
        self.assertEqual((offers["5"].price, offers["5"].mrp), (2399.0, 5331.0))

    def test_flipkart_card_without_a_price_is_skipped(self):
        # "Currently unavailable" / "Coming soon": the exchange or EMI line is not the price.
        html = """<html><body>
        <div data-id="COMAAAAAAAAAAAA1"><a href="/a/p/itm1?pid=COMAAAAAAAAAAAA1"><img alt="Acer Aspire 14"/>
          <div>Currently unavailable</div><div>Upto ₹15,250 Off on Exchange</div></a></div>
        <div data-id="MOBAAAAAAAAAAAA2"><a href="/b/p/itm2?pid=MOBAAAAAAAAAAAA2"><img alt="Apple iPhone 17 (Black, 256 GB)"/>
          <div>Coming Soon</div><div>No Cost EMI from ₹7,150/month</div><div>Extra ₹3,000 off on exchange</div></a></div>
        <div data-id="MOBAAAAAAAAAAAA3"><a href="/c/p/itm3?pid=MOBAAAAAAAAAAAA3"><img alt="LAVA Bold N4 Lite"/>
          <div>₹8,299</div><div>Upto ₹7,750 Off on Exchange</div></a></div>
        </body></html>"""
        result = extract_page("https://www.flipkart.com/laptops/pr?sid=6bo,b5g", html)
        self.assertEqual([(o.title, o.price, o.mrp) for o in result.offers], [("LAVA Bold N4 Lite", 8299.0, None)])

    def test_flipkart_product_page_via_json_ld(self):
        url = "https://www.flipkart.com/nothing-phone-3a/p/itm0123abcd?pid=MOBH4DQFG8NKFRDY"
        result = extract_page(url, JSON_LD_PRODUCT)
        self.assertEqual(result.kind, "product")
        offer = result.offers[0]
        self.assertEqual(offer.key, "flipkart:MOBH4DQFG8NKFRDY")
        self.assertEqual((offer.price, offer.mrp), (24999.0, 27999.0))

    def test_generic_store_with_meta_tags(self):
        html = ('<html><head><meta property="og:title" content="Gulab Jamun 1 kg"/>'
                '<meta property="product:price:amount" content="249.00"/></head><body></body></html>')
        result = extract_page("https://shop.example.in/p/gulab-jamun", html)
        self.assertEqual(result.kind, "product")
        self.assertEqual((result.offers[0].title, result.offers[0].price), ("Gulab Jamun 1 kg", 249.0))

    def test_embedded_app_json_is_mined_for_products(self):
        data = {"props": {"pageProps": {"results": [
            {"productId": "p1", "name": "Haldiram's Gulab Jamun 1 kg", "sellingPrice": 199, "mrp": 250, "slug": "/pn/gj/p1"},
            {"productId": "p2", "name": "Rasgulla 1 kg", "price": {"mrp": 240, "discounted": 180}},
            {"id": "x", "name": "Delivery slot", "price": 25},
            {"name": "Banner", "title": "Sale", "items": []},
        ]}}}
        html = f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></body></html>'
        result = extract_page("https://quick.example.in/search?q=sweets", html)
        self.assertEqual(result.kind, "listing")
        by_key = {o.key: o for o in result.offers}
        self.assertEqual(set(by_key), {"quick.example.in:p1", "quick.example.in:p2"})
        self.assertEqual((by_key["quick.example.in:p1"].price, by_key["quick.example.in:p1"].mrp), (199.0, 250.0))
        self.assertEqual(by_key["quick.example.in:p1"].url, "https://quick.example.in/pn/gj/p1")
        self.assertEqual((by_key["quick.example.in:p2"].price, by_key["quick.example.in:p2"].mrp), (180.0, 240.0))

    def test_window_assignment_json(self):
        data = {"searchData": {"results": {"products": [
            {"productId": 111, "productName": "Puma Sneakers", "price": 1499, "mrp": 5999, "landingPageUrl": "sneakers/puma/111/buy"},
        ]}}}
        html = f"<html><body><script>window.__myx = {json.dumps(data)};</script></body></html>"
        result = extract_page("https://www.myntra.com/puma-sneakers", html)
        self.assertEqual([(o.key, o.price, o.mrp) for o in result.offers], [("myntra.com:111", 1499.0, 5999.0)])
        self.assertEqual(result.offers[0].url, "https://www.myntra.com/sneakers/puma/111/buy")

    def test_page_with_nothing_useful(self):
        result = extract_page("https://quick.example.in/search?q=x", "<html><body><div id='root'></div></body></html>")
        self.assertEqual(result.kind, "none")
        self.assertIn("JavaScript", result.note)

    def test_a_product_path_in_embedded_json_starts_at_the_site_root(self):
        data = {"products": [{"id": 15684508, "name": "Comfy Matte Lipstick", "price": 269, "mrp": 299,
                              "slug": "faces-canada-comfy-matte/p/15684508"}]}
        html = f"<html><body><script>window.__PRELOADED_STATE__ = {json.dumps(data)};</script></body></html>"
        result = extract_page("https://www.nykaa.com/makeup/lips/c/15", html)
        self.assertEqual(result.offers[0].url, "https://www.nykaa.com/faces-canada-comfy-matte/p/15684508")


class StoreLists(unittest.TestCase):
    """Stores whose own website fetches a plain JSON list of products (shapes as on 12 Nov 2026)."""

    def test_reliance_digital(self):
        data = {"items": [
            {"type": "product", "name": "Lenovo IdeaPad Slim3 Laptop", "slug": "lenovo-ideapad-slim3-8866348", "uid": 8866348,
             "sellable": True, "price": {"effective": {"min": 55499, "max": 55499}, "marked": {"min": 78290, "max": 78290}}},
            {"type": "product", "name": "Sold out laptop", "slug": "gone-1", "uid": 1, "sellable": False,
             "price": {"effective": {"min": 9, "max": 9}, "marked": {"min": 78290, "max": 78290}}},
            {"type": "product", "name": "No price yet", "slug": "soon-2", "uid": 2, "sellable": True, "price": {}},
        ], "page": {"current": 1}}
        url = "https://www.reliancedigital.in/ext/raven-api/catalog/v1.0/collections/laptops/items?page_no=1&sort_on=price_asc"
        result = extract_page(url, json.dumps(data))
        self.assertEqual(result.kind, "listing")
        (offer,) = result.offers
        self.assertEqual((offer.key, offer.store, offer.price, offer.mrp),
                         ("reliancedigital.in:8866348", "reliancedigital.in", 55499.0, 78290.0))
        self.assertEqual(offer.url, "https://www.reliancedigital.in/product/lenovo-ideapad-slim3-8866348")

    def test_croma(self):
        data = {"products": [
            {"code": "324351", "name": "Apple MacBook Neo (13 inch, A18 Pro, 8GB, 512GB)", "url": "/apple-macbook-neo-/p/324351",
             "price": {"formattedValue": "₹83,490", "value": 83490.0}, "mrp": {"formattedValue": "₹89,900", "value": 89900.0}},
            {"code": "9", "name": "Priced at list", "url": "/x/p/9", "price": {"value": 500.0}, "mrp": {"value": 500.0}},
        ], "pagination": {"currentPage": 0}}
        result = extract_page("https://api.croma.com/searchservices/v1/category/20?currentPage=0&query=%3Aprice-asc",
                              json.dumps(data))
        macbook, plain = result.offers
        self.assertEqual((macbook.store, macbook.price, macbook.mrp), ("croma.com", 83490.0, 89900.0))
        self.assertEqual(macbook.url, "https://www.croma.com/apple-macbook-neo-/p/324351")
        self.assertTrue(macbook.key.startswith("croma.com:"))
        self.assertIsNone(plain.mrp, "a list price equal to the price says nothing")

    def test_ajio(self):
        data = {"products": [{
            "code": "700295523001", "name": "Men Colourblock Lace-Up Shoes ", "url": "/asian-men-shoes/p/700295523_grey",
            "fnlColorVariantData": {"brandName": "ASIAN"}, "price": {"value": 750}, "wasPriceData": {"value": 1499},
        }]}
        result = extract_page("https://www.ajio.com/api/search?currentPage=0&format=json&query=shoes%3Aprce-asc", json.dumps(data))
        (offer,) = result.offers
        self.assertEqual((offer.key, offer.title, offer.price, offer.mrp),
                         ("ajio:700295523_grey", "ASIAN Men Colourblock Lace-Up Shoes", 750.0, 1499.0))
        self.assertEqual(offer.url, "https://www.ajio.com/asian-men-shoes/p/700295523_grey")

    def test_shopify_catalogue_gives_each_product_its_cheapest_variant(self):
        product = {"id": 4546811920482, "title": "boAt Rockerz 255 Pro+", "handle": "boat-rockerz-255-pro-plus", "variants": [
            {"title": "Black", "price": "1199.00", "compare_at_price": "3990.00", "available": True},
            {"title": "Teal", "price": "999.00", "compare_at_price": "3990.00", "available": False},
            {"title": "Blue", "price": "1099.00", "compare_at_price": None, "available": True}]}
        gone = {"id": 5, "title": "All gone", "handle": "gone", "variants": [{"price": "10.00", "available": False}]}
        url = "https://www.boat-lifestyle.com/products.json?limit=250"
        (offer,) = extract_page(url, json.dumps({"products": [product, gone]})).offers
        self.assertEqual((offer.key, offer.price, offer.mrp), ("boat-lifestyle.com:4546811920482", 999.0, 3990.0))
        self.assertEqual(offer.url, "https://boat-lifestyle.com/products/boat-rockerz-255-pro-plus")
        # A variant selling out or coming back must not look like a price change.
        product["variants"][1]["available"] = True
        product["variants"][0]["available"] = False
        (again,) = extract_page(url, json.dumps({"products": [product, gone]})).offers
        self.assertEqual((again.key, again.price), (offer.key, offer.price))

    def test_unknown_json_is_mined_and_rubbish_is_reported(self):
        data = {"results": [{"productId": 7, "productName": "Some Mixer", "sellingPrice": 1499, "mrp": 2999}]}
        result = extract_page("https://shop.example.in/api/list?page=1", json.dumps(data))
        self.assertEqual([(o.key, o.price, o.mrp) for o in result.offers], [("shop.example.in:7", 1499.0, 2999.0)])
        self.assertEqual(extract_page("https://shop.example.in/api/list", '{"products": []}').kind, "none")
        self.assertEqual(extract_page("https://shop.example.in/api/list", '{"broken": ').kind, "none")

    def test_vijay_sales_cards(self):
        html = """<html><body><div class="plp-horizontal-products">
<div class="product-card" data-child-sku="259536"><div class="wide-card--desktop" data-sku="259536">
<a class="product-card__link" href="/p/259536/hp-thin-and-light-laptop"><div class="product-card__inner">
<div class="product-name">HP Thin and Light Laptop (Intel Core 3/ 8GB RAM/ 512GB SSD)</div>
<div class="product-card__price"><div class="discountedPrice" data-price="62499"><span>₹</span><span>62499</span></div>
<div class="originalPrice"><span class="mrpLabel">MRP </span><div class="price"><span>₹</span><span>67999</span></div></div>
<div class="discountPercentage"><span>"8"%</span><span>Off</span></div></div></div></a></div></div>
<div class="product-card" data-child-sku="1"><div class="product-name">No price shown</div></div>
</div></body></html>"""
        result = extract_page("https://www.vijaysales.com/c/laptops", html)
        (offer,) = result.offers
        self.assertEqual((offer.key, offer.price, offer.mrp), ("vijaysales.com:259536", 62499.0, 67999.0))
        self.assertEqual(offer.url, "https://www.vijaysales.com/p/259536/hp-thin-and-light-laptop")

    def test_list_addresses_are_asked_for_json(self):
        for url in ("https://api.croma.com/searchservices/v1/category/20?query=%3Aprice-asc",
                    "https://www.ajio.com/api/search?query=nike", "https://www.boat-lifestyle.com/products.json?limit=250",
                    "https://www.reliancedigital.in/ext/raven-api/catalog/v1.0/products?q=laptop"):
            with self.subTest(url=url):
                self.assertIn("application/json", request_headers(url)["Accept"])
        self.assertIsNone(request_headers("https://www.flipkart.com/laptops/pr?sid=6bo%2Cb5g&sort=price_asc"))


if __name__ == "__main__":
    unittest.main()
