# Bot Stats Updater

Automatically update your Discord bot statistics on top.gg and discordbotlist.com. Run as many bots as you like from a single config file.

It talks to Discord over REST only and never connects to the gateway, so it needs no privileged intents and works the same for sharded and unsharded bots.

## Features

- Multiple bots from one config file
- Server, member, shard and user-install counts posted to top.gg and discordbotlist.com
- Slash commands synced to both listings
- Optional channel that gets renamed with your live server count
- Optional Discord webhook alerts when something goes wrong
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
- `alert_webhook_url`: Discord webhook that receives `ERROR` and `CRITICAL` log lines, batched every 10s (default: none). Without it, a revoked token only shows up in `docker logs`.
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
