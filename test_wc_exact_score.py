#!/usr/bin/env python3
"""
Whole-flow tests for wc_exact_score.

Philosophy: mock as little as possible. We replace ONLY the network boundary
(requests.Session.get / .post) with canned Polymarket-shaped payloads, then run
the real main() end-to-end — parsing, kickoff filtering, ranking, the Hebrew
Telegram formatting, chunking and the recent-results derivation all execute for
real. Fixtures are built relative to the real wall clock (no time mocking) so the
kickoff-window logic is exercised exactly as in production.

Run:  python -m unittest test_wc_exact_score -v
"""

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

import wc_exact_score as wc


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def exact_market(group_title, yes, vol, kickoff, closed):
    """One Yes/No exact-score market, shaped like the Gamma API returns it."""
    yes = float(yes)
    return {
        "groupItemTitle": group_title,
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps([str(yes), str(round(1.0 - yes, 4))]),
        "volume": vol,
        "sportsMarketType": "soccer_exact_score",
        "question": f"Will the exact score be {group_title}?",
        "gameStartTime": kickoff,
        "closed": closed,
        "slug": "m-" + group_title.replace(" ", "-"),
    }


def exact_event(title, slug, kickoff, scorelines, closed):
    """An 'X vs. Y - Exact Score' event; scorelines = [(label, yes, vol), ...]."""
    return {
        "title": title,
        "slug": slug,
        "gameStartTime": kickoff,
        "markets": [exact_market(g, y, v, kickoff, closed) for (g, y, v) in scorelines],
    }


def futures_event(kickoff):
    """A tournament-futures event with NO exact-score markets (must be ignored)."""
    return {
        "title": "World Cup Group A Winner",
        "slug": "world-cup-group-a-winner",
        "gameStartTime": kickoff,
        "markets": [{
            "groupItemTitle": "Spain",
            "outcomes": json.dumps(["Yes", "No"]),
            "outcomePrices": json.dumps(["0.40", "0.60"]),
            "volume": 5000,
            "sportsMarketType": "",
            "question": "Will Spain win Group A?",
            "closed": False,
        }],
    }


class FakeResp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise wc.requests.HTTPError(str(self.status_code))


