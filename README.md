# Bot Stats Updater

Automatically update your Discord bot statistics on top.gg and discordbotlist.com.

## Installation

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Configure your bots by editing `config.json`:

**Note:** Bot ID and name are automatically detected from the Discord token.

## Configuration

### Global options

- `update_interval_minutes`: How often to update stats (default: `30`)
- `alert_webhook_url`: Discord webhook that receives `ERROR` and `CRITICAL` log lines (default: none). Without it, a revoked token only shows up in `docker logs`.
- `heartbeat_file`: File stamped after each cycle, read by the Docker healthcheck (default: `heartbeat`). Set to `""` to disable.

### Per-bot options

| Key | Required | Description |
|-----|----------|-------------|
| `bot_token` | Yes | Discord bot token |
| `topgg_token` | No | Top.gg API token |
| `dbl_token` | No | DiscordBotList.com API token |
| `shard_count` | No | Number of shards this bot actually runs. Omit to report Discord's recommended count. |
| `report_server_count` | No | `false` stops reporting servers/shards, and skips discordbotlist.com entirely. Use it for a user-install-only app (default: `true`). |
| `report_user_installs` | No | `false` stops reporting user-app installs to top.gg (default: `true`). |
| `server_count_channel_id` | No | ID of a voice/text channel to rename with the server count |
| `server_count_channel_format` | No | Custom format for the channel name (see below) |

### Server Count Channel

When `server_count_channel_id` is set, the bot will rename that channel on every stats update to reflect its current server count.

**Format string rules (`server_count_channel_format`):**

| Config value | Result |
|---|---|
| *(omitted)* | `BotName: 2553` |
| `"Servers: {count}"` | `Servers: 2553` |
| `"Servers: {server_count:,}"` | `Servers: 2,553` |
| `"Servers: {server_count} {shard_count}"` | `Servers: 2553 3` |
| `"{server_count:,} servers / {shard_count} shards"` | `2,553 servers / 3 shards` |
| `"{bot_name}: {member_count:,} users"` | `PurgeBot: 655,270 users` |
| `"Servers: {server_count:,}{if shard_count > 1} │ Shards: {shard_count}{end}"` | `Servers: 2,553 │ Shards: 3`, or `Servers: 493` for an unsharded bot |
| `"My Bot: "` | `My Bot: 2553` |

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
{if shard_count > 1} | Shards: {shard_count}{end}
{if shard_count > 1}Shards: {shard_count}{else}Unsharded{end}
```

`{if <placeholder> <op> <number>}` … optional `{else}` … `{end}`, where `<op>` is one of `>` `<` `>=` `<=` `==` `!=`. Omit the operator for a plain truthy test: `{if member_count}…{end}`.

Keep the separator *inside* the block, so dropping the block doesn't leave a dangling `|`. Nested conditionals are not supported, and a condition that can't be evaluated is left in the name as written.

If the format string contains no recognised placeholder, the server count is appended to the end. Unrecognised placeholders are left as-is, so a typo shows up in the channel name rather than crashing.

Names longer than Discord's 100-character limit are truncated, with a warning logged.

> **Rate limits:** Discord allows only 2 channel renames per 10 minutes per channel. A 5-minute cooldown is enforced automatically — if an update cycle runs before the cooldown expires, the rename is skipped and a warning is logged.

The bot requires the **Manage Channel** permission in the channel's server for this feature to work.

### Which metrics get reported

Both metrics are independent, so a bot only needs to report what it actually has. top.gg's metrics route is a `PATCH`, meaning a metric you switch off is *left at its previous value* rather than zeroed.

| Bot type | Config |
|---|---|
| Normal guild bot | nothing — both default to `true` |
| User-install-only app | `"report_server_count": false` |
| Guild bot, no user-app | `"report_user_installs": false` |

Switching both off leaves nothing to send, which is logged and skipped.

### Alerts

Set `alert_webhook_url` to a Discord webhook URL and every `ERROR` / `CRITICAL` line is batched (every 10s) and posted there — rejected tokens, failed guild counts, crashed cycles. Routine `INFO` output stays on the console only.

### Healthcheck

Each completed cycle stamps `heartbeat` with the time by which the *next* cycle must finish (two intervals plus five minutes). `docker-compose.yml` reads it, so `docker ps` shows the container as unhealthy if cycles stop running.

### Getting API Tokens

**Top.gg:** <https://top.gg/bot/YOUR_BOT_ID/webhooks>

**DiscordBotList.com:** <https://discordbotlist.com/bots/YOUR_BOT_ID/edit>
