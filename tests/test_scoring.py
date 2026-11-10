import tempfile
import unittest
from pathlib import Path

from lootwatch.config import Settings, load_traps
from lootwatch.db import PriceInfo
from lootwatch.scoring import TIER_HIGH, TIER_URGENT, evaluate_offer, evaluate_text, is_accessory, is_side_product


def make_settings(traps: str = "") -> Settings:
    settings = Settings()
    if traps:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "traps.txt"
            path.write_text(traps, encoding="utf-8")
            settings.traps = load_traps(path)
    return settings


class TextVerdicts(unittest.TestCase):
    def setUp(self):
        self.s = make_settings("nothing phone, cmf phone | 8000\niphone | 20000\n")

    def tier(self, text):
        verdict = evaluate_text(text, self.s)
        return verdict.tier if verdict else None

    def test_big_saving_is_urgent(self):
        verdict = evaluate_text("Sony WH-1000XM5 @ ₹400 (MRP ₹34,990)", self.s)
        self.assertEqual(verdict.tier, TIER_URGENT)
        self.assertIn("99% off", verdict.headline)
        self.assertIn("₹400 (MRP ₹34,990)", verdict.headline)

    def test_small_ticket_is_high_not_urgent(self):
        self.assertEqual(self.tier("USB cable @ ₹99 (MRP ₹999)"), TIER_HIGH)  # 90% off but saves ₹900

    def test_priority_follows_the_discount(self):
        self.assertIsNone(self.tier("Sunflower oil 1L @ ₹120 (MRP ₹199)"))          # 40% off: nothing
        self.assertEqual(self.tier("Amul Butter 500g @ ₹55 (MRP ₹285)"), TIER_HIGH)   # 81% off: high
        self.assertEqual(self.tier("Amul Butter 500g @ ₹9 (MRP ₹285)"), TIER_URGENT)  # 97% off: urgent, small as it is
        self.assertEqual(self.tier("Gulab Jamun 1kg @ ₹1 (MRP ₹250)"), TIER_URGENT)

    def test_below_threshold_is_ignored(self):
        self.assertIsNone(self.tier("boAt Rockerz 450 @ ₹1,499 (MRP ₹3,990)"))  # 62%
        self.assertIsNone(self.tier("Good morning deal hunters!"))

    def test_error_wording_is_urgent_even_without_an_mrp(self):
        self.assertEqual(self.tier("Price Error 😱\nRedmi Buds @ 49\nhttps://amzn.to/x"), TIER_URGENT)

    def test_upto_claims_do_not_alert(self):
        self.assertIsNone(self.tier("Upto 90% off on Fashion, shop now"))

    def test_exact_claim_without_prices_is_high(self):
        self.assertEqual(self.tier("Flat 85% off on Puma shoes today"), TIER_HIGH)

    def test_expired_and_denied_posts_are_ignored(self):
        self.assertIsNone(self.tier("Price error deal over ❌ expired"))
        self.assertIsNone(self.tier("Refer and earn ₹500, price error trick, rummy bonus @ ₹1 (MRP ₹5,000)"))

    def test_bank_offer_wording_is_not_spam(self):
        text = "Sony XM5 @ ₹400 (MRP ₹34,990) | Extra 10% off with HDFC credit card | will go OOS soon"
        self.assertEqual(self.tier(text), TIER_URGENT)

    def test_unlabelled_pair_needs_a_store_link(self):
        verdict = evaluate_text("Scratch card ₹10 – ₹1000", self.s)
        self.assertTrue(verdict is None or verdict.needs_trusted_link)

    def test_trap_fires_on_a_low_price_without_any_mrp(self):
        verdict = evaluate_text("Nothing Phone (3a) Pro 5G at just ₹400 😱 fast", self.s)
        self.assertEqual(verdict.tier, TIER_URGENT)
        self.assertIn("trap", verdict.reason)
        self.assertEqual(verdict.price, 400)

    def test_trap_ignores_normal_prices_and_accessories(self):
        self.assertIsNone(self.tier("Nothing Phone (3a) 5G @ ₹21,999 - bank offer"))
        self.assertIsNone(self.tier("Back cover for Nothing Phone 3a @ ₹149"))
        self.assertIsNone(self.tier("Tempered glass compatible with iPhone 16 @ ₹99"))

    def test_trap_reads_the_price_on_the_keyword_line(self):
        text = "iPhone 16 Pro @ ₹1,19,900\nboAt cable @ ₹99\nNoise buds @ ₹799"
        self.assertIsNone(self.tier(text))
        self.assertEqual(self.tier("boAt cable @ ₹99\niPhone 16 Pro @ ₹9,900"), TIER_URGENT)

    def test_accessory_detection(self):
        def check(text, keyword="iPhone 16"):
            start = text.index(keyword)
            return is_accessory(text, start, start + len(keyword))

        for text in ("Spigen case for iPhone 16", "iPhone 16 case @ ₹99", "iPhone 16 Pro Back Cover ₹149",
                     "iPhone 16 tempered glass", "Refurbished iPhone 16 @ ₹9,999", "iPhone 16 (Renewed) ₹8,999",
                     "20W charger compatible with iPhone 16", "iPhone 16 EMI ₹4,999"):
            with self.subTest(text=text):
                self.assertTrue(check(text))
        for text in ("iPhone 16 with free case @ ₹9,999", "iPhone 16 (charger not included) ₹9,999",
                     "iPhone 16 Pro Max 256 GB Natural Titanium", "Apple iPhone 16 @ ₹400 + free cover",
                     "iPhone 16 128GB @ ₹9,999 | case sold separately"):
            with self.subTest(text=text):
                self.assertFalse(check(text))

    def test_trap_skips_accessories_named_after_the_product(self):
        self.assertIsNone(self.tier("iPhone 16 case @ ₹99"))
        self.assertIsNone(self.tier("Nothing Phone (3a) back cover @ ₹149"))
        self.assertIsNone(self.tier("Refurbished iPhone 12 @ ₹14,999"))
        self.assertEqual(self.tier("iPhone 16 with free case @ ₹9,999"), TIER_URGENT)


