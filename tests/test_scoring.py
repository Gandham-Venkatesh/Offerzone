import tempfile
import unittest
from pathlib import Path

from lootwatch.config import Settings, load_traps
from lootwatch.db import PriceInfo
from lootwatch.scoring import (TIER_HIGH, TIER_NORMAL, TIER_URGENT, evaluate_offer, evaluate_stated, evaluate_text, evaluate_tracked,
                              is_accessory, is_finance_promo, is_side_product, tracker_near_miss)


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
            # 85-90% : a quiet alert, only when it saves ₹2,000+
            ("wipro 9W LED Bulb\nDeal Price: ₹46 (MRP ₹240)", None),
            ("Beetel Power Bank\nDeal Price: ₹690 (MRP ₹4999)", TIER_NORMAL),
            ("Symbol Jogger\nDeal Price: ₹349 (MRP ₹2599)", TIER_NORMAL),
            # under 85%: list prices are often inflated, 80% off one is an ordinary day
            ("Beetel Power Bank\nDeal Price: ₹890 (MRP ₹4999)", None),
            ("Lifelong LED Desk Lamp with Wireless Charging\nDeal Price: ₹698 (MRP ₹3,999)", None),
            ("boAt Rockerz 450 @ ₹1,499 (MRP ₹3,990)", None),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(self.tier(text), expected)

    def test_phone_cases_are_never_pushed(self):
        # Pushed on 20 Nov 2026 from the price tracker: the owner wants no phone cases at all.
        self.assertIsNone(evaluate_tracked("Samsung Galaxy S23 FE Clear Gadget Phone Case, Protective Cover with Strap",
                                           440, 2355, self.s, low=440))
        for text in ("iPhone 15 Plus Case @ ₹99 (MRP ₹1,999)", "Back cover for Redmi Note 13 @ 9 (MRP ₹999)",
                     "Tempered glass for iPhone 16 Loot @ ₹1 https://amzn.to/x", "Spigen Cyrill | iPhone 16 Case @ ₹99 (MRP ₹2,499)",
                     "BIG LOOT : OnePlus Nord CE4 Silicone Case https://amzn.to/x"):
            with self.subTest(text=text):
                self.assertIsNone(evaluate_text(text, self.s, links=["https://amzn.to/x"]))
                self.assertIsNone(evaluate_text(text, self.s, links=["https://amzn.to/x"], loot_channel=True))
        self.assertIsNone(evaluate_stated("Galaxy S24 Ultra Back Cover", "", 99, 1999, self.s))
        # A phone sold with a free case, and earbuds in their charging case, are not phone cases.
        self.assertEqual(evaluate_text("iPhone 16 with free case @ ₹4,999 (MRP ₹79,900)", self.s).tier, TIER_URGENT)
        self.assertIsNotNone(evaluate_text("boAt Airdopes 141 with charging case @ ₹199 (MRP ₹4,490)", self.s))
        # Seen on 20 Nov 2026: a case maker's earbuds are earbuds.
        self.assertIsNotNone(evaluate_text("Spigen Audio True Wireless Earbuds @ ₹299 (MRP ₹4,999)", self.s))

    def test_favourites_ring_louder_and_count_from_70_percent(self):
        self.s.favourites = ["protein", "whey", "dumbbell"]
        whey = evaluate_text("ON Whey Protein 1kg\nDeal Price: ₹999 (MRP ₹3,999)", self.s)
        self.assertEqual((whey.tier, whey.notes), (TIER_HIGH, ["on your favourites list"]), "75% off, but a favourite")
        self.assertIsNone(evaluate_text("ON Whey Protein 1kg\nDeal Price: ₹1,499 (MRP ₹3,999)", self.s), "62% off")
        self.assertIsNone(evaluate_text("Lifelong Desk Lamp\nDeal Price: ₹999 (MRP ₹3,999)", self.s), "not a favourite")
        self.assertEqual(evaluate_text("Dumbbells 10 kg pair loot @ ₹499 https://amzn.to/x", self.s,
                                       links=["https://amzn.to/x"]).tier, TIER_HIGH, "a loot on a favourite rings")
        self.assertEqual(evaluate_tracked("Whey Protein 2kg", 1999, 5499, self.s, low=1999).tier, TIER_HIGH)

    def test_price_with_a_stated_percentage(self):
        # DesiDime's format: a price and a percentage, no MRP. The saving follows from the two.
        verdict = evaluate_text("🔥 Nothing Earbuds Pro\n💰 ₹400 / 99% off\n🔗 On Flipkart", make_settings())
        self.assertEqual((verdict.tier, verdict.price, verdict.mrp), (TIER_URGENT, 400, None))
        self.assertIn("about ₹39,600 saved", verdict.reason)
        self.assertEqual(self.tier("🧹 MILAGROW Robotic Vacuum Cleaner\n💰 ₹4,294 / 86% off"), TIER_NORMAL)
        self.assertIsNone(self.tier("🧹 MILAGROW Robotic Vacuum Cleaner\n💰 ₹6,294 / 80% off"))
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
                     "Earbuds worth ₹2,999 at ₹1 with phone purchase", "Mixer Grinder worth ₹2,388 @ ₹1 on your 1st order"):
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
        # A capped coupon is never "95% off" a product. For a food app it is still worth a look as what it
        # is: a coupon, counted at what the cap leaves of it (₹160 on ₹199 is 80%), and never at max priority.
        coupon = evaluate_text("SWIGGY FLASHSALE: Up to 80% OFF Food Orders!\n• Flat 95% OFF up to ₹160 (Min Order: ₹199)", self.s)
        self.assertEqual((coupon.label, coupon.tier, coupon.max_tier, round(coupon.discount)), ("Coupon", TIER_HIGH, TIER_HIGH, 80))
        self.assertIsNone(self.tier("Mega sale: Up to 80% OFF!\n• Flat 95% OFF up to ₹160 (Min Order: ₹199)"), "any other shop: ignored")
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
        # Pushed on 19 Nov 2026 as a trap match: "Plus" is the model's name, not "and also".
        for text, keyword in (("iPhone 15 Plus Case @ ₹99", "iPhone 15"), ("Galaxy S24 Plus Back Cover @ ₹199", "Galaxy S24"),
                              ("Spigen Cyrill UltraSheer Mag | iPhone 15 Plus Case @ ₹999", "iPhone 15"),
                              ("Redmi Note 13 Pro Plus Case @ ₹149", "Redmi Note 13")):
            with self.subTest(text=text):
                self.assertTrue(check(text, keyword))
        self.assertFalse(check("iPhone 15 Plus @ ₹9,999", "iPhone 15"))
        self.assertFalse(check("iPhone 16 plus a free case @ ₹9,999"))
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


