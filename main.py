#!/usr/bin/env python3
"""
Bot Statistics Updater
Updates bot statistics on top.gg and discordbotlist.com
Supports multiple bots with automatic data fetching and scheduled updates
"""

import os
import sys
import json
import asyncio
import requests
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
import discord

from logger import BotLogger, LogLevel, LogArea
logger = BotLogger.get_instance()


class BotStatsUpdater:
    """Handles updating bot statistics across multiple bot list platforms"""

    def __init__(self, bot_id: str, topgg_token: Optional[str] = None,
                 dbl_token: Optional[str] = None):
        """
        Initialize the stats updater

        Args:
            bot_id: Your Discord bot's client ID
            topgg_token: Top.gg API token
            dbl_token: DiscordBotList.com API token
        """
        self.bot_id = bot_id
        self.topgg_token = topgg_token
        self.dbl_token = dbl_token

        # API endpoints
        self.topgg_stats_url = "https://top.gg/api/v1/projects/@me/metrics"
        self.dbl_stats_url = f"https://discordbotlist.com/api/v1/bots/{bot_id}/stats"
        self.topgg_commands_url = "https://top.gg/api/v1/projects/@me/commands"
        self.dbl_commands_url   = f"https://discordbotlist.com/api/v1/bots/{bot_id}/commands"

    def update_topgg(self, server_count: int, shard_count: Optional[int] = None) -> bool:
        """
        Update stats on top.gg

        Args:
            server_count: Number of servers/guilds the bot is in
            shard_count: Total number of shards (optional)

        Returns:
            bool: True if successful, False otherwise
        """
        if not self.topgg_token:
            logger.warning(LogArea.API, "Top.gg token not provided, skipping top.gg update")
            return False

        headers = {
            "Authorization": f"Bearer {self.topgg_token}",
            "Content-Type": "application/json"
        }

        payload: Dict[str, Any] = {
            "server_count": server_count
        }

        if shard_count is not None:
            payload["shard_count"] = shard_count

        try:
            response = requests.post(self.topgg_stats_url, json=payload, headers=headers)
            response.raise_for_status()
            logger.info(LogArea.API, f"Successfully updated top.gg stats: {server_count} servers")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(LogArea.API, f"Failed to update top.gg stats: {e}")
            if hasattr(e.response, 'text'):
                logger.error(LogArea.API, f"Response: {e.response.text}")
            return False

    def update_dbl(self, guilds: int, users: Optional[int] = None,
                   voice_connections: Optional[int] = None,
                   shard_id: Optional[int] = None) -> bool:
        """
        Update stats on discordbotlist.com

        Args:
            guilds: Number of guilds the bot is in
            users: Total user count (optional)
            voice_connections: Active voice connections (optional)
            shard_id: Shard identifier for per-shard stats (optional)

        Returns:
            bool: True if successful, False otherwise
        """
        if not self.dbl_token:
            logger.warning(LogArea.API, "DiscordBotList token not provided, skipping DBL update")
            return False

        headers = {
            "Authorization": self.dbl_token,
            "Content-Type": "application/json"
        }

        payload: Dict[str, Any] = {
            "guilds": guilds
        }

        if users is not None:
            payload["users"] = users
        if voice_connections is not None:
            payload["voice_connections"] = voice_connections
        if shard_id is not None:
            payload["shard_id"] = shard_id

        try:
            response = requests.post(self.dbl_stats_url, json=payload, headers=headers)
            response.raise_for_status()
            logger.info(LogArea.API, f"Successfully updated discordbotlist.com stats: {guilds} guilds")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(LogArea.API, f"Failed to update discordbotlist.com stats: {e}")
            if hasattr(e.response, 'text'):
                logger.error(LogArea.API, f"Response: {e.response.text}")
            return False

    def update_all(self, server_count: int, users: Optional[int] = None,
                   voice_connections: Optional[int] = None,
                   shard_count: Optional[int] = None,
                   shard_id: Optional[int] = None) -> Dict[str, bool]:
        """
        Update stats on all configured platforms

        Args:
            server_count: Number of servers/guilds the bot is in
            users: Total user count (optional)
            voice_connections: Active voice connections (optional)
            shard_count: Total number of shards (optional)
            shard_id: Shard identifier (optional)

        Returns:
            dict: Results for each platform (platform_name: success_bool)
        """
        results = {}

        logger.info(LogArea.API, f"Updating bot stats across all platforms...")

        # Update top.gg
        results['topgg'] = self.update_topgg(
            server_count=server_count,
            shard_count=shard_count
        )

        # Update discordbotlist.com
        results['dbl'] = self.update_dbl(
            guilds=server_count,
            users=users,
            voice_connections=voice_connections,
            shard_id=shard_id
        )

        successful = sum(1 for success in results.values() if success)
        total = len([k for k, v in results.items() if v is not None])
        logger.info(LogArea.API, f"Stats update complete: {successful}/{total} platforms updated successfully")

        return results

    def sync_commands_topgg(self, commands: List[Dict[str, Any]]) -> bool:
        """
        Sync slash commands to top.gg

        Args:
            commands: List of command objects from Discord

        Returns:
            bool: True if successful, False otherwise
        """
        if not self.topgg_token:
            logger.warning(LogArea.API, "Top.gg token not provided, skipping top.gg commands sync")
            return False
        headers = {"Authorization": f"Bearer {self.topgg_token}", "Content-Type": "application/json"}
        try:
            response = requests.post(self.topgg_commands_url, json=commands, headers=headers)
            response.raise_for_status()
            logger.info(LogArea.API, f"Successfully synced {len(commands)} command(s) to top.gg")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(LogArea.API, f"Failed to sync commands to top.gg: {e}")
            if hasattr(e.response, 'text'):
                logger.error(LogArea.API, f"Response: {e.response.text}")
            return False

    def sync_commands_dbl(self, commands: List[Dict[str, Any]]) -> bool:
        """
        Sync slash commands to discordbotlist.com

        Args:
            commands: List of command objects from Discord

        Returns:
            bool: True if successful, False otherwise
        """
        if not self.dbl_token:
            logger.warning(LogArea.API, "DiscordBotList token not provided, skipping DBL commands sync")
            return False
        headers = {"Authorization": self.dbl_token, "Content-Type": "application/json"}
        try:
            response = requests.post(self.dbl_commands_url, json=commands, headers=headers)
            response.raise_for_status()
            logger.info(LogArea.API, f"Successfully synced {len(commands)} command(s) to discordbotlist.com")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(LogArea.API, f"Failed to sync commands to discordbotlist.com: {e}")
            if hasattr(e.response, 'text'):
                logger.error(LogArea.API, f"Response: {e.response.text}")
            return False

    def _flatten_commands(self, commands: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Recursively flatten Discord's nested command tree into a flat list"""
        flat = []
        for cmd in commands:
            options = cmd.get('options', [])
            option_types = {o.get('type') for o in options}
            if 1 not in option_types and 2 not in option_types:
                flat.append(cmd)
                continue
            for option in options:
                if option.get('type') == 2:  # SUB_COMMAND_GROUP
                    for sub in option.get('options', []):
                        flat.append({**sub, 'name': f"{cmd['name']} {option['name']} {sub['name']}"})
                elif option.get('type') == 1:  # SUB_COMMAND
                    flat.append({**option, 'name': f"{cmd['name']} {option['name']}"})
        return flat

    def sync_all_commands(self, commands: List[Dict[str, Any]]) -> Dict[str, bool]:
        """
        Sync slash commands to all configured platforms

        Args:
            commands: List of command objects from Discord

        Returns:
            dict: Results for each platform (platform_name: success_bool)
        """
        results = {}

        flat_commands = self._flatten_commands(commands)
        logger.info(LogArea.API, f"Syncing slash commands across all platforms...")

        results['topgg'] = self.sync_commands_topgg(flat_commands)
        results['dbl'] = self.sync_commands_dbl(flat_commands)

        successful = sum(1 for success in results.values() if success)
        total = len([k for k, v in results.items() if v is not None])
        logger.info(LogArea.API, f"Commands sync complete: {successful}/{total} platforms synced successfully")

        return results


class BotStatsManager:
    """Manages multiple bots and their stats updates"""

    def __init__(self, config_path: str = "config.json"):
        """
        Initialize the bot stats manager

        Args:
            config_path: Path to the configuration file
        """
        self.config_path = config_path
        self.config = self._load_config()
        self.bots: List[discord.Client] = []
        self.updaters: Dict[str, BotStatsUpdater] = {}
        self.scheduler = AsyncIOScheduler()
        # Tracks the last time each channel (by ID) had its name updated,
        # used to enforce Discord's rate limit of 2 renames per 10 minutes.
        self._channel_last_updated: Dict[int, datetime] = {}

    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from JSON file"""
        try:
            with open(self.config_path, 'r') as f:
                config = json.load(f)
                logger.info(LogArea.CONFIG, f"Loaded configuration for {len(config.get('bots', []))} bot(s)")
                return config
        except FileNotFoundError:
            logger.error(LogArea.CONFIG, f"Configuration file not found: {self.config_path}")
            sys.exit(1)
        except json.JSONDecodeError as e:
            logger.error(LogArea.CONFIG, f"Invalid JSON in configuration file: {e}")
            sys.exit(1)

    async def _create_bot_client(self, bot_config: Dict[str, Any]) -> discord.Client:
        """Create a Discord client for a bot"""
        intents = discord.Intents.default()
        intents.guilds = True

        client = discord.Client(intents=intents)

        @client.event
        async def on_ready():
            # Auto-populate bot_id and name from the client
            bot_config['bot_id'] = str(client.user.id)
            if 'name' not in bot_config or not bot_config['name']:
                bot_config['name'] = client.user.name

            logger.info(LogArea.BOT, f"Bot '{bot_config['name']}' ({client.user}) is ready!")
            logger.info(LogArea.BOT, f"  - Bot ID: {bot_config['bot_id']}")
            logger.info(LogArea.BOT, f"  - Guilds: {len(client.guilds)}")

        return client

    async def _update_server_count_channel(
        self,
        bot_config: Dict[str, Any],
        client: discord.Client,
        server_count: int
    ):
        """
        Edit a Discord channel's name to reflect the current server count.

        Config keys (both optional):
          server_count_channel_id     — voice or text channel to rename
          server_count_channel_format — format string; use {count} as placeholder.
                                        If omitted, defaults to "<bot name>: <count>".
                                        If provided without {count}, count is appended.

        Enforces Discord's rate limit (2 renames / 10 min) by keeping a 5-minute
        cooldown per channel. Updates are skipped (with a warning) when within cooldown.
        """
        channel_id_str = bot_config.get('server_count_channel_id', '')
        if not channel_id_str:
            return

        try:
            channel_id = int(channel_id_str)
        except (ValueError, TypeError):
            logger.warning(LogArea.CHANNEL, f"Invalid server_count_channel_id: {channel_id_str!r}")
            return

        # Discord allows 2 renames per 10 min; enforce a 5-min gap to stay safe.
        COOLDOWN_SECONDS = 300
        now = datetime.now(timezone.utc)
        last_update = self._channel_last_updated.get(channel_id)
        if last_update is not None:
            elapsed = (now - last_update).total_seconds()
            if elapsed < COOLDOWN_SECONDS:
                remaining = int(COOLDOWN_SECONDS - elapsed)
                bot_name = bot_config.get('name', 'Unknown Bot')
                logger.warning(
                    LogArea.CHANNEL,
                    f"Skipping channel rename for '{bot_name}': "
                    f"rate-limit cooldown ({remaining}s remaining)"
                )
                return

        # Build the new channel name
        bot_name = bot_config.get('name', client.user.name if client.user else 'Bot')
        fmt = bot_config.get('server_count_channel_format', '')
        if fmt:
            if '{count}' in fmt:
                channel_name = fmt.replace('{count}', str(server_count))
            else:
                channel_name = f"{fmt}{server_count}"
        else:
            channel_name = f"{bot_name}: {server_count}"

        try:
            channel = client.get_channel(channel_id)
            if channel is None:
                channel = await client.fetch_channel(channel_id)

            await channel.edit(name=channel_name)
            self._channel_last_updated[channel_id] = now
            logger.info(LogArea.CHANNEL, f"Updated channel name to '{channel_name}' for '{bot_name}'")
        except discord.Forbidden:
            bot_name = bot_config.get('name', 'Unknown Bot')
            logger.error(LogArea.CHANNEL, f"Missing 'Manage Channel' permission for channel {channel_id} ('{bot_name}')")
        except discord.HTTPException as e:
            bot_name = bot_config.get('name', 'Unknown Bot')
            logger.error(LogArea.CHANNEL, f"Failed to rename channel {channel_id} for '{bot_name}': {e}")

    async def update_bot_stats(self, bot_config: Dict[str, Any], client: discord.Client):
        """Update stats for a specific bot"""
        try:
            if not client.is_ready():
                bot_name = bot_config.get('name', 'Unknown Bot')
                logger.warning(LogArea.BOT, f"Bot '{bot_name}' is not ready, skipping stats update")
                return

            # Auto-populate bot_id from client if not set
            if 'bot_id' not in bot_config:
                bot_config['bot_id'] = str(client.user.id)

            # Auto-populate name from client if not set
            if 'name' not in bot_config or not bot_config['name']:
                bot_config['name'] = client.user.name

            # Gather statistics
            guild_count = len(client.guilds)
            user_count = 0  # Not tracking users
            voice_connections = 0  # Not tracking voice connections

            logger.info(LogArea.API, f"Updating stats for '{bot_config['name']}':")
            logger.info(LogArea.API, f"  - Guilds: {guild_count}")

            # Update server-count channel name (optional, config-driven)
            await self._update_server_count_channel(bot_config, client, guild_count)

            # Get or create updater for this bot
            bot_id = bot_config['bot_id']
            if bot_id not in self.updaters:
                self.updaters[bot_id] = BotStatsUpdater(
                    bot_id=bot_id,
                    topgg_token=bot_config.get('topgg_token'),
                    dbl_token=bot_config.get('dbl_token')
                )

            # Update stats on all platforms
            results = self.updaters[bot_id].update_all(
                server_count=guild_count,
                users=user_count,
                voice_connections=voice_connections
            )

            # Log results
            for platform, success in results.items():
                status = "[OK]" if success else "[FAIL]"
                logger.info(LogArea.API, f"  {status} {platform}")

            # Resolve application_id (fallback to bot_id string if not set yet)
            application_id = client.application_id or int(bot_config['bot_id'])

            # Fetch registered global slash commands from Discord
            logger.info(LogArea.API, f"Fetching slash commands for '{bot_config['name']}'...")
            commands = await client.http.get_global_commands(application_id)
            logger.info(LogArea.API, f"  - Commands found: {len(commands)}")

            command_results = self.updaters[bot_id].sync_all_commands(commands)

            for platform, success in command_results.items():
                status = "[OK]" if success else "[FAIL]"
                logger.info(LogArea.API, f"  {status} {platform} (commands)")

        except Exception as e:
            bot_name = bot_config.get('name', 'Unknown Bot')
            logger.error(LogArea.API, f"Error updating stats for '{bot_name}': {e}")

    async def update_all_bots_stats(self):
        """Update stats for all configured bots"""
        logger.spacer()
        logger.info(LogArea.SCHEDULER, "Starting scheduled stats update")
        logger.spacer()

        tasks = []
        for i, bot_config in enumerate(self.config['bots']):
            if i < len(self.bots):
                tasks.append(self.update_bot_stats(bot_config, self.bots[i]))

        await asyncio.gather(*tasks)

        logger.spacer()
        logger.info(LogArea.SCHEDULER, "Stats update completed")
        logger.spacer()

    async def start(self):
        """Start all bots and the scheduler"""
        logger.info(LogArea.STARTUP, "Starting Bot Stats Manager...")

        # Create and login all bots
        for bot_config in self.config['bots']:
            try:
                client = await self._create_bot_client(bot_config)
                self.bots.append(client)

                # Login bot in background
                asyncio.create_task(client.start(bot_config['bot_token']))

                # Wait a bit for the bot to connect
                await asyncio.sleep(2)

            except Exception as e:
                logger.error(LogArea.STARTUP, f"Failed to start bot '{bot_config['name']}': {e}")

        # Wait for all bots to be ready
        logger.info(LogArea.STARTUP, "Waiting for all bots to be ready...")
        max_wait = 30  # seconds
        waited = 0
        while not all(bot.is_ready() for bot in self.bots) and waited < max_wait:
            await asyncio.sleep(1)
            waited += 1

        if not all(bot.is_ready() for bot in self.bots):
            logger.warning(LogArea.STARTUP, "Not all bots are ready, but continuing anyway...")

        # Do initial stats update
        logger.info(LogArea.STARTUP, "Performing initial stats update...")
        await self.update_all_bots_stats()

        # Schedule periodic updates
        interval_minutes = self.config.get('update_interval_minutes', 30)
        self.scheduler.add_job(
            self.update_all_bots_stats,
            IntervalTrigger(minutes=interval_minutes),
            id='stats_update',
            name='Update bot statistics',
            replace_existing=True
        )

        self.scheduler.start()
        logger.info(LogArea.SCHEDULER, f"Scheduled stats updates every {interval_minutes} minutes")
        logger.info(LogArea.STARTUP, "Bot Stats Manager is now running. Press Ctrl+C to stop.")

        # Keep running
        try:
            while True:
                await asyncio.sleep(3600)  # Sleep for 1 hour
        except KeyboardInterrupt:
            logger.info(LogArea.SHUTDOWN, "Shutting down...")
            await self.stop()

    async def stop(self):
        """Stop all bots and the scheduler"""
        logger.info(LogArea.SHUTDOWN, "Stopping scheduler...")
        self.scheduler.shutdown()

        logger.info(LogArea.SHUTDOWN, "Closing all bot connections...")
        for bot in self.bots:
            await bot.close()

        logger.info(LogArea.SHUTDOWN, "Bot Stats Manager stopped.")


async def main():
    """Main entry point"""
    config_file = "config.json"

    if not os.path.exists(config_file):
        logger.error(LogArea.CONFIG, f"Configuration file '{config_file}' not found!")
        logger.info(LogArea.CONFIG, "Please create a config.json file with your bot configurations.")
        sys.exit(1)

    manager = BotStatsManager(config_file)
    await manager.start()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info(LogArea.SHUTDOWN, "Received interrupt signal, shutting down...")
        sys.exit(0)
