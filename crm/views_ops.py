"""Pages added from Serviq's instructions: settings, daily work board, lost customers, penalties, VIP and top specialists."""
from __future__ import annotations

import datetime as dt

from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.forms import modelformset_factory
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import rules as R
from .jalali import fmt_date, parse_date
from .models import (Call, CannedResponse, ChecklistItem, Complaint, Customer, DailyCheck, LostCustomer, Order, Penalty,
                     PenaltyType, RecurringTask, Rules, ServiceType, Specialist, Survey, TaskDone, normalize_phone)


def is_manager(user) -> bool:
    return user.is_superuser or getattr(getattr(user, "profile", None), "role", "") in ("manager", "supervisor")


def manager_required(view):
    @login_required
    def wrapped(request, *a, **kw):
        if not is_manager(request.user):
            return HttpResponseForbidden("این صفحه فقط برای مدیر و سرپرست است.")
        return view(request, *a, **kw)
    wrapped.__name__ = view.__name__
    return wrapped


# ---------------------------------------------------------------- settings

RULE_GROUPS = [
    ("پخش کار و مشتریان", ["churn_inactive_days", "churn_min_orders", "churn_on_unhappy", "first_order_min_score",
                           "temp_order_cancel_hours", "loyal_min_orders", "loyal_window_days", "capacity_shortage_hours"]),
    ("متخصص VIP", ["vip_window", "vip_min_count", "vip_avg_min", "vip_single_min", "vip_min_score"]),
    ("نظرسنجی و شکایت", ["complaint_avg_below", "complaint_item_at_most"]),
    ("پورسانت و نیم‌بها", ["commission_rate_app", "commission_rate_staff", "commission_bonus_from_day", "commission_bonus_factor",
                           "commission_exclude_fixed_8h", "commission_count_customer_cancel"]),
    ("دیالوگ سفارش", ["meal_min_hours", "meal_inclusive"]),
    ("انضباطی", ["late_grace_minutes", "late_step_minutes", "late_step_amount"]),
    ("کیفیت مکالمه و تیکت", ["banned_words", "ticket_sla_minutes"]),
    ("متخصصان برتر ماه", ["top_w_free", "top_w_favorite", "top_w_survey", "top_w_complaint", "top_prizes", "top_growth_prize"]),
]


class RulesForm(forms.ModelForm):
    class Meta:
        model = Rules
        fields = [f for _, fs in RULE_GROUPS for f in fs]
        widgets = {"banned_words": forms.Textarea(attrs={"rows": 4})}


CALL_KIND_CHOICES = [k for k in Call.KINDS if k[0] != "other"]


