"""Natural-language, one-shot snooze deadlines.

The grammar is deliberately small and deterministic rather than delegating to
an opaque service.  It parses a deadline only at the start of the input; the
unconsumed text is a human reminder and is never sent to the resumed agent.
"""

import calendar
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta


SNOOZE_MESSAGE_MAX = 240
_DAY = 24 * 60 * 60
_MAX_SNOOZE_SECONDS = 10 * 366 * _DAY

_MONTHS = {
    name: number
    for number, names in enumerate((
        (), ("jan", "january"), ("feb", "february"),
        ("mar", "march"), ("apr", "april"), ("may",),
        ("jun", "june"), ("jul", "july"), ("aug", "august"),
        ("sep", "sept", "september"), ("oct", "october"),
        ("nov", "november"), ("dec", "december"),
    ))
    for name in names
}
_MONTH_WORD = "(?:" + "|".join(
    sorted(_MONTHS, key=len, reverse=True)) + ")"

_WEEKDAYS = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}
_WEEKDAY_WORD = "(?:" + "|".join(
    sorted(_WEEKDAYS, key=len, reverse=True)) + ")"

_PARTS_OF_DAY = {
    "morning": (9, 0),
    "noon": (12, 0),
    "afternoon": (12, 0),
    "evening": (19, 0),
    "night": (22, 0),
    "midnight": (0, 0),
}

_RELATIVE_PREFIX_RE = re.compile(r"^(?:(?:in)\s+|\+\s*)", re.I)
_RELATIVE_TERM_RE = re.compile(
    r"(?P<amount>\d+(?:\.\d+)?|an?)\s*"
    r"(?P<unit>minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|wks?|w|"
    r"months?|mos?|years?|yrs?|y)(?![a-z])",
    re.I,
)
_ISO_DATE_RE = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})(?!\d)", re.I)
_DASHED_NAMED_RE = re.compile(
    r"^(?P<day>\d{1,2})(?:st|nd|rd|th)?-(?P<month>%s)"
    r"(?:-(?P<year>\d{4}))?(?![a-z0-9])" % _MONTH_WORD, re.I)
_MONTH_FIRST_RE = re.compile(
    r"^(?P<month>%s)\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:\s*,?\s*(?P<year>\d{4}))?(?![a-z0-9])" % _MONTH_WORD, re.I)
_DAY_FIRST_RE = re.compile(
    r"^(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+(?P<month>%s)\.?"
    r"(?:\s*,?\s*(?P<year>\d{4}))?(?![a-z0-9])" % _MONTH_WORD, re.I)
_DAY_WORD_RE = re.compile(
    r"^(?P<day>day\s+after\s+tomorrow|tomorrow|tmr|tom|today|tod|tonight)"
    r"(?![a-z])", re.I)
_WEEKDAY_RE = re.compile(
    r"^(?P<next>next\s+)?(?P<weekday>%s)(?![a-z])" % _WEEKDAY_WORD, re.I)
_SPECIAL_DATE_RE = re.compile(
    r"^(?P<special>next\s+week|next\s+month|next\s+year|"
    r"this\s+weekend|next\s+weekend|end\s+of\s+(?:the\s+)?month|eom)"
    r"(?![a-z])", re.I)
_PART_OF_DAY_RE = re.compile(
    r"^(?P<part>morning|noon|afternoon|evening|night|midnight)(?![a-z])", re.I)
_RECURRENCE_RE = re.compile(
    r"^(?:every\b|daily\b|weekly\b|monthly\b|yearly\b|weekdays?\b)", re.I)

_AMPM_TIME_RE = re.compile(
    r"^(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*"
    r"(?P<ampm>[ap])(?:\.?\s*m\.?)?(?![a-z0-9])", re.I)
_COLON_TIME_RE = re.compile(
    r"^(?P<hour>\d{1,2}):(?P<minute>\d{2})(?![a-z0-9])", re.I)
_MILITARY_TIME_RE = re.compile(
    r"^(?P<hour>[01]\d|2[0-3])(?P<minute>[0-5]\d)(?![a-z0-9])", re.I)
_BARE_HOUR_RE = re.compile(r"^(?P<hour>\d{1,2})(?![a-z0-9:])", re.I)
_TIMEISH_RE = re.compile(r"^\d{1,4}(?::\S*)?(?:\s*[ap](?:\.?m\.?)?)?", re.I)


