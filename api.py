#!/usr/bin/env python3
"""
Read-only HTTP stats API, disabled unless config.json carries
"api": {"enabled": true}.

Payloads are built synchronously: an await mid-build would let a running cycle
interleave and serve a torn snapshot.
"""

from __future__ import annotations

import re
import os
import json
import ipaddress
import hmac
import socket
import asyncio
import contextlib
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple, Deque, TYPE_CHECKING

from aiohttp import web

from logger import BotLogger, LogArea

if TYPE_CHECKING:  # main imports this module, so the reverse must stay type-only.
    from main import BotStatsManager, BotSession

logger = BotLogger.get_instance()

DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8080
MIN_API_PORT = 1
MAX_API_PORT = 65535
DEFAULT_API_HISTORY = 48
MAX_API_HISTORY = 1000
# The runner default is 60s, which would turn Ctrl+C into a minute-long wait on
# an idle keep-alive connection.
API_SHUTDOWN_TIMEOUT = 5.0
API_REQUEST_BODY_LIMIT = 1024
ERROR_DETAIL_LIMIT = 200
HEALTH_RETRY_AFTER = 60

LOOPBACK_NAMES = frozenset({"localhost"})
# A container's own loopback is unreachable from the host; the published port
# is what restricts access there.
CONTAINER_API_HOST = "0.0.0.0"
KEY_PATTERN = re.compile(r'(?:[0-9]{1,20}|bot-[0-9]{1,4})\Z')

LOCAL_STEPS = ('login', 'guild_count', 'shard_count', 'user_installs',
               'channel_rename', 'slash_commands')
PLATFORM_STEPS = ('topgg_stats', 'topgg_commands', 'dbl_stats', 'dbl_commands')
ALL_STEPS = LOCAL_STEPS + PLATFORM_STEPS

# Carried forward as units, keyed on the field that proves the step ran: a fresh
# count must never sit beside a stale source or status.
_MEASUREMENT_GROUPS = (
    ('measured_at', ('measured_at', 'server_count', 'member_count',
                     'guild_pages_walked', 'server_count_truncated')),
    ('shard_count', ('shard_count', 'shard_count_source',
                     'shard_count_config_ignored')),
    ('user_install_status', ('user_install_count', 'user_install_status')),
    ('command_count', ('command_count', 'command_count_flattened')),
)

_SECRET_PATTERNS = (
    re.compile(r'https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\S+', re.I),
    re.compile(r'(?i)\b(?:authorization|bearer)\b\s*:?\s*\S+'),
    # Discord bot tokens, and the JWTs the listings issue.
    re.compile(r'\b[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{16,}\b'),
)


def is_loopback(host: str) -> bool:
    name = host.strip().lower()
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return name in LOOPBACK_NAMES


def listen_label(host: str, port: int) -> str:
    """A wildcard bind is not an address a client can be pointed at."""
    name = host.strip().lower()
    try:
        wildcard = ipaddress.ip_address(name).is_unspecified
    except ValueError:
        wildcard = not name
    return f"port {port} on all interfaces" if wildcard else f"http://{host}:{port}"


def in_container() -> bool:
    if os.path.exists('/.dockerenv'):
        return True
    try:
        with open('/proc/1/cgroup', 'r') as f:
            return any(m in f.read() for m in ('docker', 'containerd', 'kubepods'))
    except OSError:
        return False


