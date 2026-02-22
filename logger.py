from __future__ import annotations
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class LogLevel(str, Enum):
    DEBUG    = 'DEBUG'
    INFO     = 'INFO'
    WARNING  = 'WARNING'
    ERROR    = 'ERROR'
    CRITICAL = 'CRITICAL'
    NONE     = 'NONE'


class LogArea(str, Enum):
    BOT       = 'BOT'
    CONFIG    = 'CONFIG'
    STARTUP   = 'STARTUP'
    SHUTDOWN  = 'SHUTDOWN'
    API       = 'API'
    SCHEDULER = 'SCHEDULER'
    NONE      = 'NONE'


_LEVEL_ORDER = [
    LogLevel.DEBUG,
    LogLevel.INFO,
    LogLevel.WARNING,
    LogLevel.ERROR,
    LogLevel.CRITICAL,
    LogLevel.NONE,
]

_COLORS = {
    LogLevel.DEBUG:    '\x1b[90m',
    LogLevel.INFO:     '\x1b[92m',
    LogLevel.WARNING:  '\x1b[93m',
    LogLevel.ERROR:    '\x1b[91m',
    LogLevel.CRITICAL: '\x1b[95m',
    LogLevel.NONE:     '\x1b[37m',
}
_RESET  = '\x1b[0m'
_CYAN   = '\x1b[36m'


class BotLogger:
    _instance: Optional[BotLogger] = None

    def __init__(self) -> None:
        self._console_enabled: bool = True
        self._min_level: LogLevel = LogLevel.INFO

    @classmethod
    def get_instance(cls) -> BotLogger:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def configure(self, console_enabled: Optional[bool] = None,
                  min_level: Optional[LogLevel] = None) -> None:
        if console_enabled is not None:
            self._console_enabled = console_enabled
        if min_level is not None:
            self._min_level = min_level

    def _should_log(self, level: LogLevel) -> bool:
        try:
            return _LEVEL_ORDER.index(level) >= _LEVEL_ORDER.index(self._min_level)
        except ValueError:
            return True

    def _format_message(self, level: LogLevel, area: LogArea, message: str) -> str:
        now = datetime.now(timezone.utc).strftime('%H:%M:%S')
        color = _COLORS.get(level, _RESET)
        padded_level = level.value.ljust(8)
        padded_area  = area.value.ljust(10)
        return (
            f"{color}[{now}] [{padded_level}] [{padded_area}] {message}{_RESET}"
        )

    def log(self, level: LogLevel, area: LogArea, message: str) -> None:
        if not self._console_enabled or not self._should_log(level):
            return
        print(self._format_message(level, area, message), flush=True)

    def debug(self, area: LogArea, message: str) -> None:
        self.log(LogLevel.DEBUG, area, message)

    def info(self, area: LogArea, message: str) -> None:
        self.log(LogLevel.INFO, area, message)

    def warning(self, area: LogArea, message: str) -> None:
        self.log(LogLevel.WARNING, area, message)

    def error(self, area: LogArea, message: str) -> None:
        self.log(LogLevel.ERROR, area, message)

    def critical(self, area: LogArea, message: str) -> None:
        self.log(LogLevel.CRITICAL, area, message)

    def print(self, area: LogArea, message: str) -> None:
        self.log(LogLevel.NONE, area, message)

    def spacer(self, char: str = '=', length: Optional[int] = None,
               color: Optional[str] = None) -> None:
        if not self._console_enabled:
            return
        width  = length if length is not None else 54
        line   = char * width
        c      = color if color is not None else _CYAN
        print(f"{c}{line}{_RESET}", flush=True)


logger = BotLogger.get_instance()
