# Bot Stats Updater

Automatically update your Discord bot statistics on top.gg and discordbotlist.com. Run as many bots as you like from a single config file.

It talks to Discord over REST only and never connects to the gateway, so it needs no privileged intents and works the same for sharded and unsharded bots.

## Features

- Multiple bots from one config file
- Server, member, shard and user-install counts posted to top.gg and discordbotlist.com
- Slash commands synced to both listings
- Optional channel that gets renamed with your live server count
- Optional Discord webhook alerts when something goes wrong
- Configurable retries on logins, counts and listing posts
- Docker support with a healthcheck

## Installation

1. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

2. Copy `config.example.json` to `config.json` and fill in your tokens.

3. Run it:

   ```bash
   python main.py
   ```

   On Windows you can double-click `start.bat` instead.

**Note:** Bot ID and name are automatically detected from the Discord token.

### Docker

With `config.json` in place, start the container:

```bash
docker compose up -d --build
```

`config.json` is mounted into the container rather than baked into the image, so your tokens stay out of the image.

Two helper scripts are included for everyday use:

| Script | What it does |
|---|---|
| `rebuild.sh` / `rebuild.bat` | Rebuilds the image and recreates the container — use after changing the code |
| `reload.sh` / `reload.bat` | Recreates the container without rebuilding — use after changing `config.json` |

### Healthcheck

Each completed cycle stamps `heartbeat` with the time by which the *next* cycle must finish (two intervals plus five minutes). `docker-compose.yml` reads it, so `docker ps` shows the container as unhealthy if cycles stop running.

The file is created at runtime and is not part of the repository.

## Getting API Tokens

**Discord:** <https://discord.com/developers/applications> → your app → Bot → Reset Token

**Top.gg:** <https://top.gg/bot/YOUR_BOT_ID/webhooks>

**DiscordBotList.com:** <https://discordbotlist.com/bots/YOUR_BOT_ID/edit>

## Configuration

The config is read once at startup, so restart the bot after editing it.

### Global options

