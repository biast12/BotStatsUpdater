#!/usr/bin/env python3
"""
Bot Statistics Updater
Updates bot statistics on top.gg and discordbotlist.com
Supports multiple bots with automatic data fetching and scheduled updates

Counts are read over REST, never the gateway, so sharded and unsharded bots
take the same code path.
"""

import sys
import json
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple

import aiohttp
import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from logger import BotLogger, LogArea
logger = BotLogger.get_instance()

REQUEST_TIMEOUT_SECONDS = 15
GUILD_PAGE_SIZE = 200
GUILD_PAGE_LIMIT = 500
# Discord allows 2 renames per 10 min; a 5-min gap stays clear of it.
CHANNEL_RENAME_COOLDOWN_SECONDS = 300

TRANSIENT_ERRORS = (discord.DiscordException, aiohttp.ClientError, asyncio.TimeoutError, OSError)


class BotStatsUpdater:
    """Handles updating bot statistics across multiple bot list platforms"""

    def __init__(self, bot_id: str, session: aiohttp.ClientSession, label: str,
                 topgg_token: Optional[str] = None,
                 dbl_token: Optional[str] = None):
        self.session = session
        self.label = label
        self.topgg_token = topgg_token
        self.dbl_token = dbl_token

        self.topgg_stats_url = "https://top.gg/api/v1/projects/@me/metrics"
        self.topgg_commands_url = "https://top.gg/api/v1/projects/@me/commands"
        self.dbl_stats_url = f"https://discordbotlist.com/api/v1/bots/{bot_id}/stats"
        self.dbl_commands_url = f"https://discordbotlist.com/api/v1/bots/{bot_id}/commands"

    async def _send(self, method: str, url: str, *, auth: str,
                    payload: Any, what: str) -> bool:
        """Single funnel for every outbound call. Retries once on 429/5xx/network."""
        headers = {"Authorization": auth, "Content-Type": "application/json"}

        for attempt in (1, 2):
            try:
                async with self.session.request(method, url, json=payload, headers=headers) as response:
                    if response.status in (200, 201, 204):
                        return True

                    body = (await response.text())[:400]
                    logger.error(LogArea.API,
                                 f"[{self.label}] {what} failed: HTTP {response.status} {body}")

                    if response.status in (401, 403):
                        logger.critical(LogArea.API,
                                        f"[{self.label}] {what}: token rejected, fix it in config.json")
                        return False
                    if response.status != 429 and response.status < 500:
                        return False
                    if attempt == 2:
                        return False
                    await asyncio.sleep(self._retry_after(response))
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error(LogArea.API,
                             f"[{self.label}] {what} failed: {type(e).__name__}: {e} (attempt {attempt})")
                if attempt == 2:
                    return False
                await asyncio.sleep(2)

        return False

    @staticmethod
    def _retry_after(response: aiohttp.ClientResponse) -> float:
        try:
            return min(float(response.headers.get("Retry-After", 2)), 10.0)
        except (TypeError, ValueError):
            return 2.0

    async def update_topgg(self, server_count: int, shard_count: int) -> Optional[bool]:
        """Returns True sent, False failed, None when no token is configured."""
        if not self.topgg_token:
            return None

        # This route is a PATCH, so an omitted field keeps its old value; always
        # send shard_count rather than letting a stale one stand.
        payload: Dict[str, Any] = {
            "server_count": server_count,
            "shard_count": shard_count,
        }

        return await self._send("PATCH", self.topgg_stats_url,
                                auth=f"Bearer {self.topgg_token}",
                                payload=payload, what="top.gg metrics")

    async def update_dbl(self, guilds: int, users: Optional[int] = None) -> Optional[bool]:
        """Returns True sent, False failed, None when no token is configured."""
        if not self.dbl_token:
            return None

        payload: Dict[str, Any] = {"guilds": guilds}
        if users is not None:
            payload["users"] = users

        return await self._send("POST", self.dbl_stats_url,
                                auth=self.dbl_token,
                                payload=payload, what="discordbotlist.com stats")

    async def update_all(self, server_count: int, shard_count: int,
                         users: Optional[int] = None) -> Dict[str, Optional[bool]]:
        topgg, dbl = await asyncio.gather(
            self.update_topgg(server_count=server_count, shard_count=shard_count),
            self.update_dbl(guilds=server_count, users=users),
        )
        return {"topgg": topgg, "dbl": dbl}

    async def sync_commands_topgg(self, commands: List[Dict[str, Any]]) -> Optional[bool]:
        if not self.topgg_token:
            return None

        return await self._send("PUT", self.topgg_commands_url,
                                auth=f"Bearer {self.topgg_token}",
                                payload=commands, what="top.gg commands sync")

    async def sync_commands_dbl(self, commands: List[Dict[str, Any]]) -> Optional[bool]:
        if not self.dbl_token:
            return None

        return await self._send("POST", self.dbl_commands_url,
                                auth=self.dbl_token,
                                payload=commands, what="discordbotlist.com commands sync")

    def _flatten_commands(self, commands: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
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

    async def sync_all_commands(self, commands: List[Dict[str, Any]]) -> Dict[str, Optional[bool]]:
        flat_commands = self._flatten_commands(commands)
        logger.info(LogArea.API,
                    f"[{self.label}] syncing {len(commands)} command(s) "
                    f"as {len(flat_commands)} flattened")

        topgg, dbl = await asyncio.gather(
            self.sync_commands_topgg(flat_commands),
            self.sync_commands_dbl(flat_commands),
        )
        return {"topgg": topgg, "dbl": dbl}


@dataclass
class BotSession:
    """One bot's config, REST-only client and poster, bound together so they cannot drift apart."""
    index: int
    config: Dict[str, Any]
    client: Optional[discord.Client] = None
    updater: Optional[BotStatsUpdater] = None
    bot_id: Optional[str] = None
    application_id: Optional[int] = None
    name: str = ""
    dead_reason: Optional[str] = None

    @property
    def label(self) -> str:
        return self.name or self.config.get('name') or f"bot#{self.index + 1}"

    async def ensure_login(self, session: aiohttp.ClientSession) -> bool:
        """Guarantee a logged-in client. Never raises; logs every failure."""
        if self.dead_reason:
            return False
        if self.client is not None:
            return True

        token = (self.config.get('bot_token') or '').strip()
        if not token:
            self.dead_reason = "no bot_token configured"
            logger.critical(LogArea.CONFIG, f"[{self.label}] no bot_token configured, bot disabled")
            return False

        # Intents only reach Discord in a gateway IDENTIFY, which never happens here.
        client = discord.Client(intents=discord.Intents.none())
        try:
            # HTTP only, no websocket, so the mandatory-sharding rule (close code
            # 4011) cannot apply.
            await client.login(token)
        except discord.LoginFailure as e:
            # static_login opens the aiohttp session before the request that 401s.
            await client.close()
            self.dead_reason = f"invalid bot_token ({e})"
            logger.critical(LogArea.BOT,
                            f"[{self.label}] login rejected: {e} -- fix bot_token in config.json")
            return False
        except TRANSIENT_ERRORS as e:
            await client.close()
            logger.error(LogArea.BOT,
                         f"[{self.label}] login failed ({type(e).__name__}: {e}), retrying next cycle")
            return False

        self.client = client
        self.bot_id = str(client.user.id)
        self.application_id = client.application_id or client.user.id
        self.name = self.config.get('name') or client.user.name
        self.updater = BotStatsUpdater(
            bot_id=self.bot_id,
            session=session,
            label=self.label,
            topgg_token=self.config.get('topgg_token'),
            dbl_token=self.config.get('dbl_token'),
        )

        logger.info(LogArea.BOT,
                    f"[{self.label}] logged in (REST-only): bot_id={self.bot_id} "
                    f"app_id={self.application_id}")
        return True

    async def close(self) -> None:
        if self.client is not None:
            # close() drops the loop reference, so this client can never log in
            # again; a retry builds a fresh one.
            await self.client.close()
            self.client = None


class BotStatsManager:
    """Manages multiple bots and their stats updates"""

    def __init__(self, config_path: str = "config.json"):
        self.config_path = config_path
        self.config = self._load_config()
        self.sessions: List[BotSession] = []
        self.http: Optional[aiohttp.ClientSession] = None
        self.scheduler = AsyncIOScheduler()
        self._channel_last_updated: Dict[int, datetime] = {}

    def _load_config(self) -> Dict[str, Any]:
        try:
            with open(self.config_path, 'r') as f:
                config = json.load(f)
                logger.info(LogArea.CONFIG,
                            f"Loaded configuration for {len(config.get('bots', []))} bot(s)")
                return config
        except FileNotFoundError:
            logger.error(LogArea.CONFIG,
                         f"Configuration file not found: {self.config_path} -- "
                         f"copy config.example.json to {self.config_path} and fill in your tokens")
            sys.exit(1)
        except json.JSONDecodeError as e:
            logger.error(LogArea.CONFIG, f"Invalid JSON in configuration file: {e}")
            sys.exit(1)

    async def _count_guilds(self, session: BotSession) -> Tuple[int, int]:
        """
        Page GET /users/@me/guilds, returning (guilds, summed approximate members).

        Short pages are NOT the last page and the same request can return a
        different count each time, so only an empty page ends the walk and IDs are
        deduped. Raises on HTTP failure: a partial count must never be posted.
        """
        after: Optional[int] = None
        seen: set = set()
        members = pages = 0

        while pages < GUILD_PAGE_LIMIT:
            page = await session.client.http.get_guilds(GUILD_PAGE_SIZE, after=after,
                                                        with_counts=True)
            pages += 1
            if not page:
                break

            cursor = after or 0
            for guild in page:
                guild_id = int(guild['id'])
                cursor = max(cursor, guild_id)
                if guild_id in seen:
                    continue
                seen.add(guild_id)
                members += int(guild.get('approximate_member_count') or 0)

            if after is not None and cursor <= after:
                break
            after = cursor
        else:
            logger.warning(LogArea.API,
                           f"[{session.label}] stopped at the {GUILD_PAGE_LIMIT}-page cap; "
                           f"count may be short")

        logger.info(LogArea.API,
                    f"[{session.label}] counted {len(seen)} guilds across {pages} page(s), "
                    f"{members} members")
        return len(seen), members

    async def _resolve_shard_count(self, session: BotSession) -> int:
        """Configured value if set, else Discord's recommendation. Unsharded means one shard."""
        configured = session.config.get('shard_count')
        if isinstance(configured, int) and not isinstance(configured, bool) and configured >= 1:
            logger.info(LogArea.API, f"[{session.label}] shard_count {configured} from config")
            return configured

        try:
            recommended, _url, _limits = await session.client.http.get_bot_gateway()
        except Exception as e:
            logger.warning(LogArea.API,
                           f"[{session.label}] could not read /gateway/bot "
                           f"({type(e).__name__}: {e}), reporting shard_count=1")
            return 1

        logger.info(LogArea.API, f"[{session.label}] Discord recommends {recommended} shard(s)")
        return max(1, int(recommended))

    async def _update_server_count_channel(self, session: BotSession, server_count: int):
        """
        Rename a channel to reflect the current server count.

        server_count_channel_id     - voice or text channel to rename
        server_count_channel_format - uses {count} as placeholder; defaults to
                                      "<bot name>: <count>". Without {count} the
                                      count is appended.
        """
        bot_config = session.config
        channel_id_str = bot_config.get('server_count_channel_id', '')
        if not channel_id_str:
            return

        try:
            channel_id = int(channel_id_str)
        except (ValueError, TypeError):
            logger.warning(LogArea.CHANNEL,
                           f"[{session.label}] invalid server_count_channel_id: {channel_id_str!r}")
            return

        now = datetime.now(timezone.utc)
        last_update = self._channel_last_updated.get(channel_id)
        if last_update is not None:
            elapsed = (now - last_update).total_seconds()
            if elapsed < CHANNEL_RENAME_COOLDOWN_SECONDS:
                remaining = int(CHANNEL_RENAME_COOLDOWN_SECONDS - elapsed)
                logger.warning(LogArea.CHANNEL,
                               f"[{session.label}] skipping channel rename: "
                               f"rate-limit cooldown ({remaining}s remaining)")
                return

        fmt = bot_config.get('server_count_channel_format', '')
        if fmt:
            if '{count}' in fmt:
                channel_name = fmt.replace('{count}', str(server_count))
            else:
                channel_name = f"{fmt}{server_count}"
        else:
            channel_name = f"{session.label}: {server_count}"

        try:
            # No gateway means no channel cache, so always fetch.
            channel = await session.client.fetch_channel(channel_id)
            await channel.edit(name=channel_name)
        except discord.Forbidden:
            logger.error(LogArea.CHANNEL,
                         f"[{session.label}] missing 'Manage Channel' permission "
                         f"for channel {channel_id}")
        except TRANSIENT_ERRORS as e:
            logger.error(LogArea.CHANNEL,
                         f"[{session.label}] failed to rename channel {channel_id}: "
                         f"{type(e).__name__}: {e}")
        else:
            self._channel_last_updated[channel_id] = now
            logger.info(LogArea.CHANNEL,
                        f"[{session.label}] updated channel name to '{channel_name}'")

    @staticmethod
    def _log_results(session: BotSession, results: Dict[str, Optional[bool]],
                     suffix: str = "") -> None:
        for platform, outcome in results.items():
            status = "[SKIP]" if outcome is None else ("[OK]" if outcome else "[FAIL]")
            logger.info(LogArea.API, f"[{session.label}]   {status} {platform}{suffix}")

    async def update_bot_stats(self, session: BotSession):
        """Run one full update cycle for a single bot"""
        if not await session.ensure_login(self.http):
            return

        try:
            guild_count, member_count = await self._count_guilds(session)
        except TRANSIENT_ERRORS as e:
            logger.error(LogArea.API,
                         f"[{session.label}] guild count failed ({type(e).__name__}: {e}); "
                         f"skipping this cycle rather than posting a partial count")
            return

        shard_count = await self._resolve_shard_count(session)

        await self._update_server_count_channel(session, guild_count)

        results = await session.updater.update_all(
            server_count=guild_count,
            shard_count=shard_count,
            # 0 would overwrite the listing's real figure, so omit it instead.
            users=member_count or None,
        )
        self._log_results(session, results)

        try:
            commands = await session.client.http.get_global_commands(session.application_id)
        except TRANSIENT_ERRORS as e:
            logger.error(LogArea.API,
                         f"[{session.label}] fetching slash commands failed "
                         f"({type(e).__name__}: {e})")
            return

        command_results = await session.updater.sync_all_commands(commands)
        self._log_results(session, command_results, suffix=" (commands)")

    async def update_all_bots_stats(self):
        logger.spacer()
        logger.info(LogArea.SCHEDULER, "Starting scheduled stats update")
        logger.spacer()

        outcomes = await asyncio.gather(
            *(self.update_bot_stats(session) for session in self.sessions),
            return_exceptions=True,
        )
        for session, outcome in zip(self.sessions, outcomes):
            if isinstance(outcome, BaseException):
                logger.critical(LogArea.SCHEDULER,
                                f"[{session.label}] update cycle crashed: "
                                f"{type(outcome).__name__}: {outcome}")

        logger.spacer()
        logger.info(LogArea.SCHEDULER, "Stats update completed")
        logger.spacer()

    async def start(self):
        """Log in all bots and start the scheduler"""
        logger.info(LogArea.STARTUP, "Starting Bot Stats Manager (REST-only, no gateway)...")

        self.http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS, connect=5),
        )

        self.sessions = [BotSession(index=i, config=bot_config)
                         for i, bot_config in enumerate(self.config.get('bots', []))]
        if not self.sessions:
            logger.critical(LogArea.CONFIG,
                            f"No bots configured in {self.config_path} -- "
                            f"add at least one entry to \"bots\"")
            sys.exit(1)

        logins = await asyncio.gather(
            *(session.ensure_login(self.http) for session in self.sessions),
            return_exceptions=True,
        )
        for session, outcome in zip(self.sessions, logins):
            if isinstance(outcome, BaseException):
                logger.critical(LogArea.STARTUP,
                                f"[{session.label}] login raised "
                                f"{type(outcome).__name__}: {outcome}")

        ready = sum(1 for outcome in logins if outcome is True)
        logger.info(LogArea.STARTUP, f"Logged in {ready}/{len(self.sessions)} bot(s)")
        if not ready:
            logger.critical(LogArea.STARTUP,
                            "No bot logged in; check tokens and network. Login is retried each cycle.")

        await self.update_all_bots_stats()

        interval_minutes = self.config.get('update_interval_minutes', 30)
        self.scheduler.add_job(
            self.update_all_bots_stats,
            IntervalTrigger(minutes=interval_minutes),
            id='stats_update',
            name='Update bot statistics',
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        self.scheduler.start()
        logger.info(LogArea.SCHEDULER, f"Scheduled stats updates every {interval_minutes} minutes")
        logger.info(LogArea.STARTUP, "Bot Stats Manager is now running. Press Ctrl+C to stop.")

        await asyncio.Event().wait()

    async def stop(self):
        """Stop the scheduler and release every client"""
        logger.info(LogArea.SHUTDOWN, "Stopping scheduler...")
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

        for session in self.sessions:
            try:
                await session.close()
            except Exception as e:
                logger.warning(LogArea.SHUTDOWN,
                               f"[{session.label}] close failed: {type(e).__name__}: {e}")

        if self.http is not None:
            await self.http.close()

        # aiohttp closes TLS transports asynchronously; yield so they finish before
        # the loop shuts down, rather than surfacing as unclosed-socket warnings.
        await asyncio.sleep(0.25)

        logger.info(LogArea.SHUTDOWN, "Bot Stats Manager stopped.")


async def main():
    manager = BotStatsManager("config.json")
    try:
        await manager.start()
    finally:
        await manager.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info(LogArea.SHUTDOWN, "Received interrupt signal, shutting down...")
