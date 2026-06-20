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
`מונדיאל ⚽ — מה מהמרים בפולימרקט`, the most-likely scorelines per upcoming game
with kickoffs in **Israel time**, and the recent real results — **and** still
prints the English breakdown to stdout, so local testing is unaffected. Output is
HTML-formatted and chunked to respect Telegram's 4096-char limit (a game is never
split across messages). When there are no upcoming games and no recent results,
nothing is sent — a scheduled run stays quiet until there is something to report.

## WhatsApp alerts (Baileys)

The same Hebrew update can also be posted to a **WhatsApp group**, in parallel to
Telegram. WhatsApp has no bot API for groups, so this uses
[Baileys](https://github.com/WhiskeySockets/Baileys) (the WhatsApp Web
multi-device library) through a tiny Node.js bridge in [`whatsapp/`](whatsapp/).
Python shells out to it; Node is only needed for this feature.

One-time setup:

1. Install Node 18+ and the bridge's dependencies:
   ```bash
   cd whatsapp && npm install && cd ..
   ```
2. **Pair** the machine with your WhatsApp account (like linking WhatsApp Web).
   This prints a QR — scan it from your phone under
   *WhatsApp → Linked devices → Link a device*:
   ```bash
   python wc_exact_score.py --whatsapp-login
   ```
   Credentials are saved under `whatsapp/auth/` (gitignored). Treat that folder
   like a password — anyone with it can post as your account.
3. Find your group's **JID** and put it in `.env`:
   ```bash
   python wc_exact_score.py --whatsapp-groups   # lists groups: [{ "jid": "...@g.us", "name": ... }]
   ```
   ```
   WHATSAPP_GROUP_JID=123456789-123456@g.us
   ```

Verify the wiring (sends one test message, doesn't touch Polymarket):

```bash
python wc_exact_score.py --test-whatsapp
```

Then send real updates — combine with `--telegram` to post to both at once:

```bash
python wc_exact_score.py --whatsapp --hours 2
python wc_exact_score.py --telegram --whatsapp --hours 2
```

The WhatsApp message carries the same content as Telegram, rendered in WhatsApp
markup (`*bold*`, bare auto-linked Polymarket URLs) instead of HTML. As with
Telegram, nothing is sent when there are no upcoming games and no recent results.

Notes for cron: the bridge needs Node on `PATH` — set `WHATSAPP_NODE=/usr/bin/node`
(from `which node`) in `.env` if it isn't. Override the credentials location with
`WHATSAPP_AUTH_DIR=/abs/path`. Each run connects, posts, and disconnects, so the
phone need not be online once paired (multi-device works offline).

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
