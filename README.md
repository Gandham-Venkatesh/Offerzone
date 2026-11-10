# Loot monitor

Catches price errors on Indian stores and rings your phone through [ntfy](https://ntfy.sh).
It needs no paid service and no API keys, and runs on a device at home, on GitHub Actions or on
a small server.

## What it does

Each scan (default: every 60 seconds) it looks in three places, in this order of importance:

1. **Store category pages with a price floor** (`watchlist.txt`). A page such as "Laptops on
   Amazon, capped at ₹5,000, cheapest first" normally holds nothing but odds and ends. A ₹60,000
   laptop mispriced at ₹600 lands on it. The monitor alerts when something appears there whose
   list price says it should cost far more, or when something it has been watching falls to it.
   This is read from the store itself; no deal channel is involved. It is the automated form of
   the "masterlinks" that deal hunters check by hand.
2. **Products and keywords you name.** Product pages in `watchlist.txt` alert on your target
   price or on a 50% fall from what the monitor has been seeing. `traps.txt` holds lines like
   `nothing phone | 6000`: any page or post showing that product at or under ₹6,000 alerts.
3. **Public deal channels** (`channels.txt`), as a safety net. Posts are parsed for price, MRP
   and discount, de-duplicated across channels, and filtered hard. Empty the file to turn this off.

## What it cannot do

- **It cannot watch every product.** Amazon and Flipkart list hundreds of millions. It watches
  the pages you give it, about 6 pages per store per minute. Choose them deliberately.
- **It cannot promise to beat the channels.** A store's search pages can lag a price change by
  minutes, and an error on a page you are not watching is invisible to it.
- **It cannot force a store to answer.** Stores often refuse requests from cloud servers
  outright. See "Where to run it".
- **Blinkit, Zepto and Swiggy Instamart are not read directly.** Their prices come from app
  APIs tied to your delivery location and refuse plain requests. The tools that do work for them
  run inside your own logged-in browser (search GitHub for "PriceDrop Hunter" or "zepto-deals").
  Here they are covered only through the quick-commerce channels in `channels.txt`.

## How loud each alert is

| Alert | ntfy priority | When |
|---|---|---|
| **Urgent** | 5 (max) | 95% off or more (`URGENT_DISCOUNT`) · at least `MIN_DISCOUNT` off **and** saving `URGENT_MIN_SAVING` or more · something under a category page's floor · a trap fires · a watched product hits your target or falls `WATCH_DROP_PCT` from its usual price · a post says price error / glitch · a quiet deal appears in 3 channels within 20 minutes |
| **High** | 4 | At least `MIN_DISCOUNT` off but small-ticket or stated less clearly · an urgent-looking post whose short link could not be checked (shown with a note) |
| **Quiet** | 3 | A post whose link leads to a site that is not a known store. Shown with a warning, never loud: this is where look-alike scam shops turn up |

Ignored: "upto 90% off" banners, cashback/coupon/recharge amounts, posts that say the deal is
over, posts older than 20 minutes, accessories and refurbished units on store pages.

## Where to run it

The stores decide this more than the code does. Reports from people who do this, and the
warnings in similar open-source projects, agree that Amazon, Flipkart and the quick-commerce
apps often refuse data-centre addresses on the first request, while an ordinary home or mobile
connection is tolerated at a gentle pace. So:

| Where | Store pages | Channels | Notes |
|---|---|---|---|
| **A device at home** (old Android phone with Termux, a PC or laptop that stays on, a Raspberry Pi) | Best chance | Yes | A true 60-second loop. The catch is that it must stay on and online |
| **GitHub Actions** (public repo) | Often refused | Yes | Free, no card. Runs start late when GitHub is busy. See the cautions below |
| **A small cloud server** | Often refused | Yes | Always on. Oracle's free tier has Mumbai and Hyderabad regions but needs a card |

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
6. Switch the schedule on: in `.github/workflows/monitor.yml`, remove the `# ` in front of the
   `schedule:` and `- cron:` lines.

Cautions for this route:

- Scheduled runs start late when GitHub is busy, sometimes by many minutes.
- GitHub's terms say hosted runners are for work related to the repository's software, not a
  general-purpose always-on job. A personal monitor like this is a grey area; if you rely on it,
  a home device or a server is the proper home.
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
  On Amazon: `https://www.amazon.in/s?rh=n%3A<category id>%2Cp_36%3A-<cap in paise>&s=price-asc-rank`.
  `watchlist.txt` ships with the IDs for phones, laptops, TVs, tablets, fridges, washing machines,
  air conditioners and monitors.
- Set the **floor** well below the cheapest real product of that category.
- A new item under the floor only alerts if its list price is at least `FLOOR_MRP_RATIO` (4) times
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
| `MIN_DISCOUNT` | `80` | Percent off needed for any alert |
| `URGENT_DISCOUNT` | `95` | Percent off that makes an alert max priority by itself |
| `URGENT_MIN_SAVING` | `2000` | Rupees saved that make an alert max priority |
| `WATCH_DROP_PCT` | `50` | Fall against a watched product's own usual price |
| `FLOOR_MRP_RATIO` | `4` | A new item under a floor needs a list price this many times the floor |
| `MAX_POST_AGE_MIN` | `20` | Channel posts older than this are ignored |
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
- **"Loot monitor is blind"** (high priority) if every deal channel and feed fails three scans in a row,
  and "recovered" when they return.
- Any single source that fails ten scans in a row is reported once, with the reason.
- A daily summary: scans done, posts read, alerts sent, sources failing.
- Undelivered alerts are kept and retried, then dropped after 30 minutes (a late loot alert is noise).
- A site that answers 403/429/503 or shows a CAPTCHA is left alone for a growing cool-down. The
  monitor does not solve CAPTCHAs or rotate proxies.

## What has and has not been verified

Verified by the test suite (`python -m unittest discover -s tests -t .`, Python 3.11 to 3.13):
price parsing, alert priorities, floors, traps, de-duplication across sources and restarts,
trusted-link handling, delivery retries, the daily budget, the health alarms, and the whole path
from a change on a (local, simulated) page to the JSON that reaches ntfy.

**Not yet verified against the live sites.** The first `--doctor` run on your real host is the
actual test of which channels are readable, whether the stores answer that host, and whether
their pages still match the parsers. The Amazon category IDs and URL parts in `watchlist.txt`
were confirmed from Amazon's own indexed URLs; the Flipkart ones are from memory.

Price errors are often cancelled by the store, and channel posts can be wrong or bait. Check the
price on the store's own page before paying, and never pay on a site you do not recognise.