class QuickCommerce(unittest.TestCase):
    """Coupons and loot posts of food and quick-commerce apps (posts as seen on FoodJunction and swiggyLoots)."""

    def setUp(self):
        self.s = make_settings("")

    def verdict(self, text):
        return evaluate_text(text, self.s)

    def test_big_coupons(self):
        swiggy = self.verdict("Swiggy : Get Flat ₹160 off on Order Above ₹199\nCode : FLASHSALE\nValid Mostly For Metro Cities")
        self.assertEqual((swiggy.tier, swiggy.max_tier, swiggy.label), (TIER_HIGH, TIER_HIGH, "Coupon"))
        self.assertEqual(swiggy.headline, "Coupon: 80% off")
        self.assertEqual(swiggy.reason, "Swiggy coupon: ₹160 off on a ₹199 order, code FLASHSALE")
        self.assertEqual(swiggy.key, "coupon:swiggy:FLASHSALE")
        zomato = self.verdict("Zomato New Promocode❤️\nGet 70% Off On Order Above ₹99\nMaximum Discount is ₹140\nCode :) MUNCHYBLISS")
        self.assertEqual((zomato.tier, zomato.key), (TIER_NORMAL, "coupon:zomato:MUNCHYBLISS"))
        self.assertIn("70% off up to ₹140 on orders from ₹99", zomato.reason)
        capped = self.verdict("🚨 SWIGGY FLASHSALE: Up to 80% OFF Food Orders!\n• Flat 80% OFF up to ₹160 (Min Order: ₹199)\n• Flat 70% OFF up to ₹140")
        self.assertEqual(capped.tier, TIER_HIGH)
        basket = self.verdict("Cred × Bigbasket Offers\nMany Bigbasket Coupon Available\nGet ₹150 Off on Order Above ₹249")
        self.assertEqual((basket.tier, basket.key), (TIER_NORMAL, "coupon:bigbasket:60"))

    def test_a_cap_makes_a_big_percentage_small(self):
        # 80% off, but at most ₹50 on a ₹499 order: a tenth of the bill.
        self.assertIsNone(self.verdict("Zepto: Flat 80% off up to ₹50 on orders above ₹499\nCode: ZEP50"))

    def test_small_coupons_banners_and_other_shops_stay_quiet(self):
        for text in (
            "🎁Zomato Offer : 50% Off Upto ₹200\n₹100 order @ ₹50\nUse Code : HARP0532",
            "Swiggy Offer : Pick ₹50 Free Cash/ Upto ₹150 Off Coupon.\nCombine With Restaurants Giving 70% Off Discount.",
            "Blinkit: Up to 80% off on snacks",
            "BigBasket Free Shopping Offer\nThey Credited Free Cash Again",
            "Amazon: Flat 70% off on orders above ₹99 with code SAVE70",
            "Swiggy: Flat ₹60 off on orders above ₹199\nCode: SAVE60",
        ):
            with self.subTest(text=text):
                self.assertIsNone(self.verdict(text))

    def test_loot_posts_without_a_list_price(self):
        # The quick-delivery apps' loots last minutes and are what the owner wants first: they ring.
        zepto = self.verdict("Zepto Loot\n@ 24\nhttps://www.zepto.com/pn/mccain-chilli-garlic-potato-bites/pvid/bfec78f0")
        self.assertEqual((zepto.tier, zepto.max_tier, zepto.label, zepto.price), (TIER_HIGH, TIER_HIGH, "Quick loot", 24.0))
        ajio = evaluate_text("Ajio Loot @ ₹199", self.s, links=["https://ajiio.in/xyz"])
        self.assertEqual(ajio.tier, TIER_NORMAL, "a fashion app's loot comes at the ordinary priority")
        instamart = self.verdict("Swiggy Instamart Loot\nPigeon Air Fryer Large 5.5L @2999\nhttps://instamart.in/item/N67PETRK4E\nAvailable in Hyderabad")
        self.assertIn("Instamart loot post", instamart.reason)
        self.assertIsNone(self.verdict("[ OVER ] Big Loot ₹100\nhttps://instamart.in/item/IU2N2FNXT6"), "announced as over")
        self.assertIsNone(self.verdict("⚡️Swiggy Dineout Loot Starting at ₹11"), "a range, not a price")
        # A quick-delivery app's product at a give-away price is a loot even when nobody says so
        # (seen on 20 Nov 2026: "Flipkart Minutes: Cat & Dog Food @ 1/-" was missed for want of the word).
        for text in ("Bigbasket : Karwa Chauth Sargi Box at ₹19", "Flipkart Minutes: Cat & Dog Food @ 1/-\nhttps://fkrt.cc/zI0fKTk",
                     "JioMart: 26kg Rice @ 98/-\nhttps://bitli.in/CTI1CTI\nLocation specific"):
            with self.subTest(text=text):
                self.assertEqual(self.verdict(text).tier, TIER_HIGH)
        self.assertIsNone(self.verdict("Zepto: Amul Butter 500g @ 249"), "a dearer one is an ordinary listing")
        self.assertIsNone(self.verdict("Digihaat: Wired In-Ear Earphones @ 51/-"), "not one of the quick-delivery apps")
        nameless = evaluate_text("Jiomart Loot :\nhttps://bitli.in/0a3ld3J", self.s, links=["https://bitli.in/0a3ld3J"])
        self.assertEqual((nameless.tier, nameless.needs_store_link), (TIER_HIGH, True), "the price is in the app")
        coupon = evaluate_text("Loot : Apply ₹8500 Off Coupon\nhttps://fkrt.cc/z10CXg1", self.s, links=["https://fkrt.cc/z10CXg1"])
        self.assertEqual((coupon.tier, coupon.reason), (TIER_NORMAL, "loot with a ₹8,500 coupon, no price given"))
        self.assertIsNone(evaluate_text("Loot : Apply ₹100 Off Coupon\nhttps://fkrt.cc/x", self.s, links=["https://fkrt.cc/x"]))
        self.assertIsNone(self.verdict("Loot @ 24\nhttps://amzn.to/x"), "not one of these apps")

    def test_the_ordinary_rules_still_come_first(self):
        post = self.verdict("Zepto price error\nSony WH-1000XM5 @ ₹499 (MRP ₹34,990)")
        self.assertEqual((post.tier, post.label), (TIER_URGENT, "Price error?"))

    def test_search_hints_for_shopping_apps(self):
        shampoo = self.verdict("Swiggy Instamart Loot\nSearch : Shampoo\n*Sort by Discount High to Low\n*Check In Your Location")
        self.assertEqual((shampoo.tier, shampoo.label, shampoo.max_tier), (TIER_HIGH, "App loot", TIER_HIGH))
        self.assertEqual(shampoo.reason, "Instamart: search “Shampoo” and sort by discount in the app")
        self.assertEqual(shampoo.key, "apploot:instamart:shampoo", "the same hint from many channels is one alert")
        fruits = self.verdict("Swiggy Instamart\nSearch : Fruits and vegetables")
        self.assertIn("“Fruits and vegetables”", fruits.reason)
        bulb = self.verdict("Blinkit Loot\nSearch: Phillips Bulb and sort by price low to high")
        self.assertEqual(bulb.reason, "Blinkit: search “Phillips Bulb” and sort by price in the app")
        jeans = self.verdict("Myntra Loot 🔥\nSearch - Roadster jeans\nSort : Discount high to low")
        self.assertEqual(jeans.reason, "Myntra: search “Roadster jeans” and sort by discount in the app")
        self.assertIn("Ajio", self.verdict("Big loot on Ajio: search \"puma shoes\" and sort by discount").reason)
        for text in ("Search for the best deals on our website", "Instamart is live in your city, search now",
                     "Amazon: search Shampoo, sort by discount"):
            with self.subTest(text=text):
                self.assertIsNone(self.verdict(text))

    def test_big_loots_are_believed_only_from_marked_channels(self):
        # From an ordinary channel a "big loot" comes at the ordinary priority, and only when its
        # link can lead into a shop (see LootChannels for the marked ones, where it rings loudly).
        plain = evaluate_text("BIG LOOT 😱🔥", self.s, links=["https://amzn.to/4xyz"])
        self.assertEqual((plain.tier, plain.reason, plain.needs_store_link), (TIER_NORMAL, "big loot, no price given", True))
        self.assertIsNone(evaluate_text("BIG LOOT 😱🔥", self.s), "no link at all")
        self.assertIsNone(evaluate_text("BIGGEST LOOT : BUY NOW 👇", self.s, links=["https://t.me/addlist/VETj13smDQVmMWE1"]),
                          "a folder of other channels is an advert")
        quiet = evaluate_text("BIG LOOT on Zepto 🔥", self.s, links=["https://www.zepto.com/pn/x/pvid/1"])
        self.assertEqual((quiet.tier, quiet.reason, quiet.needs_store_link), (TIER_HIGH, "Zepto big loot, no price given", True))
        self.assertIsNone(evaluate_text("BIG LOOT on Zepto 🔥", self.s, links=["https://t.me/+9MtPJAnaAnBkMmU1"]),
                          "naming the app is not enough without a link into it")
        marked = evaluate_text("BIG LOOT on Zepto 🔥", self.s, links=["https://www.zepto.com/pn/x/pvid/1"], loot_channel=True)
        self.assertEqual((marked.tier, marked.label), (TIER_URGENT, "Big loot"))
        self.assertIsNone(evaluate_text("BIG LOOT : Swiggy Instamart", self.s, links=["https://t.me/+9MtPJAnaAnBkMmU1"],
                                        loot_channel=True), "a link to another channel is an advert for that channel")
        self.assertIsNone(evaluate_text("Big loot on Zepto", self.s, loot_channel=True), "no link at all")

    def test_loots_stated_as_a_bare_price(self):
        # "Loot @ ₹1": a few rupees is a loot on any store, once the link is seen to lead into one.
        for text, link in (("Blinkit Loot ₹1 🔥", "https://blinkit.com/prn/x/prid/123"),
                           ("Swiggy Instamart Loot @ ₹1", "https://www.swiggy.com/instamart/item/ABC123"),
                           ("Nykaa loot ₹9 lipstick", "https://www.nykaa.com/x")):
            with self.subTest(text=text):
                verdict = evaluate_text(text, self.s, links=[link])
                self.assertEqual((verdict.tier, verdict.label, verdict.needs_store_link), (TIER_HIGH, "Loot", False))
        anywhere = evaluate_text("Loot @ ₹1\nhttps://fkrt.cc/xyz", self.s, links=["https://fkrt.cc/xyz"])
        self.assertEqual((anywhere.tier, anywhere.reason, anywhere.needs_store_link), (TIER_HIGH, "loot at ₹1", True))
        # A higher price counts in the apps such posts name, quietly: nothing tells how good it is.
        ajio = evaluate_text("Ajio Loot @ ₹199", self.s, links=["https://ajiio.in/xyz"])
        self.assertEqual((ajio.tier, ajio.label, ajio.price), (TIER_NORMAL, "Quick loot", 199))
        # Any other price comes at the ordinary priority: the channel calls it a loot, the list price is unknown.
        hundred = evaluate_text("Loot @ ₹100\nhttps://amzn.to/x", self.s, links=["https://amzn.to/x"])
        self.assertEqual((hundred.tier, hundred.price, hundred.needs_store_link), (TIER_NORMAL, 100, True))
        self.assertIn("no list price", hundred.reason)
        self.assertIsNone(evaluate_text("Loot @ ₹100\nhttps://t.me/+S1zaVBoakn3KkBbY", self.s,
                                        links=["https://t.me/+S1zaVBoakn3KkBbY"]), "an advert for another channel")
        # Seen on 20 Nov 2026 in two channels: a sale, not one loot.
        banner = "Ajio Loot : Men Polo T-Shirts Starts @199\n🔗 https://ajiio.co/OjVxz6\nOversized T-Shirts Starts @60."
        self.assertIsNone(evaluate_text(banner, self.s, links=["https://ajiio.co/OjVxz6"]))
        self.assertIsNone(evaluate_text("Myntra Loot : Men's Casual Shirt Starts Rs.179\nhttps://myntr.it/hFx4RH0", self.s,
                                        links=["https://myntr.it/hFx4RH0"]))
        self.assertIsNone(evaluate_text("Boat Earbuds @ ₹1 https://amzn.to/x", self.s, links=["https://amzn.to/x"]),
                          "a price alone, without the channel calling it a loot, is not enough")
        # Pushed on 20 Nov 2026: gold sells at the day's metal price, so its "loot" is a card offer.
        for text in ("Loot : 24KT Gold Coin (1 Gram) @10901 https://amzn.to/x", "Loot @ ₹51,105 : BHIMA JEWELS 4 GM GOLD RING https://amzn.to/x",
                     "Silver Coin 10g loot ₹1,099 https://amzn.to/x"):
            with self.subTest(text=text):
                self.assertIsNone(evaluate_text(text, self.s, links=["https://amzn.to/x"]))
        self.assertEqual(evaluate_text("Gold Coin loot @ ₹1 https://amzn.to/x", self.s, links=["https://amzn.to/x"]).tier,
                         TIER_HIGH, "a gold coin at one rupee is a price error all the same")
        self.assertIsNotNone(evaluate_text("Dabur Honey Gold Loot @ ₹99 https://amzn.to/x", self.s, links=["https://amzn.to/x"]))
        stated = evaluate_text("Loot ₹1 Maggi (MRP ₹14) https://blinkit.com/prn/x", self.s, links=["https://blinkit.com/prn/x"])
        self.assertTrue(stated is None or stated.label != "Loot", "with a list price the ordinary rules decide")

    def test_both_can_be_switched_off(self):
        self.s.quick_coupon_pct = 0
        self.s.quick_loots = False
        self.assertIsNone(self.verdict("Swiggy : Get Flat ₹160 off on Order Above ₹199\nCode : FLASHSALE"))
        self.assertIsNone(self.verdict("Blinkit Loot ₹1\nhttps://bitli.in/Z6dkVo8"))


