import tempfile
import unittest
from pathlib import Path

from lootwatch.config import Settings, load_traps
from lootwatch.db import PriceInfo
from lootwatch.scoring import TIER_HIGH, TIER_NORMAL, TIER_URGENT, evaluate_offer, evaluate_text, is_accessory, is_side_product


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

    def test_priority_ladder(self):
        cases = [
            # 95%+ : urgent when it saves real money, high when it is a give-away price, else nothing
            ("Sony WH-1000XM5 @ ₹400 (MRP ₹34,990)", TIER_URGENT),
            ("Gulab Jamun 1kg @ ₹1 (MRP ₹250)", TIER_HIGH),
            ("Amul Butter 500g @ ₹9 (MRP ₹285)", TIER_HIGH),
            ("USB cable @ ₹29 (MRP ₹999)", None),                     # 97% off a "₹999" cable: everyday noise
            ("Smart band @ ₹99 (MRP ₹1,999)", TIER_HIGH),              # 95%, saves ₹1,900
            # 90-95% : high when it saves ₹1,000+, urgent when it saves ₹20,000+
            ("Sounce cleaning kit\nDeal Price: ₹79 (MRP ₹999)", None),
            ("Fire-Boltt watch @ ₹1,099 (MRP ₹14,999)", TIER_HIGH),
            ("iPhone 16 (128 GB)\nDeal Price: ₹6,999 (MRP ₹79,900)", TIER_URGENT),
            # 80-90% : a quiet alert, only when it saves ₹2,000+
            ("wipro 9W LED Bulb\nDeal Price: ₹46 (MRP ₹240)", None),
            ("Beetel Power Bank\nDeal Price: ₹890 (MRP ₹4999)", TIER_NORMAL),
            ("Symbol Jogger\nDeal Price: ₹349 (MRP ₹2599)", TIER_NORMAL),
            # under 80%
            ("boAt Rockerz 450 @ ₹1,499 (MRP ₹3,990)", None),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(self.tier(text), expected)

    def test_price_with_a_stated_percentage(self):
        # DesiDime's format: a price and a percentage, no MRP. The saving follows from the two.
        verdict = evaluate_text("🔥 Nothing Earbuds Pro\n💰 ₹400 / 99% off\n🔗 On Flipkart", make_settings())
        self.assertEqual((verdict.tier, verdict.price, verdict.mrp), (TIER_URGENT, 400, None))
        self.assertIn("about ₹39,600 saved", verdict.reason)
        self.assertEqual(self.tier("🧹 MILAGROW Robotic Vacuum Cleaner\n💰 ₹6,294 / 80% off"), TIER_NORMAL)
        self.assertIsNone(self.tier("🌿 Plant Stand\n💰 ₹290 / 82% off"))
        self.assertEqual(self.tier("Jiomart 99% off - Rs. 1\nRead More - https://ddime.in/h6UM"), TIER_HIGH)
        self.assertIsNone(self.tier("77% off on - Samsung 24-Inch Curved Monitor - Rs. 3735"))

    def test_percentage_alone_needs_to_be_extreme(self):
        self.assertIsNone(self.tier("86% Off - LED Motorcycle Driving Spot Lights\nhttps://fkm.asia/4zn5"))
        self.assertIsNone(self.tier("Min 80% Off Bewakoof Clothing."))
        self.assertIsNone(self.tier("Flat 85% off on Puma shoes today"))
        self.assertEqual(self.tier("96% Off - Philips Trimmer\nhttps://fkm.asia/x"), TIER_HIGH)

    def test_a_price_written_right_before_the_word_mrp(self):
        for text in ("Samsung Galaxy A55 5G @ ₹400 MRP ₹45,999", "Deal Price: ₹400 MRP: ₹45,999", "at Rs.400 MRP Rs.45,999",
                     "@400 MRP 45999", "Price ₹400 MRP (MRP ₹45,999)", "X ₹34,990 MRP ₹400 only",
                     "Redmi Note 13 Pro Max ₹999 (MRP ₹24,999)", "Nike Air Max @499 (MRP ₹12,995)"):
            with self.subTest(text=text):
                self.assertEqual(self.tier(text), TIER_URGENT)

    def test_offer_amounts_never_become_the_deal_price(self):
        for text in ("HP 15 Laptop i5 13th Gen at ₹62,999 (MRP ₹89,999) + ₹4,000 Bank Discount",
                     "Samsung 43 inch 4K TV @ 24,990 (MRP ₹52,900) + ₹1500 SBI Discount",
                     "HP 15 Laptop at ₹62,999 + ₹4,000 Bank Discount (MRP ₹89,999)",
                     "Loot For Needy : Air Purifier @12499.\nApply ₹500 Off Coupon : https://x.to/a\nRegular Price ₹19999.\n"
                     "Filter @599 : https://x.to/b",
                     "Loot For Needy : 1.5 Ton AC @31999.\nBank Offer ₹2000 : https://x.to/a\nRegular Price ₹45999.",
                     "Laptop MRP ₹65,000. Pay ₹2,708 x 24 months", "Hero Splendor MRP ₹82,000 | Down payment ₹3,999",
                     "Laptop ₹55,990 (MRP ₹1,05,000) + mouse for ₹1", "Samsung TV ₹24,990 (MRP ₹52,900), installation for ₹499"):
            with self.subTest(text=text):
                self.assertIsNone(self.tier(text))

    def test_an_unsure_reading_is_held_at_high(self):
        # A second, very different amount stands where the price should be.
        verdict = evaluate_text("Laptop ₹62,999 + ₹4,000 something (MRP ₹89,999)", self.s)
        self.assertEqual((verdict.tier, verdict.max_tier), (TIER_HIGH, TIER_HIGH))
        self.assertIn("another amount", verdict.reason)
        # A give-away price that needs a qualifying purchase (the real "₹1 air fryer" post, and its usual wordings).
        for text in ("Make A Cart Value Above ₹2999+ & Get kent ₹8000 Airfryer at ₹1",
                     "Make A Cart Value Above ₹2999+ & Get kent Airfryer Worth ₹8000 at ₹1",
                     "Instamart : Get Boat Earbuds Worth ₹2,499 At ₹9 On Orders Above ₹999",
                     "Earbuds worth ₹2,999 at ₹1 with phone purchase", "Tata Play Binge worth ₹2,388 @ ₹1 for 1st month"):
            with self.subTest(text=text):
                self.assertEqual(self.tier(text), TIER_HIGH)

    def test_a_percentage_that_describes_the_product_is_not_a_discount(self):
        for text in ("INALSA Air Fryer 4.2L 1400W, 90% Less Oil, Timer @ 2,499", "Save 90% oil @ 2,499",
                     "Livpure RO Water Purifier (99% less water wastage) @ 6999", "🌟Philips Air Fryer with 90% Less Fat at ₹6,999.",
                     "Samsung AC 5 star, 95% savings on bill @ 31,990",
                     "KENT Classic Hot Air Fryer 4L 1300 W | Vapour Steam Technology | 80% Less Oil @ 2999"):
            with self.subTest(text=text):
                self.assertIsNone(self.tier(text))

    def test_hot_loot_wording_does_not_cancel_the_alert(self):
        base = "Samsung Galaxy A55 5G (8GB/256GB)\nDeal Price: ₹400 (MRP ₹45999)\nhttps://amzn.to/x\n"
        for last in ("Hurry before it's OOS", "Grab fast before it is out of stock", "Already OOS in some pincodes, check yours",
                     "Almost sold out, be quick", "Grab before it's sold out", "Fast!! Stock over anytime", "Offer over soon",
                     "Order before the deal is gone", "Back in stock! (was sold out earlier)",
                     "Again Live 🔥 (last time expired in 5 min)", "Coupon expired? Try without coupon, still ₹400"):
            with self.subTest(last=last):
                self.assertEqual(self.tier(base + last), TIER_URGENT)
        # One colour gone, or "loot price back", is a live deal.
        for last in ("Blue colour is out of stock now, black available", "Blue OOS", "Some pincodes OOS", "Loot Price Back"):
            with self.subTest(last=last):
                self.assertEqual(self.tier(base + last), TIER_URGENT)
        # Stock wording about one store, inside a longer post: sent one step quieter, with a note, instead of dropped.
        verdict = evaluate_text(base + "Amazon OOS\nFlipkart link: https://fkrt.cc/x", self.s)
        self.assertEqual((verdict.tier, verdict.max_tier), (TIER_HIGH, TIER_HIGH))
        self.assertIn("may already be over", verdict.notes[0])
        self.assertIsNone(self.tier("Symbol Jogger\nDeal Price: ₹349 (MRP ₹2599)\nAmazon OOS\nFlipkart link: https://fkrt.cc/x"))
        # A post that announces the end stays silent, however the announcement is worded.
        for last in ("Expired", "Sold out in 2 min", "OOS", "Deal over ❌", "Update: it was fixed, price increased",
                     "Update: orders will be cancelled, deal over", "Coupon code expired, deal over"):
            with self.subTest(last=last):
                self.assertIsNone(self.tier(base + last))
        for text in ("That was a price error, sold out in seconds", "iPhone 16 at ₹9,999 was live for 5 min, now sold out",
                     "Nothing Phone (3a) @ ₹400 was a price error, deal over now",
                     "Price error deal over, hope you all grabbed it 🎉"):
            with self.subTest(text=text):
                self.assertIsNone(self.tier(text))

    def test_layouts_without_a_labelled_mrp(self):
        # A real channel layout: the usual price sits after a pipe, behind a coupon amount.
        self.assertEqual(self.tier("Air Conditioner Loot @1999 : https://amzn.to/4j4F0Xv\n*Apply ₹500 Coupon | Regular ₹45000+"),
                         TIER_URGENT)
        # A price and a percentage, with other amounts elsewhere in the post.
        self.assertEqual(self.tier("💻 HP Pavilion 15 laptop at a huge discount\nA 13th-gen i5 laptop\n💰 ₹9,999 / 92% off\n"
                                   "💳 ₹1,500 HDFC bank discount\n👉 Read More: https://ddime.in/x"), TIER_URGENT)
        verdict = evaluate_text("📱 Google Pixel 9 — a ₹79,999 phone for pocket change\n💰 ₹400 / 99% off", make_settings())
        self.assertEqual((verdict.tier, verdict.price), (TIER_URGENT, 400))
        self.assertEqual(self.tier("❄ Voltas AC deal\n💰 ₹2,499 / 95% off\n📦 Installation ₹1,499"), TIER_URGENT)
        # "100% off" beside a price is a rounded 99.5% or more.
        verdict = evaluate_text("100% off on - ASUS Vivobook 16 Intel Core i5 - (16 GB/512 GB SSD) Laptop - Rs. 400", self.s)
        self.assertEqual((verdict.tier, verdict.price), (TIER_URGENT, 400))
        self.assertIn("100% off", verdict.headline)
        self.assertEqual(self.tier("Jiomart 100% off - Rs. 1"), TIER_HIGH)
        self.assertIsNone(self.tier("Plant Stand ₹19 / 95% off"))  # ₹361 saved: the rupee rule still applies
        # A range or a budget is not a price.
        self.assertIsNone(self.tier("Flat 80% Off On Puma Shoes Starting ₹599"))

    def test_a_percentage_and_a_price_must_stand_together(self):
        # Real post, 10 Nov 2026: a daily digest. "99% off" belongs to one thread, "Rs 2700" to another.
        digest = ("🔥Top 5 Hot Threads Today\n🌡1332° - Jiomart 99% off\nhttps://ddime.in/h6WF\n"
                  "🌡572° - Cred Deals of the Day & Daily Rewards (10th November)\nhttps://ddime.in/h6WG\n"
                  "🌡520° - Atomberg Renesa Elite Smart |Voice Control| High Air Flow - Rs 2700\nhttps://ddime.in/h6WH")
        self.assertIsNone(self.tier(digest))  # digests are on the default ignore list ("hot threads")
        plain = make_settings()
        plain.deny_words = ""
        verdict = evaluate_text(digest, plain)
        self.assertEqual((verdict.tier, verdict.price), (TIER_HIGH, None))  # and without it: a bare claim, never "₹2,700 at 99% off"
        # A bare price line next to the percentage line does belong to it.
        verdict = evaluate_text("Sony WH-1000XM5\nPrice: ₹400\nDiscount: 99% off\nhttps://amzn.to/x", plain)
        self.assertEqual((verdict.tier, verdict.price), (TIER_URGENT, 400))

    def test_accessories_named_after_the_device_without_any_accessory_word(self):
        s = make_settings("iphone | 12000\nnothing phone | 6000\n")

        def tier(text):
            verdict = evaluate_text(text, s)
            return verdict.tier if verdict else None

        # Real post, 10 Nov 2026: an OtterBox case.
        for text in ("🔥 HOT DEAL 🔥\nOtterBox React Apple iPhone 14 PRO Stardust Clear @ 2922\nhttps://amznn.cc/tf7f1gf",
                     "Spigen Ultra Hybrid iPhone 16 Pro @ 999", "Zeskit CineFlex Apple iPhone 16 Pro Max @ 1499"):
            with self.subTest(text=text):
                self.assertIsNone(tier(text))
        # A device named among the things an accessory fits (found by a reviewer: each rang as urgent).
        s2 = make_settings("iphone | 12000\nipad | 6000\nmacbook | 20000\n")
        for text in ("🌟Apple 20W USB-C Power Adapter (for iPhone, iPad & AirPods) at ₹1,499.",
                     "GRAB : boAt Type C A750 Fast Charging Cable for iPhone 15, iPad Pro, MacBook @249.",
                     "🔥 HOT DEAL 🔥\nUSB C Hub 7 in 1 for MacBook Pro/Air, iPad Pro @ 1499",
                     "Mi 20W USB-C Charger compatible with iPhone, iPad\nDeal Price: ₹549 (MRP ₹1299)",
                     "STRIFF Adjustable Cell Phone Stand, Foldable Mobile Holder, Compatible with iPhone, iPad\n₹149",
                     "92% off on - Sounce 5 in 1 Electronic Cleaning Kit for Keyboard, AirPods, iPhone, Laptop - Rs. 79"):
            with self.subTest(text=text):
                verdict = evaluate_text(text, s2)
                self.assertFalse(verdict is not None and verdict.label == "Trap hit")
        # Sale and shop words in front of the name do not make it an accessory.
        for text in ("Flipkart Big Billion Days Apple iPhone 16 at ₹9,999", "Brand new sealed Apple iPhone 16 @ 9,999",
                     "Vijay Sales Apple iPhone 16 @ 9,999", "🔥 HOT DEAL 🔥\nApple iPhone 14 PRO (Deep Purple, 128 GB) @ 2922",
                     "Sony XM5 and Nothing Phone (3a) both at ₹400", "Latest model Nothing Phone (3a) at ₹400",
                     "Someone ordered Nothing Phone (3a) @ ₹400?? Flipkart is still showing it",
                     "Someone ordered Apple iPhone 16 @ ₹400??"):
            with self.subTest(text=text):
                self.assertEqual(tier(text), TIER_URGENT)

    def test_trap_on_any_product_of_a_real_post_layout(self):
        # Brand and model words before the name, pipes inside a store title, a price with no rupee
        # sign, a product named only in the link: every one of these stopped a trap at some point.
        cases = [
            ("gulab jamun, rasgulla | 20", "₹15\nCheck out Haldiram's Nagpur Gulab Jamun on Instamart: https://instamart.in/item/NG3BW9LPQT?share=true", 15),
            ("gulab jamun, rasgulla | 20", "Loot : Bikaji Bengali Rasgulla 1 kg @15.\nhttps://amzn.to/4a2bvOn", 15),
            ("gulab jamun, rasgulla | 20", "Buy Mother Dairy Rasgulla 1 kg Tin\nhttps://fkrt.co/rjckyk\n₹9", 9),
            ("gulab jamun, rasgulla | 20", "Zepto Loot\n@ 9\nhttps://www.zepto.com/pn/haldirams-gulab-jamun-1-kg-tin/pvid/bfec78f0-068e", 9),
            ("gulab jamun, rasgulla | 20", "Bigbasket Loot at ₹9\nhttps://www.bigbasket.com/pd/40330601/bikano-rasgulla-1-kg-tin/\nCheck In Your Location", 9),
            ("gulab jamun, rasgulla | 20", "Loot - ₹15\nCheck out Haldiram's Gulab Jamun on Instamart: https://instamart.in/item/ZYL23RY6ZV\nAvailable @ 560001", 15),
            ("gulab jamun, rasgulla | 20", "🔥Blinkit LOOT @₹7\nSearch : Gulab Jamun\nApply Low To High Filter\n₹21 https://blinkit.com/prn/x/prid/635814\n₹39 https://blinkit.com/prn/x/prid/523153", 7),
            ("oneplus 13 | 5000", "🌟Flagship Killer OnePlus 13 at ₹619.\n🔗https://amzn.to/4a2bvOn", 619),
            ("oneplus 13 | 5000", "🔥 HOT DEAL 🔥\nOnePlus 13 | Smarter with OnePlus AI (12GB RAM, 256GB Storage Midnight Ocean) @ 699", 699),
            ("iphone | 12000", "🌟iPhone 16 | A18 chip, 48MP Fusion camera at ₹9,999.", 9999),
            ("nothing phone | 6000", "72% off on - Nothing Phone (3a) Pro | Snapdragon 7s Gen 3, 50MP Periscope - Rs. 400", 400),
            ("nothing phone | 6000", "Nothing Phone (3a) at 400\nhttps://amznn.cc/hynn1kx", 400),
            ("nothing phone | 6000", "Nothing Phone Loot 400 : https://fkrt.cc/zCrHGpN", 400),
            ("nothing phone | 6000", "Loot @ ₹400\nhttps://www.flipkart.com/nothing-phone-3a-black-128-gb/p/itm0123456789abc?pid=MOBH8G3PZHHGZWXF", 400),
            ("dry iron | 1079", "GRAB : Wipro Elato 1200W Heavyweight Dry Iron @1079.\nhttps://amzn.to/4Ac5cFH", 1079),
            ("jogger | 349", "Amazon Brand - Symbol Men's Stretchable Regular fit Jogger P...\nDeal Price: ₹349 (MRP ₹2599)", 349),
            ("cleaning kit | 79", "Sounce 5 in 1 Electronic Cleaning Kit for Keyboard, AirPods,...\nDeal Price: ₹79 (MRP ₹999)", 79),
            ("descaler | 77", "Amazon Basics Hardstain & Descaler Eco-Friendly Cleaning Sp...\nDeal Price: ₹77 (MRP ₹999)", 77),
            ("power bank | 799", "Blinkit :\nAmbrane Wireless Magsafe Power Bank for ₹799\nhttps://blinkit.com/prn/x/prid/756297", 799),
            ("galaxy z fold, galaxy z flip | 10000", "Samsung Galaxy Z Fold7 5G (Blue Shadow, 256 GB) @ ₹4,999", 4999),
        ]
        for trap, text, price in cases:
            with self.subTest(text=text):
                verdict = evaluate_text(text, make_settings(trap + "\n"))
                self.assertIsNotNone(verdict)
                self.assertEqual((verdict.tier, verdict.label, verdict.price), (TIER_URGENT, "Trap hit", price))
        # "oneplus 13" is not the OnePlus 13R, and a link's code is not a product name.
        self.assertIsNone(evaluate_text("OnePlus 13R 5G (Astral Trail, 256 GB) @ ₹4,999", make_settings("oneplus 13 | 5000\n")))
        self.assertIsNone(evaluate_text("iPhone 16 https://amzn.to/4ab7Rs9", make_settings("iphone | 12000\n")))

    def test_trap_product_claimed_at_a_huge_discount_without_a_price(self):
        # One channel posts only "N% Off - product". For a product with a trap on it that is worth a look from 90%.
        s = make_settings("macbook | 20000\niphone | 12000\n")
        verdict = evaluate_text("91% Off - Apple 2025 MacBook Air (13-inch, Apple M4 chip, 16GB, 256GB) - Midnight\nhttps://fkm.asia/4zn5", s)
        self.assertEqual((verdict.tier, verdict.label, verdict.price), (TIER_HIGH, "Trap product", None))
        self.assertIsNone(evaluate_text("91% Off - Spigen Case for iPhone 16\nhttps://fkm.asia/4zn5", s))
        self.assertIsNone(evaluate_text("86% Off - Apple 2025 MacBook Air\nhttps://fkm.asia/4zn5", s))
        self.assertIsNone(evaluate_text("91% Off - LED Motorcycle Driving Spot Lights\nhttps://fkm.asia/4zn5", s))

    def test_trap_leaves_alone_what_is_not_the_product_s_price(self):
        s = make_settings("iphone | 12000\nsamsung galaxy s25 | 10000\nipad | 6000\n")
        for text in ("iPhone 16 (128GB)\n₹4000 Bank Offer\nDeal Price ₹62,999\nhttps://amzn.to/4a2bvOn",
                     "Flipkart Big Billion Days\niPhone 16 : https://amzn.to/4a2bvOn\nSamsung S24 : https://amzn.to/4a2bvOn\n"
                     "₹5000 Bank Offer on SBI Cards",
                     "iPhone 16 : https://amzn.to/4a2bvOn\nAirPods @ ₹9,999 : https://amzn.to/4a2bvOn",
                     "Apple Days Sale\niPhone 16 from ₹64,900\niPad from ₹29,900\nAirPods @ ₹9,999",
                     "iPhone 16 gets ₹10,000 price cut on Flipkart", "₹10,000 Price Drop On iPhone 16",
                     "Pre-reserve Samsung Galaxy S25 at ₹1,999 & get benefits worth ₹5,000",
                     "Win iPhone 16 @ ₹1 | Lucky draw"):
            with self.subTest(text=text):
                self.assertIsNone(evaluate_text(text, s))

    def test_error_wording_variants_and_denials(self):
        for text in ("PRICE-ERROR Sony XM5 @ 4999", "Mistake Price: Sony XM5 @ 4999", "Amazon Bug: Sony XM5 @ 4999"):
            with self.subTest(text=text):
                self.assertEqual(self.tier(text), TIER_URGENT)
        for text in ("JBL Tune 770NC glitch free audio @ 4999", "Not a price error, Sony XM5 @ 4999 is the regular sale price",
                     "All price error orders cancelled by Flipkart"):
            with self.subTest(text=text):
                self.assertIsNone(self.tier(text))

    def test_capped_coupons_and_closed_deals_are_not_deals(self):
        self.assertIsNone(self.tier("SWIGGY FLASHSALE: Up to 80% OFF Food Orders!\n• Flat 95% OFF up to ₹160 (Min Order: ₹199)"))
        self.assertIsNone(self.tier("[ OVER ] Big Loot ₹1 (MRP ₹5,000) : https://instamart.in/item/x"))

    def test_below_threshold_is_ignored(self):
        self.assertIsNone(self.tier("boAt Rockerz 450 @ ₹1,499 (MRP ₹3,990)"))  # 62%
        self.assertIsNone(self.tier("Good morning deal hunters!"))

    def test_error_wording_is_urgent_even_without_an_mrp(self):
        self.assertEqual(self.tier("Price Error 😱\nRedmi Buds @ 49\nhttps://amzn.to/x"), TIER_URGENT)

    def test_upto_claims_do_not_alert(self):
        self.assertIsNone(self.tier("Upto 90% off on Fashion, shop now"))

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
                     "20W charger compatible with iPhone 16", "Spigen case iPhone 16 @ 99", "Used iPhone 16 @ 9,999",
                     "iPhone 16 (Used) at ₹9,999", "Apple AirTag (works with iPhone 16) at ₹2703",
                     "iPhone 16 MagSafe Power Bank 10000mAh @ 999", "iPhone 16 Pro Max Case with MagSafe @ 299",
                     "Lanyard for iPhone 16 @ 99", "Case for iPhone 16 @ 99", "Back case for iPhone 16 ₹149",
                     "Best cover for iPhone 16 @ 99", "Game for iPhone 16 @ 99"):
            with self.subTest(text=text):
                self.assertTrue(check(text))
        for text in ("iPhone 16 with free case @ ₹9,999", "iPhone 16 (charger not included) ₹9,999",
                     "iPhone 16 Pro Max 256 GB Natural Titanium", "Apple iPhone 16 @ ₹400 + free cover",
                     "iPhone 16 128GB @ ₹9,999 | case sold separately",
                     # ordinary words that used to switch a trap off
                     "In case you missed: iPhone 16 @ 9,999", "Big Billion Days Part 2: iPhone 16 at ₹4,999",
                     "Game changer loot 🔥 iPhone 16 @ 9,999", "Remote chance but try: iPhone 16 at ₹4,999",
                     "iPhone 16 at ₹9,999, No Cost EMI available", "iPhone 16 @ 9,999 (used HDFC card)",
                     "Lowest price ever for iPhone 16: ₹9,999", "In case you missed iPhone 16 @ 9,999",
                     "Best offer for iPhone 16 @ 9,999"):
            with self.subTest(text=text):
                self.assertFalse(check(text))

    def test_trap_takes_only_the_price_written_for_that_product(self):
        s = make_settings("nothing phone, cmf phone | 6000\niphone | 12000\nmacbook | 20000\nipad | 6000\n"
                          "samsung galaxy s25 | 10000\noneplus 13 | 5000\n")

        def tier(text):
            verdict = evaluate_text(text, s)
            return verdict.tier if verdict else None

        # A bank discount, an exchange bonus, a price cut, an EMI, a budget heading, another product's price.
        for text in ("🌟iPhone 16 (128GB) at ₹62,999 + ₹4000 Bank Discount.\n🔗https://amzn.to/4a2bvOn",
                     "iPhone 16 at ₹62,999 + ₹3000 Exchange Bonus", "iPhone 16 Price Dropped By ₹10,000",
                     "OnePlus 13 at ₹59,999 with ₹5000 bank discount", "Samsung Galaxy S25 ₹8000 Upgrade Bonus | Buy at ₹72,999",
                     "iPhone 16 at ₹62,999 (Including ₹4000 Bank Discount)", "MacBook Air M4 at ₹83,990 + ₹10000 Bank Discount",
                     "iPad Air M3 at ₹54,900. ₹4000 Bank Discount", "iPhone 16 ₹62,999 + ₹4000 Bank Offer",
                     "iPhone 16 on No Cost EMI starting ₹2,917", "iPhone 16 EMI at ₹3,999", "iPhone 16 EMI ₹4,999",
                     "MacBook Air M4 - just ₹6,999 a month on EMI", "Flat 10% off max ₹500 on iPhone 16",
                     "🌟BEST Deals On Smartphones (According To Budget).\nMaster Link : https://amzn.to/3TFUNyU\n"
                     "Under ₹10k : https://amzn.to/4BTIe7Q\nUnder ₹20k : https://amzn.to/4j8vjY9\niPhone : https://amzn.to/4j8vjY5",
                     "boAt cable @ ₹99\niPhone 16 Pro deals live now", "iPhone 16 Pro deals live now\nboAt cable @ ₹99",
                     "boAt cable\n₹99\niPhone 16 Pro", "iPhone 16 https://amzn.to/4ab7Rs9",
                     "🔥Samsung Galaxy S25 at ₹74,999\n👉https://amznn.cc/y\n🔥boAt cable at ₹99\n👉https://amznn.cc/x",
                     "Yesterday iPhone 16 was at ₹9,999, today back to ₹69,900"):
            with self.subTest(text=text):
                self.assertIsNone(tier(text))
        # Accessories and things that merely work with the product.
        for text in ("Mobile Tripod Stand with Remote | iPhone & Android | 360° Rotation\nDeal Price: ₹299 (MRP ₹999)",
                     "iPhone 16 MagSafe Power Bank 10000mAh @ 999", "Apple AirTag (works with iPhone) at ₹2703",
                     "iPad 10th Gen Smart Folio at ₹2,999", "Portronics MagSafe wallet | iPhone 15/16 | @ 499",
                     "Used iPhone 13 @ 9,999", "iPhone 13 (Used) at ₹9,999"):
            with self.subTest(text=text):
                self.assertIsNone(tier(text))
        # Every shape that names the product and its price still fires.
        for text in ("Nothing Phone (3a) ₹400", "iPhone 16 ₹69,900 → ₹9,999", "Loot @ 400 : Nothing Phone (3a)",
                     "₹400 only - Nothing Phone (3a)", "Nothing Phone (3a) Pro 5G (8GB/256GB)\nDeal Price: ₹400 (MRP ₹45999)",
                     "Nothing Phone (3a) @ 400 MRP 24,999 😱", "iPhone 16 Pro Max - ₹9,999", "🌟iPhone 16 Pro Max @9,999",
                     "99% off on - Apple iPhone 16 Pro Max - Rs. 1499", "Nothing Phone (3a) 5G at ₹400, No Cost EMI available",
                     "In case you missed: Nothing Phone (3a) @ 400", "Big Billion Days Part 2: iPhone 16 at ₹4,999",
                     "Game changer loot 🔥 Nothing Phone (3a) @ 400", "Nothing Phone (3a) @ 400\nOrder now: https://x.to/y",
                     "Nothing Phone (3a) | 8GB/256GB | ₹400", "Flipkart BBD | Nothing Phone (3a) | ₹400",
                     "Loot price ₹400 😱\nNothing Phone (3a) Pro",
                     "Nothing Phone (3a) Pro 5G\n8GB RAM, 256GB Storage, 50MP Camera\n6.77 inch AMOLED\nDeal Price: ₹400",
                     "📱 Nothing Phone (3a) — flagship at an unreal price\nA phone everyone wants\n💰 ₹299 / 100% off",
                     "🔥Apple iPhone 16 at ₹9,999\n👉https://amznn.cc/x\n🔥Samsung Galaxy S25 at ₹74,999\n👉https://amznn.cc/y"):
            with self.subTest(text=text):
                self.assertEqual(tier(text), TIER_URGENT)

    def test_trap_skips_accessories_named_after_the_product(self):
        self.assertIsNone(self.tier("iPhone 16 case @ ₹99"))
        self.assertIsNone(self.tier("Nothing Phone (3a) back cover @ ₹149"))
        self.assertIsNone(self.tier("Refurbished iPhone 12 @ ₹14,999"))
        self.assertEqual(self.tier("iPhone 16 with free case @ ₹9,999"), TIER_URGENT)


