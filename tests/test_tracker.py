import json

import pytest

import reddit_tracker as rt


class FakeResponse:
    def __init__(self, status=200, json_data=None, text="", headers=None):
        self.status_code = status
        self._json = json_data
        self.text = text or (json.dumps(json_data) if json_data is not None else "")
        self.headers = headers or {"Content-Type": "application/json"}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []

    def _next(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        return self._next("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._next("POST", url, kwargs)


def listing(*posts):
    return {
        "data": {
            "children": [
                {
                    "data": {
                        "id": pid,
                        "subreddit": "buildapcsales",
                        "title": title,
                        "author": "someone",
                        "permalink": f"/r/buildapcsales/comments/{pid}/x/",
                        "created_utc": created,
                        "selftext": "",
                        "link_flair_text": "GPU",
                    }
                }
                for pid, title, created in posts
            ]
        }
    }


ATOM = """<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><author><name>/u/alice</name></author><category term="learnpython" label="r/learnpython"/>
<content type="html">&lt;p&gt;Hello &amp;amp; welcome&lt;/p&gt;</content><id>t3_abc123</id>
<link href="https://www.reddit.com/r/learnpython/comments/abc123/x/" />
<published>2026-10-02T00:00:42+00:00</published><title>First post</title></entry></feed>"""


class FakeClient:
    mode = "json"

    def __init__(self, batches):
        self.batches = list(batches)

    def fetch_new(self, subreddits, limit=100):
        return self.batches.pop(0)

    def min_poll_interval(self):
        return 0


class RecordingNotifier:
    def __init__(self, fail_on=None):
        self.sent = []
        self.fail_on = fail_on

    def send(self, text):
        if self.fail_on and self.fail_on in text:
            raise RuntimeError("boom")
        self.sent.append(text)


def post(pid, title=None, created=0, body="", sub="sub", author="me", flair=None):
    return rt.Post(pid, sub, title or f"title {pid}", author, "https://x", created, body, flair)


def test_parse_listing():
    posts = rt._parse_listing(listing(("a1", "RTX deal", 10)))
    assert posts[0].id == "a1"
    assert posts[0].flair == "GPU"
    assert posts[0].short_url == "https://redd.it/a1"


def test_parse_atom():
    (p,) = rt._parse_atom(ATOM)
    assert (p.id, p.subreddit, p.author, p.title) == ("abc123", "learnpython", "alice", "First post")
    assert p.body == "Hello & welcome"
    assert p.created_utc > 0


def test_json_blocked_falls_back_to_rss():
    session = FakeSession([
        FakeResponse(403, text="<html>blocked</html>", headers={"Content-Type": "text/html"}),
        FakeResponse(200, text=ATOM, headers={"Content-Type": "application/atom+xml"}),
    ])
    client = rt.RedditClient("ua", session=session)
    posts = client.fetch_new(["a", "b"])
    assert client.mode == "rss"
    assert [p.id for p in posts] == ["abc123"]
    assert session.calls[1][1] == "https://www.reddit.com/r/a+b/new/.rss"


def test_oauth_fetches_token_once():
    session = FakeSession([
        FakeResponse(json_data={"access_token": "tok", "expires_in": 86400}),
        FakeResponse(json_data=listing(("a1", "x", 1))),
        FakeResponse(json_data=listing(("a2", "y", 2))),
    ])
    client = rt.RedditClient("ua", "id", "secret", session=session, clock=lambda: 1000.0)
    client.fetch_new(["a"])
    client.fetch_new(["a"])
    assert [c[0] for c in session.calls] == ["POST", "GET", "GET"]
    assert session.calls[1][2]["headers"]["Authorization"] == "bearer tok"


def test_rate_limit_raises():
    session = FakeSession([FakeResponse(429, headers={"x-ratelimit-reset": "42"})])
    with pytest.raises(rt.RateLimited) as exc:
        rt.RedditClient("ua", session=session).fetch_new(["a"])
    assert exc.value.retry_after == 42


def test_first_run_seeds_without_notifying(tmp_path):
    notifier = RecordingNotifier()
    client = FakeClient([[post("old1"), post("old2")], [post("old1"), post("new1", "hello")]])
    tracker = rt.Tracker(client, notifier, ["sub"], rt.SeenStore(tmp_path / "s.json"))
    assert tracker.poll_once() == []
    assert notifier.sent == []
    sent = tracker.poll_once()
    assert [p.id for p in sent] == ["new1"]
    assert "hello" in notifier.sent[0] and "https://redd.it/new1" in notifier.sent[0]


def test_state_persists_across_restarts(tmp_path):
    path = tmp_path / "s.json"
    rt.Tracker(FakeClient([[post("a")]]), RecordingNotifier(), ["sub"], rt.SeenStore(path)).poll_once()
    notifier = RecordingNotifier()
    tracker = rt.Tracker(FakeClient([[post("a"), post("b")]]), notifier, ["sub"], rt.SeenStore(path))
    assert [p.id for p in tracker.poll_once()] == ["b"]


def test_keywords_filter(tmp_path):
    (tmp_path / "s.json").write_text('{"seen": []}')
    notifier = RecordingNotifier()
    client = FakeClient([[post("a", "Cheap RTX 4090"), post("b", "Monitor sale"), post("c", "x", body="rtx 4090 inside")]])
    tracker = rt.Tracker(client, notifier, ["sub"], rt.SeenStore(tmp_path / "s.json"), keywords=["RTX 4090"])
    assert [p.id for p in tracker.poll_once()] == ["a", "c"]
    assert "b" in tracker.store  # non-matching posts aren't re-checked


def test_failed_send_is_retried(tmp_path):
    (tmp_path / "s.json").write_text('{"seen": []}')
    notifier = RecordingNotifier(fail_on="second")
    client = FakeClient([[post("a", "first", 1), post("b", "second", 2)], [post("a", "first", 1), post("b", "second", 2)]])
    tracker = rt.Tracker(client, notifier, ["sub"], rt.SeenStore(tmp_path / "s.json"))
    assert [p.id for p in tracker.poll_once()] == ["a"]
    notifier.fail_on = None
    assert [p.id for p in tracker.poll_once()] == ["b"]


def test_many_posts_become_one_digest(tmp_path):
    (tmp_path / "s.json").write_text('{"seen": []}')
    notifier = RecordingNotifier()
    client = FakeClient([[post(f"p{i}", f"title {i}", i) for i in range(8)]])
    tracker = rt.Tracker(client, notifier, ["sub"], rt.SeenStore(tmp_path / "s.json"), digest_threshold=5)
    assert len(tracker.poll_once()) == 8
    assert len(notifier.sent) == 1 and notifier.sent[0].startswith("🆕 8 new posts")


def test_digest_respects_whatsapp_length_limit():
    text = rt.format_digest([post(f"p{i}", "x" * 200) for i in range(100)])
    assert len(text) <= rt.WHATSAPP_MAX_CHARS
    assert "more" in text


def test_parse_subreddits():
    assert rt.parse_subreddits("r/Python, /r/learnpython  buildapcsales") == ["Python", "learnpython", "buildapcsales"]
    with pytest.raises(SystemExit):
        rt.parse_subreddits("bad-name!")


def test_callmebot_bad_key_raises():
    session = FakeSession([FakeResponse(200, text="<p>APIKey is invalid.</p>")])
    with pytest.raises(RuntimeError):
        rt.CallMeBotNotifier("+1", "k", session=session).send("hi")


def test_twilio_sends_whatsapp_addresses():
    session = FakeSession([FakeResponse(201, json_data={"sid": "SM1"})])
    rt.TwilioNotifier("AC1", "tok", "+14155238886", "+15551234567", session=session).send("hi")
    data = session.calls[0][2]["data"]
    assert data == {"From": "whatsapp:+14155238886", "To": "whatsapp:+15551234567", "Body": "hi"}


HIGHLIGHTS = rt.parse_highlights(
    "WTS: wts, selling, sell, resell, for sale, sale, ₹, rs, inr, "
    "-wtb, -looking for, -anyone selling, -budget, -where should i sell, -suggestions; "
    "Gaming PC: gaming pc, gaming rig, pc build, desktop, rig, prebuilt"
)


def fresh_tracker(tmp_path, batches, **kwargs):
    (tmp_path / "s.json").write_text('{"seen": []}')
    notifier = RecordingNotifier()
    tracker = rt.Tracker(FakeClient(batches), notifier, ["sub"], rt.SeenStore(tmp_path / "s.json"), **kwargs)
    return tracker, notifier


@pytest.mark.parametrize("title, tags", [
    ("[WTS] [Mumbai] Gaming PC (i5-14600K / RTX 4070 Super) - 95,000", ["WTS", "Gaming PC"]),
    ("Old i3 Gaming PC + GTX 1050 Ti — ₹16,000", ["WTS", "Gaming PC"]),
    ("[SELLING] ASUS ROG G17 IN FLAWLESS CONDITION", ["WTS"]),
    ("Black Myth Wukong PS5 in brand new condition for sale", ["WTS"]),
    ("Trying to sell a De-lidded Ryzen 5 5500GT", ["WTS"]),
    ("wts/trade/exchange nikon coolpix", ["WTS"]),
    ("[Desktop] [Lenovo] IdeaCentre Gaming 3 | RTX 3060", ["Gaming PC"]),
    ("Ghost of Yotei (PS5 disc) resale", []),          # "sale" inside "resale"
    ("Original Xbox controller", []),                  # "rig" inside "original"
    ("Looking to buy ps4 pro or xbox series s", []),
    ("Check my gamingpc", ["Gaming PC"]),
    ("Colorful GTX 1050 Ti — ₹11,000", ["WTS"]),
    ("[Phone][Android] S24| 8gb,256gb|Mumbai|₹45000", ["WTS"]),
    ("[SSD][WD] SN7100 | 1 TB | Raiganj| Rs.16000", ["WTS"]),
    ("Iphone 15 6/128 resell", ["WTS"]),
    ("Anyone selling their 3050 Laptop around Hyderabad? my budget is slim", []),
    ("Graphics Card died, looking for a used GPU under ₹15,000", []),
    ("WTB gaming pc, have cash", ["Gaming PC"]),       # exclusions only apply to their own label
    ("2020 Range Rover Sports SE", []),
    ("Suggestions for the best value for money gpu under ₹30k", []),
])
def test_highlight_tags(tmp_path, title, tags):
    tracker, _ = fresh_tracker(tmp_path, [], highlights=HIGHLIGHTS)
    assert tracker.tags_for(post("x", title)) == tags


def test_sale_flair_counts_as_wts(tmp_path):
    tracker, _ = fresh_tracker(tmp_path, [], highlights=HIGHLIGHTS)
    assert tracker.tags_for(post("x", "RTX 4070 build", flair="Sale")) == ["WTS"]


def test_highlighted_posts_skip_the_digest(tmp_path):
    batch = [post(f"p{i}", f"Question {i}", i) for i in range(8)] + [post("hot", "WTS gaming pc", 9)]
    tracker, notifier = fresh_tracker(tmp_path, [batch], highlights=HIGHLIGHTS, digest_threshold=5)
    assert len(tracker.poll_once()) == 9
    assert notifier.sent[0].startswith("🔥 WTS · Gaming PC\nr/sub\n*WTS gaming pc*")
    assert notifier.sent[1].startswith("🆕 8 new posts")
    assert len(notifier.sent) == 2


def test_crossposts_become_one_message(tmp_path):
    batch = [
        post("a", "WTS Multiple Electronics items", 1, sub="IndiaUsedTech", author="bob"),
        post("b", "WTS Multiple Electronics items!", 2, sub="Indiangaming_Resale", author="bob"),
        post("c", "WTS Multiple Electronics items", 3, sub="IndiaUsedTech", author="alice"),
    ]
    tracker, notifier = fresh_tracker(tmp_path, [batch])
    assert [p.id for p in tracker.poll_once()] == ["a", "c"]
    assert "r/IndiaUsedTech, r/Indiangaming_Resale" in notifier.sent[0]
    assert "b" in tracker.store


def test_crosspost_in_a_later_poll_is_not_resent(tmp_path):
    first = [post("a", "Selling RTX 3060", 1, sub="IndianPCHardware", author="bob")]
    later = first + [post("b", "Selling RTX 3060", 200, sub="IndiaUsedTech", author="bob")]
    tracker, notifier = fresh_tracker(tmp_path, [first, later])
    tracker.poll_once()
    assert tracker.poll_once() == []
    assert len(notifier.sent) == 1
    # ...and that survives a restart
    assert rt.SeenStore(tmp_path / "s.json").has_key(rt.crosspost_key(later[1]))


def test_old_state_file_without_crosspost_keys_loads(tmp_path):
    (tmp_path / "s.json").write_text('{"seen": ["a"]}')
    store = rt.SeenStore(tmp_path / "s.json")
    assert "a" in store and store.keys == []


def test_parse_highlights_rejects_missing_label():
    with pytest.raises(SystemExit):
        rt.parse_highlights("wts, selling")


def test_blank_numeric_settings_use_defaults(monkeypatch):
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "")
    assert rt._env_number("POLL_INTERVAL_SECONDS", 60) == 60
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "abc")
    with pytest.raises(SystemExit):
        rt._env_number("POLL_INTERVAL_SECONDS", 60)


