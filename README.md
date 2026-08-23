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

### Per-bot options

| Key | Required | Description |
|-----|----------|-------------|
| `bot_token` | Yes | Discord bot token |
| `topgg_token` | No | Top.gg API token |
| `dbl_token` | No | DiscordBotList.com API token |
| `shard_count` | No | Number of shards this bot actually runs. Omit to report Discord's recommended count. |
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
| `"My Bot: "` | `My Bot: 2553` |

**Available placeholders:**

| Placeholder | Value |
|---|---|
| `{server_count}` | Servers the bot is in |
| `{count}` | Alias of `{server_count}` |
| `{shard_count}` | The same number reported to top.gg (see `shard_count` above) |
| `{member_count}` | Total members across all servers — Discord's approximate counts, summed, the same figure sent to discordbotlist.com |
| `{bot_name}` | The bot's name, from `name` or auto-detected from the token |
| `{bot_id}` | The bot's user ID |

Add `:,` inside any numeric placeholder for thousands separators: `{member_count:,}` → `655,270`.

If the format string contains no recognised placeholder, the server count is appended to the end. Unrecognised placeholders are left as-is, so a typo shows up in the channel name rather than crashing.

Names longer than Discord's 100-character limit are truncated, with a warning logged.

> **Rate limits:** Discord allows only 2 channel renames per 10 minutes per channel. A 5-minute cooldown is enforced automatically — if an update cycle runs before the cooldown expires, the rename is skipped and a warning is logged.

The bot requires the **Manage Channel** permission in the channel's server for this feature to work.

### Getting API Tokens

**Top.gg:** <https://top.gg/bot/YOUR_BOT_ID/webhooks>

**DiscordBotList.com:** <https://discordbotlist.com/bots/YOUR_BOT_ID/edit>
