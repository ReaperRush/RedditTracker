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

1. Save **+34 684 770 005** as a contact. Check
   [their page](https://www.callmebot.com/blog/free-api-whatsapp-messages/)
   in case the number has changed.
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

## 2. (Recommended) Reddit API credentials

The tracker works without credentials. It uses Reddit's public feed, which is
limited to about one check per minute. With credentials it checks every 15
seconds by default and is more reliable, especially on cloud servers, where
Reddit often blocks anonymous requests.

1. Go to https://www.reddit.com/prefs/apps → *create another app* → type **script**.
   Use `http://localhost` as the redirect URI.
2. Copy the client ID (the string under the app name) and the secret into
   `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET`, and set `REDDIT_USERNAME`.

If Reddit won't let you create an app, leave these empty. The RSS fallback
needs no credentials.

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
[Reddit API credentials](#2-recommended-reddit-api-credentials) to
`/opt/reddit-tracker/.env` and restart the service.

### With Docker

`docker compose up -d` (add `--profile waha` if you use WAHA).

Avoid scheduled CI jobs such as GitHub Actions cron. They run at most every
5 minutes, often late, and Reddit blocks most of their IPs.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `SUBREDDITS` | (required) | Comma-separated subreddits |
| `KEYWORDS` | (none) | Comma-separated; notify only when one appears in the title or body |
| `HIGHLIGHTS` | (none) | `Label: term, term, -exclude; Label: …`, see below |
| `POLL_INTERVAL_SECONDS` | 15 (OAuth) / 60 | Seconds between checks |
| `WHATSAPP_PROVIDER` | `callmebot` | `callmebot`, `waha`, `telegram`, `twilio` or `console` |
| `DIGEST_THRESHOLD` | 5 | More new posts than this at once → one combined message |
| `NOTIFY_ON_START` | `false` | Notify about the current posts on the very first run |
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

The WTS rule in `.env.example` treats a price in the title (₹, Rs, INR) as a
sale, because many r/IndiaUsedTech listings look like
`[Phone] S24 | Mumbai | ₹45000` and never say "WTS". It was tuned against
about 100 recent posts from those four subreddits.

## Tests

```bash
pip install pytest && python -m pytest
```
