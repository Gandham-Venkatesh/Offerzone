import unittest

from lootwatch.textparse import fmt_inr, normalize, parse_text


class PricePairs(unittest.TestCase):
    """(text, expected price, expected mrp, expected confidence)"""

    CASES = [
        ("boAt Airdopes 141 @ ₹899 (MRP ₹4,490) + ₹50 cashback", 899, 4490, "labelled"),
        ("Realme Narzo 70 Pro 5G\n✅ Deal Price : ₹14,999\n❌ MRP : ₹24,999\n🔥 40% Off", 14999, 24999, "labelled"),
        ("Nothing Phone worth 60k for just 400 - loot lo", 400, 60000, "labelled"),
        ("Noise ColorFit Pro 4 at Rs.1,499 (Regular Price 5,999)", 1499, 5999, "labelled"),
        ("Sony WH-1000XM5 400/- only (MRP 34,990/-)", 400, 34990, "labelled"),
        ("Loot : Amul Butter 500g @ 1 || MRP 285 || Blinkit", 1, 285, "labelled"),
        ("M.R.P.: ₹1,49,999\nOffer Price: ₹9,999", 9999, 149999, "labelled"),
        ("Earbuds under ₹999 | 128GB Pendrive ₹349 (MRP ₹1,200)", 349, 1200, "labelled"),
        ("iPhone 16 (128GB) ₹69,900 → ₹51,999 with bank offers ₹4,000 off", 51999, 69900, "arrow"),
        ("Price dropped from Rs 2,499 to Rs 499", 499, 2499, "arrow"),
        ("Samsung M35 5G ₹14,999 ₹24,999", 14999, 24999, "inferred"),
        ("Was ₹3,999, now ₹799 on Myntra", 799, 3999, "labelled"),
        ("1) boAt 141 - ₹899 (MRP ₹4,490)\n2) Noise Buds - ₹999 (MRP ₹1,999)", 899, 4490, "labelled"),
    ]

    def test_pairs(self):
        for text, price, mrp, conf in self.CASES:
            with self.subTest(text=text):
                parsed = parse_text(text)
                self.assertEqual(parsed.price, price)
                self.assertEqual(parsed.mrp, mrp)
                self.assertEqual(parsed.pair_conf, conf)

    def test_struck_through_amount_is_the_mrp(self):
        # The Telegram parser rewrites <s>₹60,000</s> as "(MRP ₹60,000)".
        parsed = parse_text("Nothing Phone 3a Pro  (MRP ₹60,000)  ₹400 only")
        self.assertEqual((parsed.price, parsed.mrp), (400, 60000))
        self.assertGreater(parsed.calc_discount, 99)


class NotPrices(unittest.TestCase):
    """Amounts tied to offers, counts and specs must never be read as a price/MRP pair."""

    CASES = [
        "Pack of 2 ₹199",
        "Recharge ₹199 get ₹50 cashback",
        "Get ₹500 voucher on recharge of ₹199",
        "Jio ₹299 and ₹349 plans now with 2GB/day",
        "Upto 90% off on Fashion | Extra 10% off with coupon | Free delivery above ₹499",
        "Flat ₹200 off on orders above ₹999",
        "Save ₹300 with coupon, min purchase ₹1,499",
        "OnePlus Nord CE4 5G (8GB RAM, 128GB) sale @ 12 PM today",
        "Win ₹1000 Amazon voucher, pay ₹1 to enter",
        "No cost EMI from ₹1,250/month on ₹14,999 phone",
        "Call 9876543210 for 5000mAh power bank @ 50% off",
    ]

    def test_no_pair(self):
        for text in self.CASES:
            with self.subTest(text=text):
                parsed = parse_text(text)
                self.assertEqual(parsed.pair_conf, "", msg=f"paired {parsed.price} / {parsed.mrp}")
                self.assertEqual(parsed.calc_discount, 0.0)

    def test_spec_numbers_are_not_amounts(self):
        parsed = parse_text("Samsung 55 inch 4K TV, 8GB + 128GB, 5000mAh, 120Hz @ ₹29,990")
        self.assertEqual([a.value for a in parsed.amounts], [29990])


class Claims(unittest.TestCase):
    def test_percent_claims(self):
        cases = [
            ("Flat 80% Off On Puma Shoes Starting ₹599", 80, None),
            ("Upto 90% off on Fashion", None, 90),
            ("Min 70% off + Extra 10% cashback on Myntra", 70, None),
            ("Get 85% discount today", 85, None),
            ("100% original product with 5% GST", None, None),
            ("Extra 10% off with HDFC cards", None, None),
            ("Up to 60% Off | Flat 85% off on boAt", 85, 60),
        ]
        for text, exact, upto in cases:
            with self.subTest(text=text):
                parsed = parse_text(text)
                self.assertEqual(parsed.pct_claim, exact)
                self.assertEqual(parsed.pct_upto, upto)

    def test_error_wording(self):
        for text in ("Price Error 😱 grab fast", "pricing mistake on Flipkart", "GLITCH deal!!", "Bug price ₹9",
                     "𝗣𝗿𝗶𝗰𝗲 𝗘𝗿𝗿𝗼𝗿 : Sony headphones", "mispriced on Amazon"):
            with self.subTest(text=text):
                self.assertTrue(parse_text(text).error_phrase)
        for text in ("Big loot deal, steal price", "No error in this listing price", "Debugging tools at ₹99"):
            with self.subTest(text=text):
                self.assertEqual(parse_text(text).error_phrase, "")

    def test_expired(self):
        for text in ("Deal over ❌", "Expired", "OOS now", "Now OOS ❌", "Out of stock", "price increased again",
                     "Sold out in 2 min", "Loot ended", "Stock finished", "It's out of stock", "Already OOS",
                     "Nothing Phone @ 400\nOOS"):
            with self.subTest(text=text):
                self.assertTrue(parse_text(text).expired)

    def test_about_to_expire_is_not_expired(self):
        for text in ("Fast!! Will go OOS soon", "Grab before it goes out of stock", "Hurry before stock ends",
                     "Will be sold out soon", "Buy before it gets sold out", "May expire anytime, be quick",
                     "Price error, order before it gets expired", "OOS soon - hurry", "Can go OOS in minutes"):
            with self.subTest(text=text):
                self.assertFalse(parse_text(text).expired)


class Formatting(unittest.TestCase):
    def test_indian_grouping(self):
        self.assertEqual(fmt_inr(400), "₹400")
        self.assertEqual(fmt_inr(149999), "₹1,49,999")
        self.assertEqual(fmt_inr(12345678), "₹1,23,45,678")
        self.assertEqual(fmt_inr(None), "?")

    def test_normalize_flattens_fancy_text(self):
        self.assertEqual(normalize("𝗣𝗿𝗶𝗰𝗲 ₹４００"), "Price ₹400")
        self.assertEqual(normalize("a | b || c"), "a\nb\nc")

    def test_huge_and_hostile_input_is_bounded(self):
        parsed = parse_text("₹" + "9" * 5000 + " @ " * 3000 + "MRP " * 3000)
        self.assertEqual(parsed.pair_conf, "")


if __name__ == "__main__":
    unittest.main()
