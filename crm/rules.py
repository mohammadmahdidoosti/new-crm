"""Business rules, following Serviq's written instructions. Every threshold lives in the editable `Rules` row.

- پخش کار: «گزارش شاخص‌های اولویت‌بندی پخش سفارشات»
- نیم‌بها: «دستورالعمل محاسبه پورسانت نیم‌بها» و «فرآیند بررسی کارکرد هفتگی»
- VIP: «دیالوگ متخصصین VIP» (۱۰۰ سفارش آخر، میانگین بالای ۴٫۷۵، هیچ امتیاز زیر ۴)
- محدودیت ثبت سفارش، شکایات، جریمه‌ها، متخصصان برتر ماه، نرخ پوشش سفارشات
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (Availability, Call, ChecklistItem, Complaint, FollowUpTask, LostCustomer, Order, Penalty, PenaltyType,
                     Rules, Specialist, Survey)


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


# ---------------------------------------------------------------- customers

def customer_status(customer, rules: Rules | None = None) -> dict:
    """First-timer / churned / unhappy last time, using both this system and the old CRM's history."""
    rules = rules or Rules.get()
    done = customer.orders.filter(status=Order.DONE)
    n = done.count() + customer.legacy_order_count
    last = done.order_by("-start").values_list("start", flat=True).first()
    last_date = timezone.localtime(last).date() if last else None
    if customer.legacy_last_order and (not last_date or customer.legacy_last_order > last_date):
        last_date = customer.legacy_last_order
    inactive = (timezone.localdate() - last_date).days if last_date else None
    churned = bool(n >= rules.churn_min_orders and inactive is not None and inactive >= rules.churn_inactive_days)
    unhappy = False
    if rules.churn_on_unhappy:
        s = customer.surveys.filter(confirmed=True).order_by("-call_date", "-id").first()
        unhappy = bool(s and (s.result == "unhappy" or (s.average is not None and s.average < rules.complaint_avg_below)))
    return {"orders": n, "last": last_date, "inactive_days": inactive, "churned": churned, "unhappy_last": unhappy}


def order_restrictions(customer, specialist=None, call: Call | None = None) -> list[str]:
    """«دستورالعمل مشتریان دارای محدودیت ثبت سفارش» — reasons an order must not be registered."""
    if customer is None:
        return ["مشتری مشخص نیست"]
    out = []
    if customer.blocked:
        out.append("مشتری بلاک است" + (f" ({customer.block_reason})" if customer.block_reason else ""))
    if customer.debt:
        out.append(f"مشتری {customer.debt:,} تومان بدهی تسویه‌نشده دارد")
    if specialist and customer.blocked_by_specialists.filter(pk=specialist.pk).exists():
        out.append(f"{specialist} این مشتری را بلاک کرده؛ فقط با نیروی دیگر قابل ثبت است")
    if customer.risky:
        out.append("اعزام نیرو برای این مشتری طبق سوابق مشکل‌ساز است")
    missing = [label for ok, label in ((customer.phone, "موبایل"), (customer.address, "آدرس"), (customer.source, "نحوه آشنایی"))
               if not ok]
    if missing:
        out.append("اطلاعات ناقص: " + "، ".join(missing))
    if call:
        rejected = ((call.analysis or {}).get("extracted") or {}).get("conditions_rejected") or []
        if rejected:
            out.append("مشتری این موارد هفت‌گانه را نپذیرفته: " + "، ".join(map(str, rejected)))
    return out


# ---------------------------------------------------------------- specialists: VIP eligibility

def vip_status(sp: Specialist, rules: Rules | None = None) -> dict:
    rules = rules or Rules.get()
    avgs = [float(a) for a in Survey.objects.filter(specialist=sp, confirmed=True, average__isnull=False)
            .order_by("-call_date", "-id").values_list("average", flat=True)[:rules.vip_window]]
    n = len(avgs)
    avg = round(sum(avgs) / n, 2) if n else None
    low = min(avgs) if avgs else None
    enough = n >= rules.vip_min_count
    eligible = bool(enough and avg >= float(rules.vip_avg_min) and low >= float(rules.vip_single_min))
    return {"n": n, "avg": avg, "min": low, "enough": enough, "eligible": eligible,
            "promote": eligible and not sp.is_vip, "demote": enough and sp.is_vip and not eligible}


