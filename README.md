# World Cup exact-score → Telegram

Queries the public [Polymarket Gamma API](https://gamma-api.polymarket.com) for
World Cup **exact-score** (correct-score) markets on games kicking off within the
next N hours, ranks each game's scorelines by implied probability (default) or
traded volume, appends the **real final scores** from the matching window in the
recent past, and optionally pushes a **Hebrew** summary (kickoffs in Israel time)
to a Telegram channel as a pre-kickoff alert.

## Install

```bash
pip install requests        # the only dependency
```

`zoneinfo` (stdlib) supplies the Israel timezone from the OS tz database. On a
minimal system without it (rare on Linux), `pip install tzdata`.

## Run locally (stdout)

```bash
python wc_exact_score.py                  # next 24h, most-likely exact scores
python wc_exact_score.py --hours 12 --top 8
python wc_exact_score.py --no-results     # skip the recent-results section
python wc_exact_score.py --debug          # diagnose what the API returns
```

Scorelines are ranked by `prob` (implied probability) — the top row is the most
likely exact score. `vol` (money traded on that scoreline) breaks ties when two
scores share the same probability. The "Any Other Score" catch-all is excluded.

Each game also shows its **match-winner (1X2) odds** — `P(home win)` / `P(draw)` /
`P(away win)` — independent of the exact score, e.g.
`moneyline: Netherlands 56% / Draw 24% / Sweden 20%`. Polymarket carries these in a
separate `X vs. Y` event (the `moneyline` `sportsMarketType`, one Yes/No market per
side) which the tool matches back to the fixture by title + kickoff. If a game has
no moneyline market the line is omitted.

The **recent results** section (on by default) reports the actual final scores of
matches that kicked off in the last N hours — derived from whichever exact-score
market resolved to Yes. It costs one extra API call; disable with `--no-results`.

It queries the `fifa-world-cup` tag by default — that's the one carrying the
individual **match** markets (each match's exact-score ladder is its own
`X vs. Y - Exact Score` event). The `world-cup` tag holds only tournament
futures (group winners, awards, player props) and has no per-match scores.
Override with `--tag` if Polymarket reorganizes.

## Telegram alerts

One-time setup:

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Add the bot as an **admin** of your target channel (required to post).
3. `cp .env.example .env` and fill in:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_CHAT_ID=@yourchannel        # or -100... for a private channel
   ```

Verify the bot/channel wiring in one shot (sends a test message, doesn't touch
Polymarket):

```bash
python wc_exact_score.py --test-telegram
```

A failure prints Telegram's own reason (e.g. `chat not found`, `not enough
rights`). Once that works:

```bash
python wc_exact_score.py --telegram --hours 2
```

`--telegram` sends the channel a **Hebrew** message — header
`מונדיאל ⚽ — מה מהמרים בפולימרקט`, a 🏆 match-winner odds line per game
(`🏆 🇳🇱 56% · תיקו 24% · 🇸🇪 20%`), the most-likely scorelines per upcoming game
with kickoffs in **Israel time**, and the recent real results — **and** still
prints the English breakdown to stdout, so local testing is unaffected. Output is
HTML-formatted and chunked to respect Telegram's 4096-char limit (a game is never
split across messages). When there are no upcoming games and no recent results,
nothing is sent — a scheduled run stays quiet until there is something to report.

## Tests

Whole-flow tests mock only the network boundary (HTTP get/post) and run the real
`main()` end-to-end — parsing, ranking, the Hebrew formatting and the results
derivation all execute against canned Polymarket-shaped payloads:

```bash
python -m unittest test_wc_exact_score -v
```

The script auto-loads `./.env` (existing environment variables take precedence),
so no `python-dotenv` dependency and no extra cron wiring is needed. Point it
elsewhere with `--env-file /path/to/.env`.

## Schedule with cron

Every 30 minutes, alert on games kicking off within the next 2 hours:

```cron
*/30 * * * * cd /data/dev/world-cup-updates && /usr/bin/python3 wc_exact_score.py --telegram --hours 2 >> wc.log 2>&1
```

Notes:

- Use an absolute `python3` path (`which python3`) — cron's `PATH` is minimal.
- `cd` into the repo so `./.env` is found (or pass `--env-file`).
- A missing/invalid secret exits non-zero and logs to `wc.log`, so a
  misconfigured job surfaces immediately rather than failing silently.

## Run in Docker

The image runs the script **once and exits** — you schedule it from the host
(cron, a systemd timer, or a Kubernetes `CronJob`). This keeps the image small
and the scheduling where the OS already does it well. Build it once:

```bash
docker build -t wc-alerts .
```

Send a single alert — mount your `.env` read-only where the script looks for it:

```bash
docker run --rm -v "$PWD/.env:/app/.env:ro" wc-alerts --telegram --hours 2
```

Everything after the image name is passed straight to `wc_exact_score.py`, so
`--test-telegram`, `--no-results`, `--debug`, etc. all work. (The default command
is `--telegram --hours 2`.) You can also pass the secrets inline instead of
mounting a file — the script reads them from the environment:

```bash
docker run --rm \
  -e TELEGRAM_BOT_TOKEN=123456:ABC... \
  -e TELEGRAM_CHAT_ID=@yourchannel \
  wc-alerts --telegram --hours 2
```

### Schedule it with host cron

Every 30 minutes, alert on games kicking off within the next 2 hours:

```cron
*/30 * * * * docker run --rm -v /opt/wc/.env:/app/.env:ro wc-alerts --telegram --hours 2 >> /var/log/wc-alerts.log 2>&1
```

Notes:

- Use an **absolute** path for the mounted `.env` — cron has no `$PWD`.
- Each fire is a fresh, short-lived container (`--rm` cleans it up); no
  long-running daemon or `--restart` policy to manage.
- A missing/invalid secret exits non-zero, so a misconfigured job surfaces in
  the log rather than failing silently. With nothing to report the run is quiet.
- The schedule fires in the **host's** timezone; kickoff times in the message
  are always shown in Israel time regardless.
