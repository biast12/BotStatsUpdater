# Bot Stats Updater

Automatically update your Discord bot statistics on top.gg and discordbotlist.com.

## Installation

1. Install dependencies:

```bash
pip install -r requirements.txt
```

1. Configure your bots by editing `config.json`:

**Note:** Bot ID and name are automatically detected from the Discord token.

## Configuration

- `update_interval_minutes`: How often to update stats (default: 30 minutes)
- `bot_token`: Discord bot token (required)
- `topgg_token`: Top.gg API token (optional)
- `dbl_token`: DiscordBotList.com API token (optional)

### Getting API Tokens

**Top.gg:** <https://top.gg/bot/YOUR_BOT_ID/webhooks>

**DiscordBotList.com:** <https://discordbotlist.com/bots/YOUR_BOT_ID/edit>