@dataclass(frozen=True)
class ParsedSnooze:
    wake_at: float
    reminder: str
    expression: str


def _clean_reminder(text):
    text = "".join(ch for ch in text
                   if ord(ch) >= 32 and not 0x7f <= ord(ch) <= 0x9f)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(?:[-—:]+\s*)", "", text)
    if len(text) > SNOOZE_MESSAGE_MAX:
        raise ValueError(
            "reminder is limited to %d characters" % SNOOZE_MESSAGE_MAX)
    return text


def _clock(text, allow_bare_hour=False, allow_military=False):
    """Return ``(hour, minute, consumed)`` for a clock at text[0:]."""
    part = _PART_OF_DAY_RE.match(text)
    if part:
        hour, minute = _PARTS_OF_DAY[part.group("part").lower()]
        return hour, minute, part.end()

    match = _AMPM_TIME_RE.match(text)
    if match:
        hour = int(match.group("hour"))
        minute = int(match.group("minute") or 0)
        if not 1 <= hour <= 12:
            raise ValueError("12-hour times use an hour from 1 to 12")
        if not 0 <= minute <= 59:
            raise ValueError("minutes must be between 00 and 59")
        if match.group("ampm").lower() == "a":
            hour = 0 if hour == 12 else hour
        else:
            hour = 12 if hour == 12 else hour + 12
        return hour, minute, match.end()

    match = _COLON_TIME_RE.match(text)
    if match:
        hour, minute = int(match.group("hour")), int(match.group("minute"))
        if not 0 <= hour <= 23:
            raise ValueError("24-hour times use an hour from 00 to 23")
        if not 0 <= minute <= 59:
            raise ValueError("minutes must be between 00 and 59")
        return hour, minute, match.end()

    if allow_military:
        match = _MILITARY_TIME_RE.match(text)
        if match:
            return int(match.group("hour")), int(match.group("minute")), match.end()

    if allow_bare_hour:
        match = _BARE_HOUR_RE.match(text)
        if match:
            hour = int(match.group("hour"))
            if not 0 <= hour <= 23:
                raise ValueError("hour must be between 0 and 23")
            return hour, 0, match.end()
    return None


def _optional_clock(raw, end, default=(9, 0)):
    """Parse a clock following a date expression, if present."""
    tail = raw[end:]
    marker = re.match(r"^(?:\s*,?\s*)(?:(?:at)\s+|[@T]\s*)", tail, re.I)
    if marker:
        start = end + marker.end()
        clock = _clock(raw[start:], allow_bare_hour=True, allow_military=True)
        if not clock:
            raise ValueError("add a time after 'at', such as 3pm or 15:00")
        hour, minute, consumed = clock
        return hour, minute, start + consumed

    whitespace = re.match(r"^\s*,?\s+", tail)
    if whitespace:
        start = end + whitespace.end()
        clock = _clock(raw[start:])
        if clock:
            hour, minute, consumed = clock
            return hour, minute, start + consumed
        # A number immediately after a date almost always represents a mistyped
        # time.  Do not silently turn it into the reminder.
        timeish = _TIMEISH_RE.match(raw[start:])
        if timeish:
            shown = timeish.group(0).strip()
            raise ValueError(
                "couldn't understand time %r; try 3pm, 3:30pm, or 15:00" % shown)
    return default[0], default[1], end


def _target(year, month, day, hour, minute, now_dt, explicit_year=True):
    try:
        result = datetime(year, month, day, hour, minute)
    except ValueError as exc:
        raise ValueError("invalid snooze date: %s" % exc)
    if result.timestamp() <= now_dt.timestamp():
        if explicit_year:
            raise ValueError("that date and time has already passed")
        try:
            result = result.replace(year=result.year + 1)
        except ValueError as exc:
            raise ValueError("invalid snooze date: %s" % exc)
    return result


def _replace_clock(value, hour, minute):
    return value.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _add_months(value, months):
    index = value.year * 12 + (value.month - 1) + months
    year, month0 = divmod(index, 12)
    day = min(value.day, calendar.monthrange(year, month0 + 1)[1])
    return value.replace(year=year, month=month0 + 1, day=day)