def default_api_host() -> str:
    return CONTAINER_API_HOST if in_container() else DEFAULT_API_HOST


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _iso_epoch(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    return _iso(datetime.fromtimestamp(value, timezone.utc))


def _round(value: Optional[float], places: int = 1) -> Optional[float]:
    return None if value is None else round(value, places)


_FALSEY = frozenset({'0', 'false', 'no', 'off'})


def _wants(request: web.Request, name: str) -> bool:
    """Bare presence means on, but ?x=0 must not."""
    value = request.query.get(name)
    return value is not None and value.strip().lower() not in _FALSEY


class Redactor:
    """Strips known secrets out of free text before it reaches a response."""

    def __init__(self) -> None:
        self._literals: List[str] = []

    def learn(self, value: Any) -> None:
        if isinstance(value, str) and len(value.strip()) >= 8:
            self._literals.append(value.strip())

    def __call__(self, text: Any, limit: int = ERROR_DETAIL_LIMIT) -> Optional[str]:
        if text is None:
            return None
        clean = str(text)
        for literal in self._literals:
            clean = clean.replace(literal, '[redacted]')
        for pattern in _SECRET_PATTERNS:
            clean = pattern.sub('[redacted]', clean)
        # Truncate last: cutting first could leave half a secret behind the sweep.
        return clean[:limit]


redact = Redactor()


@dataclass
class ChannelOutcome:
    state: str
    name: Optional[str] = None
    renamed_at: Optional[datetime] = None
    cooldown_remaining: float = 0.0
    error: Optional[str] = None


@dataclass
class Step:
    """`failures` is the length of the current failure run, not a list."""
    result: str = 'not_reached'
    reason: Optional[str] = None
    failures: int = 0
    last_error: Optional[str] = None
    fields_sent: List[str] = field(default_factory=list)
    fields_omitted: List[str] = field(default_factory=list)

    def ok(self, result: str = 'ok') -> None:
        self.result = result
        self.last_error = None

    def failed(self, detail: Any) -> None:
        self.result = 'failed'
        self.last_error = redact(detail)

    def skipped(self, reason: Optional[str]) -> None:
        self.result = 'skipped'
        self.reason = reason


@dataclass
class BotCycleRecord:
    """
    What one cycle observed for one bot.

    Mutated only by that cycle's coroutine, then published in a single
    assignment, so a reader sees whole cycles only.
    """
    number: int
    started_at: datetime
    finished_at: Optional[datetime] = None
    outcome: str = 'running'
    stopped_after: Optional[str] = None
    error: Optional[str] = None

    measured_at: Optional[datetime] = None
    server_count: Optional[int] = None
    server_count_truncated: bool = False
    guild_pages_walked: Optional[int] = None
    member_count: Optional[int] = None
    shard_count: Optional[int] = None
    shard_count_source: Optional[str] = None
    shard_count_config_ignored: bool = False
    user_install_count: Optional[int] = None
    user_install_status: Optional[str] = None
    command_count: Optional[int] = None
    command_count_flattened: Optional[int] = None

    channel_state: str = 'never'
    channel_name: Optional[str] = None
    channel_renamed_at: Optional[datetime] = None
    channel_cooldown: float = 0.0

    steps: Dict[str, Step] = field(
        default_factory=lambda: {name: Step() for name in ALL_STEPS})

    @classmethod
    def begin(cls, number: int) -> 'BotCycleRecord':
        return cls(number=number, started_at=datetime.now(timezone.utc))

    def step(self, name: str) -> Step:
        return self.steps[name]

    def apply_channel(self, outcome: ChannelOutcome) -> None:
        self.channel_state = outcome.state
        self.channel_cooldown = outcome.cooldown_remaining
        if outcome.state == 'ok':
            self.channel_name = outcome.name
            self.channel_renamed_at = outcome.renamed_at
            self.step('channel_rename').ok()
        elif outcome.state in ('forbidden', 'failed'):
            self.step('channel_rename').failed(outcome.error)
        elif outcome.state == 'invalid_id':
            self.step('channel_rename').failed('invalid server_count_channel_id')
        elif outcome.state == 'cooldown':
            self.step('channel_rename').skipped('cooldown')
        else:
            self.step('channel_rename').skipped('not_configured')

    def stop(self, step: str, detail: Any) -> None:
        self.outcome = 'failed'
        self.stopped_after = step
        self.step(step).failed(detail)

    def crash(self, exc: BaseException) -> None:
        self.outcome = 'crashed'
        self.error = redact(f"{type(exc).__name__}: {exc}")

    def finish(self) -> None:
        self.finished_at = datetime.now(timezone.utc)
        if self.outcome == 'running':
            self.outcome = 'ok'

    @property
    def duration(self) -> Optional[float]:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    def inherit(self, previous: Optional['BotCycleRecord']) -> None:
        """Continue each failure streak, and carry forward what this cycle never
        got far enough to observe -- so an outage does not blank the figures."""
        for name, current in self.steps.items():
            before = previous.steps.get(name) if previous is not None else None
            if current.result == 'failed':
                # Counted here rather than at the failure site so that the first
                # cycle after a restart still reports a streak of 1, not 0.
                current.failures = (before.failures if before is not None else 0) + 1
            elif current.result in ('ok', 'sent', 'skipped'):
                # A skipped step concluded, so it is not failing; only
                # not_reached leaves the streak unresolved.
                current.failures = 0
                current.last_error = None
            elif before is not None:
                current.failures = before.failures
                current.last_error = current.last_error or before.last_error

        if previous is None:
            return

        for marker, group in _MEASUREMENT_GROUPS:
            if getattr(self, marker) is None:
                for name in group:
                    setattr(self, name, getattr(previous, name))

        if self.steps['channel_rename'].result == 'not_reached':
            self.channel_state = previous.channel_state
            self.channel_cooldown = previous.channel_cooldown
            self.channel_name = previous.channel_name
            self.channel_renamed_at = previous.channel_renamed_at

    def digest(self) -> Dict[str, Any]:
        return {
            "number": self.number,
            "started_at": _iso(self.started_at),
            "duration_seconds": _round(self.duration, 2),
            "outcome": self.outcome,
            "stopped_after": self.stopped_after,
            "server_count": self.server_count,
            "member_count": self.member_count,
            "shard_count": self.shard_count,
            "user_install_count": self.user_install_count,
            "failed_steps": sorted(name for name, step in self.steps.items()
                                   if step.result == 'failed'),
        }


@dataclass
class BotStats:
    last: Optional[BotCycleRecord] = None
    history: Deque[Dict[str, Any]] = field(default_factory=deque)
    cycles: int = 0
    failed_cycles: int = 0

    def commit(self, record: BotCycleRecord, history_size: int) -> None:
        record.inherit(self.last)
        self.cycles += 1
        if record.outcome != 'ok':
            self.failed_cycles += 1
        if history_size:
            if self.history.maxlen != history_size:
                self.history = deque(self.history, maxlen=history_size)
            self.history.append(record.digest())
        self.last = record  # single rebind: readers only ever see whole cycles


def _configured(value: Any) -> bool:
    """The same truthiness test the posting code uses, so "" reads as absent."""
    return bool(value)


def _flag(value: Any, default: bool = True) -> bool:
    return default if value is None else bool(value)


def _next_run(manager: 'BotStatsManager') -> Optional[datetime]:
    """
    None until the scheduler starts.

    The job is added after the first inline cycle, and a pending Job leaves its
    next_run_time slot unset, so this is a getattr rather than an attribute read.
    """
    try:
        job = manager.scheduler.get_job('stats_update')
    except Exception:
        return None
    value = getattr(job, 'next_run_time', None) if job is not None else None
    # APScheduler hands these back in the machine's local zone; the rest of the
    # project is UTC.
    return value.astimezone(timezone.utc) if isinstance(value, datetime) else None


def _bot_status(session: 'BotSession', record: Optional[BotCycleRecord]) -> str:
    if session.dead_reason:
        return 'disabled'
    if session.bot_id is None:
        return 'starting' if record is None else 'never_logged_in'
    if record is not None and (record.outcome != 'ok' or
                               any(s.result == 'failed' for s in record.steps.values())):
        return 'degraded'
    return 'ok'


def bot_payload(manager: 'BotStatsManager', session: 'BotSession',
                detailed: bool) -> Dict[str, Any]:
    record = session.stats.last
    payload: Dict[str, Any] = {
        "id": session.bot_id,
        "name": session.label,
        "status": _bot_status(session, record),
        "server_count": record.server_count if record else None,
        "member_count": record.member_count if record else None,
        "shard_count": record.shard_count if record else None,
        "user_install_count": record.user_install_count if record else None,
        "command_count": record.command_count if record else None,
        "channel_name": record.channel_name if record else None,
        "updated_at": _iso(record.measured_at) if record else None,
    }
    if detailed:
        payload["detail"] = _bot_detail(manager, session, record)
    return payload


def _platform_payload(session: 'BotSession', record: Optional[BotCycleRecord],
                      token_key: str, stats_step: str, commands_step: str,
                      threshold: int) -> Dict[str, Any]:
    configured = _configured(session.config.get(token_key))

    def slot(name: str, with_fields: bool) -> Dict[str, Any]:
        step = record.steps[name] if record is not None else Step()
        body: Dict[str, Any] = {
            "result": step.result,
            "reason": step.reason if step.reason else (None if configured else 'no_token'),
            "consecutive_failures": step.failures,
            "alerting": step.result == 'failed' and step.failures >= threshold,
            "last_error": step.last_error,
        }
        if with_fields:
            body["fields_sent"] = list(step.fields_sent)
            body["fields_omitted"] = list(step.fields_omitted)
        return body

    return {
        "configured": configured,
        "stats": slot(stats_step, with_fields=stats_step == 'topgg_stats'),
        "commands": slot(commands_step, with_fields=False),
    }


def _bot_detail(manager: 'BotStatsManager', session: 'BotSession',
                record: Optional[BotCycleRecord]) -> Dict[str, Any]:
    threshold = manager.gate.threshold
    config = session.config

    if config.get('name'):
        name_source = 'config'
    elif session.name:
        name_source = 'discord'
    else:
        name_source = 'placeholder'

    report_servers = _flag(config.get('report_server_count'))
    report_installs = _flag(config.get('report_user_installs'))
    channel_id = config.get('server_count_channel_id')
    channel_format = config.get('server_count_channel_format')

    return {
        "ref": session.ref,
        "application_id": (str(session.application_id)
                           if session.application_id is not None else None),
        "name_source": name_source,
        "dead_reason": redact(session.dead_reason),
        "cycles": session.stats.cycles,
        "failed_cycles": session.stats.failed_cycles,
        "last_cycle": None if record is None else {
            "number": record.number,
            "started_at": _iso(record.started_at),
            "finished_at": _iso(record.finished_at),
            "duration_seconds": _round(record.duration, 2),
            "outcome": record.outcome,
            "stopped_after": record.stopped_after,
            "error": record.error,
        },
        "diagnostics": {
            "server_count_truncated": record.server_count_truncated if record else False,
            "guild_pages_walked": record.guild_pages_walked if record else None,
            "shard_count_source": record.shard_count_source if record else None,
            "shard_count_config_ignored": (record.shard_count_config_ignored
                                           if record else False),
            "user_install_count_status": record.user_install_status if record else None,
            "command_count_flattened": record.command_count_flattened if record else None,
        },
        "reporting": {
            "server_count": {
                "enabled": report_servers,
                "reason": None if report_servers else 'report_server_count',
            },
            "user_installs": {
                "enabled": report_installs,
                "reason": None if report_installs else 'report_user_installs',
            },
        },
        "platforms": {
            "topgg": _platform_payload(session, record, 'topgg_token',
                                       'topgg_stats', 'topgg_commands', threshold),
            "discordbotlist": _platform_payload(session, record, 'dbl_token',
                                                'dbl_stats', 'dbl_commands', threshold),
        },
        "channel": {
            "configured": _configured(channel_id),
            "channel_id": str(channel_id) if _configured(channel_id) else None,
            "format": channel_format if _configured(channel_format) else None,
            "format_source": 'config' if _configured(channel_format) else 'default',
            "state": record.channel_state if record else 'never',
            "last_renamed_at": _iso(record.channel_renamed_at) if record else None,
            "cooldown_seconds_remaining": _round(record.channel_cooldown) if record else 0.0,
        },
        "steps": {
            name: {
                "result": (record.steps[name].result if record else 'not_reached'),
                "consecutive_failures": (record.steps[name].failures if record else 0),
                "alerting": bool(record and record.steps[name].result == 'failed'
                                 and record.steps[name].failures >= threshold),
                "last_error": (record.steps[name].last_error if record else None),
            }
            for name in LOCAL_STEPS
        },
    }


def _problems(manager: 'BotStatsManager') -> List[Dict[str, Any]]:
    threshold = manager.gate.threshold
    found: List[Dict[str, Any]] = []
    for session in manager.sessions:
        if session.dead_reason:
            found.append({"ref": session.ref, "name": session.label,
                          "step": "login", "consecutive_failures": None,
                          "alerting": True, "detail": redact(session.dead_reason)})
            continue
        record = session.stats.last
        if record is None:
            continue
        if record.outcome == 'crashed':
            found.append({"ref": session.ref, "name": session.label,
                          "step": "cycle", "consecutive_failures": None,
                          "alerting": True, "detail": record.error})
        for name in ALL_STEPS:
            step = record.steps[name]
            if step.result != 'failed':
                continue
            found.append({"ref": session.ref, "name": session.label, "step": name,
                          "consecutive_failures": step.failures,
                          "alerting": step.failures >= threshold,
                          "detail": step.last_error})
    return found


def _service_payload(manager: 'BotStatsManager') -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    next_run = _next_run(manager)
    deadline = manager.heartbeat_deadline
    alerts = manager.alerts.stats() if manager.alerts is not None else None

    return {
        "started_at": _iso(manager.started_at),
        "uptime_seconds": _round((now - manager.started_at).total_seconds()),
        "cycle_state": 'running' if manager.cycle_running else 'idle',
        "cycles_completed": manager.cycles_completed,
        "last_cycle": None if manager.cycle_finished_at is None else {
            "number": manager.last_cycle_number,
            "started_at": _iso(manager.last_cycle_started_at),
            "finished_at": _iso(manager.cycle_finished_at),
            "duration_seconds": _round(manager.cycle_duration, 2),
        },
        "schedule": {
            "interval_minutes": manager.interval_minutes,
            "scheduler_state": 'running' if manager.scheduler.running else 'starting',
            "next_run_in_seconds": (_round((next_run - now).total_seconds())
                                    if next_run is not None else None),
        },
        "heartbeat": {
            "enabled": manager.heartbeat_enabled,
            "deadline_at": _iso_epoch(deadline),
            "seconds_remaining": (_round(deadline - now.timestamp())
                                  if deadline is not None else None),
            "last_write_ok": manager.heartbeat_ok,
        },
        "retries": {
            "max_attempts": manager.policy.max_attempts,
            "attempt_delay_seconds": manager.policy.delay,
            "cycle_budget_seconds": _round(manager.policy.cycle_budget),
        },
        "alerting": {
            "webhook_configured": manager.alerts is not None,
            "alert_after_failures": manager.gate.threshold,
            "pending_lines": alerts["pending"] if alerts else None,
            "dropped_lines": alerts["dropped"] if alerts else None,
        },
        "bots": {
            "logged_in": sum(1 for s in manager.sessions if s.bot_id is not None),
            "disabled": sum(1 for s in manager.sessions if s.dead_reason),
        },
        "auth_required": manager.api is not None and manager.api.auth_required,
        "config_warnings": list(manager.config_warnings),
    }


def _bots_payload(manager: 'BotStatsManager', detailed: bool) -> List[Dict[str, Any]]:
    bots = []
    for session in manager.sessions:
        try:
            bots.append(bot_payload(manager, session, detailed))
        except Exception as e:
            logger.warning(LogArea.STATS_API,
                           f"[{session.ref}] could not serialize: {type(e).__name__}: {e}")
            bots.append({"id": None, "name": session.label, "status": "unknown"})
    return bots


def root_payload(manager: 'BotStatsManager', detailed: bool) -> Dict[str, Any]:
    next_run = _next_run(manager)
    servers = members = installs = 0
    for session in manager.sessions:
        record = session.stats.last
        if record is None:
            continue
        servers += record.server_count or 0
        members += record.member_count or 0
        installs += record.user_install_count or 0

    payload: Dict[str, Any] = {
        "updated_at": _iso(manager.cycle_finished_at),
        "next_update_at": _iso(next_run),
        "totals": {
            "bots": len(manager.sessions),
            "server_count": servers,
            "member_count": members,
            "user_install_count": installs,
        },
        "bots": _bots_payload(manager, detailed),
    }
    if detailed:
        payload["service"] = _service_payload(manager)
        payload["problems"] = _problems(manager)
    return payload


def _checks(manager: 'BotStatsManager') -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc)
    starting = manager.cycles_completed == 0
    checks: List[Dict[str, Any]] = [{
        "name": "first_cycle",
        "status": "warn" if starting else "pass",
        "detail": ("no cycle has completed yet" if starting
                   else f"{manager.cycles_completed} cycle(s) completed"),
    }]

    deadline = manager.heartbeat_deadline
    if deadline is None:
        checks.append({"name": "cycle_freshness", "status": "pass",
                       "detail": "no deadline yet"})
    else:
        remaining = deadline - now.timestamp()
        checks.append({
            "name": "cycle_freshness",
            "status": "pass" if remaining > 0 else "fail",
            "detail": (f"deadline in {remaining / 60:.0f}m" if remaining > 0
                       else f"overdue by {-remaining / 60:.0f}m"),
        })

    running = manager.scheduler.running
    checks.append({
        "name": "scheduler",
        "status": "pass" if running or starting else "fail",
        "detail": "running" if running else "not started",
    })

    total = len(manager.sessions)
    logged_in = sum(1 for s in manager.sessions if s.bot_id is not None)
    dead = [s.ref for s in manager.sessions if s.dead_reason]
    if dead:
        login_status, detail = "fail", f"disabled, needs a config fix: {', '.join(dead)}"
    elif logged_in == total:
        login_status, detail = "pass", f"{logged_in}/{total} logged in"
    elif logged_in == 0 and not starting:
        login_status, detail = "fail", f"0/{total} logged in"
    else:
        login_status, detail = "warn", f"{logged_in}/{total} logged in"
    checks.append({"name": "bot_logins", "status": login_status, "detail": detail})

    problems = _problems(manager)
    # A listing outage is never a fail: it must not make an orchestrator restart
    # a container that is otherwise working.
    failing = [f"{p['ref']} {p['step']}" for p in problems
               if p['step'] in PLATFORM_STEPS]
    checks.append({
        "name": "listing_posts",
        "status": "warn" if failing else "pass",
        "detail": "; ".join(failing) if failing else "no failing posts",
    })

    other = [f"{p['ref']} {p['step']}" for p in problems
             if p['step'] in ('guild_count', 'channel_rename', 'slash_commands', 'cycle')]
    checks.append({
        "name": "bot_steps",
        "status": "warn" if other else "pass",
        "detail": "; ".join(other) if other else "no failing steps",
    })

    if manager.heartbeat_enabled:
        ok = manager.heartbeat_ok is not False
        checks.append({"name": "heartbeat_write",
                       "status": "pass" if ok else "warn",
                       "detail": "written" if ok else "last write failed"})
    return checks