class FinancePromotions(unittest.TestCase):
    """Card, loan and referral promotions that loot channels post as "Big Loot" (posts as seen on 12 Nov 2026)."""

    def setUp(self):
        self.s = make_settings("")

    def test_promotions_never_alert(self):
        for text in (
            "🚨Scapia New Referral Loot : Apply Lifetime Free Scapia Card & Get Tanishq Gift Voucher 🎁\nRefer 6 → ₹25000 Tanishq GV",
            "BIG LOOT : Get Lifetime Free HDFC Tata Neu Credit Card.\nApply Here : https://bitl.in/gJtQNQi\nMust Apply Referral Code - SUCH1940",
            "Some of our Followers Casually Applied for Scapia Card and Got Approved with Very Good Limits.\nApply Here : https://bitli.in/On8YptX",
            "🔥 ROAR CREDIT CARD – LIFETIME FREE! 💳\n💸 Up to 20% Cashback\n🆓 ₹0 Joining + ₹0 Annual Fee",
            "NEED AN URGENT PERSONAL LOAN? GET UP TO ₹15 LAKHS!\n• 100% Digital | Instant Approval",
            "✅BIGGEST LOOT🔥: GET ₹788 Amazon Gift Card 😍\n*Apply Lifetime FREE AXIS MyZone Credit Card, & Enjoy ₹788 FREE Amazon GV",
            "Price error 😱 ₹1 card offer: apply for the IndusInd Legend credit card today",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_finance_promo(text))
                self.assertIsNone(evaluate_text(text, self.s))

    def test_a_link_to_a_bank_is_a_promotion_too(self):
        self.assertIsNone(evaluate_text("Big loot 🔥 Rs 500 free voucher, 99% off", self.s,
                                        links=["https://apply.scapia.cards/landing_page?referral_code=x"]))
        self.assertIsNone(evaluate_text("Price error!! iPhone voucher @ 99", self.s, links=["https://www.hdfcbank.com/x"]))

    def test_bank_offers_that_come_with_a_product_deal_are_not_promotions(self):
        for text in (
            "Lenovo Idea Tab at 16,499\n*Apply 500 Coupon + *2,000 Off With SBI Credit Card.",
            "iPhone 16 @ ₹54,999\n₹4,000 off with HDFC credit card, apply coupon",
            "Samsung Galaxy S25 @ ₹54,999 (MRP ₹1,04,999)\nExtra 10% off with ICICI credit card",
            "Zomato: apply code MUNCHYBLISS for 70% off",
        ):
            with self.subTest(text=text):
                self.assertFalse(is_finance_promo(text))
        deal = evaluate_text("Price error! Sony Bravia 55 inch @ ₹2,499 (MRP ₹79,990)\n₹1,000 off with SBI credit card", self.s)
        self.assertEqual(deal.tier, TIER_URGENT)

    def test_deal_site_cards_are_filtered_the_same_way(self):
        self.assertIsNone(evaluate_stated("Lifetime Free HDFC Swiggy Credit Card", "Apply now", 0.01, 500, self.s))
        self.assertIsNone(evaluate_stated("Free voucher worth ₹5,000", "", 1, 5000, self.s, links=["https://www.sbicard.com/apply"]))


