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
        # The price written directly before the word MRP, in either order.
        ("Samsung Galaxy A55 5G @ ₹400 MRP ₹45,999", 400, 45999, "labelled"),
        ("Deal Price: ₹400 MRP: ₹45,999", 400, 45999, "labelled"),
        ("at Rs.400 MRP Rs.45,999", 400, 45999, "labelled"),
        ("@400 MRP 45999", 400, 45999, "labelled"),
        ("Price ₹400 MRP (MRP ₹45,999)", 400, 45999, "labelled"),  # a struck-through MRP after the word MRP
        ("Sony WH-1000XM5 ₹34,990 MRP ₹400 only", 400, 34990, "labelled"),
        ("Phone at ₹60,000 MRP for just ₹400", 400, 60000, "labelled"),
        # An offer amount beside the MRP is not the price, however close it stands.
        ("HP 15 Laptop at ₹62,999 (MRP ₹89,999) + ₹4,000 Bank Discount", 62999, 89999, "labelled"),
        ("Samsung 43 inch 4K TV @ 24,990 (MRP ₹52,900) + ₹1500 SBI Discount", 24990, 52900, "labelled"),
        ("HP 15 Laptop at ₹62,999 + ₹4,000 Bank Discount (MRP ₹89,999)", 62999, 89999, "labelled"),
        ("1.5 Ton AC @31999 + ₹2000 Bank Offer (MRP ₹45,999)", 31999, 45999, "labelled"),
        ("1.5 Ton AC @31999.\nBank Offer ₹2000 : https://x.to/a\nRegular Price ₹45999.", 31999, 45999, "labelled"),
        ("Air Purifier @12499.\nApply ₹500 Off Coupon : https://x.to/a\nRegular Price ₹19999.\nFilter @599 : https://x.to/b",
         12499, 19999, "labelled"),
        ("Laptop ₹55,990 (MRP ₹1,05,000) + mouse for ₹1", 55990, 105000, "labelled"),
        ("Samsung TV ₹24,990 (MRP ₹52,900), installation for ₹499", 24990, 52900, "labelled"),
        ("Effective price after bank discount ₹51,999 (MRP ₹69,900)", 51999, 69900, "labelled"),
        # "Pro Max" is a model name, not a "max ₹500" cap; "discounted" is not "discount".
        ("Redmi Note 13 Pro Max ₹999 (MRP ₹24,999)", 999, 24999, "labelled"),
        ("Nike Air Max @499 (MRP ₹12,995)", 499, 12995, "labelled"),
        ("Samsung Galaxy A55 ₹400 Discounted Price, MRP ₹45,999", 400, 45999, "labelled"),
        ("Samsung Galaxy A55 @ ₹400 extra fast delivery (MRP ₹45,999)", 400, 45999, "labelled"),
        # "Regular ₹X" after a pipe, two segments away from the price (a real channel layout).
        ("Air Conditioner Loot @1999 : https://amzn.to/4j4F0Xv\n*Apply ₹500 Coupon | Regular ₹45000+", 1999, 45000, "labelled"),
        ("boAt 141 @ ₹899 (MRP ₹4,490) | Noise Buds @ ₹1,799 (MRP ₹1,999)", 899, 4490, "labelled"),
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
        "Laptop MRP ₹65,000. Pay ₹2,708 x 24 months",
        "Hero Splendor MRP ₹82,000 | Down payment ₹3,999",
        "iPhone 16 MRP ₹79,900, exchange bonus ₹8,000 + ₹4000 HDFC Bank Discount",
        "Flat 10% off max ₹500 on orders, MRP ₹4,999",
        "Earbuds under ₹999 (MRP ₹4,999)",
        "Puma shoes starting ₹599, MRP ₹5,999",
        "Maggi Original ₹12 pack of 4",
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

    def test_a_price_is_still_a_price_when_the_next_line_starts_with_a_unit_like_word(self):
        for text in ("Nothing Phone (3a) @ 400\nOrder now: https://x.to/y", "Nothing Phone (3a) @ 400\nNo coupon needed",
                     "Nothing Phone (3a) @ 400 order fast", "Sony XM5 @ 400\nStar deal of the day"):
            with self.subTest(text=text):
                self.assertEqual([a.value for a in parse_text(text).amounts], [400])

    def test_links_are_never_read_as_amounts(self):
        # "Rs9" at the end of a short-link code is not nine rupees.
        self.assertEqual(parse_text("iPhone 16 https://amzn.to/4ab7Rs9").amounts, [])
        self.assertEqual(parse_text("Loot amzn.to/4ab7Rs9 fkrt.cc/Rs400now").amounts, [])
        parsed = parse_text("Sony XM5 @ ₹400 https://x.to/Rs9 (MRP ₹34,990)")
        self.assertEqual((parsed.price, parsed.mrp), (400, 34990))

    def test_offer_amounts_are_excluded_and_ranges_are_not_prices(self):
        def kinds(text):
            return [(a.value, a.kind) for a in parse_text(text).amounts]

        self.assertEqual(kinds("iPhone 16 at ₹62,999 + ₹4000 Bank Discount"), [(62999, "sell"), (4000, "excluded")])
        self.assertEqual(kinds("OnePlus 13 at ₹59,999 with ₹5000 bank discount"), [(59999, "sell"), (5000, "excluded")])
        self.assertEqual(kinds("Samsung Galaxy S25 ₹8000 Upgrade Bonus"), [(8000, "excluded")])
        self.assertEqual(kinds("iPhone 16 Price Dropped By ₹10,000"), [(10000, "excluded")])
        self.assertEqual(kinds("No Cost EMI starting ₹2,917"), [(2917, "excluded")])
        self.assertEqual(kinds("just ₹6,999 a month on EMI"), [(6999, "excluded")])
        self.assertEqual(kinds("Under ₹10k : link"), [(10000, "range")])
        self.assertEqual(kinds("Shoes Starts at ₹959."), [(959, "range")])
        # ...but wording that only looks similar keeps its price.
        self.assertEqual(kinds("boAt Airdopes @ ₹399 Bank offer available"), [(399, "sell")])
        self.assertEqual(kinds("Dry Fruits ₹349 Gift Pack"), [(349, "plain")])
        self.assertEqual(kinds("Nothing Phone @ ₹400 back in stock"), [(400, "sell")])
        self.assertEqual(kinds("iPhone 16 Pro Max - ₹9,999"), [(9999, "plain")])

    def test_a_second_amount_in_the_price_s_place_is_flagged(self):
        self.assertTrue(parse_text("Laptop ₹62,999 + ₹4,000 something (MRP ₹89,999)").ambiguous)
        self.assertFalse(parse_text("Laptop ₹62,999 (MRP ₹89,999) + ₹4,000 something").ambiguous)
        self.assertFalse(parse_text("JBL ₹2,999 | Sony XM5 ₹400 (MRP ₹34,990)").ambiguous)

    def test_purchase_conditions_are_noticed_on_the_price_s_own_line(self):
        self.assertTrue(parse_text("Get Boat Earbuds Worth ₹2,499 At ₹9 On Orders Above ₹999").conditional)
        self.assertTrue(parse_text("Earbuds worth ₹2,999 at ₹1 with phone purchase").conditional)
        self.assertFalse(parse_text("Sony XM5 @ ₹400 (MRP ₹34,990)\nFree delivery on orders above ₹499").conditional)


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
            # A percentage that describes the product is not money off.
            ("INALSA Air Fryer 4.2L 1400W, 90% Less Oil, Timer @ 2,499", None, None),
            ("Livpure RO Water Purifier (99% less water wastage) @ 6999", None, None),
            ("Samsung AC 5 star, 95% savings on bill @ 31,990", None, None),
            ("Save 90% oil @ 2,499", None, None),
            ("Save 90% on Puma shoes today", 90, None),
            ("100% off on - ASUS Vivobook 16 - Rs. 400", 100, None),
        ]
        for text, exact, upto in cases:
            with self.subTest(text=text):
                parsed = parse_text(text)
                self.assertEqual(parsed.pct_claim, exact)
                self.assertEqual(parsed.pct_upto, upto)

    def test_error_wording(self):
        for text in ("Price Error 😱 grab fast", "pricing mistake on Flipkart", "GLITCH deal!!", "Bug price ₹9",
                     "𝗣𝗿𝗶𝗰𝗲 𝗘𝗿𝗿𝗼𝗿 : Sony headphones", "mispriced on Amazon", "PRICE-ERROR Sony XM5", "Mistake Price!!",
                     "Amazon Bug: Sony XM5 @ 4999", "Misprice on Flipkart"):
            with self.subTest(text=text):
                self.assertTrue(parse_text(text).error_phrase)
        for text in ("Big loot deal, steal price", "No error in this listing price", "Debugging tools at ₹99",
                     "JBL Tune 770NC glitch free audio @ 4999", "Not a price error, this is the regular sale price",
                     "All price error orders cancelled by Flipkart"):
            with self.subTest(text=text):
                self.assertEqual(parse_text(text).error_phrase, "")

    def test_expired(self):
        for text in ("Deal over ❌", "Expired", "OOS now", "Now OOS ❌", "Out of stock", "price increased again",
                     "Sold out in 2 min", "Loot ended", "Stock finished", "It's out of stock", "Already OOS",
                     "Nothing Phone @ 400\nOOS", "[ OVER ] Big Loot ₹1 (MRP ₹5,000)", "Price error deal over ❌ expired",
                     "Sony XM5 @ ₹400 | Expired"):
            with self.subTest(text=text):
                self.assertEqual(parse_text(text).expired, "over")

    def test_about_to_expire_is_not_expired(self):
        for text in ("Fast!! Will go OOS soon", "Grab before it goes out of stock", "Hurry before stock ends",
                     "Will be sold out soon", "Buy before it gets sold out", "May expire anytime, be quick",
                     "Price error, order before it gets expired", "OOS soon - hurry", "Can go OOS in minutes",
                     # the urgency wording of a hot loot, in the forms a reviewer found being dropped
                     "Hurry before it's OOS", "Grab fast before it is out of stock", "Already OOS in some pincodes, check yours",
                     "Almost sold out, be quick", "Grab before it's sold out", "Fast!! Stock over anytime", "Offer over soon",
                     "Deal over soon", "Order before the deal is gone", "It's OOS for some, try now",
                     "Back in stock! (was sold out earlier)", "Again Live 🔥 (last time expired in 5 min)",
                     "Coupon expired? Try without coupon, still ₹400", "Before price increased",
                     "Hurry before the price is back to normal", "Before stock over", "Not expired yet", "Fast before OOS",
                     "Till stock lasts", "Big sale over ₹999 orders"):
            with self.subTest(text=text):
                self.assertEqual(parse_text(text).expired, "")

    def test_stock_wording_inside_a_sentence_is_only_a_hint(self):
        for text in ("Offer closed for new users since morning", "Sony XM5 is out of stock on Amazon now", "Amazon OOS"):
            with self.subTest(text=text):
                self.assertEqual(parse_text(text).expired, "maybe")

    def test_one_colour_or_some_pincodes_gone_is_not_the_deal_gone(self):
        for text in ("Blue colour is out of stock now, black available", "Blue OOS", "Some pincodes OOS",
                     "Black OOS, others live", "128GB variant sold out", "Loot Price Back", "Loot is back"):
            with self.subTest(text=text):
                self.assertEqual(parse_text(text).expired, "")

    def test_a_status_closing_a_sentence_is_an_announcement(self):
        # Found by a reviewer: each of these rang as urgent although the post says the deal is gone.
        for text in ("That was a price error, sold out in seconds", "iPhone 16 at ₹9,999 was live for 5 min, now sold out",
                     "Nothing Phone (3a) @ ₹400 was a price error, deal over now",
                     "iPhone 16 at ₹9,999 not available now, deal over", "Update: it was fixed, price increased",
                     "Update: orders will be cancelled, deal over", "Coupon code expired, deal over",
                     "Price error deal over, hope you all grabbed it 🎉", "Deal over. New deal: Sony XM5 @ ₹400"):
            with self.subTest(text=text):
                self.assertEqual(parse_text(text).expired, "over")

    def test_prices_written_without_a_rupee_sign(self):
        def kinds(text):
            return [(a.value, a.kind) for a in parse_text(text).amounts]

        # Real posts: one channel writes "at 599", another "Loot 239 :".
        self.assertEqual(kinds("Snitch Shirt at 599\nhttps://mytr.cc/hyjbl1f"), [(599, "sell")])
        self.assertEqual(kinds("PS5 Slim Disc at 69990\nhttps://amznn.cc/hynn1kx"), [(69990, "sell")])
        self.assertEqual(kinds("Coffee Loot 239 : https://amzn.to/4Ca9kYD\nApply 20% Off Coupon."), [(239, "sell")])
        self.assertEqual(kinds("Wipro Irons Starting at 599"), [(599, "range")])
        self.assertEqual(kinds("Gulab Jamun 1kg at 1\nhttps://amznn.cc/hynn1kx"), [(1, "sell")])
        for text in ("Sale starts at 12", "Deal goes live at 12", "Sale today at 9", "Rated at 45 by users", "Loot 5 products today",
                     "Open at 10 AM tomorrow", "at 50% off", "Loot 5 :"):
            with self.subTest(text=text):
                self.assertEqual(kinds(text), [])
        # A pincode is not a price.
        self.assertEqual(kinds("Loot - ₹15\nAvailable @ 560001"), [(15, "plain"), (560001, "excluded")])
        self.assertEqual(kinds("Available at ₹560001"), [(560001, "sell")])

    def test_words_of_a_link_s_address_are_kept_and_its_codes_are_not(self):
        parsed = parse_text("Zepto Loot\n@ 9\nhttps://www.zepto.com/pn/haldirams-gulab-jamun-1-kg-tin/pvid/bfec78f0-068e-471b?x=SUPER_SAVER")
        self.assertIn("haldirams gulab jamun", parsed.text)
        self.assertNotIn("SUPER", parsed.text)
        self.assertEqual([a.value for a in parsed.amounts], [9])
        parsed = parse_text("Loot @ ₹400\nhttps://www.flipkart.com/nothing-phone-3a-black-128-gb/p/itm0123456789abc?pid=MOBH8G3PZHHGZWXF")
        self.assertIn("nothing phone 3a black 128", parsed.text)
        self.assertEqual([a.value for a in parsed.amounts], [400])

    def test_more_offer_wording_around_a_price(self):
        def kinds(text):
            return [(a.value, a.kind) for a in parse_text(text).amounts]

        self.assertEqual(kinds("iPhone 16 (128GB)\n₹4000 Bank Offer\nDeal Price ₹62,999"), [(4000, "excluded"), (62999, "sell")])
        self.assertEqual(kinds("iPhone 16 gets ₹10,000 price cut on Flipkart"), [(10000, "excluded")])
        self.assertEqual(kinds("₹10,000 Price Drop On iPhone 16"), [(10000, "excluded")])
        # ...and wording that only looks like an offer keeps the price.
        self.assertEqual(kinds("Nothing Phone (3a) ₹400 Back Again"), [(400, "plain")])
        self.assertEqual(kinds("Nothing Phone (3a) 5G at ₹400 No Cost EMI available"), [(400, "sell")])
        self.assertEqual(kinds("Sony WH-1000XM5 @ ₹400 Save ₹34,590 (MRP ₹34,990)")[0], (400, "sell"))

    def test_an_mrp_written_first_takes_the_amount_right_after_it(self):
        parsed = parse_text("HP 15 Laptop  (MRP ₹74,990)  ₹52,990 + bag at ₹1")
        self.assertEqual((parsed.price, parsed.mrp, parsed.ambiguous), (52990, 74990, True))
        parsed = parse_text("Samsung 253 L Fridge (MRP ₹39,990)\nDeal: ₹23,990\nExtended warranty @ ₹1")
        self.assertEqual((parsed.price, parsed.mrp), (23990, 39990))
        # A line that names a product and gives its MRP is followed by its price, not preceded by it.
        parsed = parse_text("boAt Airdopes 141 @ ₹899\nSony Bravia 55 inch 4K TV MRP ₹99,900\nDeal Price ₹54,990")
        self.assertEqual((parsed.price, parsed.mrp), (54990, 99900))

    def test_percentage_stated_twice_or_on_its_own_line(self):
        card = ("🧊 Samsung 189 L Refrigerator — 99% off on Flipkart\nA compact single-door fridge with base drawer\n"
                "💰 ₹149 / 99% off\n📦 2026 model")
        self.assertEqual(parse_text(card).claim_price, 149)
        self.assertEqual(parse_text("Sony WH-1000XM5 Wireless Headphones @ ₹400\n99% Off 😱\nhttps://amzn.to/x").claim_price, 400)
        self.assertIsNone(parse_text("100% Off On 2nd Unit (Buy 1 Get 1) @ ₹499").pct_claim)
        # A free-delivery threshold is not a condition on the price; a cart value on the next line is.
        self.assertFalse(parse_text("Sony XM5 @ ₹400 (MRP ₹34,990) | Free delivery on orders above ₹499").conditional)
        self.assertTrue(parse_text("Kent Airfryer Worth ₹8000 at ₹1\nMake A Cart Value Above ₹2999+").conditional)
        self.assertTrue(parse_text("boAt Airdopes worth ₹2,999 @ ₹1 for new customers").conditional)


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
