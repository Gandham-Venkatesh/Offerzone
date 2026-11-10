# Loot monitor

Watches Indian deal channels, feeds and store pages around the clock and rings your phone
through [ntfy](https://ntfy.sh) when something looks like a price error. Runs in the cloud
(GitHub Actions or a small free server) or in Termux. Needs no paid service and no API keys.

## What it does, and what it cannot do

**It does three things each scan (default: every 60 seconds):**

1. **Reads many loot channels at once** through Telegram's public web preview, works out the
   price, MRP and discount from each new post, and pushes only the ones that matter. The same
   deal posted in five channels reaches you once.
2. **Price-checks your watchlist** (`watchlist.txt`) directly on the store and alerts when a
   product falls far below the price it has been seeing, or under your target.
3. **Springs keyword traps** (`traps.txt`): "nothing phone at ₹6,000 or less" fires on a post that
   only says *Nothing Phone @ 400*, with no MRP and no "price error" wording.

**It cannot scan "every product" on Amazon or Flipkart.** Nobody can from one free machine: that is
hundreds of millions of pages. Big trackers do it with affiliate data feeds, millions of
browser-extension users and paid proxy networks. This comes at it from the other end: those
trackers and tipsters feed the loot channels, and this reads all of them every minute and filters
hard. Your watchlist and traps then cover the things you care about most, directly.

**It will not be first every time.** A channel post reaches you roughly one scan after it appears.
What changes is that you no longer have to be staring at Telegram when it does.

## How loud each alert is

| Alert | ntfy priority | When |
|---|---|---|
| **Urgent** | 5 (max) | The post says price error / glitch · a trap fires · a stated price-and-MRP pair is at least `MIN_DISCOUNT` off **and** saves at least `URGENT_MIN_SAVING` · a watched product hits your target or drops `WATCH_DROP_PCT` from its usual price · a quiet deal shows up in 3 channels within 20 minutes |
| **High** | 4 | At least `MIN_DISCOUNT` off, but small-ticket or stated less clearly · an urgent-looking deal whose short link could not be checked (shown with a note) |
| **Quiet** | 3 | The deal's link leads to a site that is not a known store. Shown with a warning, never loud: this is where look-alike scam shops turn up |

Ignored: "upto 90% off" banners, cashback/coupon/recharge amounts, expired posts, posts older
than 20 minutes, and anything matching `DENY_WORDS`.

## Setup A: GitHub Actions (free, no card, about ten minutes)

1. Create a **public** repository and push these files to it. (Public is what makes the minutes free.)
2. Pick a long random ntfy topic, e.g. `loot-7f3k9q2xw8mz4t`. Anyone who knows the name can read
   and post to it, so treat it like a password and do not reuse an old one.
3. In the repository: **Settings → Secrets and variables → Actions → New repository secret**
   - `NTFY_TOPIC` = your topic
   - `NTFY_TOKEN` = an access token from a free ntfy account (optional but recommended here:
     ntfy limits anonymous senders per IP address, and GitHub's addresses are shared with
     everyone else's jobs; with a token the allowance should be counted against your account)
4. **Actions tab → Loot monitor → Run workflow → mode `test-ntfy`.** Your phone should ring.
5. **Run workflow → mode `doctor`.** Open the run's log: it lists every channel and watchlist page
   as OK or FAIL from GitHub's network. Remove what fails from the `.txt` files.
6. Nothing else to do. The schedule scans every 5 minutes, and each run keeps scanning once a
   minute until the next run is due.

Things to know about this route:

- GitHub starts scheduled runs late when it is busy, sometimes by many minutes. There are gaps.
  For a monitor where minutes matter, Setup B is the better home.
- GitHub switches off schedules in a public repository after 60 days without repository activity
  and emails you about it. Any small commit, or re-enabling it in the Actions tab, keeps it going.
- The run logs are public. They show deal text, never your topic or token.

## Setup B: a small always-on server (best results)

Any Linux machine that stays on works: a free-tier cloud VM, a Raspberry Pi, an old laptop.
Scans run every 60 seconds with no gaps.

```bash
git clone https://github.com/<you>/<repo>.git loot-monitor
```

```bash
cd loot-monitor && bash deploy/install.sh
```

The installer prints the remaining three commands (set the topic in `.env`, test the push,
start the service). After editing `.env` or a `.txt` list: `sudo systemctl restart lootwatch`.

Oracle Cloud's Always Free tier is the usual choice for a free VM, and it has Mumbai and
Hyderabad regions. Two cautions: sign-up needs a card, and Oracle
[may reclaim Always Free instances it considers idle](https://docs.oracle.com/iaas/Content/FreeTier/resourceref.htm)
(CPU under 20% for 7 days). Set `HEALTHCHECK_URL` (below) so you hear about it if that happens.

One thing to weigh when choosing a region: Telegram was
[blocked across India for a week in June 2026](https://www.aljazeera.com/amp/news/2026/6/16/india-blocks-telegram-until-monday-due-to-student-exam-fraud-concerns).
If that happens again, a server in an Indian region loses the channels (you get the "blind" alarm),
while one outside India, such as a GitHub runner, keeps reading them.

## Setup C: Termux, as before

```bash
pkg install python git
```

```bash
git clone https://github.com/<you>/<repo>.git loot-monitor && cd loot-monitor && pip install -r requirements.txt
```

```bash
python -m lootwatch --doctor
```

`python termux_price_error_monitor.py ...` still works and takes the same options. Because a phone
has a normal home/mobile address, store pages that refuse cloud servers usually load here.

## On your phone

Install the ntfy app, subscribe to your topic, then in Android's notification settings for ntfy
give the **max priority** category a loud, distinct sound and let it override Do Not Disturb.
That is what makes an urgent alert impossible to sleep through while everything else stays polite.

## The files you edit

| File | What goes in it |
|---|---|
| `channels.txt` | Public Telegram channels. **Put the ones you already trust first.** |
| `traps.txt` | `keywords \| max price` lines |
| `watchlist.txt` | Product or search URLs, optionally `\| target price \| label` |
| `feeds.txt` | RSS/Atom feeds of deal sites (optional) |
| `.env` | Settings, on a server or in Termux (copy `.env.example`). On GitHub use Secrets/Variables |

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

`--doctor` also shows, for each channel, how many of the posts on its page right now would have
alerted, and which link hosts those posts use. Run it whenever you change thresholds or channels.

## Settings

| Name | Default | Meaning |
|---|---|---|
| `NTFY_TOPIC` | – | **Required.** Your ntfy topic |
| `NTFY_TOKEN` | – | ntfy access token (optional) |
| `NTFY_SERVER` | `https://ntfy.sh` | Use your own ntfy server if you have one |
| `MIN_DISCOUNT` | `80` | Percent off needed for any alert |
| `URGENT_MIN_SAVING` | `2000` | Rupees saved needed for the max-priority alert |
| `WATCH_DROP_PCT` | `50` | Watchlist: drop against the product's own usual price |
| `MAX_POST_AGE_MIN` | `20` | Posts older than this are ignored |
| `DEDUP_HOURS` | `12` | Same deal is not pushed again within this time unless it gets 10% cheaper |
| `TRENDING_CHANNELS` / `TRENDING_WINDOW_MIN` | `3` / `20` | When a quiet deal turns urgent |
| `DAILY_ALERT_LIMIT` | `200` | Keeps you inside ntfy.sh's free daily allowance; urgent alerts get the last of it |
| `CHECK_INTERVAL` | `60` | Seconds between scans |
| `HEARTBEAT_HOUR` | `9` | Daily "still running" note (India time). `-1` turns it off |
| `HEALTHCHECK_URL` | – | A [healthchecks.io](https://healthchecks.io) ping URL; it alerts you if scans stop |
| `EXTRA_STORE_DOMAINS` | – | Extra shop domains to treat as trusted, comma separated |
| `DENY_WORDS` | see `.env.example` | Posts containing these are ignored |

## It tells you when it is not working

A monitor that fails silently is worse than none. This one:

- sends **"Loot monitor started"** after its first ever scan, with how many sources were readable;
- sends **"Loot monitor is blind"** (high priority) if every deal source fails three scans in a row,
  and "recovered" when they come back;
- reports any single source that fails ten scans in a row, once;
- sends a daily summary: scans done, posts read, alerts sent, sources failing;
- keeps undelivered alerts and retries them, and drops them after 30 minutes (a late loot alert is noise);
- leaves a site alone for a growing cool-down when it answers 403/429/503 or shows a CAPTCHA,
  instead of hammering it. It does not solve CAPTCHAs or rotate proxies.

## What has and has not been verified

Verified by the test suite (`python -m unittest discover -s tests -t .`, Python 3.11 to 3.13):
text parsing of prices/MRPs/discounts, alert tiers, traps, de-duplication across channels and
restarts, trusted-link handling, delivery retries, the daily budget, the health alarms, and the
whole path from a new post on a (local, simulated) channel page to the JSON that reaches ntfy.

**Not yet verified against the live sites:** the machine this was written on had no access to
Telegram, ntfy or the stores. So the first `--doctor` run on your real host is the actual test of
(a) which channels in `channels.txt` are readable and active, (b) whether Amazon and Flipkart serve
that host at all, and (c) whether their pages still match the parsers. Expect to prune the lists
after that run. Quick-commerce pages (Blinkit, Zepto, Instamart) are not expected to work from a
plain fetch; their coverage comes from the quick-commerce channels.

Price errors are often cancelled by the store, and channel posts can be wrong or bait. Check the
price on the store's own page before paying, and never pay on a site you do not recognise.
