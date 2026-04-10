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
| `server_count_channel_id` | No | ID of a voice/text channel to rename with the server count |
| `server_count_channel_format` | No | Custom format for the channel name (see below) |

### Server Count Channel

When `server_count_channel_id` is set, the bot will rename that channel on every stats update to reflect its current server count.

**Format string rules (`server_count_channel_format`):**

| Config value | Result (42 servers) |
|---|---|
| *(omitted)* | `BotName: 42` |
| `"Servers: {count}"` | `Servers: 42` |
| `"My Bot: "` | `My Bot: 42` |

If `{count}` is present in the format string it is replaced with the count; if it is absent the count is appended to the end.

> **Rate limits:** Discord allows only 2 channel renames per 10 minutes per channel. A 5-minute cooldown is enforced automatically — if an update cycle runs before the cooldown expires, the rename is skipped and a warning is logged.

The bot requires the **Manage Channel** permission in the channel's server for this feature to work.

### Getting API Tokens

**Top.gg:** <https://top.gg/bot/YOUR_BOT_ID/webhooks>

**DiscordBotList.com:** <https://discordbotlist.com/bots/YOUR_BOT_ID/edit>
