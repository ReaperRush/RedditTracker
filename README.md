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
- A failed WhatsApp send is retried on the next check.
- Backs off when Reddit rate-limits it.

## 1. Set up WhatsApp delivery

Pick one.

### Option A: CallMeBot (free, about 2 minutes, best for personal use)

1. Open https://www.callmebot.com/blog/free-api-whatsapp-messages/ and add
   the phone number shown there to your contacts.
2. From WhatsApp, send that contact: `I allow callmebot to send me messages`
3. You get a reply with your API key.
4. In `.env`, set `WHATSAPP_PROVIDER=callmebot`, `CALLMEBOT_PHONE=+<your number>`
   and `CALLMEBOT_APIKEY=<key>`.

### Option B: Twilio (paid per message, more reliable)

1. Create a Twilio account and open *Messaging → Try it out → Send a WhatsApp message*.
2. From your phone, send the `join <code>` message to the sandbox number.
3. Set `WHATSAPP_PROVIDER=twilio`, plus `TWILIO_ACCOUNT_SID`,
   `TWILIO_AUTH_TOKEN` and `TWILIO_TO=whatsapp:+<your number>`.

The sandbox requires you to send it a message again every 72 hours. For
long-term use, register your own WhatsApp sender in Twilio and set
`TWILIO_FROM`.

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

For instant alerts, the tracker has to run all the time. Some options:

- **Your own always-on machine / Raspberry Pi / VPS:** use the included
  `reddit-tracker.service` (systemd) so it starts on boot and restarts on failure.
- **Docker:**
  ```bash
  docker build -t reddit-tracker .
  docker run -d --restart=always --env-file .env -v reddit-tracker-data:/data reddit-tracker
  ```

Avoid scheduled CI jobs such as GitHub Actions cron. They run at most every
5 minutes, often late, and Reddit blocks most of their IPs.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `SUBREDDITS` | (required) | Comma-separated subreddits |
| `KEYWORDS` | (none) | Comma-separated; notify only when one appears in the title or body |
| `HIGHLIGHTS` | (none) | `Label: term, term, -exclude; Label: …`, see below |
| `POLL_INTERVAL_SECONDS` | 15 (OAuth) / 60 | Seconds between checks |
| `WHATSAPP_PROVIDER` | `callmebot` | `callmebot`, `twilio` or `console` |
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