class OfferVerdicts(unittest.TestCase):
    def setUp(self):
        self.s = make_settings("nothing phone | 8000\n")

    def test_drop_against_own_history(self):
        steady = PriceInfo(baseline=24999, observations=5, last_price=24999, first_seen=False)
        verdict = evaluate_offer("Nothing Phone (3a)", 12000, 27999, steady, self.s)
        self.assertEqual(verdict.tier, TIER_URGENT)
        self.assertIn("dropped 52%", verdict.reason)
        self.assertIsNone(evaluate_offer("Nothing Phone (3a)", 22999, 27999, steady, self.s))  # an 8% dip

    def test_a_drop_is_reported_once_not_on_every_check(self):
        still_low = PriceInfo(baseline=24999, observations=6, last_price=9000, first_seen=False)
        self.assertIsNone(evaluate_offer("Some Phone", 9000, None, still_low, self.s))

    def test_first_sighting_never_counts_as_a_drop(self):
        new = PriceInfo(baseline=None, observations=0, last_price=None, first_seen=True)
        self.assertIsNone(evaluate_offer("Some Phone", 12000, 13000, new, self.s))

    def test_target_price(self):
        info = PriceInfo(baseline=500, observations=3, last_price=500, first_seen=False)
        self.assertEqual(evaluate_offer("Gulab Jamun 1kg", 99, 300, info, self.s, target=100).tier, TIER_URGENT)
        self.assertIsNone(evaluate_offer("Gulab Jamun 1kg", 450, 500, info, self.s, target=100))

    def test_listing_items_only_alert_through_traps_or_drops(self):
        new = PriceInfo(baseline=None, observations=0, last_price=None, first_seen=True)
        # A case at "95% off" its inflated MRP in a search result is everyday noise.
        self.assertIsNone(evaluate_offer("Back Cover for Nothing Phone 3a", 99, 1999, new, self.s, listing=True))
        # ...but the phone itself at ₹400 trips the trap on first sight.
        verdict = evaluate_offer("Nothing Phone (3a) Pro 5G (Black, 256 GB)", 400, 59999, new, self.s, listing=True)
        self.assertEqual(verdict.tier, TIER_URGENT)

    # -- price floors on search / category pages ("nothing here should cost this little") --
    NEW = PriceInfo(baseline=None, observations=0, last_price=None, first_seen=True)

    def floor(self, title, price, info, mrp=59990, **kwargs):
        return evaluate_offer(title, price, mrp, info, self.s, target=4000, listing=True, **kwargs)

    def test_floor_alerts_on_a_new_entry_under_it(self):
        verdict = self.floor("HP Laptop 15s, 12th Gen Intel Core i5, 16GB RAM, Backlit Keyboard, 1.69 kg", 600, self.NEW)
        self.assertEqual((verdict.tier, verdict.label), (TIER_URGENT, "Under the floor"))
        self.assertIn("₹4,000 floor, list price ₹59,990", verdict.reason)
        self.assertIsNone(self.floor("HP Laptop 15s", 42990, self.NEW))  # an ordinary price

    def test_new_cheap_item_needs_a_list_price_far_above_the_floor(self):
        # Listings are noisy: a toy with a Rs 1,999 or Rs 9,999 "MRP" turning up under a Rs 4,000 floor is not an error.
        self.assertIsNone(self.floor("Kids Toy Laptop Learning Machine", 999, self.NEW, mrp=1999))
        self.assertIsNone(self.floor("Kids Toy Laptop Learning Machine", 999, self.NEW, mrp=9999))
        self.assertIsNone(self.floor("Mini Notebook", 999, self.NEW, mrp=None))
        self.assertIsNotNone(self.floor("Lenovo IdeaPad Slim 3", 999, self.NEW, mrp=16000))  # 4 x the floor

    def test_first_read_of_a_listing_is_only_a_baseline(self):
        self.assertIsNone(self.floor("HP Laptop 15s", 600, self.NEW, baseline=True))

    def test_floor_ignores_what_was_already_cheap_and_catches_what_falls(self):
        always_cheap = PriceInfo(baseline=999, observations=4, last_price=999, first_seen=False)
        self.assertIsNone(self.floor("Kids Toy Laptop", 999, always_cheap))
        was_normal = PriceInfo(baseline=45000, observations=4, last_price=45000, first_seen=False)
        verdict = self.floor("Dell Inspiron 15", 600, was_normal)
        self.assertEqual((verdict.tier, verdict.label), (TIER_URGENT, "Under the floor"))
        self.assertIn("fell from ₹45,000", verdict.reason)
        self.assertIsNotNone(self.floor("Dell Inspiron 15", 600, was_normal, mrp=None))  # our own history is enough
        still_there = PriceInfo(baseline=45000, observations=5, last_price=600, first_seen=False)
        self.assertIsNone(self.floor("Dell Inspiron 15", 600, still_there))  # reported once, not on every scan
        hovering = PriceInfo(baseline=4200, observations=5, last_price=4200, first_seen=False)
        self.assertIsNone(self.floor("Budget Tablet", 3900, hovering, mrp=6000))  # a small dip under the floor is not news

    def test_floor_skips_accessories_spares_and_used_units(self):
        for title in ("Laptop Skin Sticker for 15.6 inch", "Charger Adapter 65W Compatible with HP Laptops",
                      "Dell Latitude 7490 (Renewed) Core i5", "Refurbished Lenovo ThinkPad T480",
                      "Keyboard Replacement for Dell Inspiron", "Laptop Stand Adjustable Aluminium",
                      "Spigen Ultra Hybrid Back Cover Case for iPhone 16", "ESR Tempered Glass Screen Protector for iPad",
                      "Portronics Laptop Sleeve 15.6 inch", "Zebronics Wireless Mouse"):
            with self.subTest(title=title):
                self.assertTrue(is_side_product(title))
                self.assertIsNone(self.floor(title, 300, self.NEW))
        for title in ("HP Laptop 15s, Backlit Keyboard, Fast Charge", "Samsung Galaxy M35 5G (Without Charger)",
                      "Apple MacBook Air M3 with 30W Adapter", "Sony Bravia 55 inch 4K TV with Voice Remote",
                      "ASUS Vivobook 15, Intel Core i3, Fingerprint, Backlit Keyboard, Thin & Light Laptop",
                      "boAt Airdopes 141 Bluetooth TWS in Ear Earbuds with 42H Playtime, Charging Case for travel",
                      "Lenovo IdeaPad Slim 3 Laptop for Students, 16GB RAM"):
            with self.subTest(title=title):
                self.assertFalse(is_side_product(title))

    def test_target_and_trap_fire_on_crossing_not_on_every_check(self):
        crossed = PriceInfo(baseline=500, observations=3, last_price=99, first_seen=False)
        self.assertIsNone(evaluate_offer("Gulab Jamun 1kg", 99, 300, crossed, self.s, target=100))
        cheap_before = PriceInfo(baseline=400, observations=3, last_price=400, first_seen=False)
        self.assertIsNone(evaluate_offer("Nothing Phone (1) display unit", 400, None, cheap_before, self.s, listing=True))
        fell = PriceInfo(baseline=24999, observations=3, last_price=24999, first_seen=False)
        self.assertEqual(evaluate_offer("Nothing Phone (3a) 5G", 400, None, fell, self.s, listing=True).label, "Trap hit")

    def test_first_check_of_a_product_page_at_a_huge_discount(self):
        new = PriceInfo(baseline=None, observations=0, last_price=None, first_seen=True)
        self.assertEqual(evaluate_offer("Smart Watch", 499, 4999, new, self.s).tier, TIER_HIGH)


if __name__ == "__main__":
    unittest.main()
