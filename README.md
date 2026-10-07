# Reddit → WhatsApp tracker

Watches a set of subreddits and sends you a WhatsApp message the moment a new
post appears.

```
🔥 WTS · Gaming PC
r/IndianPCHardware
*[WTS] [Mumbai] Gaming PC (i5-14600K / RTX 4070 Super) - 95,000*
u/someone · Sale
https://redd.it/1abcde
```

- One request per check covers all your subreddits.
- **Highlights:** posts matching your categories (e.g. WTS, Gaming PC) get a
  🔥 header and are always sent individually. Everything else still comes
  through as 🆕.
- **Priority alerts:** choose which categories buzz your phone, and
  optionally drop everything else. The default `.env.example` sends **only
  gaming PCs and graphics cards that are for sale**
  (`ONLY_HIGHLIGHTS=WTS+Gaming PC, WTS+GPU`), each with a 🚨 and a sound,
  plus silent "what's my PC worth?" posts from owners who might sell.
- A listing cross-posted to several of your subs is sent once, naming all of them.
- Optional keyword filter (title or body) if you only want matching posts.
- Remembers what it already sent (`state.json`), so restarts don't cause duplicates.
- The first run stays quiet and only marks the current posts as seen.
- A burst of posts arrives as one combined message instead of many separate ones.
- A failed send is retried on the next check.
- Backs off when Reddit rate-limits it.

## 1. Set up notifications

