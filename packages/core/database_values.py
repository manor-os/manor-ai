"""Keep text that PostgreSQL cannot store from failing whole writes.

PostgreSQL text and JSONB reject NUL, and JSONB rejects an unpaired UTF-16
surrogate (``Unicode low surrogate must follow a high surrogate``); asyncpg
cannot even encode one into a text parameter. Such characters reach Python
from browser text cut in the middle of an emoji, or from ``json.loads`` of a
``"\\ud83d"`` escape. One of them in a tool result used to fail the save of an
otherwise good answer.

Every engine built by ``packages.core.database`` serializes JSON through
``dumps_storable_json`` and passes text parameters through
``storable_parameters``: an unpaired surrogate becomes U+FFFD and NUL is
dropped. Valid text, including emoji and CJK, is stored unchanged.
"""
from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import event

_UNSTORABLE_CHAR = re.compile("[\x00\ud800-\udfff]")
# ``json.dumps`` escapes NUL and every surrogate. A paired escape is a valid
# non-BMP character; only NUL and an unpaired half need repair. A literal
# backslash in the data can match too, which only costs a no-op repair pass.
_UNSTORABLE_JSON_ESCAPE = re.compile(
    r"\\u0000"
    r"|\\ud[89ab][0-9a-f]{2}(?!\\ud[c-f][0-9a-f]{2})"
    r"|(?<!\\ud[89ab][0-9a-f]{2})\\ud[c-f][0-9a-f]{2}"
)


def storable_text(value: str) -> str:
    """Drop NUL and replace each unpaired surrogate with U+FFFD."""

    if _UNSTORABLE_CHAR.search(value) is None:
        return value
    value = value.replace("\x00", "")
    # Adjacent high+low halves join into one character; lone halves -> U+FFFD.
    return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def storable_json(value: Any) -> Any:
    """Apply ``storable_text`` to every string, including keys, in JSON data."""

    if isinstance(value, str):
        return storable_text(value)
    if isinstance(value, dict):
        return {
            storable_text(key) if isinstance(key, str) else key: storable_json(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [storable_json(item) for item in value]
    return value


def dumps_storable_json(value: Any) -> str:
    """``json.dumps`` whose output PostgreSQL JSON/JSONB always accepts."""

    text = json.dumps(value)
    if _UNSTORABLE_JSON_ESCAPE.search(text) is None:
        return text
    return json.dumps(storable_json(value))


def storable_parameters(parameters: Any) -> Any:
    """Repair the string bind parameters of one driver-level execute."""

    if isinstance(parameters, str):
        return storable_text(parameters)
    if isinstance(parameters, dict):
        repaired = {key: storable_parameters(item) for key, item in parameters.items()}
        return parameters if all(repaired[key] is parameters[key] for key in parameters) else repaired
    if isinstance(parameters, (list, tuple)):
        repaired = [storable_parameters(item) for item in parameters]
        if all(new is old for new, old in zip(repaired, parameters)):
            return parameters
        return type(parameters)(repaired)
    return parameters


def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    return statement, storable_parameters(parameters)


def install_storable_text(sync_engine) -> None:
    """Repair text parameters on every statement this engine executes."""

    event.listen(sync_engine, "before_cursor_execute", _before_cursor_execute, retval=True)
