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


def post(pid, title="t", created=0, body=""):
    return rt.Post(pid, "sub", title, "me", "https://x", created, body)


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