class ChecklistForm(forms.ModelForm):
    kinds = forms.MultipleChoiceField(label="برای این تماس‌ها", choices=CALL_KIND_CHOICES, required=False,
                                      widget=forms.CheckboxSelectMultiple)

    class Meta:
        model = ChecklistItem
        fields = ["title", "group", "guide", "order", "active"]
        widgets = {"guide": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        if self.instance and self.instance.pk:
            self.fields["kinds"].initial = [k.strip() for k in self.instance.call_kinds.split(",") if k.strip()]

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.call_kinds = ",".join(self.cleaned_data.get("kinds") or [])
        if commit:
            obj.save()
        return obj


class TaskForm(forms.ModelForm):
    days = forms.MultipleChoiceField(label="روزها", choices=RecurringTask.DAYS, required=False, widget=forms.CheckboxSelectMultiple)

    class Meta:
        model = RecurringTask
        fields = ["title", "place", "assignees", "active", "order"]
        widgets = {"assignees": forms.CheckboxSelectMultiple}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["assignees"].queryset = get_user_model().objects.filter(is_active=True)
        self.fields["assignees"].label_from_instance = lambda u: (getattr(getattr(u, "profile", None), "display_name", "")
                                                                  or u.get_full_name() or u.username)
        if self.instance and self.instance.pk:
            self.fields["days"].initial = [d.strip() for d in self.instance.weekdays.split(",") if d.strip()]

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.weekdays = ",".join(self.cleaned_data.get("days") or [])
        if commit:
            obj.save()
            self.save_m2m()
        return obj


TABS = [("rules", "قوانین"), ("dispatch", "ترتیب پخش کار"), ("call", "چک‌لیست مکالمه"), ("survey", "معیارهای نظرسنجی"),
        ("fault", "دلایل شکایت"), ("penalties", "جدول جریمه‌ها"), ("tasks", "کارهای تکراری"), ("canned", "پاسخ‌های آماده"),
        ("services", "خدمات"), ("users", "کارشناس‌ها")]


@manager_required
def settings_page(request):
    tab = request.GET.get("tab", "rules")
    rules = Rules.get()
    ctx = {"tab": tab, "tabs": TABS}
    if tab == "rules":
        form = RulesForm(request.POST or None, instance=rules)
        if request.method == "POST" and form.is_valid():
            form.save()
            messages.success(request, "قوانین ذخیره شد.")
            return redirect(f"{request.path}?tab=rules")
        ctx["groups"] = [(title, [form[f] for f in fs]) for title, fs in RULE_GROUPS]
        ctx["form"] = form
    elif tab == "dispatch":
        labels = Rules.TIER_LABELS
        if request.method == "POST":
            chosen = [request.POST.get(f"t{i}") for i in range(len(labels))]
            if sorted(c for c in chosen if c) != sorted(labels) or len(set(chosen)) != len(labels):
                messages.error(request, "هر اولویت باید دقیقاً یک بار انتخاب شود.")
            else:
                rules.dispatch_tiers = ",".join(chosen)
                rules.save(update_fields=["dispatch_tiers"])
                messages.success(request, "ترتیب پخش کار ذخیره شد.")
                return redirect(f"{request.path}?tab=dispatch")
        ctx["tiers"] = rules.tiers()
        ctx["labels"] = list(labels.items())
    elif tab in ("call", "survey", "fault"):
        kind = {"call": ChecklistItem.CALL, "survey": ChecklistItem.SURVEY, "fault": ChecklistItem.FAULT}[tab]
        if tab == "call":
            FS = modelformset_factory(ChecklistItem, form=ChecklistForm, extra=2)
        else:
            FS = modelformset_factory(ChecklistItem, fields=["title", "guide", "order", "active"], extra=2,
                                      widgets={"guide": forms.Textarea(attrs={"rows": 2})})
        group = request.GET.get("group", "")
        qs = ChecklistItem.objects.filter(kind=kind)
        if group:
            qs = qs.filter(group=group)
        fs = FS(request.POST or None, queryset=qs, prefix="ci")
        if request.method == "POST" and fs.is_valid():
            for obj in fs.save(commit=False):
                obj.kind = kind
                obj.save()
            messages.success(request, "فهرست ذخیره شد.")
            return redirect(request.get_full_path())
        ctx["fs"] = fs
        ctx["groups_list"] = sorted(set(ChecklistItem.objects.filter(kind=ChecklistItem.CALL).values_list("group", flat=True)))
        ctx["group"] = group
    elif tab == "penalties":
        FS = modelformset_factory(PenaltyType, fields=["title", "amount", "unit", "note", "fault_titles", "active", "order"], extra=1)
        fs = FS(request.POST or None, queryset=PenaltyType.objects.all(), prefix="pt")
        if request.method == "POST" and fs.is_valid():
            fs.save()
            messages.success(request, "جدول جریمه‌ها ذخیره شد.")
            return redirect(f"{request.path}?tab=penalties")
        ctx["fs"] = fs
    elif tab == "tasks":
        FS = modelformset_factory(RecurringTask, form=TaskForm, extra=1)
        fs = FS(request.POST or None, queryset=RecurringTask.objects.all(), prefix="rt")
        if request.method == "POST" and fs.is_valid():
            fs.save()
            messages.success(request, "کارهای تکراری ذخیره شد.")
            return redirect(f"{request.path}?tab=tasks")
        ctx["fs"] = fs
    elif tab == "canned":
        FS = modelformset_factory(CannedResponse, fields=["category", "title", "text", "active"], extra=2,
                                  widgets={"text": forms.Textarea(attrs={"rows": 2})})
        fs = FS(request.POST or None, queryset=CannedResponse.objects.all(), prefix="cr")
        if request.method == "POST" and fs.is_valid():
            fs.save()
            messages.success(request, "پاسخ‌های آماده ذخیره شد.")
            return redirect(f"{request.path}?tab=canned")
        ctx["fs"] = fs
    elif tab == "services":
        FS = modelformset_factory(ServiceType, fields=["name", "category", "is_vip", "active"], extra=1)
        fs = FS(request.POST or None, queryset=ServiceType.objects.all(), prefix="sv")
        if request.method == "POST" and fs.is_valid():
            fs.save()
            messages.success(request, "خدمات ذخیره شد.")
            return redirect(f"{request.path}?tab=services")
        ctx["fs"] = fs
    elif tab == "users":
        ctx["users"] = get_user_model().objects.all().select_related("profile")
    return render(request, "crm/settings.html", ctx)


# ---------------------------------------------------------------- daily work board

@login_required
def daily_board(request):
    today = timezone.localdate()
    d = parse_date(request.GET.get("date")) or today
    user = request.user
    if request.method == "POST":
        if "task" in request.POST:
            task = get_object_or_404(RecurringTask, pk=request.POST["task"])
            if request.POST.get("undo"):
                TaskDone.objects.filter(task=task, user=user, date=d).delete()
            else:
                TaskDone.objects.get_or_create(task=task, user=user, date=d)
        elif "check_kind" in request.POST:
            order = get_object_or_404(Order, pk=request.POST["order"])
            kind = request.POST["check_kind"]
            result = request.POST.get("result", "")
            if result in dict(DailyCheck.RESULTS[kind]):
                late = int(request.POST.get("late") or 0)
                DailyCheck.objects.update_or_create(kind=kind, order=order, date=d, defaults={
                    "result": result, "late_minutes": late, "note": request.POST.get("note", "")[:300],
                    "specialist_ok": request.POST.get("specialist_ok", ""), "user": user})
                if kind == "morning" and late:
                    order.late_minutes = late
                    order.save(update_fields=["late_minutes"])
                if kind == "periodic" and request.POST.get("specialist_ok") == "no":
                    Complaint.objects.get_or_create(order=order, customer=order.customer, specialist=order.specialist,
                                                    status="open", source="customer", defaults={
                                                        "reason_text": "عدم رضایت از متخصص ثابت (سفارش دوره‌ای)",
                                                        "description": request.POST.get("note", ""), "agent": user})
        return redirect(f"{request.path}?date={fmt_date(d)}")

    tasks = [t for t in RecurringTask.objects.filter(active=True).prefetch_related("assignees") if t.due_on(d)]
    mine = [t for t in tasks if not t.assignees.exists() or user in t.assignees.all()]
    done_ids = set(TaskDone.objects.filter(user=user, date=d).values_list("task_id", flat=True))
    morning = Order.objects.filter(start__date=d, status__in=[Order.ASSIGNED]).select_related("specialist", "customer").order_by("start")
    tomorrow = d + dt.timedelta(days=1)
    periodic = Order.objects.filter(start__date=tomorrow, is_periodic=True).exclude(status=Order.CANCELLED) \
        .select_related("specialist", "customer").order_by("start")
    checks = {(c.kind, c.order_id): c for c in DailyCheck.objects.filter(date=d)}
    yesterday = d - dt.timedelta(days=1)
    to_survey = Order.objects.filter(status=Order.DONE, start__date=yesterday, surveys__isnull=True).select_related("customer", "specialist")
    no_answer = Survey.objects.filter(result="no_answer", call_date__gte=d - dt.timedelta(days=3), call_date__lt=d) \
        .select_related("customer", "specialist")
    for o in morning:
        o.check = checks.get(("morning", o.pk))
    for o in periodic:
        o.check = checks.get(("periodic", o.pk))
    ctx = {"d": d, "today": d == today, "tasks": mine, "done_ids": done_ids, "morning": morning, "periodic": periodic,
           "to_survey": to_survey, "no_answer": no_answer, "temp_due": R.temp_orders_due(),
           "morning_results": DailyCheck.RESULTS["morning"], "periodic_results": DailyCheck.RESULTS["periodic"],
           "prev": fmt_date(d - dt.timedelta(days=1)), "next": fmt_date(d + dt.timedelta(days=1)),
           "is_thursday": d.weekday() == 3}
    if ctx["is_thursday"]:
        ctx["thu"] = R.thursday_groups(R.week_start(d))
    return render(request, "crm/daily.html", ctx)


# ---------------------------------------------------------------- lost customers

class LostForm(forms.ModelForm):
    class Meta:
        model = LostCustomer
        fields = ["date", "name", "phone", "source", "service", "gender_needed", "reason", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2}), "date": forms.HiddenInput}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["date"].required = False