class FakeSession:
    """Routes GET by the `closed` param to upcoming vs finished fixtures, and
    records every Telegram POST so tests can assert on the outgoing payload."""

    def __init__(self, upcoming, finished):
        self.upcoming, self.finished = upcoming, finished
        self.posts = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        params = params or {}
        if int(params.get("offset", 0)) > 0:          # second page -> stop paging
            return FakeResp([])
        is_closed = str(params.get("closed")).lower() == "true"
        return FakeResp(self.finished if is_closed else self.upcoming)

    def post(self, url, json=None, timeout=None):
        self.posts.append({"url": url, "json": json})
        return FakeResp({"ok": True, "result": {"message_id": 1}})


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.kick_future = iso(self.now + timedelta(hours=3))    # within a 24h look-ahead
        self.kick_past = iso(self.now - timedelta(hours=3))      # within a 24h look-back
        # Drop any real secrets so non-telegram tests stay hermetic.
        for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            os.environ.pop(k, None)
        self._real_session = wc.requests.Session

    def tearDown(self):
        wc.requests.Session = self._real_session
        for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            os.environ.pop(k, None)

    def install(self, upcoming, finished):
        fake = FakeSession(upcoming, finished)
        wc.requests.Session = lambda: fake
        return fake

    def run_main(self, argv):
        # never read a real .env file
        argv = list(argv) + ["--env-file", "/nonexistent/.env"]
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = wc.main(argv)
        return rc, buf.getvalue()

    # 1) Upcoming flow: scorelines ranked by probability, futures event ignored.
    def test_upcoming_ranked_by_price_and_filters_non_matches(self):
        upcoming = [
            exact_event("Alpha vs. Beta", "alpha-beta-exact-score", self.kick_future,
                        [("Alpha 1 - 0 Beta", 0.05, 100),
                         ("Alpha 2 - 1 Beta", 0.30, 5000),
                         ("Alpha 1 - 1 Beta", 0.20, 9000)], closed=False),
            futures_event(self.kick_future),
        ]
        self.install(upcoming, [])
        rc, out = self.run_main(["--hours", "24", "--no-results"])
        self.assertEqual(rc, 0)
        self.assertIn("Alpha vs. Beta", out)
        self.assertNotIn("Group A Winner", out)                 # futures event excluded
        # default sort is price: 0.30 before 0.20 before 0.05
        self.assertLess(out.index("Alpha 2 - 1 Beta"), out.index("Alpha 1 - 1 Beta"))
        self.assertLess(out.index("Alpha 1 - 1 Beta"), out.index("Alpha 1 - 0 Beta"))

    # 1b) 'Any Other Score' is excluded BEFORE the top-N slice, even when it has
    #     the highest price -> --top 3 shows 3 concrete scorelines, not 2 + bucket.
    def test_any_other_score_excluded_before_top_n(self):
        upcoming = [exact_event("Alpha vs. Beta", "alpha-beta-exact-score", self.kick_future,
                                [("Alpha 2 - 1 Beta", 0.20, 5000),
                                 ("Alpha 1 - 1 Beta", 0.18, 4000),
                                 ("Alpha 1 - 0 Beta", 0.15, 3000),
                                 ("Alpha 0 - 0 Beta", 0.05, 1000),
                                 ("Exact Score: Any Other Score", 0.40, 9000)], closed=False)]
        self.install(upcoming, [])
        rc, out = self.run_main(["--hours", "24", "--no-results", "--top", "3"])
        self.assertEqual(rc, 0)
        self.assertNotIn("Any Other", out)                      # bucket never shown...
        for shown in ("Alpha 2 - 1 Beta", "Alpha 1 - 1 Beta", "Alpha 1 - 0 Beta"):
            self.assertIn(shown, out)                           # ...3 real scorelines are
        self.assertNotIn("Alpha 0 - 0 Beta", out)               # 4th concrete one is past top-3

    # 1c) Price is primary, volume breaks ties: a higher-priced score outranks a
    #     huge-volume one, and equal-priced scores order by volume.
    def test_price_primary_volume_breaks_ties(self):
        upcoming = [exact_event("Alpha vs. Beta", "alpha-beta-exact-score", self.kick_future,
                                [("Alpha 1 - 0 Beta", 0.115, 100),     # tie @0.115, low vol
                                 ("Alpha 2 - 1 Beta", 0.115, 9000),    # tie @0.115, high vol
                                 ("Alpha 0 - 0 Beta", 0.115, 5000),    # tie @0.115, mid vol
                                 ("Alpha 3 - 0 Beta", 0.20, 10)],      # higher price, tiny vol
                                closed=False)]
        self.install(upcoming, [])
        rc, out = self.run_main(["--hours", "24", "--no-results", "--top", "4"])
        self.assertEqual(rc, 0)
        order = [out.index(s) for s in ("Alpha 3 - 0 Beta",   # highest price -> first
                                        "Alpha 2 - 1 Beta",   # then ties by volume: 9000
                                        "Alpha 0 - 0 Beta",   # 5000
                                        "Alpha 1 - 0 Beta")]  # 100
        self.assertEqual(order, sorted(order))

    # 1d) Hebrew block formatting: flags, inline time (no tz hint), each scoreline
    #     tagged with the favoured team, and the fixture title carrying the link.
    def test_hebrew_block_formatting(self):
        from datetime import timedelta
        upcoming = [exact_event("Netherlands vs. Sweden", "nl-se-exact-score", self.kick_future,
                                [("Netherlands 2 - 1 Sweden", 0.30, 9000),   # leader by volume
                                 ("Netherlands 1 - 1 Sweden", 0.20, 1000)], closed=False)]
        games = wc.build_games(upcoming, self.now, self.now + timedelta(hours=24))
        block = wc.format_game_hebrew(games[0], 5)
        self.assertIn("🇳🇱", block)                              # home flag
        self.assertIn("🇸🇪", block)                              # away flag
        self.assertIn("הולנד", block)                           # Netherlands -> Hebrew
        self.assertIn("שוודיה", block)                          # Sweden -> Hebrew
        self.assertNotIn("Netherlands", block)                  # English name not shown
        self.assertIn(wc.fmt_jerusalem(self.now + timedelta(hours=3)), block)  # time inline
        self.assertNotIn("שעון ישראל", block)                   # tz hint dropped
        # each scoreline names the team it favours (draw -> 'תיקו'); the higher
        # score 2-1 favours Netherlands, 1-1 is a draw
        self.assertIn("2-1 הולנד", block)
        self.assertIn("1-1 תיקו", block)
        self.assertNotIn("הכי הרבה כסף", block)                 # money line dropped
        # the Polymarket link now wraps the fixture title (no standalone '#')
        title = block.split("\n")[0]
        self.assertIn('<a href="https://polymarket.com/event/nl-se-exact-score">', title)
        self.assertIn("הולנד", title)                           # the fixture is the link text
        self.assertNotIn("#</a>", block)                        # no standalone '#' link
        # every prediction line now carries a Hebrew word, so Telegram aligns it
        # RTL natively — no bidi control characters anywhere in the block
        self.assertNotIn("⁦", block)                       # no LTR isolate
        self.assertNotIn("‏", block)                       # no RLM
        pred = [ln for ln in block.split("\n") if "%" in ln and "·" in ln]
        self.assertTrue(pred)
        self.assertTrue(all(any("֐" <= ch <= "׿" for ch in ln)
                            for ln in pred))                    # Hebrew char present

    # 2) Telegram flow: real Hebrew message, Jerusalem time, both sections, payload shape.
    def test_telegram_hebrew_message(self):
        upcoming = [exact_event("Alpha vs. Beta", "alpha-beta-exact-score", self.kick_future,
                                [("Alpha 2 - 1 Beta", 0.30, 5000),
                                 ("Alpha 1 - 1 Beta", 0.20, 9000)], closed=False)]
        finished = [exact_event("Gamma vs. Delta", "gamma-delta-exact-score", self.kick_past,
                                [("Gamma 3 - 0 Delta", 1.0, 4000),
                                 ("Gamma 0 - 0 Delta", 0.0, 100)], closed=True)]
        fake = self.install(upcoming, finished)
        os.environ["TELEGRAM_BOT_TOKEN"] = "TESTTOKEN"
        os.environ["TELEGRAM_CHAT_ID"] = "@testchan"
        rc, out = self.run_main(["--telegram", "--hours", "24"])
        self.assertEqual(rc, 0)
        self.assertTrue(fake.posts, "no Telegram message was sent")

        p = fake.posts[0]["json"]
        self.assertEqual(p["parse_mode"], "HTML")
        self.assertEqual(p["chat_id"], "@testchan")
        self.assertEqual(fake.posts[0]["url"], "https://api.telegram.org/botTESTTOKEN/sendMessage")

        text = "\n".join(post["json"]["text"] for post in fake.posts)
        self.assertIn("מונדיאל", text)                          # Hebrew header
        self.assertIn(wc.fmt_jerusalem(self.now + timedelta(hours=3)), text)  # Israel-local kickoff
        self.assertIn("תוצאות", text)                           # results subheader
        self.assertIn("Gamma 3", text)                          # real final score, each team
        self.assertIn("Delta 0", text)                          # paired with its own goals
        self.assertLessEqual(len(p["text"]), wc.TELEGRAM_MAX_CHARS)

    # 3) Results derivation: winning (Yes≈1) market becomes the score; unresolved is skipped.
    def test_recent_results_from_resolved_markets(self):
        finished = [
            exact_event("Gamma vs. Delta", "gamma-delta-exact-score", self.kick_past,
                        [("Gamma 2 - 1 Delta", 1.0, 4000),
                         ("Gamma 0 - 0 Delta", 0.0, 100)], closed=True),
            # closed but NOT resolved to any Yes (all prices low) -> must be skipped
            exact_event("Epsilon vs. Zeta", "epsilon-zeta-exact-score", self.kick_past,
                        [("Epsilon 1 - 0 Zeta", 0.10, 50),
                         ("Epsilon 0 - 1 Zeta", 0.10, 50)], closed=True),
        ]
        self.install([], finished)
        rc, out = self.run_main(["--hours", "24"])
        self.assertEqual(rc, 0)
        self.assertIn("Recent results", out)
        self.assertIn("Gamma 2 - 1 Delta", out)                 # derived from the Yes=1 market
        self.assertNotIn("Epsilon", out)                        # unresolved match not shown


if __name__ == "__main__":
    unittest.main()
