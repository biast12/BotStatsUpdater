#!/usr/bin/env python3
"""
Bot Statistics Updater
Updates bot statistics on top.gg and discordbotlist.com
Supports multiple bots with automatic data fetching and scheduled updates

Counts are read over REST, never the gateway, so sharded and unsharded bots
take the same code path.
"""

import re
import sys
import json
import math
import random
import asyncio
import contextlib
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple, Deque, Callable, Awaitable

import aiohttp
import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from logger import BotLogger, LogArea, LogLevel
from api import (BotCycleRecord, BotStats, ChannelOutcome, StatsAPI, redact,
                 default_api_host, DEFAULT_API_PORT, MIN_API_PORT, MAX_API_PORT,
                 DEFAULT_API_HISTORY, MAX_API_HISTORY)
logger = BotLogger.get_instance()

REQUEST_TIMEOUT_SECONDS = 15
ALERT_FLUSH_SECONDS = 10
ALERT_MAX_CHARS = 1800
HEARTBEAT_GRACE_SECONDS = 300
GUILD_PAGE_SIZE = 200
GUILD_PAGE_LIMIT = 500
# Discord allows 2 renames per 10 min; a 5-min gap stays clear of it.
CHANNEL_RENAME_COOLDOWN_SECONDS = 300

DEFAULT_MAX_ATTEMPTS = 2
MAX_ATTEMPTS_CEILING = 5
DEFAULT_ATTEMPT_DELAY = 2.0
ATTEMPT_DELAY_CEILING = 60.0
# Longer than this, skip the cycle rather than retry sooner than asked.
RETRY_ABANDON_SECONDS = 120.0
RETRY_JITTER = 0.25
CYCLE_BUDGET_FRACTION = 0.8
# login, guild count, metrics post, command fetch, command sync
RETRY_STEPS_PER_CYCLE = 5
DEFAULT_INTERVAL_MINUTES = 30.0
MIN_INTERVAL_MINUTES = 0.05
MAX_INTERVAL_MINUTES = 10080.0

DEFAULT_ALERT_AFTER_FAILURES = 1
MAX_ALERT_AFTER_FAILURES = 50
ALERT_MAX_PENDING = 500

TRANSIENT_ERRORS = (discord.DiscordException, aiohttp.ClientError, asyncio.TimeoutError, OSError)

CHANNEL_NAME_LIMIT = 100
CHANNEL_PLACEHOLDER = re.compile(r'\{(\w+)(:,)?\}')
CHANNEL_CONDITIONAL = re.compile(
    r'\{if\s+(\w+)\s*(?:(>=|<=|==|!=|>|<)\s*(-?\d+))?\s*\}(.*?)(?:\{else\}(.*?))?\{end\}',
    re.DOTALL,
)

_COMPARISONS = {
    '>': lambda a, b: a > b,
    '<': lambda a, b: a < b,
    '>=': lambda a, b: a >= b,
    '<=': lambda a, b: a <= b,
    '==': lambda a, b: a == b,
    '!=': lambda a, b: a != b,
}


def _condition_holds(key: str, op: Optional[str], threshold: Optional[str],
                     values: Dict[str, Any]) -> Optional[bool]:
    """True/False, or None when the condition cannot be evaluated at all."""
    if key not in values:
        return None
    value = values[key]
    if op is None:
        return bool(value)
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return _COMPARISONS[op](value, int(threshold))


def render_channel_name(fmt: str, values: Dict[str, Any]) -> str:
    """
    Anything unrecognised is left literal rather than raising. Nested
    conditionals are not supported.
    """
    def resolve_block(match: re.Match) -> str:
        holds = _condition_holds(match.group(1), match.group(2), match.group(3), values)
        if holds is None:
            return match.group(0)
        return (match.group(4) if holds else match.group(5)) or ''

    def substitute(match: re.Match) -> str:
        key, thousands = match.group(1), match.group(2)
        if key not in values:
            return match.group(0)
        value = values[key]
        return f"{value:,}" if thousands and isinstance(value, int) else str(value)

    return CHANNEL_PLACEHOLDER.sub(substitute, CHANNEL_CONDITIONAL.sub(resolve_block, fmt))


_MISSING = object()

# Replayed by the API; the console copy scrolls away.
CONFIG_WARNINGS: List[str] = []


def _config_warn(message: str) -> None:
    CONFIG_WARNINGS.append(message)
    logger.warning(LogArea.CONFIG, message)


def _config_flag(config: Dict[str, Any], key: str, default: bool) -> bool:
    """Only a real JSON boolean counts: the string "false" is truthy."""
    value = config.get(key, _MISSING)
    if value is _MISSING:
        return default
    if not isinstance(value, bool):
        _config_warn(f"{key}={value!r} is not true or false, using {str(default).lower()}")
        return default
    return value


def _config_text(config: Dict[str, Any], key: str, default: str) -> str:
    value = config.get(key, _MISSING)
    if value is _MISSING:
        return default
    if not isinstance(value, str):
        _config_warn(f"{key}={value!r} is not text, using {default!r}")
        return default
    return value


def _config_number(config: Dict[str, Any], key: str, default: float,
                   minimum: float, maximum: float, *, integer: bool) -> float:
    """Absent -> default silently. Present but unusable -> default with a warning."""
    value = config.get(key, _MISSING)
    if value is _MISSING:
        return default
    wanted = int if integer else (int, float)
    # bool is an int subclass, so `true` would otherwise pass as 1.
    if isinstance(value, bool) or not isinstance(value, wanted):
        kind = "whole number" if integer else "number"
        _config_warn(f"{key}={value!r} is not a {kind}, using {default:g}")
        return default
    # json.load accepts bare NaN/Infinity, and asyncio.sleep(inf) never returns.
    if isinstance(value, float) and not math.isfinite(value):
        _config_warn(f"{key}={value!r} is not finite, using {default:g}")
        return default
    if value < minimum:
        _config_warn(f"{key}={value} is below {minimum:g}, using {minimum:g}")
        return minimum
    if value > maximum:
        _config_warn(f"{key}={value} is above {maximum:g}, using {maximum:g}")
        return maximum
    return value if integer else float(value)