def test_waha_sends_to_phone_chat_id():
    session = FakeSession([FakeResponse(201, json_data={"id": "x"})])
    rt.WahaNotifier("http://waha:3000/", "+91 98765-43210", api_key="k", session=session).send("hi")
    method, url, kwargs = session.calls[0]
    assert url == "http://waha:3000/api/sendText"
    assert kwargs["json"] == {"session": "default", "chatId": "919876543210@c.us", "text": "hi"}
    assert kwargs["headers"] == {"X-Api-Key": "k"}


def test_waha_error_raises():
    session = FakeSession([FakeResponse(422, text="session not ready")])
    with pytest.raises(RuntimeError):
        rt.WahaNotifier("http://waha:3000", "919876543210", session=session).send("hi")


def test_telegram_converts_bold_and_escapes():
    session = FakeSession([FakeResponse(200, json_data={"ok": True})])
    rt.TelegramNotifier("TOKEN", "42", session=session).send(rt.format_post(post("a", "RAM <16GB> & SSD")))
    method, url, kwargs = session.calls[0]
    assert url == "https://api.telegram.org/botTOKEN/sendMessage"
    assert kwargs["json"]["parse_mode"] == "HTML"
    assert "<b>RAM &lt;16GB&gt; &amp; SSD</b>" in kwargs["json"]["text"]


def test_telegram_not_ok_raises():
    session = FakeSession([FakeResponse(400, json_data={"ok": False, "description": "chat not found"})])
    with pytest.raises(RuntimeError):
        rt.TelegramNotifier("TOKEN", "42", session=session).send("hi")
