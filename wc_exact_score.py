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
    python wc_exact_score.py                      # next 24h, most-likely exact scores
    python wc_exact_score.py --hours 12 --top 8
    python wc_exact_score.py --no-results         # skip the recent-results section
    python wc_exact_score.py --debug              # show what the API actually returns
    python wc_exact_score.py --json results.json  # also dump raw results
    python wc_exact_score.py --telegram           # ALSO push to a Telegram channel
    python wc_exact_score.py --test-telegram      # send one test message, verify wiring, exit

Note: the default tag is 'fifa-world-cup', which carries the individual MATCH
markets (each match's exact-score ladder is its own "X vs. Y - Exact Score"
event). The 'world-cup' tag holds only tournament futures (group winners,
awards, player props) and has NO per-match exact-score markets.

Telegram (pre-kickoff alerts):
    Set up once:
      1. Create a bot via @BotFather, copy its token.
      2. Add the bot as an *admin* of your target channel.
      3. Put secrets in a .env file next to this script (see .env.example):
           TELEGRAM_BOT_TOKEN=123456:ABC...
           TELEGRAM_CHAT_ID=@yourchannel          # or -100... for a private channel
    Then `--telegram` sends the same ranked output to the channel (and still
    prints to stdout, so local runs are unaffected). With no qualifying games
    nothing is sent, so a scheduled run stays quiet until there's something to say.

    Schedule it with cron, e.g. every 30 min alerting on games within 2h:
      */30 * * * * cd /path/to/repo && /usr/bin/python3 wc_exact_score.py \
                   --telegram --hours 2 >> wc.log 2>&1
    (The script auto-loads ./.env, so cron needs no extra env wiring.)

Dependencies: requests  (pip install requests)

--------------------------------------------------------------------------
Example output  (python wc_exact_score.py --top 5)
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
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

GAMMA = "https://gamma-api.polymarket.com"

TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_MAX_CHARS = 4096                                        # Telegram per-message hard cap

EXACT_SCORE_TYPES = {"scores", "exact_score", "exact-score", "correct_score", "correctscore"}
SCORELINE_RE = re.compile(r"^\s*\d+\s*[-–:]\s*\d+\s*$")          # "2-1", "0 - 0"
EXACT_SCORE_TEXT_RE = re.compile(r"exact\s*score|correct\s*score", re.IGNORECASE)
SCORE_DIGITS_RE = re.compile(r"(\d+)\s*[-–:]\s*(\d+)")           # pull "0 - 1" out of any label
EXACT_SCORE_SUFFIX_RE = re.compile(r"\s*[-–]\s*Exact Score\s*$", re.IGNORECASE)

# Display timezone for the Telegram message. zoneinfo is stdlib (3.9+) and reads
# the OS tz database, so `requests` stays the only pip dependency. If the tz data
# is missing (rare on Linux; possible on bare Windows -> `pip install tzdata`),
# fall back to a fixed +03:00, correct for the World Cup window (Israel summer/IDT).
def _jerusalem_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Asia/Jerusalem")
    except Exception:
        return timezone(timedelta(hours=3))

JERUSALEM_TZ = _jerusalem_tz()


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


def fetch_events(session, tag_slug, end_date_min, closed=False, active=True, page_size=100):
    """
    Page through GET /events for a tag. We do NOT filter on start_date here:
    for sports, an event's startDate is when the market OPENED (often well in
    the past), so a start_date_min filter would wrongly drop upcoming games.
    Real kickoff time is filtered client-side.

    `closed=False` keeps upcoming/live events (end_date_min=now drops finished
    games). `closed=True` fetches resolved events for the recent-results lookup
    (pass end_date_min = now - window to bound how far back we page).
    """
    events, offset = [], 0
    while True:
        params = {
            "tag_slug": tag_slug,
            "related_tags": "true",
            "closed": "true" if closed else "false",
            "end_date_min": iso_z(end_date_min),
            "limit": page_size,
            "offset": offset,
        }
        if active is not None:
            params["active"] = "true" if active else "false"
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
    # Polymarket prefixes the type per sport, e.g. "soccer_exact_score", so match
    # on a substring rather than an exact set (the set stays as an extra safety net).
    if smt in EXACT_SCORE_TYPES or "exact_score" in smt or "correct_score" in smt:
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


def clean_title(title):
    """Drop the ' - Exact Score' suffix so a heading reads as just the fixture."""
    return EXACT_SCORE_SUFFIX_RE.sub("", str(title or "")).strip()


