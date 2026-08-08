from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from contextvars import ContextVar, Token
from typing import Any, TextIO

LOGGER_NAME = "dane_meteo_stacje"
_HANDLER_NAME = "dane-meteo-stacje-json"
_LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}
_logger = logging.getLogger(LOGGER_NAME)
_logger.addHandler(logging.NullHandler())
_logger.propagate = False
_request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)


def bind_request_id(request_id: str) -> Token[str | None]:
    return _request_id_context.set(request_id)


def reset_request_id(token: Token[str | None]) -> None:
    _request_id_context.reset(token)


def configure_logging(level: str = "INFO", *, stream: TextIO | None = None) -> None:
    normalized_level = level.upper()
    numeric_level = _LOG_LEVELS.get(normalized_level)
    if numeric_level is None:
        raise ValueError(f"Unsupported log level: {level}")

    for handler in list(_logger.handlers):
        if handler.get_name() == _HANDLER_NAME:
            _logger.removeHandler(handler)

    handler = logging.StreamHandler(stream)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(handler)
    _logger.setLevel(numeric_level)


def log_event(
    event: str,
    *,
    level: int = logging.INFO,
    request_id: str | None = None,
    fields: Mapping[str, Any] | None = None,
    **extra_fields: Any,
) -> None:
    payload: dict[str, Any] = {"event": event}
    effective_request_id = request_id or _request_id_context.get()
    if effective_request_id is not None:
        payload["request_id"] = effective_request_id
    if fields:
        payload.update(fields)
    payload.update(extra_fields)
    _logger.log(level, json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
