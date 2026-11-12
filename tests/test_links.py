import unittest

from lootwatch.config import load_channels, load_traps, load_watchlist, parse_channel
from lootwatch.links import (LinkResolver, classify_host, deal_links, host_of, product_key, same_product_name,
                             text_fingerprint)
from tests.helpers import FakeWeb


class SameProductByName(unittest.TestCase):
    """Two sources naming one product, when no link ties them together (titles as seen on 12 Nov 2026)."""

    def test_the_same_product_written_two_ways(self):
        for first, second in (
            ("MILTON Micronova Jr. Gift Set Inner Stainless Steel Casserole, Set of 3", "Lowest : Milton Micronova Jr. Gift Set (Set of 3)"),
            ("CableCreation 5-in-1 USB C Hubs,Dock USBC Hub Multiport Adapter to HDMI", "CableCreation 5-in-1 USB C Hubs,Dock USBC Hub Multiport Adapter"),
            ("Sony Bravia 55 inch 4K TV", "Sony Bravia TV loot"),
            ("Apple iPhone 15 (Black, 128 GB)", "iPhone 15 128GB Black"),
            ("Nothing Phone (3a) Pro 5G", "Nothing phone 3a pro price error"),
            ("Signoraware Lunchbox", "SignoraWare Lunchbox"),
        ):
            with self.subTest(first=first, second=second):
                self.assertTrue(same_product_name(first, second))
                self.assertTrue(same_product_name(second, first))

    def test_different_products_that_share_words(self):
        for first, second in (
            ("Samsung Galaxy M35 5G (Daybreak Blue, 128 GB)", "Samsung Galaxy A06 (Gold, 64 GB)"),
            ("Apple iPhone 15 (Black, 128 GB)", "Apple iPhone 16 (Black, 128 GB)"),
            ("boAt Rockerz 255 Pro+", "boAt Rockerz 450 Pro"),
            ("Nothing Phone (3a) Pro", "Nothing Phone (2a) Plus"),
            ("Fire-Boltt Glitz Women Smart Watch with 1.19 AMOLED Display", "Fire-Boltt Rise Smart Watch 1.85 HD Display"),
            ("Fire-Boltt Ninja Call Pro Plus Smart Watch", "Fire-Boltt Trio 4.64 cm Smart Watch"),
            ("Upto 70% Off On Signoraware Products", "Up to 61% Off On Signoraware Products"),
            ("Nike Revolution 7", "Adidas Duramo"),
            ("Loot", "Loot 999"),
        ):
            with self.subTest(first=first, second=second):
                self.assertFalse(same_product_name(first, second))
                self.assertFalse(same_product_name(second, first))