class OfferVerdicts(unittest.TestCase):
    def setUp(self):
        self.s = make_settings("nothing phone | 8000\n")

    def test_drop_on_a_category_page_is_graded_by_its_size(self):
        # Real alert, 11 Nov 2026: a Chromebook seen at ₹36,502 came back at ₹14,990, an ordinary price for it,
        # and rang as urgent. Nobody chose that product; only a fall to a fraction of the price is an error.
        seen_high = PriceInfo(baseline=36502, observations=3, last_price=36502, first_seen=False, last_age=60.0, quiet_price=36502)
        title = "Lenovo Chromebook 11IJL9 Intel Celeron Dual Core N4500 - (4 GB/64 GB EMMC Storage/Chrome OS)"
        verdict = evaluate_offer(title, 14990, None, seen_high, self.s, target=5000, listing=True)
        self.assertEqual((verdict.tier, verdict.label), (TIER_NORMAL, "Price drop"))
        self.assertEqual(verdict.headline, "Price drop: 59% off · ₹14,990 (was ₹36,502)")
        self.assertEqual(evaluate_offer(title, 11990, None, seen_high, self.s, target=5000, listing=True).tier, TIER_HIGH)   # 67%
        self.assertEqual(evaluate_offer(title, 5990, None, seen_high, self.s, target=5000, listing=True).tier, TIER_URGENT)  # 84%
        # On a product page you added yourself, a 50% fall stays urgent.
        self.assertEqual(evaluate_offer(title, 14990, None, seen_high, self.s).tier, TIER_URGENT)
        # What really happened: ₹36,502 is that Chromebook's list price. It was read once at full price and
        # then again at the ₹14,990 its card always shows. That is no fall at all...
        self.assertIsNone(evaluate_offer(title, 14990, 36502, seen_high, self.s, target=5000, listing=True))
        # ...unless it lands far below its usual discount as well.
        self.assertEqual(evaluate_offer(title, 5990, 36502, seen_high, self.s, target=5000, listing=True).tier, TIER_URGENT)

    def test_drop_against_own_history(self):
        steady = PriceInfo(baseline=24999, observations=5, last_price=24999, first_seen=False)
        verdict = evaluate_offer("Nothing Phone (3a)", 12000, 27999, steady, self.s)
        self.assertEqual(verdict.tier, TIER_URGENT)
        self.assertIn("dropped 52%", verdict.reason)
        self.assertIsNone(evaluate_offer("Nothing Phone (3a)", 22999, 27999, steady, self.s))  # an 8% dip

    def test_a_drop_is_reported_once_not_on_every_check(self):
        still_low = PriceInfo(baseline=24999, observations=6, last_price=9000, first_seen=False, quiet_price=9000,
                              quiet_alerted=True)
        self.assertIsNone(evaluate_offer("Some Phone", 9000, None, still_low, self.s))
        self.assertIsNone(evaluate_offer("Some Phone", 8500, None, still_low, self.s))  # a little lower is the same event

    def test_a_clearly_deeper_fall_is_news_again(self):
        # 60,000 -> 29,000 was reported; three hours later it is mispriced at 400.
        halved = PriceInfo(baseline=60000, observations=6, last_price=29000, first_seen=False, quiet_price=29000,
                           quiet_alerted=True)
        verdict = evaluate_offer("Google Pixel 10 (Obsidian, 128 GB)", 400, None, halved, self.s)
        self.assertEqual((verdict.tier, verdict.label), (TIER_URGENT, "Price drop"))

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
        self.assertIsNone(self.floor("Mini Notebook 11 inch", 999, self.NEW, mrp=1999))
        self.assertIsNone(self.floor("Mini Notebook 11 inch", 999, self.NEW, mrp=9999))
        self.assertIsNone(self.floor("Mini Notebook", 999, self.NEW, mrp=None))
        self.assertIsNone(self.floor("Mini Notebook 11 inch", 999, self.NEW, mrp=11999))
        self.assertIsNone(self.floor("Kids Toy Laptop Learning Machine", 999, self.NEW, mrp=12000))  # a toy, whatever its "MRP"
        self.assertIsNotNone(self.floor("Lenovo IdeaPad Slim 3", 999, self.NEW, mrp=12000))  # 3 x the floor

    def test_first_read_of_a_listing_is_only_a_baseline(self):
        self.assertIsNone(self.floor("HP Laptop 15s", 600, self.NEW, baseline=True))

    def test_floor_ignores_what_was_already_cheap_and_catches_what_falls(self):
        always_cheap = PriceInfo(baseline=999, observations=4, last_price=999, first_seen=False, quiet_price=999)
        self.assertIsNone(self.floor("Mini Notebook 11 inch", 999, always_cheap))
        was_normal = PriceInfo(baseline=45000, observations=4, last_price=45000, first_seen=False)
        verdict = self.floor("Dell Inspiron 15", 600, was_normal)
        self.assertEqual((verdict.tier, verdict.label), (TIER_URGENT, "Under the floor"))
        self.assertIn("fell from ₹45,000", verdict.reason)
        self.assertIsNotNone(self.floor("Dell Inspiron 15", 600, was_normal, mrp=None))  # our own history is enough
        still_there = PriceInfo(baseline=45000, observations=5, last_price=600, first_seen=False, quiet_price=600,
                                quiet_alerted=True)
        self.assertIsNone(self.floor("Dell Inspiron 15", 600, still_there))  # reported once, not on every scan
        self.assertIsNotNone(self.floor("Dell Inspiron 15", 500, still_there))  # ...unless it falls clearly further

    def test_a_product_that_comes_back_after_a_long_absence_is_judged_afresh(self):
        # Day 0: a phone appeared at 400 and alerted. It was fixed and left the cheapest-first page, so
        # the last price on record is still 400. Three days later the same listing is mispriced again.
        day = 86400.0
        back = PriceInfo(baseline=400, observations=1, last_price=400, first_seen=False, last_age=3 * day, quiet_price=400,
                         quiet_alerted=True)
        verdict = self.floor("Samsung Galaxy S25 Ultra 5G (Titanium, 256 GB)", 450, back, mrp=129999)
        self.assertEqual((verdict.tier, verdict.label), (TIER_URGENT, "Under the floor"))
        minute_ago = PriceInfo(baseline=400, observations=1, last_price=400, first_seen=False, last_age=60.0, quiet_price=400,
                               quiet_alerted=True)
        self.assertIsNone(self.floor("Samsung Galaxy S25 Ultra 5G (Titanium, 256 GB)", 450, minute_ago, mrp=129999))

    def test_what_was_normal_when_the_page_was_first_read_stays_quiet(self):
        # A no-name phone that always sits under the floor with an inflated list price, and a phone
        # whose ordinary price is just under a trap price: there at the first read, so not news.
        day = 86400.0
        junk = PriceInfo(baseline=2399, observations=9, last_price=2399, first_seen=False, last_age=60.0, quiet_price=2399)
        self.assertIsNone(evaluate_offer("I Kall K800 (Blue, 32 GB)", 2099, 7999, junk, self.s, target=2500, listing=True))
        away = PriceInfo(baseline=2399, observations=9, last_price=2399, first_seen=False, last_age=3 * day, quiet_price=2399)
        self.assertIsNone(evaluate_offer("I Kall K800 (Blue, 32 GB)", 2399, 7999, away, self.s, target=2500, listing=True))
        s = make_settings("iphone | 12000\n")
        iphone = PriceInfo(baseline=11999, observations=9, last_price=11999, first_seen=False, last_age=3 * day, quiet_price=11999)
        self.assertIsNone(evaluate_offer("Apple iPhone 13 (Midnight, 128 GB)", 11999, 49900, iphone, s, listing=True))
        self.assertIsNone(evaluate_offer("Apple iPhone 13 (Midnight, 128 GB)", 10499, 49900, iphone, s, listing=True))
        # ...but a real fall from that normal price is.
        verdict = evaluate_offer("Apple iPhone 13 (Midnight, 128 GB)", 400, 49900, iphone, s, listing=True)
        self.assertEqual((verdict.tier, verdict.label), (TIER_URGENT, "Trap hit"))

    def test_cheap_but_never_reported_is_still_open(self):
        # First sighting showed no list price, so nothing was sent; a minute later the card shows it.
        unreported = PriceInfo(baseline=400, observations=1, last_price=400, first_seen=False, last_age=60.0, quiet_price=None)
        verdict = self.floor("Samsung Galaxy S25 Ultra 5G (Titanium, 256 GB)", 400, unreported, mrp=129999)
        self.assertEqual(verdict.label, "Under the floor")

    def test_new_product_far_below_its_list_price_above_the_floor(self):
        # A dropped digit lands above the floor and has no history: the card's own discount decides.
        verdict = self.floor("HP Pavilion 15 Intel Core i5 13th Gen - (16 GB/512 GB SSD)", 7499, self.NEW, mrp=74990)
        self.assertEqual((verdict.tier, verdict.label), (TIER_HIGH, "Far below list price"))
        self.assertIn("90% off its list price ₹74,990", verdict.reason)
        self.assertEqual(self.floor("Sony Bravia 55 inch 4K TV", 4500, self.NEW, mrp=99900).tier, TIER_URGENT)  # 95%+
        # Ordinary no-name listings show 50-70% off inflated list prices: quiet.
        self.assertIsNone(self.floor("Uniboom 60 cm (24 inch) HD Ready LED TV", 5753, self.NEW, mrp=19990))
        self.assertIsNone(self.floor("DOMO Slate SL39 Tablet", 8490, self.NEW, mrp=24990))

    def test_page_sorted_by_discount_has_no_floor_and_never_rings_at_max(self):
        # A floor of 0 marks a page sorted by biggest discount: clearance stock at 95% off is its normal top.
        def new_on_page(title, price, mrp):
            return evaluate_offer(title, price, mrp, self.NEW, self.s, target=0, listing=True)

        tv = new_on_page("Croma 109 cm (43 inch) 4K Ultra HD LED Smart Google TV", 2999, 59990)
        self.assertEqual((tv.tier, tv.max_tier, tv.label), (TIER_HIGH, TIER_HIGH, "Far below list price"))
        # No floor: nothing "falls under" it, however cheap, and small things stay quiet.
        self.assertIsNone(new_on_page("GOQii Mask", 14, 299))
        self.assertIsNone(new_on_page("Philips TAE2146 Wired Earphone with Mic", 164, 899), "saves under Rs 2,000")
        self.assertIsNone(new_on_page("Inbase TPU Back Cover for SAMSUNG Galaxy S23+", 24, 2499), "an accessory")
        self.assertIsNone(new_on_page("LG 80 cm (32 inch) HD Ready LED TV", 11999, 24990), "52% off is nothing special here")
        # What was on the page when it was first read never alerts this way.
        seen = PriceInfo(baseline=2999, observations=3, last_price=2999, first_seen=False, last_age=60.0, quiet_price=2999)
        self.assertIsNone(evaluate_offer("Croma 109 cm (43 inch) 4K Ultra HD LED Smart Google TV", 2999, 59990, seen, self.s,
                                         target=0, listing=True))
        # On a real floor page the same newcomer may ring at max priority, and needs the same real saving.
        self.assertEqual(self.floor("Sony Bravia 55 inch 4K TV", 4500, self.NEW, mrp=99900).tier, TIER_URGENT)
        # Not for accessories, small list prices, products already known, or pages without a floor.
        self.assertIsNone(self.floor("Laptop Skin Sticker for 15.6 inch", 4500, self.NEW, mrp=39999 // 4))
        self.assertIsNone(self.floor("Mini Notebook", 4500, self.NEW, mrp=11999))
        known = PriceInfo(baseline=7499, observations=3, last_price=7499, first_seen=False, last_age=60.0, quiet_price=None)
        self.assertIsNone(self.floor("HP Pavilion 15 Intel Core i5", 7499, known, mrp=74990))
        self.assertIsNone(evaluate_offer("HP Pavilion 15 Intel Core i5", 7499, 74990, self.NEW, self.s, listing=True))
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
                self.assertIsNone(self.floor(title, 300, self.NEW, mrp=12999))
        for title in ("HP Laptop 15s, Backlit Keyboard, Fast Charge", "Samsung Galaxy M35 5G (Without Charger)",
                      "Apple MacBook Air M3 with 30W Adapter", "Sony Bravia 55 inch 4K TV with Voice Remote",
                      "ASUS Vivobook 15, Intel Core i3, Fingerprint, Backlit Keyboard, Thin & Light Laptop",
                      "boAt Airdopes 141 Bluetooth TWS in Ear Earbuds with 42H Playtime, Charging Case for travel",
                      "Lenovo IdeaPad Slim 3 Laptop for Students, 16GB RAM"):
            with self.subTest(title=title):
                self.assertFalse(is_side_product(title))

    def test_a_large_list_price_overrules_the_accessory_word_test(self):
        # "Stylus" is an accessory word, but this is a phone: accessories do not carry such list prices.
        title = "Motorola Edge 60 Stylus (Pantone Surf the Web, 256 GB)"
        self.assertTrue(is_side_product(title))
        verdict = evaluate_offer(title, 400, 28999, self.NEW, self.s, target=2500, listing=True)
        self.assertEqual(verdict.label, "Under the floor")
        self.assertFalse(is_side_product("BenQ Mobiuz 27 inch QHD G-Sync Compatible with HDR10 Gaming Monitor"))
        # Used and made-for wording is never overruled.
        for title in ("Refurbished Lenovo ThinkPad T480", "Charger Adapter 65W Compatible with HP Laptops"):
            self.assertIsNone(evaluate_offer(title, 400, 59990, self.NEW, self.s, target=2500, listing=True))
        # A low floor does not turn every inflated accessory price into a "large" one.
        self.assertIsNone(evaluate_offer("Monitor Stand Riser with Drawer", 499, 4999, self.NEW, self.s, target=900, listing=True))

    def test_target_and_trap_fire_on_crossing_not_on_every_check(self):
        crossed = PriceInfo(baseline=500, observations=3, last_price=99, first_seen=False, quiet_price=99, quiet_alerted=True)
        self.assertIsNone(evaluate_offer("Gulab Jamun 1kg", 99, 300, crossed, self.s, target=100))
        cheap_before = PriceInfo(baseline=400, observations=3, last_price=400, first_seen=False, quiet_price=400)
        self.assertIsNone(evaluate_offer("Nothing Phone (1) display unit", 400, None, cheap_before, self.s, listing=True))
        fell = PriceInfo(baseline=24999, observations=3, last_price=24999, first_seen=False)
        self.assertEqual(evaluate_offer("Nothing Phone (3a) 5G", 400, None, fell, self.s, listing=True).label, "Trap hit")

    def test_trap_on_a_listing_needs_proof_that_it_is_the_device(self):
        new = PriceInfo(baseline=None, observations=0, last_price=None, first_seen=True)
        # Spare parts are named after the phone too. Without a list price that fits the device: quiet, not urgent.
        for title, price, mrp in (("Nothing Phone (2a) Mobile Display Combo Folder", 1899, 3999),
                                  ("Nothing Phone (3a) Back Glass Panel", 399, 999),
                                  ("Nothing Phone (3a) Battery", 899, None)):
            with self.subTest(title=title):
                verdict = evaluate_offer(title, price, mrp, new, self.s, listing=True)
                self.assertEqual((verdict.tier, verdict.max_tier, verdict.label), (TIER_NORMAL, TIER_NORMAL, "Trap match?"))
        verdict = evaluate_offer("Nothing Phone (3a) Pro 5G (Black, 256 GB)", 400, 59999, new, self.s, listing=True)
        self.assertEqual((verdict.tier, verdict.label), (TIER_URGENT, "Trap hit"))
        # On a product page you chose yourself the title is proof enough.
        self.assertEqual(evaluate_offer("Nothing Phone (3a) Pro 5G", 400, None, new, self.s).tier, TIER_URGENT)

    def test_first_check_of_a_product_page_at_a_huge_discount(self):
        new = PriceInfo(baseline=None, observations=0, last_price=None, first_seen=True)
        self.assertEqual(evaluate_offer("Smart Watch", 499, 4999, new, self.s).tier, TIER_HIGH)


if __name__ == "__main__":
    unittest.main()
