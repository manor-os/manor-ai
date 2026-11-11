"""Turn a human cadence ("每周一上午9点", "weekdays at 8:30am") into a schedule.

Shared by every writer that receives a cadence as words instead of a cron
expression. The parser only reads the cadence text it is given; prose such as
an automation description is never scanned, because it routinely names days
("covers Monday–Sunday") that are not the schedule.

``parse_cadence`` returns ``None`` when the words name no clock cadence. It
never guesses a daily default, so a writer can ask for an explicit schedule
instead of silently creating the wrong one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from packages.core.cron import validate_cron_expression


@dataclass(frozen=True)
class Cadence:
    """Scheduler fields for one clock cadence."""

    schedule_kind: str
    cron_expr: str | None = None
    every_seconds: float | None = None

    @property
    def job_type(self) -> str:
        return "cron" if self.schedule_kind == "cron" else "interval"


_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_NUMBER = r"[零〇一二两三四五六七八九十]+"
_NUMBER = rf"(?:\d{{1,2}}|{_CN_NUMBER})"


def _number(value: str) -> int | None:
    value = value.strip()
    if value.isdigit():
        return int(value)
    if not value or any(ch not in _CN_DIGITS and ch != "十" for ch in value):
        return None
    if "十" not in value:
        total = 0
        for ch in value:
            total = total * 10 + _CN_DIGITS[ch]
        return total
    tens, _, ones = value.partition("十")
    tens_value = _CN_DIGITS.get(tens, 0) if tens else 1
    ones_value = _CN_DIGITS.get(ones, 0) if ones else 0
    return tens_value * 10 + ones_value


_EN_WEEKDAYS = {
    "sunday": 0, "sun": 0,
    "monday": 1, "mon": 1,
    "tuesday": 2, "tue": 2, "tues": 2,
    "wednesday": 3, "wed": 3,
    "thursday": 4, "thu": 4, "thur": 4, "thurs": 4,
    "friday": 5, "fri": 5,
    "saturday": 6, "sat": 6,
}
_CN_WEEKDAYS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 0, "天": 0, "七": 0}
_CN_WEEK_PREFIX = r"(?:周|星期|礼拜|禮拜)"
_CN_WEEKDAY_CHAR = r"[一二三四五六日天七]"
_EN_WEEKDAY = r"(?:" + "|".join(sorted(_EN_WEEKDAYS, key=len, reverse=True)) + r")"
_RANGE_SEP = r"\s*(?:至|到|-|–|—|~|～|through|thru|to)\s*"


def _weekday_range(start: int, end: int) -> list[int]:
    # Cron counts Sunday as 0; a range ending on Sunday wraps to the week end.
    if end == 0 and start > 0:
        end = 7
    days = list(range(start, end + 1)) if start <= end else list(range(start, 7)) + list(range(0, end + 1))
    return [day % 7 for day in days]


def _weekdays(text: str) -> list[int]:
    days: set[int] = set()
    for match in re.finditer(
        rf"{_CN_WEEK_PREFIX}({_CN_WEEKDAY_CHAR}){_RANGE_SEP}{_CN_WEEK_PREFIX}?({_CN_WEEKDAY_CHAR})",
        text,
    ):
        days.update(_weekday_range(_CN_WEEKDAYS[match.group(1)], _CN_WEEKDAYS[match.group(2)]))
    for match in re.finditer(
        rf"\b({_EN_WEEKDAY})\b{_RANGE_SEP}\b({_EN_WEEKDAY})\b", text,
    ):
        days.update(_weekday_range(_EN_WEEKDAYS[match.group(1)], _EN_WEEKDAYS[match.group(2)]))
    # "周一、三、五" names later days without repeating the prefix. A count
    # such as "每周三次" (three times a week) is not a weekday.
    for match in re.finditer(
        rf"{_CN_WEEK_PREFIX}({_CN_WEEKDAY_CHAR}(?:\s*[、,，和及与/]\s*{_CN_WEEKDAY_CHAR})*)(?![次回遍个])",
        text,
    ):
        days.update(_CN_WEEKDAYS[ch] for ch in re.findall(_CN_WEEKDAY_CHAR, match.group(1)))
    for match in re.finditer(rf"\b({_EN_WEEKDAY})s?\b", text):
        days.add(_EN_WEEKDAYS[match.group(1)])
    return sorted(days)


_CN_PERIODS = {
    "凌晨": "early", "早上": "am", "早晨": "am", "清晨": "am", "上午": "am", "早": "am",
    "中午": "noon", "午间": "noon", "下午": "pm", "午后": "pm", "傍晚": "pm",
    "晚上": "pm", "夜里": "pm", "夜间": "pm", "晚": "pm",
}


def _time_of_day(text: str) -> tuple[int, int] | None:
    match = re.search(r"(?<![\d:])(\d{1,2})(?:[:：](\d{2}))?\s*(a\.?m\.?|p\.?m\.?)(?![a-z])", text)
    if match:
        hour = int(match.group(1)) % 12
        if match.group(3).startswith("p"):
            hour += 12
        return hour, min(int(match.group(2) or 0), 59)

    periods = "|".join(sorted(_CN_PERIODS, key=len, reverse=True))
    match = re.search(
        rf"({periods})?\s*({_NUMBER})\s*[点點时時](?:\s*(半|一刻|三刻|{_NUMBER})\s*分?)?",
        text,
    )
    if match:
        hour = _number(match.group(2))
        if hour is not None and hour <= 24:
            period = _CN_PERIODS.get(match.group(1) or "")
            if period == "pm" and hour < 12:
                hour += 12
            elif period == "noon" and hour < 11:
                hour += 12
            elif period == "early" and hour == 12:
                hour = 0
            minute_text = match.group(3) or ""
            minute = {"半": 30, "一刻": 15, "三刻": 45}.get(minute_text)
            if minute is None:
                minute = _number(minute_text) if minute_text else 0
            return hour % 24, min(minute or 0, 59)

    match = re.search(r"(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)(?!\d)", text)
    if match:
        return int(match.group(1)), int(match.group(2))
    if re.search(r"\bnoon\b|正午", text):
        return 12, 0
    if re.search(r"\bmidnight\b|午夜|半夜", text):
        return 0, 0
    if re.search(r"\bafternoon\b", text):
        return 13, 0
    if re.search(r"\b(?:evening|night|nightly)\b", text):
        return 17, 0
    # A part of the day named without an hour ("每周日晚上").
    for pattern, hour in _CN_PERIOD_DEFAULT_HOURS:
        if re.search(pattern, text):
            return hour, 0
    return None


_CN_PERIOD_DEFAULT_HOURS = (
    (r"凌晨", 6),
    (r"早上|早晨|清晨|每早", 8),
    (r"上午", 9),
    (r"中午|午间|午饭", 12),
    (r"下午|午后", 14),
    (r"傍晚", 18),
    (r"晚上|夜里|夜间|每晚|晚间", 20),
)


_INTERVAL_UNITS = (
    (r"minutes?|mins?|m\b|分钟|分", 60),
    (r"hours?|hrs?|h\b|小时|钟头|个小时|個小時", 3600),
    (r"days?|d\b|天|日", 86400),
)


def _interval_seconds(text: str) -> float | None:
    for unit_pattern, seconds in _INTERVAL_UNITS:
        match = re.search(rf"\bevery\s+(\d+)\s*(?:{unit_pattern})", text)
        if match is None:
            match = re.search(rf"每\s*隔?\s*({_NUMBER})\s*(?:个|個)?\s*(?:{unit_pattern})", text)
        if match:
            amount = _number(match.group(1))
            if amount:
                return float(amount * seconds)
    if re.search(r"\bhourly\b|\bevery\s+hour\b|每\s*(?:个|個)?\s*(?:小时|小時|钟头)", text):
        return 3600.0
    return None


def _month_days(text: str) -> list[int]:
    match = re.search(r"(?:每\s*(?:个|個)?\s*月|月度|每月)(.*)", text)
    if match:
        tail = match.group(1)
        days = [
            value for value in (
                _number(m.group(1)) for m in re.finditer(rf"({_NUMBER})\s*[号號日]", tail)
            )
            if value and 1 <= value <= 31
        ]
        if days:
            return sorted(set(days))
        if re.search(r"月初|第一天|首日", text) or not re.search(r"月底|最后一天|月末", text):
            return [1]
        return []
    if re.search(r"月初", text):
        return [1]
    if not re.search(r"\bmonth(?:ly)?\b|\beach month\b|\bevery month\b", text):
        return []
    days = [
        int(m.group(1)) for m in re.finditer(r"\b(\d{1,2})(?:st|nd|rd|th)\b", text)
        if 1 <= int(m.group(1)) <= 31
    ]
    if re.search(r"\bfirst (?:day|business day)\b", text):
        days.append(1)
    if days:
        return sorted(set(days))
    if re.search(r"\blast day\b|\bend of (?:the )?month\b", text):
        return []
    return [1]


def _is_weekday_rule(text: str) -> bool:
    return bool(re.search(
        r"\bweekdays?\b|\bbusiness days?\b|\bwork ?days?\b|\bworking days?\b|工作日|每个工作日",
        text,
    ))


def _is_unsupported_period(text: str) -> bool:
    """Cadences five-field cron cannot express; refusing beats a wrong clock."""
    if re.search(
        rf"\bbi-?weekly\b|\bevery other (?:week|month)\b|\bevery \d+ (?:weeks|months)\b"
        rf"|\bquarterly\b|\bevery quarter\b|\byearly\b|\bannually\b"
        rf"|每\s*隔?\s*{_NUMBER}\s*(?:个|個)?\s*(?:周|週|星期|礼拜|月)|每季度?|每年|隔周",
        text,
    ):
        return True
    # "first Monday of the month" needs calendar arithmetic, not a day list.
    names_month = re.search(r"每\s*(?:个|個)?\s*月|\bmonth(?:ly)?\b|\beach month\b", text)
    return bool(names_month and _weekdays(text))


def parse_cadence(text: Any) -> Cadence | None:
    """Resolve cadence words to scheduler fields, or ``None`` if none are named."""

    raw = " ".join(str(text or "").split())
    if not raw:
        return None
    lowered = raw.lower()
    found_cron = _find_cron_expr(raw)
    if found_cron:
        return Cadence("cron", cron_expr=found_cron)

    interval = _interval_seconds(lowered)
    if interval is not None:
        return Cadence("every", every_seconds=interval)

    if _is_unsupported_period(lowered):
        return None
    time_of_day = _time_of_day(lowered)
    hour, minute = time_of_day or (9, 0)

    if _is_weekday_rule(lowered):
        return Cadence("cron", cron_expr=f"{minute} {hour} * * 1-5")
    if re.search(r"\bweekends?\b|周末|週末", lowered):
        return Cadence("cron", cron_expr=f"{minute} {hour} * * 0,6")
    month_days = _month_days(lowered)
    if month_days:
        days = ",".join(str(day) for day in month_days)
        return Cadence("cron", cron_expr=f"{minute} {hour} {days} * *")
    if re.search(r"月底|最后一天|月末|\blast day\b|\bend of (?:the )?month\b", lowered):
        # The five-field grammar has no "last day"; refuse rather than guess.
        return None
    weekdays = _weekdays(lowered)
    if weekdays:
        return Cadence("cron", cron_expr=f"{minute} {hour} * * {_cron_list(weekdays)}")
    if re.search(r"\bweekly\b|\bevery week\b|\beach week\b|每\s*(?:个|個)?\s*(?:周|星期|礼拜|禮拜)|每週", lowered):
        return Cadence("cron", cron_expr=f"{minute} {hour} * * 1")
    if re.search(
        r"\bdaily\b|\bevery ?day\b|\beach day\b|\bnightly\b|\bevery (?:morning|evening|night)\b"
        r"|每天|每日|天天|每晚|每早",
        lowered,
    ):
        return Cadence("cron", cron_expr=f"{minute} {hour} * * *")
    return None


def _cron_list(values: list[int]) -> str:
    parts: list[str] = []
    start = previous = values[0]
    for value in values[1:] + [None]:
        if value is not None and value == previous + 1:
            previous = value
            continue
        parts.append(str(start) if start == previous else f"{start}-{previous}" if previous - start > 1 else f"{start},{previous}")
        if value is not None:
            start = previous = value
    return ",".join(parts)


def _find_cron_expr(text: str) -> str | None:
    tokens = text.split()
    for start in range(0, max(0, len(tokens) - 4)):
        candidate = " ".join(tokens[start:start + 5])
        if not re.fullmatch(r"[\d*/,\-]+(?: [\d*/,\-]+){4}", candidate):
            continue
        try:
            return validate_cron_expression(candidate)
        except ValueError:
            continue
    return None


_EVENT_TRIGGER = re.compile(
    r"^on_[a-z0-9_]+$|^when_[a-z0-9_]+$|\bwhen(?:ever)?\b|\bupon\b|\bon (?:new|each|every|receiv)"
    r"|\b(?:each|every) time\b|\bas soon as\b|\btriggered by\b|\bon_[a-z]+"
    r"|每当|每次收到|收到.*(?:时|后)|一旦|有新|触发|来信",
)


def looks_like_event_trigger(text: Any) -> bool:
    return bool(_EVENT_TRIGGER.search(" ".join(str(text or "").split()).lower()))


_TIMEZONE_ALIASES = (
    (r"北京时间|北京時間|北京\s*(?=[早上下午晚凌中\d一二三四五六七八九十])|中国时间|中國時間|beijing time|china time|\bbeijing\b|上海时间", "Asia/Shanghai"),
    (r"香港时间|香港時間|hong kong time", "Asia/Hong_Kong"),
    (r"台北时间|台北時間|taipei time", "Asia/Taipei"),
    (r"新加坡时间|singapore time", "Asia/Singapore"),
    (r"东京时间|日本时间|東京時間|tokyo time|japan time", "Asia/Tokyo"),
    (r"伦敦时间|london time|\buk time\b", "Europe/London"),
    (r"太平洋时间|pacific time|\bp[sd]t\b", "America/Los_Angeles"),
    (r"东部时间|eastern time|\be[sd]t\b", "America/New_York"),
    (r"\butc\b|\bgmt\b|协调世界时", "UTC"),
)


def timezone_in_text(text: Any) -> str | None:
    """Return the IANA timezone a cadence names, if any."""

    raw = " ".join(str(text or "").split())
    for match in re.finditer(r"\b([A-Za-z]+(?:/[A-Za-z0-9_+\-]+){1,2})\b", raw):
        try:
            return normalize_timezone(match.group(1))
        except ValueError:
            continue
    lowered = raw.lower()
    for pattern, zone in _TIMEZONE_ALIASES:
        if re.search(pattern, lowered):
            return zone
    return None


def normalize_timezone(value: Any) -> str:
    """Return a valid IANA timezone name or raise ``ValueError``."""

    name = str(value or "").strip()
    if not name:
        raise ValueError("timezone is required")
    if name.upper() in {"UTC", "GMT", "Z"}:
        return "UTC"
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone {name!r}; use an IANA name such as 'Asia/Shanghai'") from exc
    return name


def _trigger_text(automation: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key in ("schedule", "cadence", "trigger"):
        value = automation.get(key)
        if isinstance(value, Mapping):
            value = value.get("cron") or value.get("cron_expr") or value.get("text") or ""
        if value:
            parts.append(str(value))
    return " ".join(parts)


def automation_cadence(automation: Mapping[str, Any]) -> Cadence | None:
    """Resolve a Draft automation's schedule from structured fields or its trigger words."""

    for key in ("cron_expr", "cron"):
        value = automation.get(key)
        if value:
            return Cadence("cron", cron_expr=validate_cron_expression(value))
    trigger = automation.get("trigger")
    if isinstance(trigger, Mapping):
        for key in ("cron", "cron_expr"):
            if trigger.get(key):
                try:
                    return Cadence("cron", cron_expr=validate_cron_expression(trigger[key]))
                except ValueError:
                    break
    every = automation.get("every_seconds")
    if every not in (None, ""):
        seconds = float(every)
        if seconds <= 0:
            raise ValueError("every_seconds must be positive")
        return Cadence("every", every_seconds=seconds)
    return parse_cadence(_trigger_text(automation))


def automation_timezone(automation: Mapping[str, Any], *, default: Any = "UTC") -> str:
    """Explicit timezone, else one named in the trigger, else ``default``."""

    explicit = automation.get("timezone")
    if explicit:
        return normalize_timezone(explicit)
    named = timezone_in_text(_trigger_text(automation))
    if named:
        return named
    try:
        return normalize_timezone(default)
    except ValueError:
        return "UTC"