@login_required
def lost_list(request):
    initial = {"date": timezone.localdate()}
    call = Call.objects.filter(pk=request.GET.get("call") or 0).first()
    if call:
        ex = (call.analysis or {}).get("extracted") or {}
        initial.update({"name": ex.get("customer_name") or (call.customer.name if call.customer else ""),
                        "phone": call.customer.phone if call.customer else "", "service": ex.get("service", ""),
                        "reason": "conditions" if ex.get("conditions_rejected") else
                        {"استعلام": "inquiry", "هزینه بالا": "price", "عدم ظرفیت": "capacity", "عدم تطابق نیرو": "mismatch",
                         "رد سفارش": "rejected"}.get(ex.get("reason_not_ordered"), "")})
    form = LostForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.agent = request.user
        obj.date = obj.date or timezone.localdate()
        obj.customer = Customer.objects.filter(phone=normalize_phone(obj.phone)).first() if obj.phone else None
        obj.call = call
        obj.save()
        messages.success(request, "ثبت شد.")
        return redirect("lost_list")
    start = R.month_start(timezone.localdate())
    qs = LostCustomer.objects.select_related("agent")
    reason = request.GET.get("reason")
    if reason:
        qs = qs.filter(reason=reason)
    from django.db.models import Count
    by_reason = LostCustomer.objects.filter(date__gte=start).values("reason").annotate(n=Count("id")).order_by("-n")
    labels = dict(LostCustomer.REASONS)
    return render(request, "crm/lost.html", {
        "form": form, "page": Paginator(qs, 30).get_page(request.GET.get("page")), "reasons": LostCustomer.REASONS,
        "by_reason": [(labels.get(r["reason"], r["reason"]), r["n"]) for r in by_reason],
        "coverage": R.coverage_rate(start, timezone.localdate())})