class LootChannels(unittest.TestCase):
    """Channels marked "!" in channels.txt (the owner's two Offerzone channels): their word is believed."""

    def setUp(self):
        self.s = make_settings("iphone | 12000\n")

    def judge(self, text, marked=True):
        import re

        return evaluate_text(text, self.s, links=re.findall(r"https?://\S+", text), loot_channel=marked)

    def test_a_big_loot_with_a_shop_link_rings_at_max_priority(self):
        for text in ("BIG LOOT 😱🔥\nhttps://amzn.to/4xyz", "Big Loot 🔥🔥\n🤑Loot ₹16 Per Peice\n➡️Add 10 Qty - https://myntr.it/MPy0kwy",
                     "BIG LOOT on Zepto ₹19\nhttps://www.zepto.com/pn/x/pvid/1", "Mega loot 😍 https://instamart.in/item/ABC123",
                     "BIGGEST LOOT 🔥 https://bitli.in/abc"):
            with self.subTest(text=text):
                verdict = self.judge(text)
                self.assertEqual((verdict.tier, verdict.max_tier), (TIER_URGENT, TIER_URGENT))
                unmarked = self.judge(text, marked=False)
                self.assertTrue(unmarked is None or unmarked.tier < TIER_URGENT,
                                "from any other channel never max priority (a quick-delivery app's loot rings high)")
        self.assertTrue(self.judge("BIG LOOT 😱🔥\nhttps://amzn.to/4xyz").needs_store_link, "the link must lead into a shop")
        self.assertEqual(self.judge("Big Loot 🔥 Loot ₹16 https://myntr.it/x").price, 16)

    def test_app_loots_ring_loudly_and_every_deal_is_pushed(self):
        self.assertEqual(self.judge("Instamart Loot ₹99\nhttps://instamart.in/item/ABC123").tier, TIER_HIGH)
        self.assertEqual(self.judge("Ajio loot: Puma shoes @ 599\nhttps://ajiio.in/xyz").tier, TIER_HIGH)
        self.assertEqual(self.judge("Swiggy Instamart Loot\nSearch : Shampoo\n*Sort by Discount High to Low").tier, TIER_HIGH)
        # Whatever else a marked channel posts into a shop is loud as well (the owner's choice).
        plain = self.judge("Milton Casserole Set @450\nhttps://amzn.to/4AxwlmB")
        self.assertEqual((plain.tier, plain.needs_store_link), (TIER_HIGH, True))
        self.assertIsNone(self.judge("Milton Casserole Set @450\nhttps://amzn.to/4AxwlmB", marked=False))
        stronger = self.judge("Sony WH-1000XM5 @ ₹499 (MRP ₹34,990) price error\nhttps://amzn.to/x")
        self.assertEqual(stronger.tier, TIER_URGENT, "the ordinary rules still count, and the louder wins")

    def test_what_is_never_pushed_even_from_a_marked_channel(self):
        for text in (
            "BIG LOOT : Get Lifetime Free HDFC Tata Neu Credit Card.\nApply Here : https://bitl.in/gJtQNQi",
            "🚨 Big Loot : Nike Shoes FREE Using Scapia Coins\nApply Here ➡️ https://rb.gy/vaeryx",
            "BIGGEST LOOT : BUY NOW🔥👇.\nLink - https://t.me/addlist/VETj13smDQVmMWE1\nClick On ADD FOLDER",
            "BIGGEST LOOT : BUY NOW 😱🔥\nLink - https://Offerzone.in/telegram",
            "91 CLUB BIGGEST LOOT 🎁\nLink :- https://91app11.com/#/register?invitationCode=284",
            "[ OVER ] Big Loot ₹100 : https://instamart.in/item/IU2N2FNXT6",
            "Good morning loot lovers!",
            "Join our WhatsApp channel https://whatsapp.com/channel/abc",
        ):
            with self.subTest(text=text):
                self.assertIsNone(self.judge(text))

    def test_a_sale_banner_is_not_a_big_loot(self):
        banner = self.judge("Big Loot: Mobile Accessories Start @29\nhttps://bitli.in/gxyBp6n")
        self.assertEqual(banner.tier, TIER_HIGH, "loud, as everything from a marked channel, but no max-priority big loot")

    def test_posts_from_the_owners_screenshots(self):
        """The Offerzone and Offerzone Tricks posts the owner showed, as they were posted."""
        phone = "BIGGEST LOOT : Nothing Phone 4a (8GB, 256GB) at ₹400.\n\nhttps://amzn.to/4Af8zeJ"
        boat = "BIG LOOT : Upto 97% Off On Boat Earbuds.\n\nhttps://link.amazon/B020Kn9Ho"
        for text in (phone, boat):
            with self.subTest(text=text):
                self.assertEqual(self.judge(text).tier, TIER_URGENT)
                # later the channel edits the post; seen only then, it is not pushed
                self.assertIsNone(self.judge(text + "\n\nEdit - Over Now ❌.\n\nMissing Loots? Check Out This Post : Click Here."))
        instamart = self.judge("Swiggy Instamart Loot\n\nSearch : NOICE")
        self.assertEqual((instamart.tier, instamart.label), (TIER_HIGH, "App loot"))
        for text in (
            "BIG LOOT 👆 Don't Miss 🔥.\n\nGet Nike Shoes for FREE (Using Scapia Coins) + Get Gold Vouchers ❤️‍🔥.\n\n"
            "Apply From Here : https://bitl.in/h4anXGT\n\nMore Details : https://t.me/c/1702197669/13357\n"
            "Note - Jiska Reject Hua Tha Vo Bhi Try Fast With Trick, Approved Horha Hai.\n*Try With Axis Bank - Approved Jaldi Hoga.",
            "BIG LOOT : Get Lifetime Free HDFC Tata Neu Credit Card 🔥😍.\n\nApply here >> https://bitl.in/hdjPYf4\n\n"
            "Referral Code >> BINA802\n\n✅Zero Joining Fee\n✅Zero Annual Fee (FD Base)\n📇 No CIBIL? No worries! Just Open FD & Get Approved.",
            "Ozians!!\n\nFor More Loot Deals 🥳, MUST JOIN All Channels 🔥👇.\n\nLink : https://Offerzone.in/telegram\n\n"
            "*Big & Heavy Loots will Post in Offerzone Only ❤️‍🔥.",
            "BIGGEST LOOT : Lucky Ozians Received Vivo X300 at ₹7000 🔥❤️‍🔥. Post Link - https://t.me/c/1357275556/9999",
        ):
            with self.subTest(text=text):
                self.assertIsNone(self.judge(text))


