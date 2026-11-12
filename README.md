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
   or more from the price the monitor has been seeing alerts.
3. **Products and keywords you name.** Product pages in `watchlist.txt` alert on your target
   price. `traps.txt` holds lines like `nothing phone | 6000`: any page or post showing that
   product at or under ₹6,000 alerts.
4. **Deal sites and public deal channels** (`feeds.txt`, `channels.txt`). DesiDime's and
   DealsMagnet's new-deal pages are read directly, minutes before those deals are repeated on
   Telegram; a price tracker's live list of drops covers Amazon; and 28 public Telegram channels
   are read as a safety net. Posts are parsed for price, MRP and discount, de-duplicated across
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
- **It cannot read invite-only Telegram channels.** Only channels with a public preview page
  can be read without a Telegram account. The largest "Offerzone" channels are invite-only.
- **Blinkit, Zepto and Swiggy Instamart are not read directly.** Their prices come from app
  APIs tied to your delivery location and refuse plain requests. The tools that do work for them
  run inside your own logged-in browser (search GitHub for "PriceDrop Hunter" or "zepto-deals").
  Here they are covered through DesiDime, where their coupons and loots are posted within
  minutes, and through the quick-commerce channels in `channels.txt`.

## How loud each alert is

**From a store page:**

| Alert | ntfy priority | When |
|---|---|---|
| **Urgent** | 5 (max) | something under a category page's floor · a trap (with a list price that fits the device) · your target price · a fall of 80% or more (`URGENT_DROP_PCT`) from the price the monitor saw it at · on a product page you added yourself, a fall of 50% · a product new to a floor page at 95% or more off its list price, saving ₹2,000 or more |
| **High** | 4 | on a category page, a fall of 65% to 80% · a product new to a floor page, or to a page sorted by discount, at `NEW_LISTING_DISCOUNT` (85%) or more off its list price, saving ₹2,000 or more · a product page at 80% or more off on its first check |
| **Quiet** | 3 | on a category page, a fall of 50% to 65% (`WATCH_DROP_PCT`) · a listing named like a trap product whose card shows no list price that fits it (spare parts are named after the phone too) |

A fall on a category page only counts when it saves ₹1,000 or more (`MIN_SAVING`): thousands of
products are followed, and a ₹449 gadget going to ₹199 is nobody's loot.

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
| **Urgent** | 5 (max) | 95% off or more, saving ₹2,000 or more · 90% off or more, saving ₹20,000 or more · the post says price error / glitch · a trap |
| **High** | 4 | 90% off or more, saving ₹1,000 or more, or priced ₹10 or less (a "₹1 deal") · only a percentage is given and it is 95% or more |
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

Each needs ₹2,000 or more saved, and goes one step quieter when the product has been cheaper
than this before.

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
| **High** | 4 | a coupon that takes 80% or more off the order it needs ("₹160 off on ₹199") |
| **Quiet** | 3 | a coupon worth 60% to 80% of its order (`QUICK_COUPON_PCT`) · a post that calls something a loot and names a price ("Zepto loot @ 24"), although no list price can be checked (`QUICK_LOOTS`) |

A percentage coupon is counted at what its cap leaves of it: "80% off up to ₹50 on orders above
₹499" is a tenth of the bill and stays quiet. These never ring at max priority, and they are
usually tied to a city or an account.

Ignored: "upto 90% off" banners, capped coupons of ordinary shops ("80% off up to ₹160"), amounts that belong to an
offer (cashback, coupons, bank and exchange discounts, EMIs, recharges), ranges ("under ₹999",
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

Prices are read in the ways the watched channels really write them: "₹599", "@599", "at 599",
"Loot 239 :", "Deal Price: ₹349 (MRP ₹2,599)", "₹6,294 / 80% off". When a post names its product
only in the link ("Zepto Loot / @ 9 / zepto.com/pn/haldirams-gulab-jamun..."), the words of the
link's address are read as the name.

## Where to run it

What the live checks showed (10 and 12 November 2026):

| Where | Flipkart, Croma, Reliance Digital, Ajio, Vijay Sales, brand stores | Amazon | Deal sites and channels |
|---|---|---|---|
| **GitHub Actions** (public repo, free, no card) | Readable (all 144 shipped pages) | Refused | Readable (28 of 28 channels, all three deal sites) |
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
7. To stop: Actions → Loot monitor → "..." → Disable workflow, then cancel the run in progress.
   Setting `KEEP_RUNNING` to `0` only stops the runs from starting each other; the schedule in
   the workflow still starts a run whenever GitHub gets round to it.

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
| `feeds.txt` | Deal sites read as pages (DesiDime, DealsMagnet, the price tracker) and RSS/Atom feeds |
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
| `MIN_SAVING` / `URGENT_MIN_SAVING` / `BIG_SAVING` | `1000` / `2000` / `20000` | Rupees saved needed on each rung |
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
| `DEDUP_HOURS` | `12` | Same deal is not pushed again within this time unless it gets 10% cheaper |
| `TRENDING_CHANNELS` / `TRENDING_WINDOW_MIN` | `3` / `20` | When a quiet deal turns urgent |
| `DAILY_ALERT_LIMIT` | `200` | Stays inside ntfy.sh's free daily allowance; urgent alerts get the last of it |
| `CHECK_INTERVAL` | `60` | Seconds between scans |
| `SITE_GAPS` | – | Seconds between two requests to a site, e.g. `flipkart.com=3-5,t.me=0.5-1`. Slower is always safe |
| `LOOP_SECONDS` | `840` | GitHub Actions only: how long one run scans before it hands over |
| `HEARTBEAT_HOUR` | `9` | Daily "still running" note (India time). `-1` turns it off |
| `HEALTHCHECK_URL` | – | A [healthchecks.io](https://healthchecks.io) ping URL; it alerts you if scans stop |
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
