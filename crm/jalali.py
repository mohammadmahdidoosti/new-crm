"""Jalali (Persian calendar) parsing and display."""
from __future__ import annotations

import datetime as dt
import re

import jdatetime
from django.utils import timezone

from .models import PERSIAN_DIGITS

WEEKDAYS = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]


def parse_date(value) -> dt.date | None:
    """'1405/8/29', '1405-08-29', '۱۴۰۵/۰۸/۲۹', a datetime (sheet cells holding Gregorian or mis-typed Jalali)."""
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        value = value.date()
    if isinstance(value, dt.date):
        # Google Sheets exports some Jalali dates as fake Gregorian dates like 1404-07-13.
        if value.year < 1500:
            try:
                return jdatetime.date(value.year, value.month, value.day).togregorian()
            except ValueError:
                return None
        return value
    s = str(value).translate(PERSIAN_DIGITS).strip()
    m = re.match(r"(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})", s)
    if not m:
        return None
    y, mo, d = map(int, m.groups())
    try:
        if y < 1500:
            return jdatetime.date(y, mo, d).togregorian()
        return dt.date(y, mo, d)
    except ValueError:
        return None


def parse_datetime(value) -> dt.datetime | None:
    """'1405/9/2 9:0:0' -> aware datetime in Tehran time."""
    d = parse_date(value)
    if not d:
        return None
    s = str(value).translate(PERSIAN_DIGITS)
    m = re.search(r"(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?", s)
    h, mi = (int(m.group(1)), int(m.group(2))) if m else (8, 0)
    naive = dt.datetime(d.year, d.month, d.day, min(h, 23), min(mi, 59))
    return timezone.make_aware(naive, timezone.get_default_timezone())


def fmt_date(d) -> str:
    if not d:
        return "-"
    if isinstance(d, dt.datetime):
        d = timezone.localtime(d).date() if timezone.is_aware(d) else d.date()
    return jdatetime.date.fromgregorian(date=d).strftime("%Y/%m/%d")


def fmt_datetime(d) -> str:
    if not d:
        return "-"
    loc = timezone.localtime(d) if timezone.is_aware(d) else d
    return jdatetime.datetime.fromgregorian(datetime=loc).strftime("%Y/%m/%d %H:%M")


def weekday_name(d: dt.date) -> str:
    return WEEKDAYS[d.weekday()]