def score_digits(label):
    """Pull the bare 'H - A' out of a scoreline label, e.g.
    'Netherlands 0 - 1 Sweden' -> '0 - 1'. None if the label has no score
    (the 'Any Other Score' bucket)."""
    m = SCORE_DIGITS_RE.search(str(label or ""))
    return f"{m.group(1)} - {m.group(2)}" if m else None


def finished_result(event):
    """For a CLOSED exact-score event, the market that resolved to Yes (price ≈ 1)
    is the actual final score. Returns the winning market's full label
    (e.g. 'Türkiye 0 - 1 Paraguay', or the 'Any Other Score' bucket), or None if
    nothing resolved yet / it isn't an exact-score event."""
    for m in event.get("markets") or []:
        if not is_exact_score_market(m):
            continue
        yp = yes_price(m)
        if yp is not None and yp >= 0.9:
            return scoreline_label(m)
    return None


def fmt_jerusalem(dt):
    """Format a UTC datetime in Israel local time, e.g. '20/06 20:00'."""
    return dt.astimezone(JERUSALEM_TZ).strftime("%d/%m %H:%M")


def specific_scores(scores):
    """Drop the 'Any Other Score' catch-all (any label with no numeric score) so
    displays show only concrete scorelines. Crucially this filters BEFORE the
    top-N slice, so --top 3 yields 3 real scores rather than 3-minus-the-bucket.
    Falls back to the full list if a game somehow has only the catch-all."""
    concrete = [r for r in scores if score_digits(r["scoreline"]) is not None]
    return concrete or scores


def score_sort_key(row):
    """Sort key (used with reverse=True): most likely first, ties broken by money.
    Price is rounded to the displayed precision (1 decimal of a percent = 3
    decimals of price) so two scorelines that *show* the same percentage are
    treated as a genuine tie and ordered by volume."""
    price = row["yes_price"] if row["yes_price"] is not None else -1.0
    return (round(price, 3), row["volume"])


def build_games(events, now, start_max):
    """Upcoming events with exact-score markets kicking off in (now, start_max],
    each with its scorelines ranked by probability (volume breaks ties)."""
    games = []
    for ev in events:
        ks = event_kickoff(ev)
        if ks is None or not (now <= ks <= start_max):
            continue
        scores = collect_exact_scores(ev)
        if not scores:
            continue
        scores.sort(key=score_sort_key, reverse=True)
        games.append({
            "title": clean_title(ev.get("title") or ev.get("slug")),
            "slug": ev.get("slug"),
            "kickoff_utc": iso_z(ks),
            "kickoff_il": fmt_jerusalem(ks),
            "scores": scores,
        })
    games.sort(key=lambda g: g["kickoff_utc"])
    return games


def build_results(events, start_min, now):
    """Finished matches that kicked off in [start_min, now], with the real final
    score derived from whichever exact-score market resolved to Yes."""
    results = []
    for ev in events:
        ks = event_kickoff(ev)
        if ks is None or not (start_min <= ks <= now):
            continue
        label = finished_result(ev)
        if not label:
            continue
        results.append({
            "title": clean_title(ev.get("title") or ev.get("slug")),
            "slug": ev.get("slug"),
            "kickoff_utc": iso_z(ks),
            "kickoff_il": fmt_jerusalem(ks),
            "result_label": label,
            "score": score_digits(label),
        })
    results.sort(key=lambda r: r["kickoff_utc"], reverse=True)   # most recent first
    return results


