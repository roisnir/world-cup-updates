# World Cup exact-score → Telegram

Queries the public [Polymarket Gamma API](https://gamma-api.polymarket.com) for
World Cup **exact-score** (correct-score) markets on games kicking off within the
next N hours, ranks each game's scorelines by implied probability or traded
volume, and optionally pushes the result to a Telegram channel as a pre-kickoff
alert.

## Install

```bash
pip install requests        # the only dependency
```

## Run locally (stdout)

```bash
python wc_exact_score.py                  # next 24h, ranked by volume (money)
python wc_exact_score.py --sort price     # rank by implied probability (most likely)
python wc_exact_score.py --hours 12 --top 8
python wc_exact_score.py --debug          # diagnose what the API returns
```

`prob` (price) = most likely scoreline; `vol` = most money traded. They differ,
so the top-probability row and the "most money on" row need not match.

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

`--telegram` sends the same ranked output to the channel **and** still prints to
stdout, so local testing is unaffected. Output is HTML-formatted and chunked to
respect Telegram's 4096-char limit (a game is never split across messages). When
no games qualify, nothing is sent — a scheduled run stays quiet until there is
something to report.

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