def _relative(raw, now_dt):
    prefix = _RELATIVE_PREFIX_RE.match(raw)
    pos = prefix.end() if prefix else 0
    match = _RELATIVE_TERM_RE.match(raw, pos)
    if not match:
        return None
    months = 0
    seconds = 0.0
    end = pos
    while match:
        amount_text = match.group("amount").lower()
        amount = 1.0 if amount_text in ("a", "an") else float(amount_text)
        unit = match.group("unit").lower()
        if amount <= 0:
            raise ValueError("snooze duration must be greater than zero")
        if unit.startswith(("mo", "y")):
            if not amount.is_integer():
                raise ValueError("month and year durations must be whole numbers")
            months += int(amount) * (12 if unit.startswith("y") else 1)
        elif unit.startswith("m"):
            seconds += amount * 60
        elif unit.startswith("h"):
            seconds += amount * 3600
        elif unit.startswith("d"):
            seconds += amount * _DAY
        else:
            seconds += amount * 7 * _DAY
        end = match.end()

        # Accept "1h30m", "1h 30m", and "1 hour and 30 minutes". Only
        # consume the separator if another valid duration term follows it.
        separator = re.match(r"(?:\s+and\s+|\s+)?", raw[end:], re.I)
        next_pos = end + separator.end()
        match = _RELATIVE_TERM_RE.match(raw, next_pos)
        if match:
            pos = next_pos

    if months > 120:
        raise ValueError("snooze duration is more than 10 years")
    target = _add_months(now_dt, months) if months else now_dt
    if seconds:
        target = datetime.fromtimestamp(target.timestamp() + seconds)
    if target.timestamp() - now_dt.timestamp() > _MAX_SNOOZE_SECONDS:
        raise ValueError("snooze duration is more than 10 years")

    # Natural forms can add an exact clock: "in 3 days at 3pm".  Requiring
    # "at" avoids consuming a numeric reminder after a compact duration.
    suffix = raw[end:]
    at = re.match(r"^\s+at\s+", suffix, re.I)
    if at:
        start = end + at.end()
        clock = _clock(raw[start:], allow_bare_hour=True, allow_military=True)
        if not clock:
            raise ValueError("add a time after 'at', such as 3pm or 15:00")
        hour, minute, consumed = clock
        target = _replace_clock(target, hour, minute)
        end = start + consumed
        if target.timestamp() <= now_dt.timestamp():
            raise ValueError("that date and time has already passed")

    return target, end


def _named_date(raw, now_dt):
    for pattern in (_ISO_DATE_RE, _DASHED_NAMED_RE,
                    _MONTH_FIRST_RE, _DAY_FIRST_RE):
        match = pattern.match(raw)
        if not match:
            continue
        values = match.groupdict()
        month_value = values["month"]
        month = (int(month_value) if month_value.isdigit()
                 else _MONTHS[month_value.lower()])
        day = int(values["day"])
        explicit_year = values.get("year") is not None
        year = int(values["year"]) if explicit_year else now_dt.year
        hour, minute, end = _optional_clock(raw, match.end())
        return _target(year, month, day, hour, minute, now_dt, explicit_year), end
    return None


def _day_word(raw, now_dt):
    match = _DAY_WORD_RE.match(raw)
    if not match:
        return None
    word = re.sub(r"\s+", " ", match.group("day").lower())
    if word == "tonight":
        hour, minute, end = _optional_clock(raw, match.end(), default=(22, 0))
        target = _replace_clock(now_dt, hour, minute)
        if target.timestamp() <= now_dt.timestamp():
            target += timedelta(days=1)
        return target, end
    days = 0 if word in ("today", "tod") else (2 if word.startswith("day after") else 1)
    hour, minute, end = _optional_clock(raw, match.end())
    target = _replace_clock(now_dt + timedelta(days=days), hour, minute)
    if target.timestamp() <= now_dt.timestamp():
        raise ValueError("that time today has passed; include a future time")
    return target, end


def _weekday(raw, now_dt):
    match = _WEEKDAY_RE.match(raw)
    if not match:
        return None
    weekday = _WEEKDAYS[match.group("weekday").lower()]
    hour, minute, end = _optional_clock(raw, match.end())
    days = (weekday - now_dt.weekday()) % 7
    target = _replace_clock(now_dt + timedelta(days=days), hour, minute)
    if target.timestamp() <= now_dt.timestamp():
        target += timedelta(days=7)
    if match.group("next"):
        target += timedelta(days=7)
    return target, end