if __name__ == "__main__":
    unittest.main()


class TheDaysNoise(unittest.TestCase):
    """Pushed loudly on 21 Nov 2026 and none of them a loot worth the ring, or left out and one."""

    def setUp(self):
        self.s = make_settings()
        self.link = ["https://amzn.to/x"]

    def test_memberships_subscriptions_tv_packs_and_restaurant_deals_are_not_loots(self):
        for text in ("Free Loot : Get Free 1 Month Amazon Prime Membership.\n@ ₹2\nhttps://amzn.to/x",
                     "99% off on - TataPlay Jingalala Saturdays Offer - TataPlay Hindi TV shows - Rs. 1\n"
                     "Read More - https://ddime.in/x",
                     "99% off on - Tata Play Jingalala Saturdays Hindi TV Shows HD New for ₹1",
                     "Tata Play Binge worth ₹2,388 @ ₹1 for 1st month",
                     "Swiggy Dineout Deals for ₹1\nhttps://amzn.to/x",
                     "Swiggy Dineout Loot Starting at ₹11\nGo to Swiggy App > Dineout > Check Above Banner",
                     "YouTube Premium 3 months free loot https://amzn.to/x",
                     "Swiggy One Lite membership @ ₹1 loot https://amzn.to/x"):
            with self.subTest(text=text):
                self.assertIsNone(evaluate_text(text, self.s, links=self.link))
                self.assertIsNone(evaluate_text(text, self.s, links=self.link, loot_channel=True))
        self.assertIsNone(evaluate_stated("Amazon Prime Membership 1 Month Free", "", 2, None, self.s))
        # A product that comes with a service, a sale for Prime members, a product named Prime: deals.
        self.assertEqual(evaluate_text("Echo Dot (5th Gen) @ ₹199 (MRP ₹5,499) with free 6 months Amazon Music "
                                       "subscription", self.s).tier, TIER_URGENT)
        for text in ("Prime Day sale: boAt Airdopes 141 @ ₹199 (MRP ₹4,490)",
                     "boAt Airdopes Prime 701 ANC @ ₹199 (MRP ₹4,490)",
                     "Prime members: boAt Airdopes 141 @ ₹199 (MRP ₹4,490)"):
            with self.subTest(text=text):
                self.assertIsNotNone(evaluate_text(text, self.s))
        # A gym membership, for an owner who asked for gym deals, is judged as any deal.
        self.s.favourites = ["gym"]
        self.assertIsNotNone(evaluate_text("Cult gym membership 3 months loot @ ₹9 https://amzn.to/x", self.s,
                                           links=self.link))

    def test_payment_app_offers_are_not_loots(self):
        text = "Paytm offer Exclusive : Get Combo worth ₹999 @ ₹10\nhttps://paytm.com/offer/x"
        self.assertIsNone(evaluate_text(text, self.s, links=["https://paytm.com/offer/x"]))
        # From a channel the owner marked, it is believed as any post that leads somewhere to buy.
        self.assertIsNotNone(evaluate_text(text, self.s, links=["https://paytm.com/offer/x"], loot_channel=True))
        # A deal that also links to a payment app is still a deal.
        self.assertIsNotNone(evaluate_text("boAt Airdopes 141 @ ₹199 (MRP ₹4,490)", self.s,
                                           links=["https://amzn.to/x", "https://paytm.com/offer/x"]))

    def test_app_credit_is_not_a_price(self):
        for text in ("Bigbasket Again Credit ₹75\nCheck in your account & use for free shopping\nhttps://t.me/x",
                     "Bigbasket Again Credit 75 Rs\nhttps://t.me/x",
                     "₹75 credited to your BigBasket wallet, use it for free shopping https://t.me/x"):
            with self.subTest(text=text):
                self.assertIsNone(evaluate_text(text, self.s, links=["https://t.me/x"]))
        verdict = evaluate_text("BigBasket: Fortune Sunflower Oil 1L @ ₹75\nhttps://www.bigbasket.com/pd/1", self.s,
                                links=["https://www.bigbasket.com/pd/1"])
        self.assertEqual((verdict.tier, verdict.label), (TIER_HIGH, "Quick loot"))

    def test_a_give_away_price_needs_something_worth_having(self):
        for text in ("100% off on - Sunfeast Marie Light - Rs. 0 (MRP ₹10)\nRead More - https://ddime.in/x",
                     "100% off on - Sunfeast Marie Light\nDeal Price: ₹0 (MRP ₹10)"):
            with self.subTest(text=text):
                self.assertIsNone(evaluate_text(text, self.s))
        self.assertEqual(evaluate_text("Philips 9W LED bulb @ ₹1 (MRP ₹199)", self.s).tier, TIER_HIGH)
        self.assertEqual(evaluate_text("Jiomart 99% off - Rs. 1\nRead More - https://ddime.in/h6UM", self.s).tier, TIER_HIGH)

    def test_a_price_error_needs_numbers_that_look_like_one(self):
        self.assertIsNone(evaluate_text("Price Error Deal: 50% Off\nhttps://amzn.to/x", self.s, links=self.link))
        self.assertIsNone(evaluate_text("Price error!! Prestige Kettle @ ₹999 (MRP ₹1,999)", self.s))
        self.assertIsNone(evaluate_stated("Price error: Prestige Kettle", "", 999, 1999, self.s))
        # Numbers that bear it out, or no numbers at all: max priority, as before.
        for verdict in (evaluate_text("Price error!! Sony WH-1000XM5 @ ₹499 (MRP ₹34,990)", self.s),
                        evaluate_text("Price error on this one, hurry https://amzn.to/x", self.s, links=self.link),
                        evaluate_stated("Price error: Sony WH-1000XM5", "", 499, 34990, self.s)):
            self.assertEqual((verdict.tier, verdict.label), (TIER_URGENT, "Price error?"))

    def test_clothing_judged_by_its_list_price_alone_is_quiet(self):
        verdict = evaluate_text("Vbuyz Blue Printed Hand Embroidery A-Line Cotton Stitched Kurta\n@ ₹100 (MRP ₹2,499)\n"
                                "https://amzn.to/x", self.s, links=self.link)
        self.assertEqual((verdict.tier, verdict.max_tier), (TIER_NORMAL, TIER_NORMAL))
        self.assertIn("clothing list prices are often inflated", verdict.reason)
        self.assertEqual(evaluate_stated("Men's Cotton T-Shirt", "", 99, 1999, self.s).tier, TIER_NORMAL)
        self.assertEqual(evaluate_text("Women Kurta, Pant & Dupatta Sets at 88% off - Rs. 299", self.s).tier, TIER_NORMAL)
        # A hanger for shirts is no shirt; on the price tracker clothing is measured, not claimed.
        self.assertEqual(evaluate_text("Shirt Hanger set @ ₹99 (MRP ₹2,999)", self.s).tier, TIER_URGENT)
        self.assertEqual(evaluate_tracked("Levi's Men Slim Fit Jeans", 499, 2999, self.s, low=499).tier, TIER_URGENT)

    def test_a_give_away_price_on_the_tracker_rings_whatever_it_saves(self):
        # Left out on 21 Nov 2026 for saving under ₹500: 99% below its usual ₹190, its lowest price.
        verdict = evaluate_tracked("Parachute Advansed Ayurvedic Hair Oil (300 ml)", 2, 190, self.s, low=2, new_low=True)
        self.assertEqual((verdict.tier, verdict.label, verdict.was), (TIER_HIGH, "Price drop", 190))
        self.assertIn("a give-away price", verdict.reason)
        # It has been that cheap before, it is worth next to nothing, or it is no give-away price.
        self.assertIsNone(evaluate_tracked("Parachute Advansed Ayurvedic Hair Oil (300 ml)", 2, 190, self.s, low=1))
        self.assertIsNone(evaluate_tracked("Parle-G Biscuits 100 g", 1, 10, self.s, low=1))
        self.assertIsNone(evaluate_tracked("Dettol Soap 75 g", 25, 190, self.s, low=25))

    def test_a_protein_shampoo_is_no_protein(self):
        self.s.favourites = ["protein", "whey", "gym"]
        # Rang at high priority as a favourite: 77% off a list price, under the 85% bar for the rest.
        self.assertIsNone(evaluate_stated("Parachute Advansed Protein Shampoo, 1.2L", "", 299, 1299, self.s))
        self.assertIsNone(evaluate_text("Protein Hair Mask @ ₹299 (MRP ₹1,299)", self.s))
        self.assertEqual(evaluate_stated("MuscleBlaze Whey Protein 1kg", "", 999, 3999, self.s).tier, TIER_HIGH)
        self.assertEqual(evaluate_text("Protein Bar box of 12 @ ₹299 (MRP ₹1,299)", self.s).tier, TIER_HIGH)

    def test_hamara_mall_is_muted(self):
        # Seven grocery "loots" pushed on 21 Nov 2026; the owner asked for none of them.
        for text in ("Hamara Mall Loot: Premium Whole Jeera - 50g\n@ ₹39\nhttps://bitli.in/x",
                     "Hamara Mall Loot: NAFED Moong Whole - 500g @ ₹66 https://bitli.in/x",
                     "HamaraMall Skincare Loot @ ₹129 https://bitli.in/x"):
            with self.subTest(text=text):
                self.assertIsNone(evaluate_text(text, self.s, links=["https://bitli.in/x"]))
                self.assertIsNone(evaluate_text(text, self.s, links=["https://bitli.in/x"], loot_channel=True))

    def test_a_big_ticket_drop_rings_from_half_its_usual_price(self):
        # Left out on 21 Nov 2026 under the 60% bar: 58% below its usual ₹30,426, 73% off its list price.
        verdict = evaluate_tracked("LG 27 (68.58 cm) Ultrafine UHD 4K IPS Display", 12779, 30426, self.s, low=12779,
                                   new_low=True, mrp=47330)
        self.assertEqual((verdict.tier, verdict.label), (TIER_NORMAL, "Price drop"))
        self.assertIn("saving ₹17,647", verdict.reason)
        cases = (
            ("ADIDAS ULTRABOOST 5 Running Shoes For Men (Blue, 13.5)", 7187, 15623, 7187, 17968),  # footwear
            ("Wrangler Men's Loose Fit Jeans", 5615, 13059, 5615, None),  # clothing, and no list price
            ("Microsoft Surface Headphones 2", 20084, 43660, 20084, 40168),  # only 50% off its list price
            ("Samsonite Freeform Trolley Bag (75 cm)", 13602, 56676, 13602, 17438),  # an average above the list price
            ("Lifelong Desk Lamp", 1800, 4000, 1800, 6000),  # saves under ₹5,000
            ("LG 27 (68.58 cm) Ultrafine UHD 4K IPS Display", 12779, 30426, 11999, 47330),  # it has been cheaper
        )
        for title, price, usual, low, mrp in cases:
            with self.subTest(title=title, low=low):
                self.assertIsNone(evaluate_tracked(title, price, usual, self.s, low=low, mrp=mrp))

    def test_a_quick_app_product_at_80_percent_off_rings(self):
        for text in ("84% off on - Zepto - Oziva Apple Cider Vinegar with Moringa Tablets - Rs. 159\n"
                     "Read More - https://ddime.in/x", "Zepto: Oziva ACV Tablets @ ₹159 (MRP ₹999)"):
            with self.subTest(text=text):
                verdict = evaluate_text(text, self.s, links=["https://ddime.in/x"])
                self.assertEqual((verdict.tier, verdict.label), (TIER_HIGH, "Quick loot"))
        # Worth next to nothing, or a sale banner: no.
        self.assertIsNone(evaluate_text("Zepto: Lays Chips @ ₹2 (MRP ₹20)", self.s))
        self.assertIsNone(evaluate_text("Upto 85% off on Zepto: snacks from ₹9", self.s))
        self.assertIsNone(evaluate_text("Myntra: Oziva ACV Tablets @ ₹159 (MRP ₹999)", self.s))

    def test_digihaat_is_muted_unless_the_discount_is_huge(self):
        # Pushed on 21 Nov 2026 from several channels; the owner asked to mute DigiHaat.
        for text in ("🔥Digihaat Today's Top Loot Deals\n[Coupon Auto-Applied in Cart]\n✅Brownie Energy Bar at ₹18\n"
                     "👉https://bitli.in/fyI01RJ\n✅Dish Cleaner Cloth 10Pcs at ₹51",
                     "🔥Digihaat Loot | Grab\nFlavoured Makhana (Pack of 3) - ₹50 + free Delivery\nhttps://bitli.in/Oo4KVIZ",
                     "Digihaat: Electric Hot Water Bag @ 104/-\n⚡ Free delivery\n👉🏻 https://bitli.in/kkGnbpP",
                     "Gotrackier | DIGIHAT NEW USER LOOT DEAL https://bitli.in/x",
                     "Digi Haat loot: Bluetooth speaker @ ₹299 (MRP ₹999) https://bitli.in/x"):
            with self.subTest(text=text):
                self.assertIsNone(evaluate_text(text, self.s, links=["https://bitli.in/x"]))
                self.assertIsNone(evaluate_text(text, self.s, links=["https://bitli.in/x"], loot_channel=True))
        self.assertIsNone(evaluate_stated("DigiHaat: Makhana pack of 3", "", 50, 199, self.s))
        # A huge discount on a stated list price still rings.
        verdict = evaluate_text("Digihaat Loot: boAt Airdopes 141 @ ₹99 (MRP ₹4,490) https://bitli.in/x", self.s,
                                links=["https://bitli.in/x"])
        self.assertEqual(verdict.tier, TIER_URGENT)
        self.assertEqual(evaluate_stated("DigiHaat: Samsung 43 inch 4K TV", "", 2999, 39999, self.s).tier, TIER_URGENT)

    def test_a_phone_case_is_never_logged_as_a_near_miss(self):
        self.assertEqual(tracker_near_miss(299, 1499, 299, True, None, title="Spigen Liquid Air iPhone 16 Case"), "")
        self.assertIn("its lowest price", tracker_near_miss(299, 1499, 299, True, None, title="Bata Men Casual Sandal"))
