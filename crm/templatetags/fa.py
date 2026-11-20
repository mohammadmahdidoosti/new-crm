from django import template

from crm.jalali import fmt_date, fmt_datetime

register = template.Library()
FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


@register.filter
def jdate(v):
    return fmt_date(v)


@register.filter
def jdt(v):
    return fmt_datetime(v)


@register.filter
def fanum(v):
    return "" if v is None else str(v).translate(FA)


@register.filter
def money(v):
    try:
        return f"{int(v):,}".translate(FA)
    except (TypeError, ValueError):
        return "-"


@register.filter
def get(d, k):
    try:
        return d.get(k)
    except AttributeError:
        return None


@register.filter
def stars(v):
    try:
        return f"{float(v):.1f}".translate(FA)
    except (TypeError, ValueError):
        return "-"