# ---------------------------------------------------------------- penalties

@login_required
def penalty_list(request):
    if request.method == "POST":
        if not is_manager(request.user):
            return HttpResponseForbidden("تأیید جریمه با سرپرست است.")
        p = get_object_or_404(Penalty, pk=request.POST.get("id"))
        p.status = request.POST.get("status") if request.POST.get("status") in dict(Penalty.STATUSES) else p.status
        amt = request.POST.get("amount", "").replace(",", "")
        p.amount = int(amt) if amt.isdigit() else p.amount
        p.note = request.POST.get("note", p.note)
        p.decided_by = request.user
        p.save()
        messages.success(request, "جریمه به‌روز شد.")
        return redirect(request.get_full_path())
    st = request.GET.get("status", "proposed")
    qs = Penalty.objects.select_related("specialist", "ptype", "complaint")
    if st:
        qs = qs.filter(status=st)
    return render(request, "crm/penalties.html", {"page": Paginator(qs, 30).get_page(request.GET.get("page")), "status": st,
                                                  "statuses": Penalty.STATUSES})


@login_required
def complaint_new(request):
    """A complaint from a customer by phone, or an operations agent's own complaint about a specialist."""
    sps = Specialist.objects.filter(active=True)
    if request.method == "POST":
        sp = Specialist.objects.filter(pk=request.POST.get("specialist") or 0).first()
        reason = ChecklistItem.objects.filter(pk=request.POST.get("reason") or 0, kind=ChecklistItem.FAULT).first()
        code = (request.POST.get("order_code") or "").strip()
        order = Order.objects.filter(tracking_code=code).first() if code else None
        c = Complaint.objects.create(
            specialist=sp or (order.specialist if order else None), order=order, customer=order.customer if order else None,
            reason=reason, reason_text=reason.title if reason else request.POST.get("reason_text", "")[:200],
            description=request.POST.get("description", ""), source=request.POST.get("source", "staff"),
            status="open", agent=request.user, refer_retention=bool(request.POST.get("refer_retention")))
        messages.success(request, "شکایت ثبت شد.")
        return redirect("complaint_detail", c.pk)
    return render(request, "crm/complaint_new.html", {"specialists": sps, "reasons": ChecklistItem.of(ChecklistItem.FAULT)})


# ---------------------------------------------------------------- VIP & top specialists

@login_required
def report_vip(request):
    r = Rules.get()
    return render(request, "crm/report_vip.html", {"rows": R.vip_review(r), "r": r})


@login_required
def report_top(request):
    d = parse_date(request.GET.get("month")) or timezone.localdate()
    start = R.month_start(d)
    prev = R.month_start(start - dt.timedelta(days=1))
    return render(request, "crm/report_top.html", {
        "rows": R.top_specialists(start)[:30], "start": start, "prev": fmt_date(prev), "next": fmt_date(R.next_month(start)),
        "r": Rules.get()})


@login_required
@require_POST
def vip_apply(request, pk):
    if not is_manager(request.user):
        return HttpResponseForbidden("فقط سرپرست.")
    sp = get_object_or_404(Specialist, pk=pk)
    sp.worker_type = request.POST.get("worker_type") if request.POST.get("worker_type") in dict(Specialist.TYPES) else sp.worker_type
    sp.save(update_fields=["worker_type", "updated_at"])
    messages.success(request, f"نوع {sp} به «{sp.get_worker_type_display()}» تغییر کرد. در CRM قدیم هم اعمال کنید.")
    return redirect("report_vip")