| Option | Cost | Setup | Catch |
|---|---|---|---|
| [CallMeBot](#callmebot) | Free | 2 min | The bot doesn't always reply with a key |
| [WAHA](#waha-self-hosted-whatsapp) | Free | 15 min, Docker | Needs a second WhatsApp number to send from |
| [Telegram](#telegram) | Free | 3 min | It's Telegram, not WhatsApp |
| [Twilio](#twilio) | Trial credit, then paid | 10 min | Sandbox needs you to message it daily |

Then check it with `python reddit_tracker.py --test-message`.

### CallMeBot

1. Save **+34 623 80 11 90** as a contact (their number as of October 2026;
   it changes from time to time, so check
   [their page](https://www.callmebot.com/blog/free-api-whatsapp-messages/)
   for the current one).
2. Send it exactly: `I allow callmebot to send me messages`
3. It replies with your API key. Set `WHATSAPP_PROVIDER=callmebot`,
   `CALLMEBOT_PHONE=+91…` and `CALLMEBOT_APIKEY=…`.

**No reply?** CallMeBot says that if the key doesn't arrive within 2 minutes,
you should try again **after 24 hours**. Sending more messages in the meantime
doesn't help. Use one of the options below meanwhile.

### WAHA (self-hosted WhatsApp)

[WAHA](https://waha.devlike.pro) runs WhatsApp Web in a Docker container and
exposes it as an HTTP API. It's free, with no message limits, and doesn't need
daily check-ins.

**You need a second WhatsApp number to send from.** A message your own
account sends to itself doesn't trigger a notification on your phone. The
usual setup is **WhatsApp Business on a second SIM or eSIM**: link that
account to WAHA, and it messages your main number.

WAHA is unofficial: WhatsApp doesn't sanction it, and the linked number could
be banned. That's another reason not to use your main number as the sender.

1. In `.env`, set `WHATSAPP_PROVIDER=waha`, `WAHA_TO=+91<your main number>`
   and a long random `WAHA_API_KEY`.
2. Start WAHA together with the tracker:
   ```bash
   docker compose --profile waha up -d
   ```
3. Open http://localhost:3000/dashboard and log in as `admin`, using your
   `WAHA_API_KEY` as the password. Start the `default` session, then scan the
   QR code with the **second** phone (*WhatsApp → Linked devices → Link a device*).
4. Send a test: `docker compose exec tracker python reddit_tracker.py --test-message`

If you run the tracker outside Docker, keep `WAHA_URL=http://localhost:3000`.

### Telegram

Official, free and instant. Telegram bots are designed for this kind of alert.

1. In Telegram, message **@BotFather** → `/newbot` → copy the token into
   `TELEGRAM_BOT_TOKEN`.
2. Open your new bot and send it any message.
3. Run `python reddit_tracker.py --telegram-chat-id` and copy the printed
   line into `.env`.
4. Set `WHATSAPP_PROVIDER=telegram`.

### Twilio

1. Create a Twilio account and open *Messaging → Try it out → Send a WhatsApp message*.
2. From your phone, send the `join <code>` message to the sandbox number.
3. Set `WHATSAPP_PROVIDER=twilio`, plus `TWILIO_ACCOUNT_SID`,
   `TWILIO_AUTH_TOKEN` and `TWILIO_TO=whatsapp:+91…`.

The sandbox has two limits. It can only message you within **24 hours of your
last message to it**, so you need to text it every day. You also have to
**re-join every 3 days**. Getting past both requires a registered WhatsApp
sender and approved message templates.

## 2. (Optional) Reddit API credentials

The tracker works without credentials. It uses Reddit's public RSS feed, which
allows about one check per minute. With API credentials it checks every 15
seconds by default.

Since November 2025, Reddit no longer creates API apps instantly. New access
goes through a manual request under Reddit's Responsible Builder Policy:
visiting https://www.reddit.com/prefs/apps leads to it. Replies take about 2–4
weeks, and small personal projects are often turned down. If you're approved:

1. Put the client ID and secret in `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET`,
   and set `REDDIT_USERNAME`.
2. Restart the tracker. It switches to the API automatically and checks every
   15 seconds (`POLL_INTERVAL_SECONDS`).

## 3. Run it

```bash
pip install -r requirements.txt
cp .env.example .env        # then edit .env

python reddit_tracker.py --test-message   # check WhatsApp works
python reddit_tracker.py --dry-run --once # check Reddit works (prints instead of sending)
python reddit_tracker.py                  # run continuously
```

For instant alerts, the tracker has to run all the time, on a machine that
stays on.

### On a VPS (recommended)

Any small Ubuntu or Debian server works: 1 CPU and 512 MB RAM is plenty.
[Hetzner](https://www.hetzner.com/cloud)'s smallest plan costs about €4 a
month; Vultr and DigitalOcean also work. Pick **Ubuntu 24.04**. The server
location doesn't matter.

Log in with SSH (`ssh root@<server-ip>`) and run:

```bash
curl -fsSL https://raw.githubusercontent.com/ReaperRush/RedditTracker/master/deploy/setup-vps.sh | sudo bash
```

The script asks for your Telegram bot token and chat ID, sends a test
message, and installs the tracker as a service. The service starts on boot and
restarts if it crashes. Run the same command again later to update. Useful
commands:

```bash
journalctl -u reddit-tracker -f                                        # live logs
sudo nano /opt/reddit-tracker/.env && sudo systemctl restart reddit-tracker  # change settings
```

If the logs say **"Reddit is blocking this server's IP address"**, add
[Reddit API credentials](#2-optional-reddit-api-credentials) to
`/opt/reddit-tracker/.env` and restart the service, or try a server from a
different provider.

### With Docker

`docker compose up -d` (add `--profile waha` if you use WAHA).

Avoid scheduled CI jobs such as GitHub Actions cron. They run at most every
5 minutes, often late, and Reddit blocks most of their IPs.

## Searching all of Reddit

Sellers don't always post in the subreddits you watch. With `SEARCHES` set,
the tracker also runs those Reddit searches (sorted by relevance, posts from
today, just like searching on reddit.com), one query every
`SEARCH_INTERVAL_SECONDS` in place of a normal check. Results go through the
same rules. Results from subreddits you don't watch must also carry the
`SEARCH_REQUIRE` tags; the default `_India` keeps listings from Pakistan,
Dubai, Sri Lanka, the US and so on out. The first time each search runs, its
current results are only remembered, not sent.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `SUBREDDITS` | (required) | Comma-separated subreddits |
| `KEYWORDS` | (none) | Comma-separated; notify only when one appears in the title or body |
| `HIGHLIGHTS` | (none) | `Label: term, term, -exclude; Label: …`, see below |
| `PRIORITY_HIGHLIGHTS` | (none) | Labels that alert with sound (🚨); all other posts arrive silently (Telegram). `A+B` requires both |
| `ONLY_HIGHLIGHTS` | (none) | Labels to keep; posts without one of them are dropped. `A+B` requires both |
| `POLL_INTERVAL_SECONDS` | 15 (OAuth) / 60 | Seconds between checks |
| `WHATSAPP_PROVIDER` | `callmebot` | `callmebot`, `waha`, `telegram`, `twilio` or `console` |
| `DIGEST_THRESHOLD` | 5 | More new posts than this at once → one combined message |
| `NOTIFY_ON_START` | `false` | Notify about the current posts on the very first run |
| `SEARCHES` | (none) | Comma-separated Reddit searches to run (relevance, today) |
| `SEARCH_REQUIRE` | (none) | Tags that search results from unwatched subreddits must have |
| `SEARCH_INTERVAL_SECONDS` | 300 | How often one search runs (in place of a subreddit check) |
| `STATE_FILE` | `state.json` | Where already-seen post IDs are stored |

## Highlights

`HIGHLIGHTS` tags posts without filtering anything out. Each `Label: terms`
group is checked against the post title and flair:

- Terms match whole words, case-insensitively: `rig` doesn't match
  "original" and `sale` doesn't match "resale". Digits may touch a term, so
  `rtx` matches "RTX4060".
- Spaces in a term also match hyphens or nothing: `gaming pc` matches
  "gaming-pc" and "gamingpc".
- A term starting with `-` blocks that label. For example, `-wtb` and
  `-budget` keep "Anyone selling a 3050 laptop? budget 40k" from being
  tagged WTS.

Rules also see the subreddit, so a term like `r/IndiaUsedTech` matches every
post there. A term written `body:xyz` looks in the post's text instead of the
title.

The default rules in `.env.example`:

- **WTS:** a sale. That means sale words or a price (₹, Rs, "negotiable"),
  r/IndiaUsedTech's `[PC]` / `[PC/DESKTOP]` tags, or any post in one of the
  buy/sell subreddits. Buy requests and questions are excluded (`wtb`,
  `buying`, `fair price`, `rate my`, `help`, `?`, …). r/IndianPCHardware is
  mostly discussion, so it isn't listed there and needs real sale words.
- **Gaming PC:** a desktop or gaming PC, excluding laptops (`laptop`, `hx`,
  `loq`, `zephyrus`, …) and "pc parts" / "pc components".
- **GPU:** an NVIDIA, AMD or Intel graphics card by name or model
  (`rtx`, `gtx`, `radeon`, `3060`, `rx 6600`, `arc a`, …), excluding
  laptops, iMacs and shoes ("GTX" Gore-Tex).
- **PC parts in post** + **GPU in post:** the post's text lists desktop parts
  (motherboard, an X570/B550/Z790… chipset, cabinet, PSU) *and* a graphics
  card. Together they catch full-PC listings with vague titles like "WTS
  whole setup". Neither alone is enough, so lone parts don't get sent.
- **Possible seller:** an owner asking what their PC is worth ("What is my
  PC worth?", "how much can I sell…"). These aren't listings, but they're
  people you could message. They're sent silently, so only real listings
  make a sound.

On ~750 recent posts from the subreddits, these rules sent 36 notifications
with sound (gaming PCs and graphics cards for sale, plus one unclear
"RTX 4080/RX 9070" post) and 3 silent "what's my PC worth" posts. Everything else (laptops, phones, consoles, CPU or
RAM-only sales, questions and buy requests) was dropped.

To update a server installed earlier to the latest rules, keeping your token
and subreddits:

```bash
curl -fsSL https://raw.githubusercontent.com/ReaperRush/RedditTracker/master/deploy/setup-vps.sh | sudo UPDATE_FILTERS=1 bash
```

## Tests

```bash
pip install pytest && python -m pytest
```