def vip_review(rules: Rules | None = None) -> list[dict]:
    rules = rules or Rules.get()
    rows = []
    for sp in Specialist.objects.filter(active=True):
        st = vip_status(sp, rules)
        if st["promote"] or st["demote"]:
            rows.append({"specialist": sp, **st})
    rows.sort(key=lambda r: (not r["promote"], -(r["avg"] or 0)))
    return rows


# ---------------------------------------------------------------- dispatch

BASE_TIER = {("home", True): "home_vip", ("work", True): "work_vip", ("common", True): "common_vip",
             ("home", False): "home", ("work", False): "work", ("common", False): "common"}


def order_tier(order: Order, rules: Rules | None = None) -> str:
    """The order's highest tier in the configured order (default: منزل VIP، سفارش اول، چرن‌شده، ...)."""
    rules = rules or Rules.get()
    cands = [BASE_TIER.get((order.service.category, order.service.is_vip), "other")]
    if order.is_first_order:
        cands.append("first_order")
    st = customer_status(order.customer, rules)
    if st["churned"] or st["unhappy_last"]:
        cands.append("churned")
    tiers = rules.tiers()
    return min(cands, key=tiers.index)


def dispatch_queue(days_ahead: int = 2):
    rules = Rules.get()
    now = timezone.now()
    orders = list(Order.objects.filter(status=Order.REGISTERED, start__gte=now - dt.timedelta(hours=6),
                                       start__lte=now + dt.timedelta(days=days_ahead))
                  .select_related("customer", "service"))
    tiers = rules.tiers()
    for o in orders:
        o.tier = order_tier(o, rules)
        o.tier_label = Rules.TIER_LABELS[o.tier]
        o.tier_rank = tiers.index(o.tier) + 1
        o.must_cancel = (o.start - now) < dt.timedelta(hours=rules.temp_order_cancel_hours)
        o.flags = order_flags(o)
    orders.sort(key=lambda o: (o.tier_rank, o.start))
    return orders


def temp_orders_due(rules: Rules | None = None):
    """Orders still without a specialist inside the cancel window (سفارش موقت ← لغو و اطلاع به مشتری)."""
    rules = rules or Rules.get()
    now = timezone.now()
    return Order.objects.filter(status=Order.REGISTERED, specialist__isnull=True, start__gte=now,
                                start__lte=now + dt.timedelta(hours=rules.temp_order_cancel_hours)).select_related("customer")


def _overlaps(sp: Specialist, start, hours) -> bool:
    end = start + dt.timedelta(hours=float(hours))
    for o in sp.orders.filter(status__in=[Order.ASSIGNED], start__date=start.date()):
        o_end = o.start + dt.timedelta(hours=float(o.hours))
        if o.start < end and start < o_end:
            return True
    return False


SPECIAL_NOTES = {"سالمند": "سالمند در منزل", "کودک": "کودک در منزل", "بچه": "کودک در منزل", "بازسازی": "نظافت بعد از بازسازی",
                 "حساسیت": "حساسیت به مواد شوینده", "حیوان": "حیوان خانگی", "سگ": "حیوان خانگی", "گربه": "حیوان خانگی"}


def order_flags(order: Order) -> list[str]:
    text = f"{order.notes} {order.customer.notes}"
    return sorted({label for k, label in SPECIAL_NOTES.items() if k in text} | ({"حیوان خانگی"} if order.has_pets else set()))