def load_env_file(path):
    """
    Minimal `.env` loader (KEY=VALUE per line, # comments allowed). Existing
    environment variables always win, so an explicit `export` overrides the file.
    Kept dependency-free on purpose so `requests` stays the only requirement —
    handy for cron, where the shell environment is otherwise bare.
    Returns the number of keys set (0 if the file is missing).
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return 0
    set_count = 0
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            set_count += 1
    return set_count


HE_HEADER = "מונדיאל ⚽ — מה מהמרים בפולימרקט"

# In Telegram-HTML only & < > need escaping; quotes read cleaner left alone
# (e.g. "Côte d'Ivoire"). Slugs are URL-safe so the href needs no quote-escaping.
def _esc(text):
    return html.escape(str(text), quote=False)


def _vol_compact(v):
    """13509.0 -> '$13.5k'; 980 -> '$980'."""
    return f"${v / 1000:.1f}k" if v >= 1000 else f"${v:,.0f}"


def _flag_from_iso(iso2):
    """ISO-3166 alpha-2 -> regional-indicator flag emoji, e.g. 'NL' -> 🇳🇱."""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in iso2)


def _subdivision_flag(tag):
    """England/Scotland/Wales flags use GB subdivision tag sequences."""
    return "\U0001F3F4" + "".join(chr(0xE0000 + ord(c)) for c in tag) + "\U000E007F"


# Polymarket team name -> ISO-3166 alpha-2. Covers the 2026 field and common
# aliases; an unknown name just yields no flag (graceful).
_TEAM_ISO = {
    "Netherlands": "NL", "Sweden": "SE", "Germany": "DE", "Spain": "ES",
    "Belgium": "BE", "France": "FR", "Portugal": "PT", "Italy": "IT",
    "Croatia": "HR", "Switzerland": "CH", "Denmark": "DK", "Poland": "PL",
    "Austria": "AT", "Serbia": "RS", "Ukraine": "UA", "Czechia": "CZ",
    "Czech Republic": "CZ", "Türkiye": "TR", "Turkey": "TR", "Norway": "NO",
    "Hungary": "HU", "Greece": "GR", "Romania": "RO", "Slovenia": "SI",
    "Slovakia": "SK", "Albania": "AL", "Republic of Ireland": "IE", "Ireland": "IE",
    "Iceland": "IS", "Finland": "FI", "Russia": "RU",
    "Brazil": "BR", "Argentina": "AR", "Uruguay": "UY", "Colombia": "CO",
    "Chile": "CL", "Peru": "PE", "Paraguay": "PY", "Ecuador": "EC",
    "Bolivia": "BO", "Venezuela": "VE",
    "United States": "US", "USA": "US", "Mexico": "MX", "Canada": "CA",
    "Costa Rica": "CR", "Panama": "PA", "Honduras": "HN", "Jamaica": "JM",
    "Haiti": "HT", "Curaçao": "CW", "El Salvador": "SV", "Guatemala": "GT",
    "Trinidad and Tobago": "TT",
    "Morocco": "MA", "Senegal": "SN", "Côte d'Ivoire": "CI", "Ivory Coast": "CI",
    "Cameroon": "CM", "Ghana": "GH", "Nigeria": "NG", "Tunisia": "TN",
    "Algeria": "DZ", "Egypt": "EG", "Mali": "ML", "South Africa": "ZA",
    "Cabo Verde": "CV", "Cape Verde": "CV", "DR Congo": "CD", "Burkina Faso": "BF",
    "Guinea": "GN", "Angola": "AO",
    "Japan": "JP", "Korea Republic": "KR", "South Korea": "KR", "Korea DPR": "KP",
    "Iran": "IR", "IR Iran": "IR", "Saudi Arabia": "SA", "Australia": "AU",
    "Qatar": "QA", "Iraq": "IQ", "United Arab Emirates": "AE", "Uzbekistan": "UZ",
    "Jordan": "JO", "Oman": "OM", "China": "CN", "China PR": "CN", "Bahrain": "BH",
    "Indonesia": "ID", "Vietnam": "VN", "Thailand": "TH",
    "New Zealand": "NZ",
}
_TEAM_FLAG_SPECIAL = {
    "England": _subdivision_flag("gbeng"),
    "Scotland": _subdivision_flag("gbsct"),
    "Wales": _subdivision_flag("gbwls"),
}
_VS_RE = re.compile(r"\s+vs\.?\s+", re.IGNORECASE)


def team_flag(name):
    name = (name or "").strip()
    if name in _TEAM_FLAG_SPECIAL:
        return _TEAM_FLAG_SPECIAL[name]
    iso = _TEAM_ISO.get(name)
    return _flag_from_iso(iso) if iso else ""


def split_teams(title):
    """'Germany vs. Côte d'Ivoire' -> ('Germany', \"Côte d'Ivoire\")."""
    parts = _VS_RE.split(str(title or ""), maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return str(title or "").strip(), ""


def team_label(name):
    """Flag + name, with no stray space when the flag is unknown."""
    return f"{team_flag(name)} {_esc(name)}".strip()


def favored_team(home, away, score_label):
    """Which side a scoreline favours: home, away, or None for a draw / unknown."""
    digits = score_digits(score_label)
    nums = re.findall(r"\d+", digits) if digits else []
    if len(nums) < 2:
        return None
    h, a = int(nums[0]), int(nums[1])
    return home if h > a else away if a > h else None


def format_game_hebrew(game, top):
    """One upcoming game as a Hebrew Telegram-HTML block: fixture, Israel-local
    kickoff, the most-likely scorelines, and where the money is."""
    slug = html.escape(str(game["slug"]))                       # href value -> full escape
    concrete = specific_scores(game["scores"])                  # drop 'Any Other Score' before top-N
    home, away = split_teams(game["title"])
    # Fixture (with flags) and kickoff on one line; tz is obvious in context.
    lines = [f"<b>{team_label(home)} vs. {team_label(away)}</b> · {_esc(game['kickoff_il'])}"]
    for r in concrete[:top]:
        prob = f"{r['yes_price'] * 100:.1f}%" if r["yes_price"] is not None else "—"
        sc = (score_digits(r["scoreline"]) or "אחר").replace(" ", "")
        lines.append(f"• {sc} — {prob} · {_vol_compact(r['volume'])}")
    leader = max(concrete, key=lambda r: r["volume"])
    lsc = (score_digits(leader["scoreline"]) or "אחר").replace(" ", "")
    fav = favored_team(home, away, leader["scoreline"])
    favour = f"לטובת {team_label(fav)}" if fav else "תיקו"
    link = f'<a href="https://polymarket.com/event/{slug}">#</a>'
    lines.append(f"💰 הכי הרבה כסף על {lsc} ({favour}) {link}")
    return "\n".join(lines)


def format_results_hebrew(results, hours):
    """The recent finished matches and their real final scores, as one block."""
    lines = [f"✅ <b>תוצאות מה-{hours:g} השעות האחרונות</b>"]
    for r in results:
        home, away = split_teams(r["title"])
        fh, fa = team_flag(home), team_flag(away)
        if r["score"]:                                          # numeric scoreline known
            lines.append("• " + " ".join(filter(None, [fh, _esc(r["result_label"]), fa])))
        else:                                                   # 'Any Other Score' won
            lines.append(f"• {team_label(home)} vs. {team_label(away)} — תוצאה אחרת")
    return "\n".join(lines)


def telegram_blocks(games, results, top, hours):
    """Assemble the full Hebrew message as a list of blocks for pack_blocks()."""
    blocks = []
    head = [f"<b>{_esc(HE_HEADER)}</b>"]
    blocks.append("\n".join(head))
    for g in games:
        blocks.append(format_game_hebrew(g, top))
    if results:
        blocks.append(format_results_hebrew(results, hours))
    return blocks


def pack_blocks(blocks, limit=TELEGRAM_MAX_CHARS, sep="\n"):
    """Pack rendered blocks into the fewest messages <= `limit` chars, never
    splitting a block across messages. A single oversize block is hard-split
    as a last resort so nothing is silently dropped."""
    messages, current = [], ""
    for block in blocks:
        if len(block) > limit:
            if current:
                messages.append(current)
                current = ""
            for i in range(0, len(block), limit):
                messages.append(block[i:i + limit])
            continue
        candidate = block if not current else current + sep + block
        if len(candidate) > limit:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


def send_telegram(session, messages, token, chat_id, parse_mode="HTML"):
    """POST each message to Telegram's sendMessage. Honours 429 retry_after and
    surfaces Telegram's own error text (e.g. 'chat not found', 'not enough
    rights to send text messages') instead of a bare HTTP status."""
    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    for msg in messages:
        payload = {
            "chat_id": chat_id,
            "text": msg,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        for attempt in range(3):
            r = session.post(url, json=payload, timeout=30)
            try:
                body = r.json()
            except ValueError:
                body = {}
            if r.status_code == 429:
                retry_after = (body.get("parameters") or {}).get("retry_after", 1)
                time.sleep(retry_after + 0.5)
                continue
            if not body.get("ok", False):
                desc = body.get("description") or f"HTTP {r.status_code}"
                raise RuntimeError(f"Telegram API error ({r.status_code}): {desc}")
            break
        else:
            raise RuntimeError("Telegram API error: still rate-limited after 3 attempts")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Polymarket World Cup exact-score odds for upcoming games.")
    ap.add_argument("--hours", type=float, default=24.0, help="Look-ahead window in hours (default 24).")
    ap.add_argument("--tag", default="fifa-world-cup",
                    help="Gamma tag slug (default 'fifa-world-cup' — the tag that carries "
                         "individual match markets; 'world-cup' holds only tournament futures).")
    ap.add_argument("--top", type=int, default=5, help="Show top N scorelines per game (default 5).")
    ap.add_argument("--results", action=argparse.BooleanOptionalAction, default=True,
                    help="Include a section of real final scores from the matching window in the "
                         "recent past (default on; use --no-results to skip the extra fetch).")
    ap.add_argument("--json", metavar="PATH", help="Optional path to dump raw results as JSON.")
    ap.add_argument("--telegram", action="store_true",
                    help="Also push the ranked output to a Telegram channel "
                         "(reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID).")
    ap.add_argument("--test-telegram", action="store_true",
                    help="Send a single test message to verify the bot/chat wiring, then exit "
                         "(does not query Polymarket).")
    default_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    ap.add_argument("--env-file", default=default_env, metavar="PATH",
                    help="Path to a .env file with secrets (default: .env next to this script).")
    ap.add_argument("--debug", action="store_true", help="Print diagnostics about what the API returns.")
    args = ap.parse_args(argv)

    loaded = load_env_file(args.env_file)
    if args.debug and loaded:
        print(f"[debug] loaded {loaded} key(s) from {args.env_file}")

    tg_token = tg_chat = None
    if args.telegram or args.test_telegram:
        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        tg_chat = os.environ.get("TELEGRAM_CHAT_ID")
        missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", tg_token),
                                  ("TELEGRAM_CHAT_ID", tg_chat)) if not v]
        if missing:
            flag = "--test-telegram" if args.test_telegram else "--telegram"
            print(f"ERROR: {flag} needs {' and '.join(missing)} "
                  f"(set them in {args.env_file} or the environment).", file=sys.stderr)
            return 2

    session = requests.Session()
    session.headers.update({"User-Agent": "wc-exact-score/1.1"})

    if args.test_telegram:
        try:
            send_telegram(session, ["✅ <b>wc_exact_score</b> — Telegram wiring OK"],
                          tg_token, tg_chat)
        except (requests.RequestException, RuntimeError) as exc:
            print(f"Telegram test FAILED: {exc}", file=sys.stderr)
            return 1
        print(f"Telegram test message sent to {tg_chat}. Check the channel.")
        return 0

    now = datetime.now(timezone.utc)
    start_max = now + timedelta(hours=args.hours)
    start_min = now - timedelta(hours=args.hours)               # results look-back window

    try:
        events = fetch_events(session, args.tag, end_date_min=now, closed=False, active=True)
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

    games = build_games(events, now, start_max)

    # Recent results: the same-length window in the past, from CLOSED events.
    results = []
    if args.results:
        try:
            past = fetch_events(session, args.tag,
                                end_date_min=now - timedelta(hours=args.hours + 6),
                                closed=True, active=None)
            results = build_results(past, start_min, now)
        except requests.RequestException as exc:
            print(f"WARNING: could not fetch recent results: {exc}", file=sys.stderr)

    if not games and not results:
        print(f"No World Cup exact-score markets in the ±{args.hours:g}h window around now.")
        print("If this seems wrong, re-run with --debug to see event counts, kickoff times,")
        print("and the sportsMarketType values the API is actually returning.")
        return 0

    if games:
        print(f"World Cup exact-score markets — next {args.hours:g}h — ranked by implied probability\n")
        for g in games:
            concrete = specific_scores(g["scores"])
            print(f"=== {g['title']}   (kickoff {g['kickoff_utc']} | {g['kickoff_il']} IL)")
            print(f"    https://polymarket.com/event/{g['slug']}")
            for r in concrete[:args.top]:
                prob = f"{r['yes_price']*100:5.1f}%" if r["yes_price"] is not None else "   n/a"
                print(f"    {r['scoreline']:<28} prob={prob}   vol=${r['volume']:,.0f}")
            print()

    if results:
        print(f"Recent results — last {args.hours:g}h:")
        for r in results:
            shown = r["result_label"] if r["score"] else f"{clean_title(r['title'])} — Any Other Score"
            print(f"    {shown}   (kickoff {r['kickoff_il']} IL)")
        print()

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"games": games, "results": results}, fh, indent=2)
        print(f"Raw results written to {args.json}")

    if args.telegram:
        messages = pack_blocks(telegram_blocks(games, results, args.top, args.hours))
        try:
            send_telegram(session, messages, tg_token, tg_chat)
        except (requests.RequestException, RuntimeError) as exc:
            print(f"ERROR sending to Telegram: {exc}", file=sys.stderr)
            return 1
        print(f"Sent to Telegram in {len(messages)} message(s) "
              f"({len(games)} upcoming, {len(results)} result(s)).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