def _permanent_http(exc: BaseException) -> bool:
    """A status that will fail identically next attempt: any 4xx but 429."""
    status = getattr(exc, 'status', None)
    return isinstance(status, int) and status != 429 and 400 <= status < 500


def _retry_after_of(exc: BaseException) -> Optional[float]:
    # HTTPException carries `response`, not `retry_after`; only RateLimited has
    # the attribute, and only when max_ratelimit_timeout is set, which it is not.
    value = getattr(exc, 'retry_after', None)
    if value is None:
        headers = getattr(getattr(exc, 'response', None), 'headers', None)
        raw = headers.get('Retry-After') if headers else None
        if raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


@dataclass
class RetryPolicy:
    # One shared instance: max_instances=1 means one cycle at a time, and the
    # budget is deliberately per-cycle rather than per-bot.
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    delay: float = DEFAULT_ATTEMPT_DELAY
    cycle_budget: float = 0.0
    deadline: Optional[float] = None
    aborted: bool = False

    def attempts(self) -> range:
        return range(1, self.max_attempts + 1)

    def begin_cycle(self) -> None:
        self.aborted = False
        # loop.time() is monotonic; an NTP step must not move the deadline.
        self.deadline = (asyncio.get_running_loop().time() + self.cycle_budget
                         if self.cycle_budget > 0 else None)

    def abort(self) -> None:
        self.aborted = True

    def wait_for(self, attempt: int, retry_after: Optional[float] = None) -> Optional[float]:
        """Seconds to sleep before attempt+1, or None to stop retrying now."""
        if self.aborted or attempt >= self.max_attempts:
            return None
        # Jitter decorrelates bots that failed at the same instant.
        wait = min(self.delay * (1.0 + random.random() * RETRY_JITTER), ATTEMPT_DELAY_CEILING)
        if retry_after is not None:
            wait = max(wait, retry_after)
        if wait > RETRY_ABANDON_SECONDS:
            return None
        if (self.deadline is not None
                and asyncio.get_running_loop().time() + wait >= self.deadline):
            return None
        return wait


@dataclass
class AlertGate:
    """Holds a failure back from the webhook until it repeats `threshold` times."""
    threshold: int = DEFAULT_ALERT_AFTER_FAILURES
    streaks: Dict[str, int] = field(default_factory=dict)

    def failed(self, key: str) -> int:
        count = self.streaks.get(key, 0) + 1
        self.streaks[key] = count
        return count

    def recovered(self, key: str) -> None:
        self.streaks.pop(key, None)


def _alert_failure(gate: AlertGate, area: LogArea, key: str, message: str) -> None:
    # Below the threshold this logs WARNING, which the sink does not forward.
    count = gate.failed(key)
    if count >= gate.threshold:
        logger.error(area, message)
    else:
        logger.warning(area, f"{message} (failure {count} of {gate.threshold} "
                             f"before alerting)")


class AlertDispatcher:
    """Batches the log lines the sink feeds it and posts them to a Discord webhook."""

    def __init__(self, webhook_url: str, session: aiohttp.ClientSession):
        self.webhook_url = webhook_url
        self.session = session
        self._pending: Deque[str] = deque(maxlen=ALERT_MAX_PENDING)
        self._overflowed = 0
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None

    def collect(self, level: LogLevel, area: LogArea, message: str) -> None:
        if len(self._pending) == ALERT_MAX_PENDING:
            self._overflowed += 1
        self._pending.append(f"[{level.value}] [{area.value}] {message}")

    def stats(self) -> Dict[str, int]:
        """Counts only; the buffered lines never leave the process."""
        return {"pending": len(self._pending), "dropped": self._overflowed}

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        # cancel() only schedules it; awaiting stops this racing the task's own flush.
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for _ in range(3):
            if not self._pending:
                break
            try:
                await self._flush()
            except Exception as e:
                self._report(f"flush crashed: {type(e).__name__}: {e}")
                break
        if self._pending:
            self._report(f"{len(self._pending)} alert line(s) dropped at shutdown")

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(ALERT_FLUSH_SECONDS)
            try:
                await self._flush()
            except Exception as e:
                # Without this an unexpected raise kills alerting for the whole run.
                self._report(f"flush crashed: {type(e).__name__}: {e}")

    async def _flush(self) -> None:
        if not self._pending:
            return
        async with self._lock:
            if not self._pending:
                return
            lines = list(self._pending)
            self._pending.clear()
            if self._overflowed:
                lines.insert(0, f"[!] {self._overflowed} line(s) dropped: alert buffer full")
                self._overflowed = 0

            packed, tail = self._fit(lines)
            if tail:
                self._requeue(tail)

            body = "\n".join(packed)
            settled = False
            try:
                async with self.session.post(self.webhook_url,
                                             json={"content": f"```\n{body}\n```"}) as response:
                    if response.status >= 400:
                        self._report(f"HTTP {response.status}")
                settled = True
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                # A rejected POST consumes its batch; re-sending needs its own backoff.
                self._report(f"{type(e).__name__}: {e}")
                settled = True
            finally:
                # The buffer was emptied before the await, so a cancellation here
                # would otherwise take the batch with it.
                if not settled:
                    self._requeue(packed)

    def _requeue(self, lines: List[str]) -> None:
        """Put lines back at the front, counting any the cap evicts."""
        before = len(self._pending)
        self._pending.extendleft(reversed(lines))
        self._overflowed += before + len(lines) - len(self._pending)

    @staticmethod
    def _fit(lines: List[str]) -> Tuple[List[str], List[str]]:
        """Split into what fits one webhook message and what has to wait for the next."""
        packed: List[str] = []
        used = 0
        for index, line in enumerate(lines):
            line = line[:ALERT_MAX_CHARS]
            if packed and used + len(line) + 1 > ALERT_MAX_CHARS:
                return packed, lines[index:]
            packed.append(line)
            used += len(line) + 1
        return packed, []

    @staticmethod
    def _report(problem: str) -> None:
        # print, not logger: logging here would feed straight back into this sink.
        print(f"alert webhook failed: {problem}", flush=True)