def suggest_specialists(order: Order, limit: int = 6) -> list[dict]:
    """Quality criteria first (gender, service, VIP, first order, preferences, blocks, history), distance last."""
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
    if "حیوان خانگی" in order_flags(order):
        qs = qs.filter(ok_with_pets=True)
    blocked = set(order.customer.blocked_specialists.values_list("id", flat=True)) | \
        set(order.customer.blocked_by_specialists.values_list("id", flat=True))
    favorites = set(order.customer.favorite_specialists.values_list("id", flat=True))
    day = timezone.localtime(order.start).date()
    avail = {a.specialist_id: a for a in Availability.objects.filter(date=day)}
    since = timezone.now() - dt.timedelta(days=90)
    first = order.is_first_order
    week = week_start(day)
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
            score += 60
            why.append("نیروی مورد علاقه مشتری")
        good_before = sp.orders.filter(customer=order.customer, status=Order.DONE).exclude(complaints__isnull=False).count()
        if good_before:
            score += min(good_before, 4) * 5
            why.append(f"{good_before} کار موفق برای همین مشتری")
        if sp.score:
            score += float(sp.score) * 8
            why.append(f"امتیاز {sp.score}")
        if first:
            if sp.score and sp.score >= rules.first_order_min_score:
                score += 15
                why.append("باکیفیت برای سفارش اول")
            else:
                score -= 15
        if a and a.free_hours >= float(order.hours):
            score += 10
            why.append(a.get_state_display())
        recent = sp.complaints.filter(created_at__gte=since).count()
        if recent:
            score -= recent * 6
            why.append(f"{recent} شکایت در ۹۰ روز اخیر")
        # distance comes last: same neighbourhood, or the specialist's other order that day is nearby
        if order.district and sp.work_area and order.district in sp.work_area:
            score += 6
            why.append("محدوده کاری نزدیک")
        if order.district and sp.orders.filter(start__date=day, district=order.district,
                                               status__in=[Order.ASSIGNED, Order.DONE]).exclude(pk=order.pk).exists():
            score += 8
            why.append("سفارش دیگرش امروز در همین محدوده است")
        out.append({"specialist": sp, "score": round(score, 1), "why": why, "half_price": half_price_on(sp, day, week)})
    out.sort(key=lambda d: -d["score"])
    return out[:limit]


# ---------------------------------------------------------------- weekly half-price commission

def week_start(d: dt.date) -> dt.date:
    """Iranian week: Saturday .. Friday."""
    return d - dt.timedelta(days=(d.weekday() - 5) % 7)


def _worked_days(sp: Specialist, start: dt.date, end: dt.date, rules: Rules) -> list[dt.date]:
    """A day counts if an order was done, or (by default) cancelled by the customer after assignment.
    Orders not done because of the specialist's own fault never count."""
    q = Q(status=Order.DONE)
    if rules.commission_count_customer_cancel:
        q |= Q(status=Order.CANCELLED, cancelled_after_assign=True)
    days = sp.orders.filter(q, worker_fault=False, start__date__gte=start, start__date__lt=end).values_list("start", flat=True)
    return sorted({timezone.localtime(x).date() for x in days})


def half_price_on(sp: Specialist, day: dt.date, week: dt.date | None = None) -> bool:
    """Would an order on `day` be on the specialist's 6th/7th working day of the week?"""
    rules = Rules.get()
    week = week or week_start(day)
    return len(_worked_days(sp, week, day, rules)) + 1 >= rules.commission_bonus_from_day


def weekly_commission(start: dt.date) -> list[dict]:
    rules = Rules.get()
    end = start + dt.timedelta(days=7)
    rows = []
    for sp in Specialist.objects.filter(orders__start__date__gte=start, orders__start__date__lt=end).distinct():
        days = _worked_days(sp, start, end, rules)
        if not days:
            continue
        bonus_days = days[rules.commission_bonus_from_day - 1:]
        bonus_orders, excluded = [], 0
        for o in sp.orders.filter(status=Order.DONE, start__date__gte=start, start__date__lt=end).select_related("service"):
            if timezone.localtime(o.start).date() in bonus_days:
                if rules.commission_exclude_fixed_8h and o.is_fixed_8h:
                    excluded += 1
                else:
                    bonus_orders.append(o)
        saving = 0
        for o in bonus_orders:
            rate = rules.commission_rate_staff if o.closed_by_staff else rules.commission_rate_app
            if o.price:
                saving += int(Decimal(o.price) * rate / 100 * (1 - rules.commission_bonus_factor))
        rows.append({"specialist": sp, "days": days, "day_count": len(days), "bonus_days": bonus_days,
                     "bonus_orders": bonus_orders, "excluded_fixed": excluded, "eligible": bool(bonus_days),
                     "saving_rial": saving, "needs": max(0, rules.commission_bonus_from_day - len(days))})
    rows.sort(key=lambda r: (-r["day_count"], r["specialist"].name))
    return rows


def thursday_groups(week: dt.date) -> dict:
    """Thursday-morning list (فرآیند ۱۷): who gets half price on Thursday and/or Friday, from Saturday–Wednesday work."""
    rules = Rules.get()
    thursday = week + dt.timedelta(days=5)
    need = rules.commission_bonus_from_day - 1
    groups = {"both": [], "friday": [], "if_thursday": []}
    for sp in Specialist.objects.filter(active=True):
        n = len(_worked_days(sp, week, thursday, rules))
        if n >= need:
            groups["both"].append((sp, n))
        elif n == need - 1:
            has_thu = sp.orders.filter(start__date=thursday, status__in=[Order.ASSIGNED, Order.DONE]).exists()
            groups["friday" if has_thu else "if_thursday"].append((sp, n))
    return groups