- `update_interval_minutes`: How often to update stats (default: `30`). One update also runs immediately at startup.
- `max_attempts`: How many times each retryable step is tried before it gives up (default: `2`, maximum `5`). `1` disables retrying. See [Retries](#retries).
- `attempt_delay_seconds`: Seconds to wait between attempts (default: `2`, maximum `60`). This is a *floor*: every retry waits this plus up to 25% jitter, and when a platform answers `429` with a `Retry-After`, the longer of the two is used.
- `alert_webhook_url`: Discord webhook that receives `ERROR` and `CRITICAL` log lines, batched every 10s (default: none). Without it, a revoked token only shows up in `docker logs`.
- `alert_after_failures`: How many times in a row something must fail before it reaches the webhook (default: `1`, meaning alert straight away). `2` stays quiet about a one-off blip and only tells you once it has failed twice running. A success resets the count. See [Alert volume](#alert-volume).
- `heartbeat_file`: File stamped after each cycle, read by the Docker healthcheck (default: `heartbeat`). Set to `""` to disable.

### Per-bot options

| Key | Required | Description |
|-----|----------|-------------|
| `bot_token` | Yes | Discord bot token |
| `name` | No | Overrides the name shown in logs and in `{bot_name}`. Defaults to the bot's Discord username. |
| `topgg_token` | No | Top.gg API token. Leave it out to skip top.gg for this bot. |
| `dbl_token` | No | DiscordBotList.com API token. Leave it out to skip discordbotlist.com for this bot. |
| `shard_count` | No | Number of shards this bot actually runs. Omit to report Discord's recommended count. |
| `report_server_count` | No | `false` stops reporting servers/shards, and skips discordbotlist.com entirely. Use it for a user-install-only app (default: `true`). |
| `report_user_installs` | No | `false` stops reporting user-app installs to top.gg (default: `true`). |
| `server_count_channel_id` | No | ID of a voice/text channel to rename with the server count |
| `server_count_channel_format` | No | Custom format for the channel name (see below) |

### Which metrics get reported

Both metrics are independent, so a bot only needs to report what it actually has. top.gg's metrics route is a `PATCH`, meaning a metric you switch off is *left at its previous value* rather than zeroed.

| Bot type | Config |
|---|---|
| Normal guild bot | nothing — both default to `true` |
| User-install-only app | `"report_server_count": false` |
| Guild bot, no user-app | `"report_user_installs": false` |

Switching both off leaves nothing to send, which is logged and skipped. The server count channel is renamed either way — these options only control what gets sent to the listings.

### Slash Commands

Each update also pushes the bot's global slash commands to both listings, so their command lists stay in sync with what the bot actually has. Subcommands are flattened first, since neither site shows nested commands — `/config set` is listed as `config set`. Guild-only commands are not included.

## Retries

Anything that talks to Discord or a listing site is tried up to `max_attempts` times, waiting `attempt_delay_seconds` between attempts.

| Step | Retried | When it gives up |
|---|---|---|
| Discord login | Yes | The bot is skipped this cycle and tried again on the next one |
| Server count | Yes — the failing page is re-fetched and the walk resumes where it stopped | The whole bot is skipped; a partial count is never posted |
| Slash command fetch | Yes | Commands are not synced this cycle; the stats posted above still count |
| Posts to top.gg / discordbotlist.com | Yes | That post is logged `[FAIL]` |
| Server count channel rename | No | The rename is skipped and retried next cycle |
| Shard count | No | Falls back to `1` |
| User install count | No | Left out of the payload, so the listing keeps its previous figure |

Some failures will never succeed on a second try, so they are not retried at all:

| Condition | Why |
|---|---|
| `401` / `403` from a listing | The token is wrong — logged `CRITICAL`, so it reaches your alert webhook |
| Any other `4xx` that is not `429` | The request itself is wrong |
| Discord rejects the bot token at login | The bot is disabled for the rest of the run, so a revoked token costs no requests |
| `bot_token` missing | Config error |

If Discord rejects an *already logged-in* bot with a `401` — which is what resetting the token in the developer portal looks like — the session is dropped and the bot logs in again on the next cycle.

**Rate limits.** On a `429` the wait is the longer of `attempt_delay_seconds` and the platform's `Retry-After`, plus a little jitter so several bots that fail at the same moment don't all retry in lockstep. If a platform asks for longer than two minutes, the step is skipped until the next cycle rather than retried early. Discord's own client already retries internally, so these attempts sit on top of that — `2` is usually enough, and `5` is the maximum.

**Retries never delay the schedule.** A cycle stops retrying once it has used 80% of `update_interval_minutes`, so retries can't push a cycle past its next tick and silently skip it. If `max_attempts` and `attempt_delay_seconds` can't fit in that budget, a warning is logged at startup telling you which one to change.

**Retries don't spam your webhook.** Individual attempts are logged at `WARNING`, which is console-only; only the final failure of a step is `ERROR`. Raising `max_attempts` therefore adds no webhook traffic at all. The trade-off is that a call which fails once and then succeeds no longer reaches the webhook — look in `docker logs` for `retrying in` and `succeeded on attempt` to spot a platform that is flapping.

## Alert volume

Two separate things decide how much lands in your webhook, and they work at different timescales.

**Within one cycle**, `max_attempts` costs you nothing. Each attempt logs at `WARNING` (console only) and only the final give-up is `ERROR`, so a step that fails and then succeeds sends nothing at all, and a step that fails outright sends exactly one line whether `max_attempts` is 2 or 5.

**Across cycles** is what `alert_after_failures` controls. A platform that stays down fails once per cycle, every cycle, so at the default of `1` an overnight top.gg outage fills your webhook. Raise it and a step has to fail that many cycles in a row before it says anything:

| `alert_after_failures` | What reaches the webhook |
|---|---|
| `1` (default) | Every failed cycle, immediately |
| `2` | Nothing until it has failed twice running — a single bad cycle stays silent |
| `3` | Nothing until the third consecutive failure |

At `update_interval_minutes: 30`, setting `3` means roughly "don't tell me unless it has been broken for an hour and a half".

It delays the *first* alert; it does not thin out the ones after it. Once a step has crossed the threshold it reports every failed cycle until it recovers, so a 12-hour top.gg outage at a 30-minute interval sends 22 messages with `3` instead of 24 with `1`. The setting is there to stop a one-off blip waking you, not to summarise an ongoing outage.

The count is kept **per bot and per step**, so one bot failing does not bring another closer to alerting, and a failing top.gg post does not count towards the server-count step. Any success clears that step's count back to zero.

Suppressed failures are never hidden from you locally — they log to the console at `WARNING` with the running count, e.g. `... (failure 1 of 3 before alerting)`.

**What this never delays.** `CRITICAL` ignores `alert_after_failures` entirely and always goes out on the first occurrence, because none of it heals on its own:

- a listing rejecting your token (`401` / `403`)
- Discord rejecting the bot token at login
- a missing `bot_token`
- an update cycle crashing outright

So raising the threshold stops brief hiccups reaching you, without ever delaying the news that something is genuinely, permanently broken.

## Server Count Channel

When `server_count_channel_id` is set, the bot will rename that channel on every stats update to reflect its current server count.

**Format string rules (`server_count_channel_format`):**

| Config value | Result |
|---|---|
| *(omitted)* | `BotName: 2553` |
| `"Servers: {count}"` | `Servers: 2553` |
| `"Servers: {server_count:,}"` | `Servers: 2,553` |
| `"{bot_name}: {member_count:,} users"` | `PurgeBot: 655,270 users` |
| `"Servers: {server_count:,}{if shard_count > 1} │ Shards: {shard_count}{end}"` | `Servers: 2,553 │ Shards: 3`, or `Servers: 493` for an unsharded bot |

**Available placeholders:**

| Placeholder | Value |
|---|---|
| `{server_count}` | Servers the bot is in |
| `{count}` | Alias of `{server_count}` |
| `{shard_count}` | The same number reported to top.gg (see `shard_count` above) |
| `{member_count}` | Total members across all servers — Discord's approximate counts, summed, the same figure sent to discordbotlist.com |
| `{bot_name}` | The bot's name, from `name` or auto-detected from the token |
| `{user_install_count}` | User-app installs |
| `{bot_id}` | The bot's user ID |

Add `:,` inside any numeric placeholder for thousands separators: `{member_count:,}` → `655,270`.

**Conditional sections:**

```
{if shard_count > 1} │ Shards: {shard_count}{end}
{if shard_count > 1}Shards: {shard_count}{else}Unsharded{end}
```

`{if <placeholder> <op> <number>}` … optional `{else}` … `{end}`, where `<op>` is one of `>` `<` `>=` `<=` `==` `!=`. Omit the operator for a plain truthy test: `{if member_count}…{end}`.

Keep the separator *inside* the block, so dropping the block doesn't leave a dangling `│`. Nested conditionals are not supported, and a condition that can't be evaluated is left in the name as written.

If the format string contains no recognised placeholder, the server count is appended to the end. Unrecognised placeholders are left as-is, so a typo shows up in the channel name rather than crashing.

Names longer than Discord's 100-character limit are truncated, with a warning logged.

> **Rate limits:** Discord allows only 2 channel renames per 10 minutes per channel. A 5-minute cooldown is enforced automatically — if an update cycle runs before the cooldown expires, the rename is skipped and a warning is logged.

The bot requires the **Manage Channels** permission in the channel's server for this feature to work.

## License

MIT — see [LICENSE](LICENSE).