def health_payload(manager: 'BotStatsManager',
                   detailed: bool) -> Tuple[Dict[str, Any], int]:
    checks = _checks(manager)
    if any(c["status"] == "fail" for c in checks):
        status, code = "down", 503
    elif manager.cycles_completed == 0:
        status, code = "starting", 200
    elif any(c["status"] == "warn" for c in checks):
        status, code = "degraded", 200
    else:
        status, code = "ok", 200

    payload: Dict[str, Any] = {"status": status}
    if detailed:
        payload["checks"] = checks
        payload["problems"] = _problems(manager)
    return payload, code


def history_payload(manager: 'BotStatsManager', session: 'BotSession',
                    detailed: bool) -> Dict[str, Any]:
    public = ('started_at', 'server_count', 'member_count', 'shard_count',
              'user_install_count')
    runs = [dict(run) if detailed else {k: run[k] for k in public}
            for run in session.stats.history]
    return {
        "id": session.bot_id,
        "name": session.label,
        "retained": len(runs),
        "capacity": manager.history_size,
        "runs": runs,
    }


class StatsAPI:

    def __init__(self, manager: 'BotStatsManager', host: str, port: int,
                 token: str, allow_refresh: bool, detailed: bool):
        self.manager = manager
        self.host = host
        self.port = port
        self.allow_refresh = allow_refresh
        self.detailed = detailed
        self._token = token.encode('utf-8') if token else b''
        self._runner: Optional[web.AppRunner] = None

    @property
    def auth_required(self) -> bool:
        return bool(self._token)

    @property
    def running(self) -> bool:
        return self._runner is not None

    async def start(self) -> None:
        app = web.Application(client_max_size=API_REQUEST_BODY_LIMIT,
                              middlewares=[self._errors, self._auth])
        # Registration order decides resolution: a dynamic route registered first
        # swallows every literal one after it.
        app.router.add_get('/', self._root)
        app.router.add_get('/health', self._health)
        if self.allow_refresh:
            app.router.add_post('/refresh', self._refresh)
        app.router.add_get('/{key}/history', self._history)
        app.router.add_get('/{key}', self._bot)

        runner = web.AppRunner(app, handle_signals=False, access_log=None,
                               shutdown_timeout=API_SHUTDOWN_TIMEOUT)
        try:
            await runner.setup()
            await web.TCPSite(runner, self.host, self.port).start()
        # ValueError covers UnicodeError, which a host with an over-long label
        # or an embedded null raises before any socket call.
        except (OSError, ValueError) as e:
            # setup() already ran app.startup(), so the half-built runner has to go.
            with contextlib.suppress(Exception):
                await runner.cleanup()
            logger.error(LogArea.STATS_API,
                         f"could not bind {self.host}:{self.port} "
                         f"({type(e).__name__}: {e}); continuing without the API")
            return

        self._runner = runner
        logger.info(LogArea.STATS_API,
                    f"Stats API listening on {listen_label(self.host, self.port)} "
                    f"({'token required' if self.auth_required else 'no auth'})")
        if is_loopback(self.host) and in_container():
            # ERROR so it reaches the webhook: the log claims it is listening
            # while every request gets an empty reply.
            logger.error(LogArea.CONFIG,
                         f"api.host={self.host} is this container's own loopback, "
                         f"so the API is unreachable from the host. Set "
                         f'"host": "{CONTAINER_API_HOST}" in config.json (the '
                         f"published port is what restricts access) and reload.")
        elif not is_loopback(self.host) and not self.auth_required:
            logger.warning(LogArea.CONFIG,
                           f"api.host={self.host} is not loopback and api.token is "
                           f"empty; anything that can reach this port can read your "
                           f"bot statistics")

    async def stop(self) -> None:
        runner, self._runner = self._runner, None
        if runner is None:
            return
        try:
            await runner.cleanup()
        except Exception as e:
            logger.warning(LogArea.STATS_API,
                           f"stats API shutdown failed: {type(e).__name__}: {e}")

    @staticmethod
    def _json(request: web.Request, payload: Dict[str, Any], status: int = 200,
              headers: Optional[Dict[str, str]] = None) -> web.Response:
        compact = _wants(request, 'compact')
        body = json.dumps(payload, indent=None if compact else 2,
                          ensure_ascii=False, default=str)
        base = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        base.update(headers or {})
        return web.Response(text=body if compact else body + "\n", status=status,
                            content_type='application/json', charset='utf-8',
                            headers=base)

    @web.middleware
    async def _errors(self, request: web.Request, handler) -> web.StreamResponse:
        try:
            return await handler(request)
        except asyncio.CancelledError:
            raise
        except web.HTTPException as e:
            if e.status == 405:
                return self._json(request, {"error": "method_not_allowed"}, 405,
                                  {"Allow": e.headers.get("Allow", "GET, HEAD")})
            return self._json(
                request,
                {"error": "not_found" if e.status == 404 else "request_failed"},
                e.status)
        except Exception as e:
            # WARNING, not ERROR: the logger forwards ERROR to the alert webhook,
            # so a scrape loop over a broken handler would page you about itself.
            logger.warning(LogArea.STATS_API,
                           f"{request.method} {request.path} failed: "
                           f"{type(e).__name__}: {e}")
            return self._json(request, {"error": "internal_error"}, 500)

    @web.middleware
    async def _auth(self, request: web.Request, handler) -> web.StreamResponse:
        if not self._token:
            return await handler(request)
        scheme, _, value = request.headers.get('Authorization', '').partition(' ')
        # Bytes both sides, surrogateescape included: compare_digest raises on a
        # non-ASCII str and a strict encode raises on a non-UTF-8 header, either
        # of which would 500 instead of denying.
        if scheme.lower() == 'bearer' and hmac.compare_digest(
                value.strip().encode('utf-8', 'surrogateescape'), self._token):
            return await handler(request)
        return self._json(request, {"error": "unauthorized"}, 401,
                          {"WWW-Authenticate": 'Bearer realm="botstatsupdater"'})

    def _lookup(self, request: web.Request) -> 'BotSession':
        """
        The Discord ID, or the stable ref for a bot that has none yet.

        Not names: two bots can share one, and renaming a bot on Discord would
        silently change its address.
        """
        key = request.match_info['key']
        if KEY_PATTERN.match(key):
            for session in self.manager.sessions:
                if session.bot_id == key or session.ref == key:
                    return session
        raise web.HTTPNotFound()

    async def _root(self, request: web.Request) -> web.Response:
        return self._json(request, root_payload(self.manager, self.detailed))

    async def _health(self, request: web.Request) -> web.Response:
        payload, code = health_payload(self.manager, self.detailed)
        if _wants(request, 'brief'):
            payload = {"status": payload["status"]}
        headers = {"Retry-After": str(HEALTH_RETRY_AFTER)} if code == 503 else None
        return self._json(request, payload, code, headers)

    async def _bot(self, request: web.Request) -> web.Response:
        session = self._lookup(request)
        return self._json(request, bot_payload(self.manager, session, self.detailed))

    async def _history(self, request: web.Request) -> web.Response:
        return self._json(request, history_payload(self.manager, self._lookup(request),
                                                   self.detailed))

    async def _refresh(self, request: web.Request) -> web.Response:
        accepted, number = self.manager.request_refresh()
        if not accepted:
            return self._json(request, {"error": "cycle_running"}, 409)
        return self._json(request, {"accepted": True, "cycle_number": number}, 202)