# ---------------------------------------------------------------- penalties

def propose_penalties(complaint: Complaint) -> list[Penalty]:
    """After a fault is confirmed, propose the matching penalty from the table (a supervisor approves it)."""
    if not complaint.specialist or complaint.penalties.exists():
        return []
    rules = Rules.get()
    title = complaint.reason.title if complaint.reason else complaint.reason_text
    out = []
    for pt in PenaltyType.objects.filter(active=True):
        if title and title in [t.strip() for t in pt.fault_titles.split(",") if t.strip()]:
            amount = pt.amount
            if pt.unit == "late" and complaint.order and complaint.order.late_minutes:
                amount = rules.late_penalty(complaint.order.late_minutes)
            elif pt.unit == "order" and complaint.order and complaint.order.price:
                amount = int(complaint.order.price / 10)  # rial -> toman
            out.append(Penalty.objects.create(specialist=complaint.specialist, ptype=pt, complaint=complaint,
                                              order=complaint.order, amount=amount,
                                              note=f"پیشنهاد خودکار برای شکایت «{title}»"))
    return out


# ---------------------------------------------------------------- KPIs

def coverage_rate(start: dt.date, end: dt.date) -> dict:
    """«نرخ پوشش سفارشات» = پایان‌کارشده ÷ (پایان‌کارشده + مشتریان از دست رفته)."""
    done = Order.objects.filter(status=Order.DONE, start__date__gte=start, start__date__lte=end).count()
    lost = LostCustomer.objects.filter(date__gte=start, date__lte=end).count()
    return {"done": done, "lost": lost, "rate": round(100 * done / (done + lost), 1) if (done + lost) else None}


def month_start(d: dt.date) -> dt.date:
    """First day of the Jalali month containing d."""
    import jdatetime
    j = jdatetime.date.fromgregorian(date=d)
    return jdatetime.date(j.year, j.month, 1).togregorian()


def next_month(d: dt.date) -> dt.date:
    import jdatetime
    j = jdatetime.date.fromgregorian(date=d)
    y, m = (j.year + 1, 1) if j.month == 12 else (j.year, j.month + 1)
    return jdatetime.date(y, m, 1).togregorian()


def top_specialists(start: dt.date) -> list[dict]:
    """«معیارهای انتخاب متخصص برتر ماه»: free jobs, favourite jobs, survey scores, confirmed complaints; plus growth."""
    rules = Rules.get()
    end = next_month(start)
    prev = month_start(start - dt.timedelta(days=1))

    def score_for(s, e):
        res = {}
        for sp in Specialist.objects.filter(active=True):
            done = sp.orders.filter(status=Order.DONE, start__date__gte=s, start__date__lt=e)
            fav = done.filter(requested_specific=True).count()
            free = done.count() - fav
            avgs = [float(a) for a in sp.surveys.filter(confirmed=True, call_date__gte=s, call_date__lt=e,
                                                         average__isnull=False).values_list("average", flat=True)]
            avg = sum(avgs) / len(avgs) if avgs else None
            comps = sp.complaints.filter(created_at__date__gte=s, created_at__date__lt=e, fault="confirmed").count()
            if not (free or fav or avgs):
                continue
            total = (float(rules.top_w_free) * free + float(rules.top_w_favorite) * fav
                     + (float(rules.top_w_survey) * (avg - 4) * 10 if avg else 0) - float(rules.top_w_complaint) * comps)
            res[sp] = {"free": free, "fav": fav, "avg": round(avg, 2) if avg else None, "n_surveys": len(avgs),
                       "complaints": comps, "score": round(total, 1)}
        return res

    now, before = score_for(start, end), score_for(prev, start)
    prizes = [int(x) for x in rules.top_prizes.split(",") if x.strip().isdigit()]
    rows = sorted(({"specialist": sp, **v, "growth": round(v["score"] - before.get(sp, {}).get("score", 0), 1)}
                   for sp, v in now.items()), key=lambda r: -r["score"])
    for i, r in enumerate(rows):
        r["rank"] = i + 1
        r["prize"] = prizes[i] if i < len(prizes) else 0
    if rows:
        max(rows, key=lambda r: r["growth"])["growth_prize"] = rules.top_growth_prize
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