def _special_date(raw, now_dt):
    match = _SPECIAL_DATE_RE.match(raw)
    if not match:
        return None
    special = re.sub(r"\s+", " ", match.group("special").lower())
    base = now_dt
    if special == "next week":
        days = 7 - now_dt.weekday()
        base = now_dt + timedelta(days=days)
    elif special == "next month":
        base = _add_months(now_dt, 1)
    elif special == "next year":
        base = now_dt.replace(year=now_dt.year + 1, month=1, day=1)
    elif special in ("end of month", "end of the month", "eom"):
        last = calendar.monthrange(now_dt.year, now_dt.month)[1]
        base = now_dt.replace(day=last)
    else:
        days = (5 - now_dt.weekday()) % 7
        candidate = _replace_clock(now_dt + timedelta(days=days), 9, 0)
        if candidate.timestamp() <= now_dt.timestamp():
            days += 7
        if special == "next weekend":
            days += 7
        base = now_dt + timedelta(days=days)
    hour, minute, end = _optional_clock(raw, match.end())
    target = _replace_clock(base, hour, minute)
    if target.timestamp() <= now_dt.timestamp():
        raise ValueError("that date and time has already passed")
    return target, end


def _time_only(raw, now_dt):
    marker = re.match(r"^at\s+", raw, re.I)
    start = marker.end() if marker else 0
    clock = _clock(raw[start:], allow_bare_hour=bool(marker),
                   allow_military=bool(marker))
    if not clock:
        return None
    hour, minute, consumed = clock
    target = _replace_clock(now_dt, hour, minute)
    if target.timestamp() <= now_dt.timestamp():
        target += timedelta(days=1)
    return target, start + consumed


def parse_snooze(text, now=None):
    """Parse ``<when> [reminder]`` and return a :class:`ParsedSnooze`.

    A bare clock means its next occurrence: ``3pm`` means today when 15:00 is
    still ahead and tomorrow otherwise.  Dates without a clock use 09:00 local.
    """
    raw = str(text or "").strip()
    if not raw:
        raise ValueError(
            "enter a duration/date, e.g. 3h, 3pm, tom 3p, or sep 4 3p")
    if _RECURRENCE_RE.match(raw):
        raise ValueError(
            "recurring snoozes aren't supported; enter one future date or time")

    now_ts = time.time() if now is None else float(now)
    now_dt = datetime.fromtimestamp(now_ts)
    parsed = (_relative(raw, now_dt) or _named_date(raw, now_dt)
              or _special_date(raw, now_dt) or _day_word(raw, now_dt)
              or _weekday(raw, now_dt) or _time_only(raw, now_dt))
    if not parsed:
        raise ValueError(
            "couldn't understand the duration/date; try 3h, 3pm, tom 3p, "
            "or sep 4 3p")
    target, end = parsed
    reminder = _clean_reminder(raw[end:])
    return ParsedSnooze(target.timestamp(), reminder, raw[:end].strip())


def parse_snooze_spec(text, now=None):
    """Compatibility tuple API used by the daemon and existing callers."""
    parsed = parse_snooze(text, now=now)
    return parsed.wake_at, parsed.reminder


def format_wake_at(wake_at, now=None):
    """Compact, locale-independent label for a local snooze timestamp."""
    target = datetime.fromtimestamp(float(wake_at))
    current = datetime.fromtimestamp(time.time() if now is None else float(now))
    delta = (target.date() - current.date()).days
    if delta == 0:
        date_label = "today"
    elif delta == 1:
        date_label = "tomorrow"
    else:
        date_label = "%s %s %d" % (
            target.strftime("%a"), target.strftime("%b"), target.day)
        if target.year != current.year:
            date_label += ", %d" % target.year
    hour = target.hour % 12 or 12
    clock = "%d:%02d %s" % (hour, target.minute,
                             "AM" if target.hour < 12 else "PM")
    return "%s at %s" % (date_label, clock)


def snooze_feedback(text, now=None):
    """Return ``(header, valid)`` for the popup's live editor feedback."""
    if not str(text or "").strip():
        return (" Snooze — try 3h, 3pm, tom 3p, or sep 4 3p", False)
    try:
        parsed = parse_snooze(text, now=now)
    except (TypeError, ValueError, OverflowError) as exc:
        return (" Snooze — %s" % exc, False)
    label = " Snooze → %s" % format_wake_at(parsed.wake_at, now=now)
    if parsed.reminder:
        label += " · reminder: %s" % parsed.reminder
    return label, True
