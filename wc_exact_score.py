#!/usr/bin/env python3
"""
wc_exact_score.py

Query Polymarket for the "Exact Score" (correct-score) markets on World Cup
games kicking off within the next N hours, and rank each game's scorelines.

Data source: Polymarket Gamma API (https://gamma-api.polymarket.com) — public,
no authentication required (per docs.polymarket.com/api-reference/introduction).

Each scoreline (e.g. "1-0", "2-1", "Any Other Score") is its own binary Yes/No
market. The *Yes* price is the implied probability of that exact scoreline (i.e.
"most likely"); the market *volume* is how much money has been traded on it.

Usage:
    python wc_exact_score.py                      # next 24h, sort by volume
    python wc_exact_score.py --sort price         # rank by implied probability (most likely)
    python wc_exact_score.py --hours 12 --top 8
    python wc_exact_score.py --debug              # show what the API actually returns
    python wc_exact_score.py --json results.json  # also dump raw results

Dependencies: requests  (pip install requests)

--------------------------------------------------------------------------
Example output  (python wc_exact_score.py --sort price --top 5)
Numbers are illustrative — the top row is the SINGLE MOST LIKELY scoreline.
--------------------------------------------------------------------------
World Cup exact-score markets — next 24h — ranked by implied probability

=== Netherlands vs. Sweden   (kickoff 2026-06-20T17:00:00Z)
    https://polymarket.com/event/fifwc-nld-swe-2026-06-20
    1-0 (NLD)              prob= 11.5%   vol=$48,200
    1-1                    prob= 10.2%   vol=$61,750
    2-1 (NLD)             prob=  9.8%   vol=$53,010
    2-0 (NLD)             prob=  8.1%   vol=$22,940
    0-0                    prob=  7.0%   vol=$30,120
    -> most money on: 1-1 ($61,750)

Note: "prob" (price) = most likely; "vol" = most money traded. They differ,
so the highest-probability row and the "most money on" row need not match.
--------------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

GAMMA = "https://gamma-api.polymarket.com"

EXACT_SCORE_TYPES = {"scores", "exact_score", "exact-score", "correct_score", "correctscore"}
SCORELINE_RE = re.compile(r"^\s*\d+\s*[-–:]\s*\d+\s*$")          # "2-1", "0 - 0"
EXACT_SCORE_TEXT_RE = re.compile(r"exact\s*score|correct\s*score", re.IGNORECASE)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def get_json(session, url, params=None, retries=3, timeout=30):
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            if attempt == retries - 1:
                raise
            time.sleep(1.0 * (attempt + 1))
    return None


def parse_json_field(value, default):
    """`outcomes` / `outcomePrices` / `clobTokenIds` arrive as JSON strings."""
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def fetch_events(session, tag_slug, end_after, page_size=100):
    """
    Page through GET /events for a tag. We do NOT filter on start_date here:
    for sports, an event's startDate is when the market OPENED (often well in
    the past), so a start_date_min filter would wrongly drop upcoming games.
    Instead we keep events that end after `end_after` (drops finished games)
    and filter on real kickoff time client-side.
    """
    events, offset = [], 0
    while True:
        params = {
            "tag_slug": tag_slug,
            "related_tags": "true",
            "closed": "false",
            "active": "true",
            "end_date_min": iso_z(end_after),   # keep upcoming/live, drop finished
            "limit": page_size,
            "offset": offset,
        }
        batch = get_json(session, f"{GAMMA}/events", params=params)
        if not batch:
            break
        events.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return events


def event_kickoff(event):
    """
    Real kickoff time. For sports this is the market-level `gameStartTime`
    (or `eventStartTime`); event-level `startTime`/`eventStartTime` are also
    tried. `startDate` is used only as a last resort because it is usually the
    market-open date, not the kickoff.
    """
    times = []
    for key in ("gameStartTime", "eventStartTime", "startTime"):
        t = parse_dt(event.get(key))
        if t:
            times.append(t)
    for m in event.get("markets") or []:
        for key in ("gameStartTime", "eventStartTime"):
            t = parse_dt(m.get(key))
            if t:
                times.append(t)
    if times:
        return min(times)
    return parse_dt(event.get("startDate"))   # last resort


def is_exact_score_market(m):
    smt = (m.get("sportsMarketType") or "").strip().lower()
    if smt in EXACT_SCORE_TYPES:
        return True
    if SCORELINE_RE.match(m.get("groupItemTitle") or ""):
        return True
    if EXACT_SCORE_TEXT_RE.search(m.get("question") or ""):
        return True
    return False


def yes_price(m):
    outcomes = parse_json_field(m.get("outcomes"), [])
    prices = parse_json_field(m.get("outcomePrices"), [])
    if not prices:
        return None
    idx = 0
    for i, name in enumerate(outcomes):
        if str(name).strip().lower() == "yes":
            idx = i
            break
    try:
        return float(prices[idx])
    except (ValueError, IndexError, TypeError):
        return None


def scoreline_label(m):
    return m.get("groupItemTitle") or m.get("question") or m.get("slug") or "?"


def collect_exact_scores(event):
    rows = []
    for m in event.get("markets") or []:
        if m.get("closed") or not is_exact_score_market(m):
            continue
        try:
            vol = float(m.get("volume") or 0.0)
        except (ValueError, TypeError):
            vol = 0.0
        rows.append({
            "scoreline": scoreline_label(m),
            "yes_price": yes_price(m),
            "volume": vol,
            "market_slug": m.get("slug"),
            "clob_token_ids": parse_json_field(m.get("clobTokenIds"), []),
        })
    return rows


def main():
    ap = argparse.ArgumentParser(description="Polymarket World Cup exact-score odds for upcoming games.")
    ap.add_argument("--hours", type=float, default=24.0, help="Look-ahead window in hours (default 24).")
    ap.add_argument("--tag", default="world-cup", help="Gamma tag slug (default 'world-cup').")
    ap.add_argument("--sort", choices=("volume", "price"), default="volume",
                    help="Rank scorelines by 'volume' (money) or 'price' (probability / most likely).")
    ap.add_argument("--top", type=int, default=5, help="Show top N scorelines per game (default 5).")
    ap.add_argument("--json", metavar="PATH", help="Optional path to dump raw results as JSON.")
    ap.add_argument("--debug", action="store_true", help="Print diagnostics about what the API returns.")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    start_max = now + timedelta(hours=args.hours)

    session = requests.Session()
    session.headers.update({"User-Agent": "wc-exact-score/1.1"})

    try:
        events = fetch_events(session, args.tag, end_after=now)
    except requests.RequestException as exc:
        print(f"ERROR fetching events: {exc}", file=sys.stderr)
        return 1

    if args.debug:
        print(f"[debug] tag_slug={args.tag!r} -> {len(events)} active/unclosed events returned")
        seen_types = {}
        for ev in events[:60]:
            ks = event_kickoff(ev)
            n_markets = len(ev.get("markets") or [])
            n_exact = len(collect_exact_scores(ev))
            for m in ev.get("markets") or []:
                t = (m.get("sportsMarketType") or "").strip().lower()
                if t:
                    seen_types[t] = seen_types.get(t, 0) + 1
            in_win = ks is not None and now <= ks <= start_max
            print(f"[debug]  {ev.get('slug'):<45} kickoff={iso_z(ks) if ks else 'None':<21}"
                  f" in_window={str(in_win):<5} markets={n_markets:<3} exact={n_exact}")
        print(f"[debug] distinct sportsMarketType values seen: "
              f"{sorted(seen_types) if seen_types else '(none)'}")
        print(f"[debug] window: {iso_z(now)} .. {iso_z(start_max)}\n")

    games = []
    for ev in events:
        ks = event_kickoff(ev)
        if ks is None or not (now <= ks <= start_max):
            continue
        scores = collect_exact_scores(ev)
        if not scores:
            continue
        key = (lambda r: (r["volume"] if args.sort == "volume" else (r["yes_price"] or -1.0)))
        scores.sort(key=key, reverse=True)
        games.append({"title": ev.get("title") or ev.get("slug"),
                      "slug": ev.get("slug"),
                      "kickoff_utc": iso_z(ks),
                      "scores": scores})

    games.sort(key=lambda g: g["kickoff_utc"])

    if not games:
        print(f"No World Cup games with exact-score markets kicking off in the next {args.hours:g}h.")
        print("If this seems wrong, re-run with --debug to see event counts, kickoff times,")
        print("and the sportsMarketType values the API is actually returning.")
        return 0

    label = "volume (money)" if args.sort == "volume" else "implied probability"
    print(f"World Cup exact-score markets — next {args.hours:g}h — ranked by {label}\n")
    for g in games:
        print(f"=== {g['title']}   (kickoff {g['kickoff_utc']})")
        print(f"    https://polymarket.com/event/{g['slug']}")
        for r in g["scores"][:args.top]:
            prob = f"{r['yes_price']*100:5.1f}%" if r["yes_price"] is not None else "   n/a"
            print(f"    {r['scoreline']:<22} prob={prob}   vol=${r['volume']:,.0f}")
        money_leader = max(g["scores"], key=lambda r: r["volume"])
        print(f"    -> most money on: {money_leader['scoreline']} (${money_leader['volume']:,.0f})\n")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(games, fh, indent=2)
        print(f"Raw results written to {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
