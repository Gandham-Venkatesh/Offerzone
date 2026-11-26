# Loot monitor

Catches price errors on Indian stores and rings your phone through [ntfy](https://ntfy.sh).
It needs no paid service and no API keys, and runs on a device at home, on GitHub Actions or on
a small server.

## What it does

Each scan (default: every 60 seconds) it looks in four places:

1. **Store category pages with a price floor** (`watchlist.txt`). A page such as "Laptops on
   Flipkart, cheapest first" normally starts with the cheapest real laptops. A ₹60,000 laptop
   mispriced at ₹600 lands at the top of it. The monitor alerts when something appears under the
   page's floor whose list price says it should cost far more, when something it has been
   watching falls to it, and when a product it has never seen turns up on the page at 85% or
   more off its own list price (a dropped digit: ₹74,990 shown as ₹7,499).
   This is read from the store itself; no deal channel is involved. It is the automated form of
   the "masterlinks" that deal hunters check by hand. The same is done per brand ("Apple phones
   on Flipkart, cheapest first"), where the floor can sit much higher.
2. **Thousands of products, each against its own price history.** Every product on a watched
   page is remembered: the most popular products of each category on Flipkart, and the product
   lists of Croma, Reliance Digital, Ajio, Vijay Sales and several brand stores. A fall of half
   or more from the price the monitor has been seeing alerts, when it saves ₹1,000 or more; a
   fall of 80% or more alerts whatever the amount.
3. **Products and keywords you name.** Product pages in `watchlist.txt` alert on your target
   price. `traps.txt` holds lines like `nothing phone | 6000`: any page or post showing that
   product at or under ₹6,000 alerts.
4. **Deal sites and public deal channels** (`feeds.txt`, `channels.txt`). DealsMagnet's
   new-deal page is read directly, minutes before its deals are repeated on Telegram (DesiDime's
   would be too, but since 18 November 2026 it refuses GitHub's servers); a price tracker's live
   list of drops covers Amazon. Both are read every 20 seconds (lines marked `!` in `feeds.txt`).
   27 public Telegram channels are read from the web once a minute, or within seconds once the
   Telegram login has joined them (see "Invite-only channels"); so are the two big Offerzone
   channels (Offerzone itself has posted nothing since about 8 November 2026; Offerzone Tricks
   does), DV Offers (quick-commerce and food loots, often minutes ahead of Offerzone Tricks) and,
   on trial since 26 November 2026, Instamart/Blinkit/Zepto, which only the login can read. Six other big channels that went private were joined, read and left out:
   they repost the same deals as the public ones (notes in `channels.txt`). A loot a channel posts without a
   list price ("Loot @ ₹100", "BIG LOOT 😱") goes out at once at the ordinary priority (₹10 or
   less: high); if the price tracker lists the same Amazon or Flipkart product within the hour,
   far below its usual price, a second push says "Checked loot" at high or max priority. A
   Flipkart product the monitor's own store pages have shown is checked against those readings
   at once. Loots in the quick-delivery apps (Instamart, Zepto, Blinkit, BigBasket, Swiggy,
   Flipkart Minutes, JioMart), and their "search X, sort by discount" hints, ring at high priority.
5. **Your favourites** (`favourites.txt`: protein, whey, gym equipment, ... edit it freely). A deal
   on one of them rings one step louder, and one judged by its list price alone counts from 70% off.
   A favourite posted with only a price in a quick-delivery app ("Blinkit | 99" under a whey
   protein) rings at high priority.
   Every other deal judged only by its list price needs 85% (`MRP_MIN_DISCOUNT`), since inflated
   list prices make 80% off an everyday sight. Posts are parsed for price, MRP and discount, de-duplicated across
   all of these, and filtered hard.

## What it cannot do

- **It cannot watch every product.** Amazon and Flipkart list hundreds of millions. It reads
  the pages you give it, about a dozen per store per minute, and takes turns when a store has
  more pages than that (see "The files you edit"). An error on a product that is on none of
  those pages, and that no deal site or channel reports, is invisible to it.
- **It cannot promise to beat the channels.** A store's search pages can lag a price change by
  minutes, and the big channels are fed by people and robots that see more than any one list.
- **It cannot force a store to answer.** Amazon, Myntra, Snapdeal, Tata CLiQ and JioMart refuse
  plain requests or build their pages only inside a browser. See "Where to run it".
- **It cannot read invite-only Telegram channels by itself.** Only channels with a public
  preview page can be read without a Telegram account. The two big Offerzone channels are
  invite-only; they are read once you give it a Telegram login (see "Invite-only channels").
- **Blinkit, Zepto and Swiggy Instamart are not read directly.** Their prices come from app
  APIs tied to your delivery location and refuse plain requests. The tools that do work for them
  run inside your own logged-in browser (search GitHub for "PriceDrop Hunter" or "zepto-deals").
  Here they are covered through the deal channels, where their coupons, loots and "search this
  in the app" hints are posted within minutes (see "Food and quick-commerce apps" below).

## How loud each alert is

**From a store page:**

| Alert | ntfy priority | When |
|---|---|---|
| **Urgent** | 5 (max) | something under a category page's floor · a trap (with a list price that fits the device) · your target price · a fall of 80% or more (`URGENT_DROP_PCT`) from the price the monitor saw it at · on a product page you added yourself, a fall of 50% · a product new to a floor page at 95% or more off its list price, saving ₹2,000 or more |
| **High** | 4 | on a category page, a fall of 65% to 80% · a product new to a floor page, or to a page sorted by discount, at `NEW_LISTING_DISCOUNT` (85%) or more off its list price, saving ₹2,000 or more · a product page at 80% or more off on its first check |
| **Quiet** | 3 | on a category page, a fall of 50% to 65% (`WATCH_DROP_PCT`) · a listing named like a trap product whose card shows no list price that fits it (spare parts are named after the phone too) |

A fall on a category page only counts when it saves ₹1,000 or more (`MIN_SAVING`): thousands of
products are followed, and a ₹449 gadget going to ₹199 is nobody's loot. A fall of 80% or more
that saves less (earbuds from ₹999 to ₹9) still alerts, quietly. Max priority always needs ₹2,000
saved (`URGENT_MIN_SAVING`), on a product page of your own as well; below that such a fall is High.

A fall on a category page is graded because nobody chose those products, and "the price it was
seen at" can be a seller's passing high price: the first day of running sent an urgent alert for
a ₹14,990 Chromebook that had briefly been listed at ₹36,502.

Whatever is on a page the first time it is read counts as normal and stays quiet. A product is
reported once per event: again only when it falls clearly further, or when the same mistake is
made again after it had gone from the page for a day.

**From a deal post**, the priority follows the discount, and each rung also needs real money
saved. That second condition is there because of what real channels post all day: a ₹79 cleaning
kit with a ₹999 "MRP" is "92% off".

| Alert | ntfy priority | When |
|---|---|---|
| **Urgent** | 5 (max) | 95% off or more, saving ₹2,000 or more · 90% off or more, saving ₹20,000 or more · the post says price error / glitch (and its own numbers, if it gives any, show 70% off or more) · a trap |
| **High** | 4 | 90% off or more, saving ₹1,000 or more, or priced ₹10 or less (a "₹1 deal") on something worth ₹50 more · only a percentage is given and it is 95% or more |
| **Quiet** | 3 | 80% off or more, saving ₹2,000 or more · any post whose link leads to a site that is not a known store (with a warning) |

A deal already alerted that turns up in three channels within 20 minutes goes up one level.

**From a deal site's page** (DesiDime, DealsMagnet) the same ladder applies; the price and the
list price are taken from the card's own fields instead of being read out of the wording. A card
whose price fields are empty is an offer or a coupon, not a priced product: it alerts only on
what its wording states outright (a labelled price and MRP, a percentage with a price, "price
error", a trap), never on two bare numbers.

The same deal usually arrives from several places within the hour. It rings once: deals are
matched by the store product their link leads to (DesiDime's "Buy Now" links are followed for
that), and where no link can be followed, by name and price.

**From the price tracker** nothing is a claim: each item comes with the price the product
usually has. So the bar is how far the price stands below that, and it sits lower:

| Alert | ntfy priority | When |
|---|---|---|
| **Urgent** | 5 (max) | 80% or more below its usual price (`URGENT_DROP_PCT`) · a trap, when its usual price fits the device |
| **High** | 4 | 70% to 80% below |
| **Quiet** | 3 | 60% to 70% below (`TRACKER_DROP_PCT`) |

Each needs ₹500 or more saved (max priority: ₹2,000), and goes one step quieter when the product
has been cheaper than this before. A give-away price is the exception: ₹10 or less, 90% or more
below its usual price and the lowest the tracker has seen, it rings at High whatever it saves (a
300 ml hair oil at ₹2 that usually costs ₹190). A big-ticket drop counts from half its usual price,
at Quiet: the lowest price the tracker has seen, 60% or more under its list price and ₹3,000 or more
saved (an LG 27-inch 4K monitor at about ₹12,800 that usually costs ₹30,400; a Preethi 3-burner
glass gas stove at ₹3,284 that usually costs ₹6,569). Clothing and footwear, whose usual and list
prices are the least real, count from 70% below their usual price and ring at High only from 80%. A
product the tracker lists in several sizes or colours (a shoe in nine sizes) is pushed once.

**Flipkart products are looked at on Flipkart before they ring**, whoever reports them (the price
tracker, a deal site, a channel): sold out there, it is not pushed; far dearer there than the post
says, with no coupon or card offer named, the loot is over and is not pushed; in stock, the push says
so and at what price. A page that cannot be read changes nothing. From a channel marked `!` nothing
is dropped this way: a sold-out loot comes with a warning instead. Flipkart's answer is kept for a
while (sold out: half an hour, from one run to the next; in stock: two minutes), since the tracker
lists a drop again every few minutes and Flipkart refuses quick checks that come too often. A check waits for a gap after the
monitor's own paced reading of Flipkart, asks once more a few seconds after a refusal, and a refused
check only makes the next checks wait: Flipkart's pages are read on as before. When it
refuses, an earlier "sold out" (up to six hours old) still counts; with no answer at all the push
says "stock not checked", and a give-away price (₹10 or less) then comes at Quiet. The check reads
Flipkart's public product page, which tells whether Flipkart has the product, not whether it
delivers it to your pincode. (On 21 Nov 2026, 24 of 42
Flipkart drops on the tracker were sold out when checked, among them every cheap watch.)

An alert is held at **High**, whatever the discount, when the reading itself is unsure: the two
prices are not labelled, a second amount stands where the price should be, the price needs a
qualifying purchase ("₹1 on orders above ₹2,999"), the post hints that the deal may be over, or
its link could not be checked. A link that leads to a site that is not a known store keeps the
alert at Quiet, and copies of the post in other channels never lift it above that.

**Food and quick-commerce apps** (Instamart, Zepto, Blinkit, BigBasket, JioMart, Flipkart Minutes,
Swiggy, Zomato, Domino's, EatSure, magicpin) get a rule of their own, because their prices cannot
be read and their deals are coupons and one-off loots:

| Alert | ntfy priority | When |
|---|---|---|
| **High** | 4 | a coupon that takes 80% or more off the order it needs ("₹160 off on ₹199") · in the quick-delivery apps (Instamart, Zepto, Blinkit, BigBasket, JioMart, Flipkart Minutes, Swiggy): a loot ("Zepto loot @ 24"), a product at ₹99 or less, a product at 80% or more off ("84% off on Zepto: Oziva tablets, ₹159"), or a hint to look inside the app ("Instamart loot: search Shampoo, sort by discount high to low") |
| **Quiet** | 3 | a coupon worth 60% to 80% of its order (`QUICK_COUPON_PCT`) · a loot with a price in the other apps ("Myntra loot @ 146"), although no list price can be checked · the same hints for Ajio, Myntra and Nykaa (`QUICK_LOOTS` switches loots and hints off) |

A percentage coupon is counted at what its cap leaves of it: "80% off up to ₹50 on orders above
₹499" is a tenth of the bill and stays quiet. These never ring at max priority, and they are
usually tied to a city or an account. The same app hint from many channels arrives once.

**Channels marked `!` in `channels.txt`** (the two Offerzone channels) are believed on their own
word, because what they post is worth it:

| Alert | ntfy priority | When |
|---|---|---|
| **Urgent** | 5 (max) | the post calls it a **big loot** (big, biggest, mega, super, bumper ... loot) and its link leads into a shop: Amazon, Flipkart, Myntra, Ajio, Instamart, Zepto, Blinkit ... |
| **High** | 4 | a loot in a quick-commerce or fashion app (Instamart, Zepto, Blinkit, BigBasket, JioMart, Ajio, Myntra, Nykaa), by link or by a "search this, sort by discount" hint |
| **Quiet** | 3 | any other post of theirs whose link leads into a shop |

The link is followed before anything is pushed: a post whose link ends on another Telegram channel
or on a site that is not a shop is not pushed at all. A big loot that only names a sale ("mobile
accessories start @29") stays quiet. Whatever the ordinary rules make louder stays louder. Any
other channel can be marked the same way. Elsewhere, "big loot" is just a word: on ordinary
channels it is mostly an advert for other channels.

A post whose only links go to other Telegram channels ("BIGGEST LOOT: iPhone 17 at ₹2,952", with a
link to a Telegram folder) is held at Quiet with a warning, from any channel.

**Credit cards, loans and referral schemes never alert**, however they are headed ("BIG LOOT:
lifetime free credit card, refer 6 friends for a ₹25,000 voucher"). A post counts as one when it is
about applying for a card or loan, its fees and limits, or a referral scheme, or when its link
leads to a bank. A bank offer that comes with a product deal ("₹2,000 off with SBI credit card")
is not one of them.

**Services never alert either:** memberships and subscriptions ("Get Free 1 Month Amazon Prime
Membership" at ₹2, YouTube Premium, Swiggy One), TV packs (Tata Play, Dish TV, Airtel DTH),
restaurant deals (Swiggy Dineout), and offers whose links lead only to a payment app (Paytm,
PhonePe; from a channel marked `!` these are still believed). A product that comes with a
subscription ("Echo Dot with 6 months of Amazon Music") is still a product, and a membership for
something on your favourites list (a gym) is judged like any deal. Staycations and holiday packages
count as services too. Phone cases and pouches, printer ink and toner (a printer is still a printer),
LEGO, and gold or jewellery "loots" never alert. Nor do **cables** of any kind (Type-C to Type-C,
USB, Lightning, HDMI, LAN, power cords, and cable protectors and organisers), at any discount, unless
they are free (₹1 or less). A charger, an extension board, earphones or a mouse that comes with a
cable ("Wired Gaming Mouse | ... | 1.5M Paracord Cable") is not a cable, and a cable's line is never
read as a trap's product ("Type C Cable for iPhone 16"). A spare part or accessory on the price tracker (a vacuum filter)
comes at Quiet at most; and clothing judged by its list price alone stays at Quiet: a
₹100 kurta with an "MRP" of ₹2,499 is an everyday sight. Posts about **DigiHaat** (₹18 snack bars,
₹50 makhana packs) and about sarees are muted, from every channel, unless they show a huge discount
on a stated list price: 90% or more off, saving ₹1,000 or more.

Ignored: "upto 90% off" banners, capped coupons of ordinary shops ("80% off up to ₹160"), amounts that belong to an
offer (cashback, wallet credits, coupons, bank and exchange discounts, EMIs, recharges), ranges ("under ₹999",
"starting ₹599"), percentages that describe the product ("90% less oil"), posts that announce
the deal is over, posts older than 20 minutes (60 when the channel itself could not be read in
between), accessories and refurbished units. Every number above is a setting.

A trap only takes the price written for that product: on its own line, on a price line right
below its name, or in the same " | " part of a line. A bank discount, a budget heading ("Under
₹10k") or the price of the next product in a list is never borrowed. It does not fire on things
named after the product: cases, chargers and the like, anything "for" or "compatible with" it,
and names in which another product's name runs straight into it ("OtterBox React Apple iPhone
14 Pro").

A stated percentage only counts together with a price written beside it, or on the line next
to it. Without any price it alerts (High) from 95%, or from 90% when it is claimed for a product
you set a trap on. Daily digest posts ("Top 5 Hot Threads Today") are on the default ignore list.
A claim of 95% off or more on a price between ₹10 and ₹1,000, with no list price to back it, comes
at Quiet ("Organza Scrunchies for Women - 99% off - Rs. 199" would mean hair bands listed at
₹19,900), except for a laptop, phone, TV, AC, fridge, washing machine, camera or console, where
such a price is the price error the monitor is for. When the prices read from a post show a much
deeper discount than the post itself states (more than 10 points), the post's own figure wins and
the alert comes at Quiet.

Prices are read in the ways the watched channels really write them: "₹599", "@599", "at 599",
"Loot 239 :", "169 loot", "Blinkit | 99", "₹1 399", "Deal Price: ₹349 (MRP ₹2,599)", "₹6,294 / 80% off". When a post names its product
only in the link ("Zepto Loot / @ 9 / zepto.com/pn/haldirams-gulab-jamun..."), the words of the
link's address are read as the name.

## Where to run it

What the live checks showed (10 and 12 November 2026):

| Where | Flipkart, Croma, Reliance Digital, Ajio, Vijay Sales, brand stores | Amazon | Deal sites and channels |
|---|---|---|---|
| **GitHub Actions** (public repo, free, no card) | Readable (all 144 shipped pages) | Refused | Readable (28 of 28 channels on 12 Nov, all three deal sites) |
| **A phone at home** (Termux, plain install) | Flipkart readable; the others not tried | Refused | Telegram not reachable on that network |

- **Flipkart** answers both, and its pages are parsed correctly. It served 170 pages in a row at
  one page every 3 to 5 seconds without refusing.
- **Croma, Reliance Digital, Ajio and Shopify brand stores** hand out the product lists their own
  websites use; **Vijay Sales** pages are read as they are.
- **Amazon** answers with a JavaScript check that only a real browser passes. The monitor does not
  try to get round it, so Amazon pages ship switched off. Amazon is covered through the price
  tracker in `feeds.txt` and through the channels.
- **Myntra, Snapdeal, Tata CLiQ, JioMart, Nykaa Fashion and Meesho** refuse plain requests or build
  their pages only inside a browser. They are not read.
- **Telegram's website** is blocked or broken on some Indian networks. Where that is so, the
  channel half has to run somewhere else (`USE_CHANNELS=0` on that machine).

So GitHub Actions can run everything that works today. A device at home or a small server works
too, with the cautions below.

Run `python -m lootwatch --doctor` on the machine you are considering. It prints, for every
channel and store page, whether that machine can read it.

### At home: Termux or a PC

```bash
pkg install python git
```

```bash
git clone https://github.com/Gandham-Venkatesh/Offerzone.git && cd Offerzone && pip install -r requirements.txt
```

```bash
cp .env.example .env
```

Put your topic on the `NTFY_TOPIC=` line of `.env`, then:

```bash
python -m lootwatch --doctor
```

```bash
python -m lootwatch
```

On a PC, install Python 3.9 or newer and use `pip install -r requirements.txt -r requirements-optional.txt`.
On Android, an old phone left on its charger does this without touching your main phone's battery.

### GitHub Actions

1. The repository must be **public**: that is what makes the minutes free.
2. Pick a long random ntfy topic, e.g. `loot-7f3k9q2xw8mz4t`. Anyone who knows the name can read
   and post to it, so treat it like a password.
3. **Settings → Secrets and variables → Actions → New repository secret**: `NTFY_TOPIC`, and
   optionally `NTFY_TOKEN` (an access token from a free ntfy account; ntfy limits anonymous
   senders per IP address, and GitHub's addresses are shared with everyone else's jobs).
4. **Actions → Loot monitor → Run workflow → `test-ntfy`.** Your phone should ring three times: a
   quiet, a high and an urgent test alert, a minute apart, so you can hear each priority and
   silence it before the next one comes.
5. **Run workflow → `doctor`.** The log lists what GitHub's servers can and cannot read.
6. **Settings → Secrets and variables → Actions → Variables → New repository variable**, name
   `KEEP_RUNNING`, value `1` (a variable, not a secret), then start one run by hand
   (Run workflow → `run`). From then on the runs keep each other going: each one scans for a
   quarter of an hour, starts the next run shortly before its time is up, and leaves only once
   that run stands ready. There is always one run scanning; a changeover takes under ten seconds.
7. To stop: set `KEEP_RUNNING` to `0`. The run on duty finishes its shift, the one after it is the
   last (so it can take half an hour), and scheduled runs leave at once. To stop this minute:
   Actions → Loot monitor → "..." → Disable workflow, then cancel the run in progress. (With the
   variable deleted or empty, nothing is switched off: the schedule then starts a run whenever
   GitHub gets round to it, and each scans for one shift.)

"Run workflow" on the main branch always joins the live chain with the repository's settings and
the shipped lists (only the `doctor` and `test-ntfy` checks stand apart, and they may name other
list files). The `hops` and `shift_seconds` boxes are for test branches and are ignored on main;
on any other branch a scan is a dry run that pushes nothing.

Why not simply the schedule? GitHub starts scheduled runs late or not at all (on 10 November
2026 it started none in six hours), so the schedule in the workflow is only a safety net: it
restarts the runs if they ever stop. And why does a run wait for its successor? On 11 November a
run started its successor and left, GitHub kept that successor queued for 14 minutes and then
failed to start it, and the monitor was dark for four and a half hours. Now nobody leaves before
the relief is really there, and a relief that does not come up within three minutes is replaced.
If the scanner itself crashes during a shift, the run starts it again and still hands over; such
a run shows as failed in the Actions tab.

Cautions for this route:

- GitHub's terms say hosted runners are for work related to the repository's software, not a
  general-purpose always-on job. A personal monitor like this is a grey area, and `KEEP_RUNNING`
  makes it run without a break; if you rely on it, a home device or a server is the proper home.
- Schedules in a public repository are switched off after 60 days without repository activity.
- Run logs are public. They show deal text, never your topic or token.
- If the run on duty is itself lost (a runner that dies mid-shift), nothing scans until the
  schedule starts a run again. Set `HEALTHCHECK_URL` to hear about that within minutes.

### A small server

```bash
git clone https://github.com/Gandham-Venkatesh/Offerzone.git && cd Offerzone && bash deploy/install.sh
```

The installer prints the remaining commands. Oracle
[may reclaim Always Free instances it considers idle](https://docs.oracle.com/iaas/Content/FreeTier/resourceref.htm);
set `HEALTHCHECK_URL` so you hear about it. Telegram was
[blocked across India for a week in June 2026](https://www.aljazeera.com/amp/news/2026/6/16/india-blocks-telegram-until-monday-due-to-student-exam-fraud-concerns);
a server in an Indian region loses the channels if that happens again.

## On your phone

Install the ntfy app, subscribe to your topic, then in Android's notification settings for ntfy
give the **max priority** category a loud, distinct sound and let it override Do Not Disturb.

## The files you edit

| File | What goes in it |
|---|---|
| `watchlist.txt` | Store pages: category and brand pages with a floor, popular-product pages, product pages with a target |
| `traps.txt` | `keywords \| max price` lines |
| `feeds.txt` | Deal sites read as pages (DealsMagnet, the price tracker; DesiDime where it answers) and RSS/Atom feeds |
| `channels.txt` | Public Telegram channels |
| `.env` | Settings (copy `.env.example`). On GitHub use Secrets/Variables instead |

### Choosing category pages and floors

- Use a **leaf category** (Laptops, not Computers) sorted **cheapest first**. On Flipkart:
  `https://www.flipkart.com/laptops/pr?sid=6bo%2Cb5g&sort=price_asc`. Add a brand to it and the
  floor can sit much higher: `...&p%5B%5D=facets.brand%255B%255D%3DApple&sort=price_asc`.
- Set the **floor** well below the cheapest real product of that page.
- A new item under the floor only alerts if its list price is at least `FLOOR_MRP_RATIO` (3) times
  the floor. That is what keeps toy laptops and phone cases quiet.
- A page sorted by **biggest discount** gets the price `0`: no floor, but a product new to the
  page at 85% or more off, saving ₹2,000 or more, alerts at high priority (never max: the top of
  such a page is clearance stock at 95% off).
- Categories where cheap things with huge "MRPs" are normal (smart watches, earbuds) do not suit
  floors. Watch their **most popular** products instead (`sort=popularity`, no floor): each one
  then alerts on a fall from its own price. Traps work there too.
- `--doctor` shows the cheapest item on each page and warns if ordinary products sit under your floor.

### When a store has more pages than fit into a scan

A store is asked for a page only every few seconds (Flipkart: every 3 to 5), so about a dozen of
its pages fit into one scan. With more, the pages take turns, the one that has waited longest
first. Start a line with `!` or `!!` and the page comes round 4 or 12 times as often as an
unmarked one: `watchlist.txt` marks the big category floors `!!`, the brand floors `!`, and
leaves the popular-product pages unmarked. Whose turn it is survives a restart.

### Stores that hand out their product list

Croma, Reliance Digital, Ajio and any Shopify shop (boAt, Noise, Portronics, ...) give their
own website a plain list of products with prices. Those addresses go into `watchlist.txt` like
any page; `watchlist.txt` shows the forms that work. Sorting by price or by discount is part of
the address. A Shopify product counts at the price of its cheapest variant, in stock or not, so
that a size selling out is not mistaken for a price change.

### Invite-only channels (the big Offerzone channels)

A channel that has only an invite link (`t.me/+...`) has no public web page, so it can only be read
as a member: logged in as a Telegram account that has joined it. `channels.txt` lists the two big
Offerzone channels this way. They are skipped until you set up an account:

1. **Use a separate Telegram account if you can** (a second number). The login you give the
   monitor is a full login to that account: whoever has it can read its chats and write as it.
   Join the deal channels with that account.
2. **https://my.telegram.org** → log in → *API development tools* → create an app (any name).
   Note the **api_id** (a number) and **api_hash**.
3. **On your laptop:** `pip install telethon`, then, in this repository's folder,
   `python tools/telegram_login.py`. It asks for the api_id and api_hash, then logs in the usual
   way (phone number, the code Telegram sends, your two-step password if you have one), tells you
   which of the invite-only channels in `channels.txt` the account has joined, and prints a long
   **session string**.
4. **GitHub → Settings → Secrets and variables → Actions → New repository secret**, three times:
   `TG_API_ID`, `TG_API_HASH`, `TG_SESSION` (the session string). Never paste the session string
   anywhere else.

From the next run on, the scan log says `watching 28 Telegram channels`, and the daily note lists
the two channels with the others. What to know:

- Only the scan runs on main use the login, never a check or a test. Telegram ends a login that is
  used from two places at the same moment; if that ever happens (the daily note then shows the
  two channels failing with "Telegram ended the saved login"), run step 3 again and replace
  `TG_SESSION`.
- If the session string ever leaks, end it in Telegram: *Settings → Devices*, then make a new one.
- The two channels are found in the login's own chat list by the name `channels.txt` gives them
  (`# Offerzone`), and from then on by their chat id, kept in the state file: their invite links are
  looked up only the first time. Telegram allows those look-ups only so often, and when every run
  asked again (23-24 Nov 2026) it refused them for hours, and the channels could not be read.
- New posts are caught within seconds: the invite-only channels marked `!` (the two Offerzone
  channels) are read every 5 seconds through the login (`TG_FAST_SECONDS`), each time asking only for
  messages newer than the last one seen; the other invite-only channels once a minute, their posts
  pushed by Telegram within seconds meanwhile, so the login stays well inside what Telegram allows
  however many channels it reads. When Telegram asks for a slower pace, the next runs keep it for
  three hours.
  Telegram also pushes new posts to the login, but on a channel with millions of members that
  push came half a minute late, so asking wins. The scan log shows how late the pushes come
  (`Telegram pushed 2 post(s) 3-35s after they were published`). A post is pushed to you once,
  however it arrives.
- **Public channels are just as fast:** the login joins the public channels of `channels.txt` by
  itself (`TG_JOIN_CHANNELS`, on unless set to 0), one a minute and at most ten a run, each muted
  for good and archived so that your phone stays quiet. Telegram then pushes their posts to the
  login as they appear, and the scan log says `Telegram pushes the posts of N of the 27 public
  channels`. If Telegram asks the account to wait before joining more, the rest are joined by a
  later run. Channels not joined yet are read from their web page once a minute, as before.
- **Invite links are joined too.** Many of the big deal channels went private in October 2026 and
  left only an invite link behind. For each invite link in `channels.txt` the login is not in, the
  monitor first looks at what the link leads to and joins only a broadcast channel with 1,000
  members or more, never a group chat; muted and archived like the rest. The scan log then names
  it (`now reading the invite-only channel https://t.me/+... through the Telegram login: "Dealdost",
  3,100,000 members ...`) and shows its latest posts with what the rules make of each (`first look
  at Dealdost: #1234 2.0h ago | urgent | ...`; none of them is pushed, being old news), to judge the
  channel by. A link that cannot be joined (a request the channel's admins have to accept, a link
  that no longer works, a group chat) is left alone for a day and then tried again. A channel the
  account is in already is left as you set it, neither muted nor archived.
- **A channel on trial** (`?` in front of it in `channels.txt`) is read and joined like the rest,
  but nothing from it is pushed: every post goes to the scan log with what the rules would make of
  it (`... (on trial, not pushed): would be high (...)`), so a channel can be judged on a day of
  real posts before it reaches your phone.
- Apart from joining the channels of `channels.txt` (muted, archived), the monitor only
  reads. Telegram's terms allow that for your own account, but it is your account that does it; if
  Telegram ever asks it to slow down, it reads (or joins) less often on its own.

## Commands

```bash
python -m lootwatch --doctor      # check every source once; pushes nothing, stores nothing
```

```bash
python -m lootwatch --test-ntfy   # three test pushes a minute apart: quiet, high, urgent
```

```bash
python -m lootwatch --once --dry-run   # one real scan, alerts only printed
```

```bash
python -m lootwatch               # scan forever
```

## Settings

| Name | Default | Meaning |
|---|---|---|
| `NTFY_TOPIC` | – | **Required.** Your ntfy topic |
| `NTFY_TOKEN` | – | ntfy access token (optional) |
| `NTFY_SERVER` | `https://ntfy.sh` | Use your own ntfy server if you have one |
| `MIN_DISCOUNT` | `80` | Lowest discount in a deal post that can alert |
| `HIGH_DISCOUNT` / `URGENT_DISCOUNT` | `90` / `95` | The upper rungs of the discount ladder |
| `MIN_SAVING` / `URGENT_MIN_SAVING` / `BIG_SAVING` | `1000` / `2000` / `20000` | Rupees saved needed on each rung of the ladder for posts; a fall on a store page needs the first to count and the second to ring at max priority |
| `LOOT_PRICE` | `10` | A price this low counts even when the rupee saving is small |
| `USE_CHANNELS` / `USE_STORE_PAGES` | `1` / `1` | Switch either half off on a machine that cannot reach it |
| `WATCH_DROP_PCT` | `50` | A watched product alerts when it falls this far below the price it was seen at |
| `URGENT_DROP_PCT` | `80` | On a category page, a fall this deep is max priority; smaller ones are quieter |
| `TRACKER_DROP_PCT` | `60` | A price tracker's item alerts from this far below the product's usual price |
| `QUICK_COUPON_PCT` | `60` | A food or quick-commerce coupon alerts from this share of the order taken off. `0` turns it off |
| `QUICK_LOOTS` | `1` | Quiet alerts for "Zepto loot @ 24" posts of those apps, which carry no list price. `0` turns them off |
| `FLOOR_MRP_RATIO` | `3` | A new item under a floor needs a list price this many times the floor |
| `NEW_LISTING_DISCOUNT` | `85` | A product new to a floor page, above the floor, alerts from this % off its list price |
| `ABSENCE_HOURS` | `24` | A reported product that was gone this long is judged afresh when it returns |
| `MAX_POST_AGE_MIN` | `20` | Channel posts older than this are ignored |
| `LATE_POST_MIN` | `60` | After a gap in reading a channel (a late run), posts up to this old are still judged |
| `DEDUP_HOURS` | `12` | Same deal is not pushed again within this time unless it gets 10% cheaper; a product known by its Amazon, Flipkart, Myntra or Ajio product id, not within 3 days; one quick-commerce loot posted by two channels in words of their own ("BigBasket Loot: Upto 98% Off On Society Tea", "99% off on - Bigbasket Society Tea Loot") counts as one deal |
| `TRENDING_CHANNELS` / `TRENDING_WINDOW_MIN` | `3` / `20` | When a quiet deal turns urgent |
| `DAILY_ALERT_LIMIT` | `200` | Stays inside ntfy.sh's free daily allowance; urgent alerts get the last of it |
| `CHECK_INTERVAL` | `60` | Seconds between scans |
| `SITE_GAPS` | – | Seconds between two requests to a site, e.g. `flipkart.com=3-5,t.me=0.5-1`. Slower is always safe |
| `LOOP_SECONDS` | `840` | GitHub Actions only: how long one run scans before it hands over |
| `HEARTBEAT_HOUR` | `9` | Daily "still running" note (India time). `-1` turns it off |
| `HEALTHCHECK_URL` | – | A [healthchecks.io](https://healthchecks.io) ping URL; it alerts you if scans stop |
| `TG_API_ID` / `TG_API_HASH` / `TG_SESSION` | – | Secrets: the Telegram login for invite-only channels (see "Invite-only channels") |
| `TG_FAST_SECONDS` | 5 | Seconds between two readings of the invite-only channels through that login (0: once a scan) |
| `TG_JOIN_CHANNELS` | 1 | The login joins the channels of `channels.txt` it is not in, public ones and invite links (muted, archived), so their posts come within seconds; 0: never |
| `MRP_MIN_DISCOUNT` | 85 | A deal judged only by its list price alerts from this % off (or `MIN_DISCOUNT`, if higher) |
| `FAVOURITE_MIN_DISCOUNT` | 70 | The same for things on `favourites.txt`, which also ring one step louder |
| `FAST_FEED_SECONDS` | 20 | Seconds between two readings of a `feeds.txt` line marked `!` (0: once a scan); a site that refuses is read half as often from then on |
| `EXTRA_STORE_DOMAINS` | – | Extra shop domains to treat as trusted, comma separated |
| `DENY_WORDS` | see `.env.example` | Posts containing these are ignored |
| `CURL_IMPERSONATE` | `chrome124` | Browser profile presented to stores when `curl_cffi` is installed |

## It tells you when it is not working

- **"Loot monitor started"** after the first scan that can reach your phone.
- **"Loot monitor is blind"** (high priority) when not one deal channel or feed could be read for
  three scans in a row, whether they failed, refused or hung; "recovered" when they return.
- **"Store pages unreadable (site)"** (high priority) when no watched page of a store could be
  read for ten scans in a row, and "readable again" when that ends.
- Any single source that fails ten scans in a row is reported once, with the reason, and again
  only after it has worked in between. A listing whose products still load but no longer show
  list prices counts as failing: the floor rules cannot work without them.
- A daily summary: scans done, posts read, alerts sent, sources working, failing or not read yet.
- Undelivered alerts are kept and retried for 30 minutes, then given up (a late loot alert is
  noise). On GitHub Actions the run that had to give one up ends with an error and shows as
  failed: with a wrong or expired `NTFY_TOKEN` that is how you find out. On a server the service
  log says so (`journalctl -u lootwatch`). If the product is still at that price on a store
  page, a fresh alert is raised.
- A site that answers 403/429/503 or shows a CAPTCHA is left alone for a growing cool-down. The
  monitor does not solve CAPTCHAs or rotate proxies.

## What has and has not been verified

- **Test suite** (`python -m unittest discover -s tests -t .`, Python 3.11 to 3.13): price parsing,
  priorities, floors, traps, de-duplication across sources and restarts, link trust, delivery
  retries, the daily budget, the health alarms, and the whole path from a change on a simulated
  page to the JSON that reaches ntfy. A review by independent readers found 24 ways an alert
  could be missed or ring falsely; each has a test built from the reviewer's own example. A
  second reviewer then went through the rewritten post rules with 51,840 generated posts in the
  real channels' layouts and found eight more problems, which are fixed and tested too; the
  monitor code and the store-page rules were not re-reviewed after their rewrite.
- **Live, 10 to 12 November 2026:** the Telegram reader, the Flipkart category and search pages,
  and the priority ladder replayed against about 200 real posts. On the 12th, from GitHub's
  servers: all 28 channels, the three deal sites and all 144 store pages of the shipped lists
  were read and parsed, a scan of everything completed inside its minute seven times running with
  no source failing, and three runs in a row handed over to each other with 7 to 8 seconds
  between one's last scan and the next one's first.
- **Not verified:** Amazon (refused), how many minutes a store's listing pages lag a price change,
  how long the price tracker takes to notice a drop, and how the new sources behave over weeks
  (how often the discount-sorted pages and the tracker ring for something not worth it).

Price errors are often cancelled by the store, and channel posts can be wrong or bait. Check the
price on the store's own page before paying, and never pay on a site you do not recognise.