class Hosts(unittest.TestCase):
    def test_classification(self):
        cases = {
            "https://www.amazon.in/dp/B0ABCDEFGH": "store",
            "https://dl.flipkart.com/s/abc": "store",
            "https://amzn.to/3xyz": "affiliate",
            "https://fkrt.cc/abc": "affiliate",
            "https://swiggy.onelink.me/x": "affiliate",
            "https://bit.ly/abc": "generic",
            "https://t.me/somechannel/5": "telegram",
            "https://amazon.in.best-offers.xyz/dp/B0ABCDEFGH": "unknown",  # look-alike host
            "https://flipkart.com.loot.site/p/x": "unknown",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(classify_host(host_of(url)), expected)

    def test_product_keys(self):
        same_asin = [
            "https://www.amazon.in/dp/B0ABCDEFGH?tag=chan-21&linkCode=sl1",
            "https://www.amazon.in/Nothing-Phone-3a/dp/B0ABCDEFGH/ref=sr_1_1?th=1&psc=1",
            "https://amazon.in/gp/product/B0ABCDEFGH",
        ]
        self.assertEqual({product_key(u) for u in same_asin}, {"amazon:B0ABCDEFGH"})
        self.assertEqual(
            product_key("https://www.flipkart.com/x/p/itm0123abcd?pid=MOBH4DQFG8NKFRDY&affid=someone"),
            "flipkart:MOBH4DQFG8NKFRDY",
        )
        self.assertEqual(product_key("https://dl.flipkart.com/dl/x/p/itm0123abcd"), "flipkart:itm0123abcd")
        self.assertEqual(product_key("https://www.myntra.com/shoes/puma/puma-x/12345678/buy"), "myntra:12345678")
        self.assertIsNone(product_key("https://www.amazon.in/"))
        self.assertIsNone(product_key("https://evil.example/dp/B0ABCDEFGH"))
        # Search pages: the query identifies the page, tracking parameters do not.
        a = product_key("https://www.amazon.in/s?k=nothing+phone&ref=nb_sb_noss")
        b = product_key("https://www.amazon.in/s?k=nothing+phone&crid=123")
        c = product_key("https://www.amazon.in/s?k=iphone")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_text_fingerprint_survives_cosmetic_differences(self):
        a = text_fingerprint("🔥 LOOT!! Sony WH-1000XM5 Headphones at ₹400", 400)
        b = text_fingerprint("sony wh-1000xm5 headphones — grab fast @ 400", 400)
        self.assertEqual(a, b)
        self.assertNotEqual(a, text_fingerprint("Sony WH-1000XM5 Headphones", 4000))

    def test_deal_links_skip_telegram_and_non_http(self):
        links = ["https://t.me/joinchat/x", "tg://resolve?domain=x", "https://amzn.to/a", "https://amzn.to/a", "mailto:x@y.z"]
        self.assertEqual(deal_links(links), ["https://amzn.to/a"])


class Resolving(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.web = FakeWeb().start()

    @classmethod
    def tearDownClass(cls):
        cls.web.stop()

    def test_follows_redirects_to_a_store_without_fetching_the_store(self):
        self.web.redirects["/r/one"] = "/r/two"
        self.web.redirects["/r/two"] = "https://www.amazon.in/dp/B0ABCDEFGH?tag=chan-21"
        resolved = LinkResolver(allow_private=True).resolve(self.web.base + "/r/one")
        self.assertEqual(resolved.final, "https://www.amazon.in/dp/B0ABCDEFGH?tag=chan-21")
        self.assertEqual((resolved.kind, resolved.hops, resolved.trusted), ("store", 2, True))

    def test_meta_refresh_and_script_redirects(self):
        self.web.pages["/m"] = '<html><head><meta http-equiv="refresh" content="0;url=https://www.flipkart.com/x/p/itm1?pid=MOBH4DQFG8NKFRDY"></head></html>'
        self.web.pages["/j"] = '<html><script>window.location.href = "https://www.myntra.com/a/b/c/12345678/buy";</script></html>'
        resolver = LinkResolver(allow_private=True)
        self.assertEqual(resolver.resolve(self.web.base + "/m").kind, "store")
        self.assertEqual(product_key(resolver.resolve(self.web.base + "/j").final), "myntra:12345678")

    def test_unknown_destination_is_not_trusted(self):
        self.web.pages["/shop"] = "<html><body>OnePlus 12 at ₹99!!! pay now</body></html>"
        resolved = LinkResolver(allow_private=True).resolve(self.web.base + "/shop")
        self.assertEqual(resolved.kind, "unknown")
        self.assertFalse(resolved.trusted)

    def test_redirect_loops_and_dead_links_end_quietly(self):
        self.web.redirects["/loop"] = "/loop"
        resolver = LinkResolver(allow_private=True, budget=3)
        self.assertEqual(resolver.resolve(self.web.base + "/loop").kind, "unknown")
        self.assertEqual(resolver.resolve("http://127.0.0.1:9/dead").kind, "unknown")

    def test_refuses_private_addresses_by_default(self):
        before = self.web.hits["/r/one"]
        resolved = LinkResolver().resolve(self.web.base + "/r/one")
        self.assertEqual(resolved.error, "blocked-address")
        self.assertEqual(self.web.hits["/r/one"], before)  # no request was made
        self.assertEqual(LinkResolver().resolve("http://169.254.169.254/latest/meta-data/").error, "blocked-address")

    def test_app_deep_links_keep_the_last_web_url(self):
        self.web.redirects["/app"] = "intent://flipkart.com/x#Intent;scheme=https;end"
        resolved = LinkResolver(allow_private=True).resolve(self.web.base + "/app")
        self.assertTrue(resolved.final.endswith("/app"))


class ListFiles(unittest.TestCase):
    def write(self, text):
        import tempfile
        from pathlib import Path

        folder = tempfile.mkdtemp()
        path = Path(folder) / "list.txt"
        path.write_text(text, encoding="utf-8")
        return path

    def test_channels(self):
        self.assertEqual(parse_channel("@magiXdeals"), "magiXdeals")
        self.assertEqual(parse_channel("https://t.me/s/desidime?before=5"), "desidime")
        self.assertEqual(parse_channel("t.me/Loot_Dealsx/132204"), "Loot_Dealsx")
        self.assertIsNone(parse_channel("https://t.me/+Vc5XKzHtGyTl_c6i"))  # private invite link
        self.assertIsNone(parse_channel("https://t.me/joinchat/AAAA"))
        path = self.write("# my channels\n@one_chan\nhttps://t.me/two_chan  # the fast one\nONE_CHAN\nt.me/+secret\n\n")
        self.assertEqual(load_channels(path), ["one_chan", "two_chan"])

    def test_watchlist(self):
        path = self.write(
            "https://www.amazon.in/dp/B0ABCDEFGH | 5000 | Nothing Phone 3a\n"
            "https://www.flipkart.com/search?q=nothing+phone#top\n"
            "https://www.amazon.in/dp/B0ABCDEFGH | 9999\n"  # duplicate URL
            "not a url | 5\n"
            "https://blinkit.com/s/?q=gulab+jamun | ₹1,000\n"
        )
        items = load_watchlist(path)
        self.assertEqual(len(items), 3)
        self.assertEqual((items[0].target, items[0].label), (5000.0, "Nothing Phone 3a"))
        self.assertEqual((items[1].url, items[1].target), ("https://www.flipkart.com/search?q=nothing+phone#top", None))
        self.assertEqual(items[2].target, 1000.0)

    def test_traps(self):
        path = self.write("nothing phone, cmf phone | 8,000\niphone 16 | ₹20000\nbroken line\nps5 |\n")
        traps = load_traps(path)
        self.assertEqual([(t.name, t.max_price) for t in traps], [("nothing phone", 8000.0), ("iphone 16", 20000.0)])
        self.assertTrue(traps[0].pattern.search("NOTHING-PHONE (3a)"))
        self.assertTrue(traps[0].pattern.search("new CMF  Phone 2 Pro"))
        self.assertTrue(traps[1].pattern.search("Apple iPhone16 Pro"))
        self.assertFalse(traps[1].pattern.search("iPhone 160 watts"))
        self.assertFalse(traps[0].pattern.search("there is nothing phonetic"))


if __name__ == "__main__":
    unittest.main()