class BotStatsUpdater:
    """Handles updating bot statistics across multiple bot list platforms"""

    def __init__(self, bot_id: str, session: aiohttp.ClientSession, label: str,
                 policy: RetryPolicy, gate: AlertGate,
                 topgg_token: Optional[str] = None,
                 dbl_token: Optional[str] = None):
        self.session = session
        self.label = label
        self.policy = policy
        self.gate = gate
        self.topgg_token = topgg_token
        self.dbl_token = dbl_token

        self.topgg_stats_url = "https://top.gg/api/v1/projects/@me/metrics"
        self.topgg_commands_url = "https://top.gg/api/v1/projects/@me/commands"
        self.dbl_stats_url = f"https://discordbotlist.com/api/v1/bots/{bot_id}/stats"
        self.dbl_commands_url = f"https://discordbotlist.com/api/v1/bots/{bot_id}/commands"

    async def _send(self, method: str, url: str, *, auth: str,
                    payload: Any, what: str) -> bool:
        """Single funnel for every outbound call. Retries per the configured policy."""
        headers = {"Authorization": auth, "Content-Type": "application/json"}
        policy = self.policy
        key = f"{self.label}/{what}"

        for attempt in policy.attempts():
            try:
                async with self.session.request(method, url, json=payload,
                                                headers=headers) as response:
                    if response.status in (200, 201, 204):
                        self.gate.recovered(key)
                        if attempt > 1:
                            logger.warning(LogArea.API,
                                           f"[{self.label}] {what} succeeded on attempt "
                                           f"{attempt}/{policy.max_attempts}")
                        return True

                    # A WAF error page is often neither UTF-8 nor charset-tagged, and
                    # a strict decode would escape _send. backslashreplace, not
                    # replace: the result stays ASCII, so print() cannot fail either.
                    body = (await response.text(errors="backslashreplace"))[:400]
                    detail = f"[{self.label}] {what} failed: HTTP {response.status} {body}"

                    if response.status in (401, 403):
                        logger.critical(LogArea.API,
                                        f"{detail} -- token rejected, fix it in config.json")
                        return False
                    if response.status != 429 and response.status < 500:
                        _alert_failure(self.gate, LogArea.API, key, detail)
                        return False

                    wait = policy.wait_for(attempt, self._retry_after(response))
                    if wait is None:
                        _alert_failure(self.gate, LogArea.API, key,
                                       f"{detail} (gave up after {attempt} of "
                                       f"{policy.max_attempts} attempt(s))")
                        return False
                    logger.warning(LogArea.API,
                                   f"{detail} (attempt {attempt}/{policy.max_attempts}, "
                                   f"retrying in {wait:.1f}s)")
                # Outside the context manager so the connection is released first.
                await asyncio.sleep(wait)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                detail = f"[{self.label}] {what} failed: {type(e).__name__}: {e}"
                wait = policy.wait_for(attempt)
                if wait is None:
                    _alert_failure(self.gate, LogArea.API, key,
                                   f"{detail} (gave up after {attempt} of "
                                   f"{policy.max_attempts} attempt(s))")
                    return False
                logger.warning(LogArea.API,
                               f"{detail} (attempt {attempt}/{policy.max_attempts}, "
                               f"retrying in {wait:.1f}s)")
                await asyncio.sleep(wait)

        return False

    @staticmethod
    def _retry_after(response: aiohttp.ClientResponse) -> Optional[float]:
        """Uncapped: clamping this would retry sooner than the platform asked."""
        raw = response.headers.get("Retry-After")
        if raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) and value > 0 else None

    async def update_topgg(self, server_count: Optional[int] = None,
                           shard_count: Optional[int] = None,
                           user_install_count: Optional[int] = None) -> Optional[bool]:
        """Returns True sent, False failed, None when there is nothing to send."""
        if not self.topgg_token:
            return None

        # A PATCH leaves omitted fields at their old value, which is what makes the
        # per-metric gates work: a disabled metric is left alone, not zeroed.
        payload: Dict[str, Any] = {}
        if server_count is not None:
            payload["server_count"] = server_count
        if shard_count is not None:
            payload["shard_count"] = shard_count
        if user_install_count is not None:
            payload["user_install_count"] = user_install_count

        if not payload:
            logger.warning(LogArea.API,
                           f"[{self.label}] every top.gg metric is disabled, nothing to send")
            return None

        return await self._send("PATCH", self.topgg_stats_url,
                                auth=f"Bearer {self.topgg_token}",
                                payload=payload, what="top.gg metrics")

    async def update_dbl(self, guilds: Optional[int],
                         users: Optional[int] = None) -> Optional[bool]:
        """Returns True sent, False failed, None when there is nothing to send."""
        # guilds is the whole point of this listing, so no count means no post.
        if not self.dbl_token or guilds is None:
            return None

        payload: Dict[str, Any] = {"guilds": guilds}
        if users is not None:
            payload["users"] = users

        return await self._send("POST", self.dbl_stats_url,
                                auth=self.dbl_token,
                                payload=payload, what="discordbotlist.com stats")

    async def update_all(self, server_count: Optional[int] = None,
                         shard_count: Optional[int] = None,
                         users: Optional[int] = None,
                         user_install_count: Optional[int] = None) -> Dict[str, Optional[bool]]:
        topgg, dbl = await asyncio.gather(
            self.update_topgg(server_count=server_count, shard_count=shard_count,
                              user_install_count=user_install_count),
            self.update_dbl(guilds=server_count, users=users),
            return_exceptions=True,
        )
        return {"topgg": self._settle("top.gg", topgg),
                "dbl": self._settle("discordbotlist.com", dbl)}

    def _settle(self, platform: str, outcome: Any) -> Optional[bool]:
        # Without return_exceptions one platform raising abandons the other
        # mid-request, leaving it to mutate shared state after the cycle ends.
        if isinstance(outcome, asyncio.CancelledError):
            raise outcome
        if isinstance(outcome, BaseException):
            _alert_failure(self.gate, LogArea.API, f"{self.label}/{platform}",
                           f"[{self.label}] {platform} raised "
                           f"{type(outcome).__name__}: {outcome}")
            return False
        return outcome

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

    async def sync_all_commands(self, commands: List[Dict[str, Any]]
                                ) -> Tuple[Dict[str, Optional[bool]], int]:
        flat_commands = self._flatten_commands(commands)
        logger.info(LogArea.API,
                    f"[{self.label}] syncing {len(commands)} command(s) "
                    f"as {len(flat_commands)} flattened")

        topgg, dbl = await asyncio.gather(
            self.sync_commands_topgg(flat_commands),
            self.sync_commands_dbl(flat_commands),
            return_exceptions=True,
        )
        return ({"topgg": self._settle("top.gg commands", topgg),
                 "dbl": self._settle("discordbotlist.com commands", dbl)},
                len(flat_commands))


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
    stats: BotStats = field(default_factory=BotStats)

    @property
    def label(self) -> str:
        return self.name or self.config.get('name') or f"bot#{self.index + 1}"

    @property
    def ref(self) -> str:
        """Stable and URL-safe, unlike label: that flips to the Discord username
        at first login and its fallback contains a '#'."""
        return f"bot-{self.index + 1}"

    async def ensure_login(self, session: aiohttp.ClientSession,
                           policy: RetryPolicy, gate: AlertGate) -> bool:
        """Guarantee a logged-in client. Never raises; logs every failure."""
        if self.dead_reason:
            return False
        if self.client is not None:
            return True

        raw = self.config.get('bot_token')
        token = raw.strip() if isinstance(raw, str) else ''
        if not token:
            self.dead_reason = "no bot_token configured"
            logger.critical(LogArea.CONFIG, f"[{self.label}] no bot_token configured, bot disabled")
            return False

        client: Optional[discord.Client] = None
        retry_wait: Optional[float] = None
        for attempt in policy.attempts():
            # Intents only reach Discord in a gateway IDENTIFY, which never happens here.
            candidate = discord.Client(intents=discord.Intents.none())
            ok = False
            try:
                # HTTP only, no websocket, so the mandatory-sharding rule (close code
                # 4011) cannot apply.
                await candidate.login(token)
                ok = True
            except discord.LoginFailure as e:
                self.dead_reason = f"invalid bot_token ({e})"
                logger.critical(LogArea.BOT,
                                f"[{self.label}] login rejected: {e} -- fix bot_token in config.json")
                return False
            except TRANSIENT_ERRORS as e:
                detail = f"[{self.label}] login failed ({type(e).__name__}: {e})"
                retry_wait = (None if _permanent_http(e)
                              else policy.wait_for(attempt, _retry_after_of(e)))
                if retry_wait is None:
                    _alert_failure(gate, LogArea.BOT, f"{self.label}/login",
                                   f"{detail}, retrying next cycle")
                    return False
                logger.warning(LogArea.BOT,
                               f"{detail}, attempt {attempt}/{policy.max_attempts}, "
                               f"retrying in {retry_wait:.1f}s")
            finally:
                # static_login opens the aiohttp session before the request that 401s,
                # and close() drops the loop reference, so a retry needs a new client.
                if not ok:
                    with contextlib.suppress(Exception):
                        await candidate.close()
            if ok:
                client = candidate
                break
            await asyncio.sleep(retry_wait)

        if client is None:
            return False

        gate.recovered(f"{self.label}/login")
        self.client = client
        self.bot_id = str(client.user.id)
        self.application_id = client.application_id or client.user.id
        self.name = self.config.get('name') or client.user.name
        self.updater = BotStatsUpdater(
            bot_id=self.bot_id,
            session=session,
            label=self.label,
            policy=policy,
            gate=gate,
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
        # Resolved once: read lazily inside the retry loops these feed, a bad
        # value would re-warn on every attempt forever.
        self.interval_minutes = _config_number(
            self.config, 'update_interval_minutes', DEFAULT_INTERVAL_MINUTES,
            MIN_INTERVAL_MINUTES, MAX_INTERVAL_MINUTES, integer=False)
        self.policy = RetryPolicy(
            max_attempts=int(_config_number(self.config, 'max_attempts', DEFAULT_MAX_ATTEMPTS,
                                            1, MAX_ATTEMPTS_CEILING, integer=True)),
            delay=_config_number(self.config, 'attempt_delay_seconds', DEFAULT_ATTEMPT_DELAY,
                                 0.0, ATTEMPT_DELAY_CEILING, integer=False),
            cycle_budget=self.interval_minutes * 60 * CYCLE_BUDGET_FRACTION,
        )
        self.gate = AlertGate(threshold=int(_config_number(
            self.config, 'alert_after_failures', DEFAULT_ALERT_AFTER_FAILURES,
            1, MAX_ALERT_AFTER_FAILURES, integer=True)))
        self._warn_if_retries_outlast_interval()
        self._resolve_api_config()
        self._learn_secrets()

        self.sessions: List[BotSession] = []
        self.http: Optional[aiohttp.ClientSession] = None
        self.scheduler = AsyncIOScheduler()
        self.alerts: Optional[AlertDispatcher] = None
        self.api: Optional[StatsAPI] = None
        self._channel_last_updated: Dict[int, datetime] = {}

        self.started_at = datetime.now(timezone.utc)
        self.cycle_number = 0
        self.last_cycle_number = 0
        self.cycles_completed = 0
        self.cycle_running = False
        self.cycle_started_at: Optional[datetime] = None
        self.last_cycle_started_at: Optional[datetime] = None
        self.cycle_finished_at: Optional[datetime] = None
        self.cycle_duration: Optional[float] = None
        self.heartbeat_deadline: Optional[float] = None
        self.heartbeat_ok: Optional[bool] = None
        self._refresh_task: Optional[asyncio.Task] = None

    def _resolve_api_config(self) -> None:
        block = self.config.get('api')
        if block is None:
            block = {}
        elif not isinstance(block, dict):
            _config_warn(f"api={block!r} is not an object, the stats API stays off")
            block = {}
        # So a warning names the key as it appears in config.json.
        block = {f"api.{key}": value for key, value in block.items()}

        self.api_enabled = _config_flag(block, 'api.enabled', False)
        self.api_port = int(_config_number(block, 'api.port', DEFAULT_API_PORT,
                                           MIN_API_PORT, MAX_API_PORT, integer=True))
        raw_token = block.get('api.token', '')
        if not isinstance(raw_token, str):
            # Not echoed: config_warnings is served in the detailed payload.
            _config_warn("api.token is not text, continuing without auth")
            raw_token = ''
        # The request side strips too, so a stray space here would lock you out.
        self.api_token = raw_token.strip()
        self.api_allow_refresh = _config_flag(block, 'api.allow_refresh', False)
        self.api_detailed = _config_flag(block, 'api.detailed', False)
        self.history_size = int(_config_number(block, 'api.history_size',
                                               DEFAULT_API_HISTORY, 0, MAX_API_HISTORY,
                                               integer=True))
        if not self.api_enabled:
            self.history_size = 0  # nothing would ever read it
        fallback = default_api_host()
        self.api_host = _config_text(block, 'api.host', fallback).strip()
        if not self.api_host:
            _config_warn(f"api.host is empty, using {fallback}")
            self.api_host = fallback

    def _learn_secrets(self) -> None:
        redact.learn(self.config.get('alert_webhook_url'))
        redact.learn(self.api_token)
        for bot in self.config.get('bots') or []:
            if isinstance(bot, dict):
                for key in ('bot_token', 'topgg_token', 'dbl_token'):
                    redact.learn(bot.get(key))

    @property
    def config_warnings(self) -> List[str]:
        return CONFIG_WARNINGS

    @property
    def heartbeat_enabled(self) -> bool:
        path = self.config.get('heartbeat_file', 'heartbeat')
        return isinstance(path, str) and bool(path)

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

    def _warn_if_retries_outlast_interval(self) -> None:
        # coalesce=True drops an overrunning tick silently and the heartbeat allows
        # two intervals, so nothing else would surface this.
        extra = self.policy.max_attempts - 1
        if extra <= 0:
            return
        overhead = RETRY_STEPS_PER_CYCLE * extra * (REQUEST_TIMEOUT_SECONDS + self.policy.delay)
        if overhead <= self.policy.cycle_budget:
            return
        logger.warning(LogArea.CONFIG,
                       f"max_attempts={self.policy.max_attempts} at {self.policy.delay:g}s can "
                       f"add up to {overhead:.0f}s per bot, more than the "
                       f"{self.policy.cycle_budget:.0f}s retry budget for a "
                       f"{self.interval_minutes:g}-minute interval; retries will be cut short. "
                       f"Raise update_interval_minutes or lower max_attempts.")

    async def _retrying(self, session: BotSession, what: str,
                        call: Callable[[], Awaitable[Any]]) -> Any:
        """Retry `call()` per the policy, then re-raise. A factory, since a
        coroutine cannot be awaited twice."""
        policy = self.policy
        for attempt in policy.attempts():
            try:
                return await call()
            except TRANSIENT_ERRORS as e:
                if _permanent_http(e):
                    raise
                wait = policy.wait_for(attempt, _retry_after_of(e))
                if wait is None:
                    raise
                logger.warning(LogArea.API,
                               f"[{session.label}] {what} failed ({type(e).__name__}: {e}), "
                               f"attempt {attempt}/{policy.max_attempts}, retrying in {wait:.1f}s")
            await asyncio.sleep(wait)
        raise RuntimeError(f"{what}: retry loop exhausted")

    async def _reauth_if_unauthorized(self, session: BotSession, exc: BaseException) -> None:
        """A 401 here means the token was reset; drop the client so the next
        cycle logs in again."""
        if getattr(exc, 'status', None) != 401:
            return
        logger.warning(LogArea.BOT,
                       f"[{session.label}] Discord rejected the session token; "
                       f"logging in again next cycle")
        with contextlib.suppress(Exception):
            await session.close()

    async def _count_guilds(self, session: BotSession) -> Tuple[int, int, int, bool]:
        """
        Page GET /users/@me/guilds, returning (guilds, members, pages, truncated).

        Short pages are NOT the last page and the same request can return a
        different count each time, so only an empty page ends the walk and IDs are
        deduped. Raises on HTTP failure: a partial count must never be posted.
        """
        after: Optional[int] = None
        seen: set = set()
        members = pages = 0
        truncated = False
        attempt = 1  # one budget for the whole walk, not per page

        while pages < GUILD_PAGE_LIMIT:
            try:
                page = await session.client.http.get_guilds(GUILD_PAGE_SIZE, after=after,
                                                            with_counts=True)
            except TRANSIENT_ERRORS as e:
                if _permanent_http(e):
                    raise
                wait = self.policy.wait_for(attempt, _retry_after_of(e))
                if wait is None:
                    raise
                logger.warning(LogArea.API,
                               f"[{session.label}] guild page after={after} failed "
                               f"({type(e).__name__}: {e}), attempt {attempt}/"
                               f"{self.policy.max_attempts}, retrying in {wait:.1f}s")
                attempt += 1
                await asyncio.sleep(wait)
                continue
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
            truncated = True
            logger.warning(LogArea.API,
                           f"[{session.label}] stopped at the {GUILD_PAGE_LIMIT}-page cap; "
                           f"count may be short")

        logger.info(LogArea.API,
                    f"[{session.label}] counted {len(seen)} guilds across {pages} page(s), "
                    f"{members} members")
        return len(seen), members, pages, truncated

    async def _resolve_shard_count(self, session: BotSession) -> Tuple[int, str, bool]:
        """
        Configured value if set, else Discord's recommendation, else one.

        Also returns which of the three it was: once two sources agree the logs
        cannot tell them apart.
        """
        configured = session.config.get('shard_count')
        if isinstance(configured, int) and not isinstance(configured, bool) and configured >= 1:
            logger.info(LogArea.API, f"[{session.label}] shard_count {configured} from config")
            return configured, 'config', False

        ignored = configured is not None
        try:
            recommended, _url, _limits = await session.client.http.get_bot_gateway()
        except Exception as e:
            logger.warning(LogArea.API,
                           f"[{session.label}] could not read /gateway/bot "
                           f"({type(e).__name__}: {e}), reporting shard_count=1")
            return 1, 'fallback', ignored

        logger.info(LogArea.API, f"[{session.label}] Discord recommends {recommended} shard(s)")
        return max(1, int(recommended)), 'discord', ignored

    async def _fetch_install_count(self, session: BotSession) -> Tuple[Optional[int], str]:
        """Refetched each cycle because client.application is only a login snapshot."""
        try:
            app = await session.client.application_info()
        except Exception as e:
            logger.warning(LogArea.API,
                           f"[{session.label}] could not read install count "
                           f"({type(e).__name__}: {e})")
            return None, 'error'
        count = app.approximate_user_install_count
        # Discord omits it for an app with no user installs, which is not a failure.
        return (count, 'ok') if count is not None else (None, 'unavailable')

    def _cycle_deadline(self) -> float:
        """The time the next cycle must beat. One source, so the heartbeat file
        and /health cannot disagree."""
        return (datetime.now(timezone.utc).timestamp()
                + self.interval_minutes * 120 + HEARTBEAT_GRACE_SECONDS)

    def _write_heartbeat(self) -> None:
        """Stamp the deadline the next cycle must beat; the Docker healthcheck reads it."""
        # Before the path checks, so /health has a deadline even with the file off.
        deadline = self.heartbeat_deadline = self._cycle_deadline()
        path = self.config.get('heartbeat_file', 'heartbeat')
        if not path:
            return
        if not isinstance(path, str):
            # open(1, 'w') would write to stdout and then close it.
            logger.warning(LogArea.SCHEDULER,
                           f"heartbeat_file={path!r} is not a path, heartbeat disabled")
            return
        try:
            with open(path, 'w') as f:
                f.write(str(deadline))
        except OSError as e:
            self.heartbeat_ok = False
            logger.warning(LogArea.SCHEDULER, f"could not write heartbeat file {path}: {e}")
        else:
            self.heartbeat_ok = True

    async def _update_server_count_channel(self, session: BotSession,
                                           values: Dict[str, Any]) -> ChannelOutcome:
        """
        Rename a channel to reflect the current stats.

        A format with no recognised placeholder gets the server count appended; the
        README documents the placeholders.
        """
        server_count = values['server_count']
        bot_config = session.config
        channel_id_str = bot_config.get('server_count_channel_id', '')
        if not channel_id_str:
            return ChannelOutcome('not_configured')

        try:
            channel_id = int(channel_id_str)
        except (ValueError, TypeError):
            logger.warning(LogArea.CHANNEL,
                           f"[{session.label}] invalid server_count_channel_id: {channel_id_str!r}")
            return ChannelOutcome('invalid_id')

        now = datetime.now(timezone.utc)
        last_update = self._channel_last_updated.get(channel_id)
        if last_update is not None:
            elapsed = (now - last_update).total_seconds()
            if elapsed < CHANNEL_RENAME_COOLDOWN_SECONDS:
                remaining = int(CHANNEL_RENAME_COOLDOWN_SECONDS - elapsed)
                logger.warning(LogArea.CHANNEL,
                               f"[{session.label}] skipping channel rename: "
                               f"rate-limit cooldown ({remaining}s remaining)")
                return ChannelOutcome('cooldown', cooldown_remaining=float(remaining))

        fmt = bot_config.get('server_count_channel_format', '')
        if not fmt:
            channel_name = f"{session.label}: {server_count}"
        else:
            rendered = render_channel_name(fmt, values)
            if rendered == fmt:
                channel_name = f"{fmt}{server_count}"
            else:
                # All-conditional formats can render to nothing, which Discord rejects.
                channel_name = rendered.strip() or f"{session.label}: {server_count}"

        if len(channel_name) > CHANNEL_NAME_LIMIT:
            logger.warning(LogArea.CHANNEL,
                           f"[{session.label}] channel name is {len(channel_name)} chars, "
                           f"truncating to Discord's {CHANNEL_NAME_LIMIT}")
            channel_name = channel_name[:CHANNEL_NAME_LIMIT]

        try:
            # No gateway means no channel cache, so always fetch.
            channel = await session.client.fetch_channel(channel_id)
            await channel.edit(name=channel_name)
        except discord.Forbidden:
            _alert_failure(self.gate, LogArea.CHANNEL, f"{session.label}/channel rename",
                           f"[{session.label}] missing 'Manage Channel' permission "
                           f"for channel {channel_id}")
            return ChannelOutcome('forbidden',
                                  error="missing 'Manage Channel' permission")
        except TRANSIENT_ERRORS as e:
            _alert_failure(self.gate, LogArea.CHANNEL, f"{session.label}/channel rename",
                           f"[{session.label}] failed to rename channel {channel_id}: "
                           f"{type(e).__name__}: {e}")
            return ChannelOutcome('failed', error=f"{type(e).__name__}: {e}")
        else:
            self.gate.recovered(f"{session.label}/channel rename")
            self._channel_last_updated[channel_id] = now
            logger.info(LogArea.CHANNEL,
                        f"[{session.label}] updated channel name to '{channel_name}'")
            return ChannelOutcome('ok', name=channel_name, renamed_at=now)

    @staticmethod
    def _log_results(session: BotSession, results: Dict[str, Optional[bool]],
                     suffix: str = "") -> None:
        for platform, outcome in results.items():
            status = "[SKIP]" if outcome is None else ("[OK]" if outcome else "[FAIL]")
            logger.info(LogArea.API, f"[{session.label}]   {status} {platform}{suffix}")

    @staticmethod
    def _record_delivery(record: BotCycleRecord, step_name: str,
                         outcome: Optional[bool], reason: Optional[str]) -> None:
        step = record.step(step_name)
        if outcome is True:
            step.ok('sent')
        elif outcome is False:
            step.failed('the post was rejected; the log line carries the status')
        else:
            step.skipped(reason)

    async def update_bot_stats(self, session: BotSession):
        """Run one full update cycle for a single bot"""
        record = BotCycleRecord.begin(self.cycle_number)
        try:
            await self._run_bot_cycle(session, record)
        except asyncio.CancelledError:
            record.outcome = 'cancelled'
            raise
        except Exception as e:
            record.crash(e)
            raise  # update_all_bots_stats still logs this CRITICAL
        finally:
            record.finish()
            session.stats.commit(record, self.history_size)

    async def _run_bot_cycle(self, session: BotSession, record: BotCycleRecord):
        if not await session.ensure_login(self.http, self.policy, self.gate):
            record.stop('login', session.dead_reason or 'login failed')
            return
        record.step('login').ok()

        try:
            guild_count, member_count, pages, truncated = await self._count_guilds(session)
        except TRANSIENT_ERRORS as e:
            _alert_failure(self.gate, LogArea.API, f"{session.label}/guild count",
                           f"[{session.label}] guild count failed ({type(e).__name__}: {e}); "
                           f"skipping this cycle rather than posting a partial count")
            await self._reauth_if_unauthorized(session, e)
            record.stop('guild_count', f"{type(e).__name__}: {e}")
            return
        self.gate.recovered(f"{session.label}/guild count")
        record.measured_at = datetime.now(timezone.utc)
        record.server_count = guild_count
        record.member_count = member_count
        record.guild_pages_walked = pages
        record.server_count_truncated = truncated
        record.step('guild_count').ok()

        shard_count, shard_source, shard_ignored = await self._resolve_shard_count(session)
        record.shard_count = shard_count
        record.shard_count_source = shard_source
        record.shard_count_config_ignored = shard_ignored
        record.step('shard_count').ok()

        install_count, install_status = await self._fetch_install_count(session)
        record.user_install_count = install_count
        record.user_install_status = install_status
        if install_status == 'error':
            record.step('user_installs').failed('could not read the install count')
        elif install_status == 'unavailable':
            record.step('user_installs').skipped('no_data')
        else:
            record.step('user_installs').ok()

        record.apply_channel(await self._update_server_count_channel(session, {
            'server_count': guild_count,
            'count': guild_count,
            'shard_count': shard_count,
            'member_count': member_count,
            'user_install_count': install_count or 0,
            'bot_name': session.label,
            'bot_id': session.bot_id or '',
        }))

        report_servers = session.config.get('report_server_count', True)
        report_installs = session.config.get('report_user_installs', True)
        has_topgg = bool(session.config.get('topgg_token'))
        has_dbl = bool(session.config.get('dbl_token'))
        sends_installs = bool(report_installs) and install_count is not None

        results = await session.updater.update_all(
            server_count=guild_count if report_servers else None,
            shard_count=shard_count if report_servers else None,
            # 0 would overwrite the listing's real figure, so omit it instead.
            users=(member_count or None) if report_servers else None,
            user_install_count=install_count if report_installs else None,
        )
        self._log_results(session, results)

        # top.gg PATCHes, so an omitted metric keeps its old value there.
        server_fields = ['server_count', 'shard_count']
        topgg = record.step('topgg_stats')
        topgg.fields_sent = ([] if not report_servers else list(server_fields)) + (
            ['user_install_count'] if sends_installs else [])
        topgg.fields_omitted = (list(server_fields) if not report_servers else []) + (
            [] if sends_installs else ['user_install_count'])

        self._record_delivery(record, 'topgg_stats', results['topgg'],
                              None if has_topgg else 'no_token')
        if results['topgg'] is None and has_topgg:
            topgg.reason = 'all_metrics_disabled'
        self._record_delivery(record, 'dbl_stats', results['dbl'],
                              None if has_dbl else 'no_token')
        if results['dbl'] is None and has_dbl:
            record.step('dbl_stats').reason = 'report_server_count'

        try:
            commands = await self._retrying(
                session, "fetching slash commands",
                lambda: session.client.http.get_global_commands(session.application_id))
        except TRANSIENT_ERRORS as e:
            _alert_failure(self.gate, LogArea.API, f"{session.label}/slash commands",
                           f"[{session.label}] fetching slash commands failed "
                           f"({type(e).__name__}: {e})")
            await self._reauth_if_unauthorized(session, e)
            record.stop('slash_commands', f"{type(e).__name__}: {e}")
            return
        self.gate.recovered(f"{session.label}/slash commands")
        record.step('slash_commands').ok()
        record.command_count = len(commands)

        command_results, flattened = await session.updater.sync_all_commands(commands)
        record.command_count_flattened = flattened
        self._log_results(session, command_results, suffix=" (commands)")
        self._record_delivery(record, 'topgg_commands', command_results['topgg'],
                              None if has_topgg else 'no_token')
        self._record_delivery(record, 'dbl_commands', command_results['dbl'],
                              None if has_dbl else 'no_token')

    async def update_all_bots_stats(self):
        # max_instances covers scheduled ticks; POST /refresh bypasses the
        # scheduler entirely.
        if self.cycle_running:
            logger.warning(LogArea.SCHEDULER,
                           "a cycle is already running, skipping this trigger")
            return
        self.cycle_running = True
        self.cycle_number += 1
        self.cycle_started_at = datetime.now(timezone.utc)
        began = asyncio.get_running_loop().time()

        logger.spacer()
        logger.info(LogArea.SCHEDULER, "Starting scheduled stats update")
        logger.spacer()

        try:
            self.policy.begin_cycle()
            outcomes = await asyncio.gather(
                *(self.update_bot_stats(session) for session in self.sessions),
                return_exceptions=True,
            )
            for session, outcome in zip(self.sessions, outcomes):
                if isinstance(outcome, asyncio.CancelledError):
                    continue
                if isinstance(outcome, BaseException):
                    logger.critical(LogArea.SCHEDULER,
                                    f"[{session.label}] update cycle crashed: "
                                    f"{type(outcome).__name__}: {outcome}")

            self._write_heartbeat()
            self.cycles_completed += 1
        finally:
            self.cycle_running = False
            self.last_cycle_number = self.cycle_number
            # So a running cycle cannot lend its start time to this one.
            self.last_cycle_started_at = self.cycle_started_at
            self.cycle_finished_at = datetime.now(timezone.utc)
            self.cycle_duration = asyncio.get_running_loop().time() - began

        logger.spacer()
        logger.info(LogArea.SCHEDULER, "Stats update completed")
        logger.spacer()

    def request_refresh(self) -> Tuple[bool, int]:
        """
        Run a cycle now, leaving the schedule alone.

        Not scheduler.modify_job: that shifts the whole interval forward, and
        max_instances would swallow the run anyway.
        """
        # create_task only schedules it, so cycle_running is still false here;
        # without this a double POST starts two cycles.
        pending = self._refresh_task is not None and not self._refresh_task.done()
        if self.cycle_running or pending:
            return False, self.cycle_number
        logger.info(LogArea.STATS_API, "manual refresh requested over the API")
        self._refresh_task = asyncio.create_task(self.update_all_bots_stats())
        self._refresh_task.add_done_callback(self._refresh_done)
        return True, self.cycle_number + 1

    @staticmethod
    def _refresh_done(task: asyncio.Task) -> None:
        # Nothing awaits this task, so its exception has to be retrieved here.
        if not task.cancelled() and task.exception() is not None:
            logger.critical(LogArea.STATS_API,
                            f"manual refresh crashed: {task.exception()!r}")

    async def start(self):
        """Log in all bots and start the scheduler"""
        logger.info(LogArea.STARTUP, "Starting Bot Stats Manager (REST-only, no gateway)...")

        self.http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS, connect=5),
        )

        webhook_url = self.config.get('alert_webhook_url', '')
        if webhook_url:
            self.alerts = AlertDispatcher(webhook_url, self.http)
            logger.set_sink(self.alerts.collect)
            self.alerts.start()
            logger.info(LogArea.STARTUP, "Alerting errors to the configured webhook")

        self.sessions = [BotSession(index=i, config=bot_config)
                         for i, bot_config in enumerate(self.config.get('bots', []))]
        if not self.sessions:
            logger.critical(LogArea.CONFIG,
                            f"No bots configured in {self.config_path} -- "
                            f"add at least one entry to \"bots\"")
            sys.exit(1)

        if self.api_enabled:
            # Before the first cycle, which can take minutes.
            self.api = StatsAPI(self, self.api_host, self.api_port,
                                self.api_token, self.api_allow_refresh,
                                self.api_detailed)
            await self.api.start()

        # A separate login gather here would spend the retry budget twice over for
        # a transiently unreachable bot; the cycle logs them in itself.
        await self.update_all_bots_stats()

        # bot_id survives close(), so a bot dropped by a mid-cycle 401 still counts.
        ready = sum(1 for session in self.sessions if session.bot_id is not None)
        logger.info(LogArea.STARTUP, f"Logged in {ready}/{len(self.sessions)} bot(s)")
        if not ready:
            logger.critical(LogArea.STARTUP,
                            "No bot logged in; check tokens and network. Login is retried each cycle.")

        self.scheduler.add_job(
            self.update_all_bots_stats,
            IntervalTrigger(minutes=self.interval_minutes),
            id='stats_update',
            name='Update bot statistics',
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        self.scheduler.start()
        logger.info(LogArea.SCHEDULER,
                    f"Scheduled stats updates every {self.interval_minutes:g} minutes")
        logger.info(LogArea.STARTUP, "Bot Stats Manager is now running. Press Ctrl+C to stop.")

        await asyncio.Event().wait()

    async def stop(self):
        """Stop the scheduler and release every client"""
        if self.api is not None:
            await self.api.stop()

        logger.info(LogArea.SHUTDOWN, "Stopping scheduler...")
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        self.policy.abort()

        # Runs outside the scheduler, so nothing else stops it using self.http
        # and the clients closed just below.
        refresh, self._refresh_task = self._refresh_task, None
        if refresh is not None and not refresh.done():
            refresh.cancel()
            # CancelledError is a BaseException, so Exception alone lets the
            # cancellation escape.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await refresh

        for session in self.sessions:
            try:
                await session.close()
            except Exception as e:
                logger.warning(LogArea.SHUTDOWN,
                               f"[{session.label}] close failed: {type(e).__name__}: {e}")

        if self.alerts is not None:
            # Unhook first, or an ERROR logged after the final flush is never drained.
            logger.set_sink(None)
            await self.alerts.stop()

        if self.http is not None:
            await self.http.close()

        # aiohttp closes TLS transports asynchronously; yield so they finish before
        # the loop shuts down, rather than surfacing as unclosed-socket warnings.
        await asyncio.sleep(0.25)

        logger.info(LogArea.SHUTDOWN, "Bot Stats Manager stopped.")


async def main():
    # A Windows console is usually cp1252; an out-of-range character in a bot
    # name or API message must not take the logger down with it.
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='backslashreplace')

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
