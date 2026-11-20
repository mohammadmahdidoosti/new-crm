"""Business rules: complaints from surveys, dispatch priority & suggestions, weekly half-price commission, capacity.

All thresholds come from the editable `Rules` row, so they can be tuned without code changes.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (Availability, ChecklistItem, Complaint, FollowUpTask, Order, Rules, Specialist, Survey)


# ---------------------------------------------------------------- surveys -> complaints

def complaint_needed(survey: Survey, rules: Rules | None = None) -> bool:
    rules = rules or Rules.get()
    survey.compute_average()
    if survey.result == "no_answer":
        return False
    vals = []
    for v in (survey.scores or {}).values():
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            pass
    if not vals:
        return survey.result == "unhappy"
    if survey.average is not None and Decimal(str(survey.average)) < rules.complaint_avg_below:
        return True
    return min(vals) <= rules.complaint_item_at_most or survey.result == "unhappy"


def weakest_criteria(survey: Survey) -> list[str]:
    out = []
    for k, v in (survey.scores or {}).items():
        try:
            if float(v) < 5:
                out.append((float(v), k))
        except (TypeError, ValueError):
            pass
    return [k for _, k in sorted(out)]


@transaction.atomic
def process_survey(survey: Survey, user=None, fault_titles: list[str] | None = None) -> Complaint | None:
    """Called when a survey is confirmed. Creates the complaint and the call-back task when the rules say so."""
    survey.compute_average()
    survey.save(update_fields=["average"])
    if hasattr(survey, "complaint") and survey.complaint:
        return survey.complaint
    if not complaint_needed(survey):
        return None
    reason = None
    for title in fault_titles or []:
        reason = ChecklistItem.objects.filter(kind=ChecklistItem.FAULT, title=title).first()
        if reason:
            break
    weak = weakest_criteria(survey)
    c = Complaint.objects.create(
        survey=survey, order=survey.order, customer=survey.customer, specialist=survey.specialist, reason=reason,
        reason_text=(reason.title if reason else ("، ".join(weak[:2]) if weak else "نارضایتی در نظرسنجی")),
        score=survey.average, description=survey.notes, status="recall", agent=user, auto_created=True)
    FollowUpTask.objects.create(
        title=f"تماس مجدد با مشتری ناراضی: {survey.customer or ''}",
        details=f"میانگین نظرسنجی {survey.average}. ضعیف‌ترین معیارها: {'، '.join(weak) or '-'}",
        due=timezone.now() + dt.timedelta(hours=24), assigned_to=user, customer=survey.customer,
        specialist=survey.specialist, complaint=c, order=survey.order)
    return c


# ---------------------------------------------------------------- dispatch

def _overlaps(sp: Specialist, start, hours) -> bool:
    end = start + dt.timedelta(hours=float(hours))
    for o in sp.orders.filter(status__in=[Order.ASSIGNED], start__date=start.date()):
        o_end = o.start + dt.timedelta(hours=float(o.hours))
        if o.start < end and start < o_end:
            return True
    return False


def suggest_specialists(order: Order, limit: int = 6) -> list[dict]:
    """Ranked candidates with the reasons, for a human to choose from (semi-automatic dispatch)."""
    rules = Rules.get()
    qs = Specialist.objects.filter(active=True)
    if order.gender_needed:
        qs = qs.filter(gender=order.gender_needed)
    cat = order.service.category
    if cat == "home":
        qs = qs.filter(does_home=True)
    elif cat == "common":
        qs = qs.filter(does_common=True)
    elif cat == "work":
        qs = qs.filter(does_work=True)
    if order.has_pets:
        qs = qs.filter(ok_with_pets=True)
    blocked = set(order.customer.blocked_specialists.values_list("id", flat=True))
    favorites = set(order.customer.favorite_specialists.values_list("id", flat=True))
    day = timezone.localtime(order.start).date()
    avail = {a.specialist_id: a for a in Availability.objects.filter(date=day)}
    since = timezone.now() - dt.timedelta(days=90)
    out = []
    for sp in qs:
        if sp.id in blocked:
            continue
        a = avail.get(sp.id)
        if a and a.state in ("leave", "nonworking", "suspended"):
            continue
        if order.service.is_vip and not (sp.is_vip or (sp.score and sp.score >= rules.vip_min_score)):
            continue
        if _overlaps(sp, order.start, order.hours):
            continue
        score, why = 0.0, []
        if sp.id in favorites:
            score += 50
            why.append("نیروی مورد علاقه مشتری")
        good_before = sp.orders.filter(customer=order.customer, status=Order.DONE).exclude(
            complaints__isnull=False).count()
        if good_before:
            score += min(good_before, 4) * 5
            why.append(f"{good_before} کار موفق برای همین مشتری")
        if sp.score:
            score += float(sp.score) * 8
            why.append(f"امتیاز {sp.score}")
        if a and a.free_hours >= float(order.hours):
            score += 10
            why.append(a.get_state_display())
        if order.district and sp.work_area and order.district in sp.work_area:
            score += 15
            why.append("محدوده کاری نزدیک")
        recent = sp.complaints.filter(created_at__gte=since).count()
        if recent:
            score -= recent * 6
            why.append(f"{recent} شکایت در ۹۰ روز اخیر")
        out.append({"specialist": sp, "score": round(score, 1), "why": why})
    out.sort(key=lambda d: -d["score"])
    return out[:limit]


def dispatch_queue(days_ahead: int = 2):
    now = timezone.now()
    orders = list(Order.objects.filter(status=Order.REGISTERED, start__gte=now - dt.timedelta(hours=6),
                                       start__lte=now + dt.timedelta(days=days_ahead))
                  .select_related("customer", "service"))
    orders.sort(key=lambda o: (-o.priority, o.start))
    return orders


# ---------------------------------------------------------------- weekly commission

def week_start(d: dt.date) -> dt.date:
    """Iranian week: Saturday .. Friday."""
    return d - dt.timedelta(days=(d.weekday() - 5) % 7)


def weekly_commission(start: dt.date) -> list[dict]:
    """For each specialist: distinct working days of the week, and which orders fall on bonus days (6th, 7th...)."""
    rules = Rules.get()
    end = start + dt.timedelta(days=7)
    orders = (Order.objects.filter(status=Order.DONE, specialist__isnull=False, start__date__gte=start, start__date__lt=end)
              .select_related("specialist", "customer", "service").order_by("start"))
    by_sp = defaultdict(list)
    for o in orders:
        by_sp[o.specialist].append(o)
    rows = []
    for sp, items in by_sp.items():
        days = sorted({timezone.localtime(o.start).date() for o in items})
        bonus_days = days[rules.commission_bonus_from_day - 1:] if len(days) >= rules.commission_bonus_from_day else []
        bonus_orders = [o for o in items if timezone.localtime(o.start).date() in bonus_days]
        saving = 0
        for o in bonus_orders:
            rate = rules.commission_rate_staff if o.closed_by_staff else rules.commission_rate_app
            if o.price:
                full = Decimal(o.price) * rate / 100
                saving += int(full * (1 - rules.commission_bonus_factor))
        rows.append({"specialist": sp, "days": days, "day_count": len(days), "bonus_days": bonus_days,
                     "bonus_orders": bonus_orders, "eligible": bool(bonus_days), "saving_rial": saving,
                     "needs": max(0, rules.commission_bonus_from_day - len(days))})
    rows.sort(key=lambda r: (-r["day_count"], r["specialist"].name))
    return rows


# ---------------------------------------------------------------- capacity

def capacity(day: dt.date) -> dict:
    rules = Rules.get()
    res = {"date": day, "groups": [], "shortage": False}
    av = Availability.objects.filter(date=day).select_related("specialist")
    orders = Order.objects.filter(start__date=day).exclude(status=Order.CANCELLED).select_related("service")
    for g, label in (("m", "آقا"), ("f", "خانم")):
        free = sum(a.free_hours for a in av if a.specialist.gender == g)
        ready = sum(1 for a in av if a.specialist.gender == g and a.free_hours)
        vip_free = sum(a.free_hours for a in av if a.specialist.gender == g and a.specialist.is_vip)
        booked = float(sum(o.hours for o in orders if o.specialist and o.specialist.gender == g))
        pending = float(sum(o.hours for o in orders if not o.specialist and o.gender_needed in (g, "")))
        remaining = free - booked
        short = remaining - pending < rules.capacity_shortage_hours
        res["groups"].append({"gender": label, "ready": ready, "free_hours": free, "vip_free_hours": vip_free,
                              "booked_hours": booked, "pending_hours": pending, "remaining": remaining, "short": short})
        res["shortage"] |= short
    res["unassigned"] = orders.filter(status=Order.REGISTERED).count()
    res["has_data"] = av.exists()
    if not res["has_data"]:
        res["shortage"] = False
    return res
