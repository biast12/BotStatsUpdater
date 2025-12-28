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
from typing import Optional, Dict, Any, List
import logging
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
import discord

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


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
        self.topgg_url = f"https://top.gg/api/bots/{bot_id}/stats"
        self.dbl_url = f"https://discordbotlist.com/api/v1/bots/{bot_id}/stats"

    def update_topgg(self, server_count: int, shard_count: Optional[int] = None,
                     shard_id: Optional[int] = None) -> bool:
        """
        Update stats on top.gg

        Args:
            server_count: Number of servers/guilds the bot is in
            shard_count: Total number of shards (optional)
            shard_id: ID of the shard posting (optional, for sharded bots)

        Returns:
            bool: True if successful, False otherwise
        """
        if not self.topgg_token:
            logger.warning("Top.gg token not provided, skipping top.gg update")
            return False

        headers = {
            "Authorization": self.topgg_token,
            "Content-Type": "application/json"
        }

        payload: Dict[str, Any] = {
            "server_count": server_count
        }

        if shard_count is not None:
            payload["shard_count"] = shard_count
        if shard_id is not None:
            payload["shard_id"] = shard_id

        try:
            response = requests.post(self.topgg_url, json=payload, headers=headers)
            response.raise_for_status()
            logger.info(f"Successfully updated top.gg stats: {server_count} servers")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to update top.gg stats: {e}")
            if hasattr(e.response, 'text'):
                logger.error(f"Response: {e.response.text}")
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
            logger.warning("DiscordBotList token not provided, skipping DBL update")
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
            response = requests.post(self.dbl_url, json=payload, headers=headers)
            response.raise_for_status()
            logger.info(f"Successfully updated discordbotlist.com stats: {guilds} guilds")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to update discordbotlist.com stats: {e}")
            if hasattr(e.response, 'text'):
                logger.error(f"Response: {e.response.text}")
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

        logger.info(f"Updating bot stats across all platforms...")

        # Update top.gg
        results['topgg'] = self.update_topgg(
            server_count=server_count,
            shard_count=shard_count,
            shard_id=shard_id
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
        logger.info(f"Stats update complete: {successful}/{total} platforms updated successfully")

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

    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from JSON file"""
        try:
            with open(self.config_path, 'r') as f:
                config = json.load(f)
                logger.info(f"Loaded configuration for {len(config.get('bots', []))} bot(s)")
                return config
        except FileNotFoundError:
            logger.error(f"Configuration file not found: {self.config_path}")
            sys.exit(1)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in configuration file: {e}")
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

            logger.info(f"Bot '{bot_config['name']}' ({client.user}) is ready!")
            logger.info(f"  - Bot ID: {bot_config['bot_id']}")
            logger.info(f"  - Guilds: {len(client.guilds)}")

        return client

    async def update_bot_stats(self, bot_config: Dict[str, Any], client: discord.Client):
        """Update stats for a specific bot"""
        try:
            if not client.is_ready():
                bot_name = bot_config.get('name', 'Unknown Bot')
                logger.warning(f"Bot '{bot_name}' is not ready, skipping stats update")
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

            logger.info(f"Updating stats for '{bot_config['name']}':")
            logger.info(f"  - Guilds: {guild_count}")

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
                logger.info(f"  {status} {platform}")

        except Exception as e:
            bot_name = bot_config.get('name', 'Unknown Bot')
            logger.error(f"Error updating stats for '{bot_name}': {e}", exc_info=True)

    async def update_all_bots_stats(self):
        """Update stats for all configured bots"""
        logger.info("=" * 60)
        logger.info(f"Starting scheduled stats update at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 60)

        tasks = []
        for i, bot_config in enumerate(self.config['bots']):
            if i < len(self.bots):
                tasks.append(self.update_bot_stats(bot_config, self.bots[i]))

        await asyncio.gather(*tasks)

        logger.info("=" * 60)
        logger.info("Stats update completed")
        logger.info("=" * 60)

    async def start(self):
        """Start all bots and the scheduler"""
        logger.info("Starting Bot Stats Manager...")

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
                logger.error(f"Failed to start bot '{bot_config['name']}': {e}", exc_info=True)

        # Wait for all bots to be ready
        logger.info("Waiting for all bots to be ready...")
        max_wait = 30  # seconds
        waited = 0
        while not all(bot.is_ready() for bot in self.bots) and waited < max_wait:
            await asyncio.sleep(1)
            waited += 1

        if not all(bot.is_ready() for bot in self.bots):
            logger.warning("Not all bots are ready, but continuing anyway...")

        # Do initial stats update
        logger.info("Performing initial stats update...")
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
        logger.info(f"Scheduled stats updates every {interval_minutes} minutes")
        logger.info("Bot Stats Manager is now running. Press Ctrl+C to stop.")

        # Keep running
        try:
            while True:
                await asyncio.sleep(3600)  # Sleep for 1 hour
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            await self.stop()

    async def stop(self):
        """Stop all bots and the scheduler"""
        logger.info("Stopping scheduler...")
        self.scheduler.shutdown()

        logger.info("Closing all bot connections...")
        for bot in self.bots:
            await bot.close()

        logger.info("Bot Stats Manager stopped.")


async def main():
    """Main entry point"""
    config_file = "config.json"

    if not os.path.exists(config_file):
        logger.error(f"Configuration file '{config_file}' not found!")
        logger.info("Please create a config.json file with your bot configurations.")
        sys.exit(1)

    manager = BotStatsManager(config_file)
    await manager.start()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Received interrupt signal, shutting down...")
        sys.exit(0)
