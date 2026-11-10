# Loot monitor

Catches price errors on Indian stores and rings your phone through [ntfy](https://ntfy.sh).
It needs no paid service and no API keys, and runs on a device at home, on GitHub Actions or on
a small server.

## What it does

Each scan (default: every 60 seconds) it looks in three places, in this order of importance:

1. **Store category pages with a price floor** (`watchlist.txt`). A page such as "Laptops on
   Flipkart, cheapest first" normally starts with the cheapest real laptops. A ₹60,000 laptop
   mispriced at ₹600 lands at the top of it. The monitor alerts when something appears under the
   page's floor whose list price says it should cost far more, when something it has been
   watching falls to it, and when a product it has never seen turns up on the page at 85% or
   more off its own list price (a dropped digit: ₹74,990 shown as ₹7,499).
   This is read from the store itself; no deal channel is involved. It is the automated form of
   the "masterlinks" that deal hunters check by hand.
2. **Products and keywords you name.** Product pages in `watchlist.txt` alert on your target
   price or on a 50% fall from what the monitor has been seeing. `traps.txt` holds lines like
   `nothing phone | 6000`: any page or post showing that product at or under ₹6,000 alerts.
3. **Public deal channels** (`channels.txt`), as a safety net. Posts are parsed for price, MRP
   and discount, de-duplicated across channels, and filtered hard. Empty the file to turn this off.

## What it cannot do

- **It cannot watch every product.** Amazon and Flipkart list hundreds of millions. It watches
  the pages you give it, about 6 pages per store per minute. Choose them deliberately. An error
  on a product that is not among the first 20 to 40 results of a watched page is invisible to it.
- **It cannot promise to beat the channels.** A store's search pages can lag a price change by
  minutes, and an error on a page you are not watching is invisible to it.
- **It cannot force a store to answer.** Stores often refuse requests from cloud servers
  outright. See "Where to run it".
- **Blinkit, Zepto and Swiggy Instamart are not read directly.** Their prices come from app
  APIs tied to your delivery location and refuse plain requests. The tools that do work for them
  run inside your own logged-in browser (search GitHub for "PriceDrop Hunter" or "zepto-deals").
  Here they are covered only through the quick-commerce channels in `channels.txt`.

## How loud each alert is

**From a store page:**

| Alert | ntfy priority | When |
|---|---|---|
| **Urgent** | 5 (max) | something under a category page's floor · a trap (with a list price that fits the device) · your target price · a fall of `WATCH_DROP_PCT` from the price the monitor has been seeing · a product new to a floor page at 95% or more off its list price |
| **High** | 4 | a product new to a floor page at `NEW_LISTING_DISCOUNT` (85%) or more off its list price · a product page at 80% or more off on its first check |
| **Quiet** | 3 | a listing named like a trap product whose card shows no list price that fits it (spare parts are named after the phone too) |

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

An alert is held at **High**, whatever the discount, when the reading itself is unsure: the two
prices are not labelled, a second amount stands where the price should be, the price needs a
qualifying purchase ("₹1 on orders above ₹2,999"), the post hints that the deal may be over, or
its link could not be checked. A link that leads to a site that is not a known store keeps the
alert at Quiet, and copies of the post in other channels never lift it above that.

Ignored: "upto 90% off" banners, capped coupons ("80% off up to ₹160"), amounts that belong to an
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

A stated percentage only counts together with a price written beside it. Daily digest posts
("Top 5 Hot Threads Today") are on the default ignore list.

## Where to run it

What the first live checks showed (10 November 2026):

| Where | Flipkart pages | Amazon pages | Deal channels |
|---|---|---|---|
| **GitHub Actions** (public repo, free, no card) | Readable | Refused | Readable |
| **A phone at home** (Termux, plain install) | Readable | Refused | Not reachable on that network |

- **Flipkart** answers both, and its pages are parsed correctly.
- **Amazon** answers both with a JavaScript check that only a real browser passes. The monitor
  does not try to get round it, so Amazon pages ship switched off. Amazon deals still arrive
  through the channels.
- **Telegram's website** is blocked or broken on some Indian networks. Where that is so, the
  channel half has to run somewhere else (`USE_CHANNELS=0` on that machine).

So GitHub Actions can run everything that works today. A device at home or a small server gives a
steadier 60-second loop, with the cautions below.

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
4. **Actions → Loot monitor → Run workflow → `test-ntfy`.** Your phone should ring.
5. **Run workflow → `doctor`.** The log lists what GitHub's servers can and cannot read.
6. The schedule is on: a run starts every 5 minutes and scans once a minute until the next is due.
   To pause it: Actions → Loot monitor → "..." → Disable workflow.
7. If no run starts by itself (GitHub's scheduler can leave a new repository waiting for hours, and
   on 10 November 2026 it started none in six): **Settings → Secrets and variables → Actions →
   Variables → New repository variable**, name `KEEP_RUNNING`, value `1`, then start one run by
   hand (Run workflow → `run`). Each run then starts the next when it ends. Set the variable to
   `0`, or cancel the run in progress, to stop.

Cautions for this route:

- Scheduled runs start late when GitHub is busy, sometimes by many minutes.
- GitHub's terms say hosted runners are for work related to the repository's software, not a
  general-purpose always-on job. A personal monitor like this is a grey area, and `KEEP_RUNNING`
  makes it run without a break; if you rely on it, a home device or a server is the proper home.
- Schedules in a public repository are switched off after 60 days without repository activity.
- Run logs are public. They show deal text, never your topic or token.

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
| `watchlist.txt` | Store pages: category pages with a floor, product pages with a target |
| `traps.txt` | `keywords \| max price` lines |
| `channels.txt` | Public Telegram channels (optional safety net) |
| `feeds.txt` | RSS/Atom feeds of deal sites (optional) |
| `.env` | Settings (copy `.env.example`). On GitHub use Secrets/Variables instead |

### Choosing category pages and floors

- Use a **leaf category** (Laptops, not Computers) with a **price cap** and **cheapest-first** sort.
  On Flipkart: `https://www.flipkart.com/laptops/pr?sid=6bo%2Cb5g&sort=price_asc`. `watchlist.txt` ships
  with phones, laptops, TVs, tablets, fridges, washing machines, air conditioners and monitors.
- Set the **floor** well below the cheapest real product of that category.
- A new item under the floor only alerts if its list price is at least `FLOOR_MRP_RATIO` (3) times
  the floor. That is what keeps toy laptops and phone cases quiet.
- Categories where cheap things with huge "MRPs" are normal (smart watches, earbuds) do not suit
  floors. Use traps for those.
- `--doctor` shows the cheapest item on each page and warns if ordinary products sit under your floor.

## Commands

```bash
python -m lootwatch --doctor      # check every source once; pushes nothing, stores nothing
```

```bash
python -m lootwatch --test-ntfy   # one test push
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
| `WATCH_DROP_PCT` | `50` | Fall against a watched product's own usual price |
| `FLOOR_MRP_RATIO` | `3` | A new item under a floor needs a list price this many times the floor |
| `NEW_LISTING_DISCOUNT` | `85` | A product new to a floor page, above the floor, alerts from this % off its list price |
| `ABSENCE_HOURS` | `24` | A reported product that was gone this long is judged afresh when it returns |
| `MAX_POST_AGE_MIN` | `20` | Channel posts older than this are ignored |
| `LATE_POST_MIN` | `60` | After a gap in reading a channel (a late run), posts up to this old are still judged |
| `DEDUP_HOURS` | `12` | Same deal is not pushed again within this time unless it gets 10% cheaper |
| `TRENDING_CHANNELS` / `TRENDING_WINDOW_MIN` | `3` / `20` | When a quiet deal turns urgent |
| `DAILY_ALERT_LIMIT` | `200` | Stays inside ntfy.sh's free daily allowance; urgent alerts get the last of it |
| `CHECK_INTERVAL` | `60` | Seconds between scans |
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
  could be missed or ring falsely; each has a test built from the reviewer's own example.
- **Live, 10 November 2026:** the Telegram reader on ten channels, the Flipkart category and
  search pages, and the priority ladder replayed against about 200 real posts.
- **Not verified:** Amazon (refused), Myntra and other stores (never tried live), quick commerce
  (not read at all), and how many minutes Flipkart's listing pages lag a price change.

Price errors are often cancelled by the store, and channel posts can be wrong or bait. Check the
price on the store's own page before paying, and never pay on a site you do not recognise.
