from __future__ import annotations

import datetime as dt
import hmac
from collections import defaultdict

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Avg, Count, Q
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import analysis, rules
from .forms import CallUploadForm, ImportForm, NewSurveyForm, OrderForm, TicketForm, survey_form_class
from .importers import import_workbook
from .views_ops import is_manager
from .jalali import fmt_date, parse_date
from .models import (Call, CannedResponse, ChecklistItem, Complaint, Customer, FollowUpTask, Order, Penalty, Rules, Specialist,
                     SpecialistComplaint, Survey, Ticket, TicketMessage, normalize_phone)


def _find_specialist(q: str):
    q = (q or "").strip()
    if not q:
        return None
    p = normalize_phone(q)
    if len(p) >= 10:
        return Specialist.objects.filter(phone=p).first()
    return Specialist.objects.filter(name__icontains=q).first()


def _find_customer(phone, name=""):
    p = normalize_phone(phone)
    if not p:
        return None
    c = Customer.objects.filter(phone=p).first()
    if not c and name:
        c = Customer.objects.create(phone=p, name=name)
    return c


def _page(request, qs, n=30):
    return Paginator(qs, n).get_page(request.GET.get("page"))


# ---------------------------------------------------------------- dashboard

@login_required
def dashboard(request):
    today = timezone.localdate()
    ctx = {
        "unassigned": Order.objects.filter(status=Order.REGISTERED, start__date__gte=today).count(),
        "today_orders": Order.objects.filter(start__date=today).exclude(status=Order.CANCELLED).count(),
        "calls_to_review": Call.objects.filter(status="done").count(),
        "calls_failed": Call.objects.filter(status="failed").count(),
        "calls_queue": Call.objects.filter(status__in=["queued", "transcribing", "analyzing"]).count(),
        "draft_surveys": Survey.objects.filter(confirmed=False).count(),
        "open_complaints": Complaint.objects.exclude(status="resolved").count(),
        "open_pro_complaints": SpecialistComplaint.objects.exclude(status="resolved").count(),
        "open_tickets": Ticket.objects.exclude(status="closed").count(),
        "overdue_tickets": sum(1 for t in Ticket.objects.filter(first_response_at__isnull=True).exclude(status="closed") if t.is_overdue),
        "my_tasks": FollowUpTask.objects.filter(done=False).filter(Q(assigned_to=request.user) | Q(assigned_to__isnull=True))[:8],
        "capacity_today": rules.capacity(today),
        "temp_due": rules.temp_orders_due().count(),
        "proposed_penalties": Penalty.objects.filter(status="proposed").count(),
        "coverage": rules.coverage_rate(rules.month_start(today), today),
        "capacity_tomorrow": rules.capacity(today + dt.timedelta(days=1)),
    }
    return render(request, "crm/dashboard.html", ctx)


# ---------------------------------------------------------------- calls

@login_required
def call_list(request):
    qs = Call.objects.select_related("agent", "customer", "specialist")
    kind, status, mine = request.GET.get("kind"), request.GET.get("status"), request.GET.get("mine")
    if kind:
        qs = qs.filter(kind=kind)
    if status:
        qs = qs.filter(status=status)
    if mine:
        qs = qs.filter(agent=request.user)
    return render(request, "crm/call_list.html", {"page": _page(request, qs), "kinds": Call.KINDS, "statuses": Call.STATUSES})


@login_required
def call_upload(request):
    form = CallUploadForm(request.POST or None, request.FILES or None, initial=request.GET.dict())
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        customer = _find_customer(d["customer_phone"])
        specialist = _find_specialist(d["specialist_phone"])
        order = Order.objects.filter(tracking_code=d["order_code"].strip()).first() if d["order_code"] else None
        complaint = None
        if request.GET.get("complaint"):
            complaint = Complaint.objects.filter(pk=request.GET["complaint"]).first()
            if complaint:
                customer = customer or complaint.customer
                specialist = specialist or complaint.specialist
                order = order or complaint.order
        if order:
            customer = customer or order.customer
            specialist = specialist or order.specialist
        made = []
        for f in d["files"]:
            made.append(Call.objects.create(kind=d["kind"], agent=request.user, customer=customer, specialist=specialist,
                                            order=order, related_complaint=complaint, audio=f, note=d["note"]))
        messages.success(request, f"{len(made)} فایل در صف تبدیل به متن و تحلیل قرار گرفت.")
        return redirect("call_detail", made[0].pk) if len(made) == 1 else redirect("call_list")
    return render(request, "crm/call_upload.html", {"form": form})


@login_required
def call_detail(request, pk):
    call = get_object_or_404(Call.objects.select_related("agent", "customer", "specialist", "order"), pk=pk)
    data = call.analysis or {}
    survey = call.surveys.first()
    survey_form = None
    if survey and not survey.confirmed:
        SF = survey_form_class()
        initial = {"result": survey.result, "notes": survey.notes}
        for c in ChecklistItem.of(ChecklistItem.SURVEY):
            initial[f"c_{c.pk}"] = (survey.scores or {}).get(c.title)
        faults = (data.get("extracted") or {}).get("specialist_faults") or []
        f = ChecklistItem.objects.filter(kind=ChecklistItem.FAULT, title__in=faults).first()
        if f:
            initial["fault"] = f.pk
        survey_form = SF(initial=initial)
    groups = {c.title: (c.group or "عمومی") for c in ChecklistItem.of(ChecklistItem.CALL)}
    credit = {True: 1.0, "partial": 0.5, False: 0.0}
    checklist_groups = {}
    for it in data.get("checklist") or []:
        if isinstance(it, dict):
            g = checklist_groups.setdefault(it.get("group") or groups.get(it.get("item"), "سایر"), {"items": [], "got": 0.0, "n": 0})
            g["items"].append(it)
            if it.get("done") is not None:
                g["n"] += 1
                g["got"] += credit.get(it.get("done"), 0.0)
    for g in checklist_groups.values():
        g["score"] = round(100 * g["got"] / g["n"]) if g["n"] else None
    return render(request, "crm/call_detail.html", {
        "call": call, "a": data, "checklist_groups": checklist_groups, "ex": data.get("extracted") or {}, "survey": survey, "survey_form": survey_form,
        "pro_complaint": call.specialist_complaints.first(), "specialists": Specialist.objects.filter(active=True),
        "pro_reasons": ChecklistItem.of(ChecklistItem.PRO_COMPLAINT),
        "rejected": (data.get("extracted") or {}).get("conditions_rejected") or [],
        "not_registered": call.kind in ("order", "new_customer") and (data.get("extracted") or {}).get("order_registered") is False
        or bool((data.get("extracted") or {}).get("order_placed") is False),
        "raw": analysis.dumps(data) if data else ""})


@login_required
@require_POST
def call_action(request, pk):
    call = get_object_or_404(Call, pk=pk)
    action = request.POST.get("action")
    if action == "retry":
        call.status, call.attempts, call.error = "queued", 0, ""
        call.save(update_fields=["status", "attempts", "error"])
        messages.info(request, "دوباره در صف قرار گرفت.")
    elif action == "link":
        call.customer = _find_customer(request.POST.get("customer_phone"), request.POST.get("customer_name", "")) or call.customer
        sp = Specialist.objects.filter(pk=request.POST.get("specialist") or 0).first()
        call.specialist = sp or call.specialist
        code = (request.POST.get("order_code") or "").strip()
        call.order = Order.objects.filter(tracking_code=code).first() if code else call.order
        call.save()
        call.surveys.filter(confirmed=False).update(customer=call.customer, specialist=call.specialist, order=call.order)
        call.specialist_complaints.filter(confirmed=False).update(specialist=call.specialist, customer=call.customer, order=call.order)
        messages.success(request, "اطلاعات مکالمه به‌روز شد.")
    elif action == "review":
        call.status = "reviewed"
        call.reviewed_by = request.user
        call.reviewed_at = timezone.now()
        call.review_note = request.POST.get("review_note", "")
        call.save()
        messages.success(request, "بررسی ثبت شد.")
    elif action == "confirm_survey":
        survey = get_object_or_404(Survey, call=call, confirmed=False)
        form = survey_form_class()(request.POST)
        if form.is_valid():
            d = form.cleaned_data
            scores = {}
            for c in ChecklistItem.of(ChecklistItem.SURVEY):
                v = d.get(f"c_{c.pk}")
                if v:
                    scores[c.title] = v
            survey.scores, survey.result, survey.notes = scores, d["result"], d["notes"]
            survey.customer, survey.specialist, survey.order = call.customer, call.specialist, call.order
            survey.agent = call.agent or request.user
            survey.confirmed = True
            survey.save()
            comp = rules.process_survey(survey, user=request.user, fault_titles=[d["fault"].title] if d.get("fault") else None)
            messages.success(request, "نظرسنجی ثبت شد." + (" نمره پایین بود؛ شکایت و کار «تماس مجدد» ساخته شد." if comp else ""))
    elif action == "confirm_pro_complaint":
        pc = get_object_or_404(SpecialistComplaint, call=call, confirmed=False)
        pc.description = request.POST.get("description", pc.description)
        reason = ChecklistItem.objects.filter(pk=request.POST.get("reason") or 0).first()
        if reason:
            pc.reason, pc.reason_text = reason, reason.title
        pc.specialist, pc.customer, pc.order = call.specialist, call.customer, call.order
        pc.confirmed = True
        pc.save()
        messages.success(request, "شکایت متخصص ثبت شد.")
    elif action == "apply_recall":
        comp = call.related_complaint
        if comp:
            ex = (call.analysis or {}).get("extracted") or {}
            comp.resolution = (comp.resolution + "\n" if comp.resolution else "") + request.POST.get("outcome", ex.get("outcome", ""))
            if request.POST.get("resolved"):
                comp.status = "resolved"
                comp.tasks.update(done=True)
            comp.save()
            messages.success(request, "نتیجه تماس مجدد روی شکایت ثبت شد.")
    return redirect("call_detail", pk)


# ---------------------------------------------------------------- orders & dispatch

@login_required
def order_new(request):
    initial = {}
    call = Call.objects.filter(pk=request.GET.get("call") or 0).first()
    if call:
        ex = (call.analysis or {}).get("extracted") or {}
        initial = {"customer_phone": call.customer.phone if call.customer else "", "customer_name": ex.get("customer_name") or
                   (call.customer.name if call.customer else ""), "address": ex.get("address", ""), "district": ex.get("district", ""),
                   "hours": ex.get("hours") or 4, "has_pets": bool(ex.get("has_pets")),
                   "gender_needed": {"آقا": "m", "خانم": "f"}.get(ex.get("gender_preference"), ""),
                   "notes": ex.get("special_requests", "")}
    form = OrderForm(request.POST or None, initial=initial)
    problems = []
    if request.method == "POST" and form.is_valid():
        customer = _find_customer(form.cleaned_data["customer_phone"], form.cleaned_data["customer_name"])
        if customer and form.cleaned_data.get("address") and not customer.address:
            customer.address = form.cleaned_data["address"]
            customer.save(update_fields=["address"])
        wanted = form.cleaned_data.get("specialist")
        problems = rules.order_restrictions(customer, wanted, call)
        override = request.POST.get("override") and is_manager(request.user)
        if problems and not override:
            messages.error(request, "طبق دستورالعمل، ثبت این سفارش مجاز نیست. موارد را رفع کنید" +
                           ("، یا به‌عنوان سرپرست با تیک «ثبت با تأیید سرپرست» ثبت کنید." if is_manager(request.user) else "."))
        else:
            o = form.save(commit=False)
            o.customer = customer
            o.registered_by = request.user
            if wanted:
                o.specialist, o.status, o.requested_specific = wanted, Order.ASSIGNED, True
            o.save()
            if call and not call.order:
                call.order, call.customer = o, o.customer
                call.save(update_fields=["order", "customer"])
            messages.success(request, "سفارش ثبت شد.")
            return redirect("dispatch")
    return render(request, "crm/order_form.html", {"form": form, "call": call, "problems": problems,
                                                   "is_manager": is_manager(request.user)})


@login_required
def dispatch(request):
    days = int(request.GET.get("days") or 2)
    queue = [{"order": o, "suggestions": rules.suggest_specialists(o)} for o in rules.dispatch_queue(days)]
    return render(request, "crm/dispatch.html", {"queue": queue, "days": days})


@login_required
@require_POST
def assign(request, pk):
    o = get_object_or_404(Order, pk=pk)
    sp = get_object_or_404(Specialist, pk=request.POST.get("specialist"))
    o.specialist, o.status = sp, Order.ASSIGNED
    o.save(update_fields=["specialist", "status", "updated_at"])
    messages.success(request, f"{sp} برای سفارش {o.customer.name} تخصیص داده شد. (در CRM فعلی هم ثبت کنید تا مهاجرت کامل شود.)")
    return redirect("dispatch")


# ---------------------------------------------------------------- surveys & complaints

@login_required
def survey_list(request):
    qs = Survey.objects.select_related("customer", "specialist", "agent")
    if request.GET.get("draft"):
        qs = qs.filter(confirmed=False)
    if request.GET.get("low"):
        qs = qs.filter(average__lt=Rules.get().complaint_avg_below)
    return render(request, "crm/survey_list.html", {"page": _page(request, qs)})


@login_required
def survey_new(request):
    SF = survey_form_class()
    head, form = NewSurveyForm(request.POST or None), SF(request.POST or None)
    if request.method == "POST" and head.is_valid() and form.is_valid():
        h, d = head.cleaned_data, form.cleaned_data
        order = Order.objects.filter(tracking_code=h["order_code"].strip()).first() if h["order_code"] else None
        scores = {c.title: d[f"c_{c.pk}"] for c in ChecklistItem.of(ChecklistItem.SURVEY) if d.get(f"c_{c.pk}")}
        s = Survey.objects.create(customer=_find_customer(h["customer_phone"], h["customer_name"] or "بی‌نام"),
                                  specialist=h["specialist"] or (order.specialist if order else None), order=order,
                                  order_date=h["order_date"], agent=request.user, scores=scores, result=d["result"],
                                  notes=d["notes"], service_name=str(order.service) if order else "")
        comp = rules.process_survey(s, user=request.user, fault_titles=[d["fault"].title] if d.get("fault") else None)
        messages.success(request, "نظرسنجی ثبت شد." + (" شکایت و کار تماس مجدد ساخته شد." if comp else ""))
        return redirect("survey_new")
    return render(request, "crm/survey_new.html", {"head": head, "form": form})


@login_required
def complaint_list(request):
    status = request.GET.get("status", "active")
    qs = Complaint.objects.select_related("customer", "specialist", "reason")
    if status == "active":
        qs = qs.exclude(status="resolved")
    elif status:
        qs = qs.filter(status=status)
    return render(request, "crm/complaint_list.html", {"page": _page(request, qs), "status": status,
                                                       "statuses": Complaint.STATUSES})


@login_required
def complaint_detail(request, pk):
    c = get_object_or_404(Complaint.objects.select_related("customer", "specialist", "survey"), pk=pk)
    if request.method == "POST":
        before = c.fault
        for f in ("status", "fault", "compensation_type"):
            v = request.POST.get(f)
            if v is not None and v in dict(c._meta.get_field(f).choices):
                setattr(c, f, v)
        c.resolution = request.POST.get("resolution", c.resolution)
        c.specialist_statement = request.POST.get("specialist_statement", c.specialist_statement)
        c.refer_retention = bool(request.POST.get("refer_retention"))
        amt = (request.POST.get("compensation") or "").replace(",", "")
        c.compensation = int(amt) if amt.isdigit() else c.compensation
        c.save()
        if c.status == "resolved":
            c.tasks.update(done=True)
        msg = "شکایت به‌روز شد."
        if c.fault == "confirmed" and before != "confirmed":
            made = rules.propose_penalties(c)
            if made:
                msg += f" {len(made)} جریمه پیشنهاد شد و منتظر تأیید سرپرست است."
        messages.success(request, msg)
        return redirect("complaint_detail", pk)
    return render(request, "crm/complaint_detail.html", {
        "c": c, "statuses": Complaint.STATUSES, "faults": Complaint._meta.get_field("fault").choices,
        "comps": Complaint._meta.get_field("compensation_type").choices, "penalties": c.penalties.select_related("ptype")})


@login_required
def pro_complaint_list(request):
    qs = SpecialistComplaint.objects.select_related("specialist")
    if request.GET.get("status", "active") == "active":
        qs = qs.exclude(status="resolved")
    if request.method == "POST":
        pc = get_object_or_404(SpecialistComplaint, pk=request.POST.get("id"))
        pc.status = request.POST.get("status") or pc.status
        pc.resolution = request.POST.get("resolution", pc.resolution)
        pc.save()
        messages.success(request, "به‌روز شد.")
        return redirect(request.get_full_path())
    return render(request, "crm/pro_complaint_list.html", {"page": _page(request, qs), "statuses": SpecialistComplaint.STATUSES})


# ---------------------------------------------------------------- tickets

@login_required
def ticket_list(request):
    qs = Ticket.objects.select_related("specialist", "assigned_to")
    st = request.GET.get("status", "open")
    if st == "open":
        qs = qs.exclude(status="closed")
    elif st:
        qs = qs.filter(status=st)
    return render(request, "crm/ticket_list.html", {"page": _page(request, qs), "status": st})


@login_required
def ticket_new(request):
    form = TicketForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        t = form.save(commit=False)
        t.specialist = _find_specialist(form.cleaned_data["specialist_phone"])
        t.save()
        TicketMessage.objects.create(ticket=t, text=form.cleaned_data["text"])
        return redirect("ticket_detail", t.pk)
    return render(request, "crm/ticket_new.html", {"form": form})


@login_required
def ticket_detail(request, pk):
    t = get_object_or_404(Ticket, pk=pk)
    canned = list(CannedResponse.objects.filter(active=True).filter(Q(category="") | Q(category=t.get_category_display()))) or \
        list(CannedResponse.objects.filter(active=True))
    draft = ""
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "draft":
            try:
                draft, _ = analysis.draft_ticket_reply(t, canned)
            except Exception as e:
                messages.error(request, f"پیش‌نویس ساخته نشد: {e}")
        elif action in ("reply", "reply_close"):
            text = (request.POST.get("text") or "").strip()
            if text:
                flag = analysis.check_tone(text) if settings.OPENROUTER_API_KEY else ""
                TicketMessage.objects.create(ticket=t, from_staff=True, author=request.user, text=text, tone_flag=flag)
                t.first_response_at = t.first_response_at or timezone.now()
                t.assigned_to = t.assigned_to or request.user
                t.status = "closed" if action == "reply_close" else "waiting_pro"
                t.save()
                if flag:
                    messages.warning(request, f"پاسخ ثبت شد، ولی لحن آن نیاز به توجه دارد: {flag}")
                else:
                    messages.success(request, "پاسخ ثبت شد. متن را در CRM فعلی برای متخصص بفرستید.")
                return redirect("ticket_detail", pk)
        elif action == "pro_message":
            text = (request.POST.get("text") or "").strip()
            if text:
                TicketMessage.objects.create(ticket=t, text=text)
                t.status = "waiting_staff"
                t.save(update_fields=["status", "updated_at"])
            return redirect("ticket_detail", pk)
    return render(request, "crm/ticket_detail.html", {"t": t, "canned": canned, "draft": draft})


# ---------------------------------------------------------------- reports

@login_required
def report_commission(request):
    d = parse_date(request.GET.get("week")) or timezone.localdate()
    start = rules.week_start(d)
    return render(request, "crm/report_commission.html", {
        "rows": rules.weekly_commission(start), "start": start, "end": start + dt.timedelta(days=6),
        "thu": rules.thursday_groups(start),
        "prev": fmt_date(start - dt.timedelta(days=7)), "next": fmt_date(start + dt.timedelta(days=7)), "r": Rules.get()})


@login_required
def report_capacity(request):
    d = parse_date(request.GET.get("date")) or timezone.localdate()
    days = [rules.capacity(d + dt.timedelta(days=i)) for i in range(7)]
    return render(request, "crm/report_capacity.html", {"days": days, "start": d})


@login_required
def report_agents(request):
    since = timezone.now() - dt.timedelta(days=int(request.GET.get("days") or 30))
    stats = (Call.objects.filter(created_at__gte=since, status__in=["done", "reviewed"], agent__isnull=False)
             .values("agent__id", "agent__username", "agent__first_name", "agent__last_name", "agent__profile__display_name")
             .annotate(n=Count("id"), checklist=Avg("checklist_score"), tone=Avg("tone_score"), advice=Avg("advice_score"))
             .order_by("-n"))
    # per checklist item: how often each agent misses it
    misses = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for c in Call.objects.filter(created_at__gte=since, status__in=["done", "reviewed"], agent__isnull=False).only("agent_id", "analysis"):
        for it in (c.analysis or {}).get("checklist") or []:
            if isinstance(it, dict) and it.get("done") is not None:
                m = misses[c.agent_id][it.get("item")]
                m[1] += 1
                m[0] += {True: 0, "partial": 0.5}.get(it.get("done"), 1)
    items = [c.title for c in ChecklistItem.of(ChecklistItem.CALL)]
    rows = []
    for s in stats:
        name = s["agent__profile__display_name"] or f'{s["agent__first_name"]} {s["agent__last_name"]}'.strip() or s["agent__username"]
        per = [(f"{round(100 * (1 - m[0] / m[1]))}٪" if (m := misses[s["agent__id"]].get(i)) and m[1] else "-") for i in items]
        rows.append({**s, "name": name, "per": per})
    return render(request, "crm/report_agents.html", {"rows": rows, "items": items, "days": request.GET.get("days") or 30})


@login_required
def specialist_report(request):
    since = timezone.now() - dt.timedelta(days=int(request.GET.get("days") or 90))
    rows = (Specialist.objects.filter(active=True)
            .annotate(n_complaints=Count("complaints", filter=Q(complaints__created_at__gte=since), distinct=True),
                      avg_survey=Avg("surveys__average", filter=Q(surveys__call_date__gte=since.date())),
                      n_surveys=Count("surveys", filter=Q(surveys__call_date__gte=since.date()), distinct=True))
            .order_by("-n_complaints", "avg_survey"))
    return render(request, "crm/report_specialists.html", {"rows": rows, "days": request.GET.get("days") or 90})


# ---------------------------------------------------------------- tasks & import

@login_required
def task_list(request):
    if request.method == "POST":
        FollowUpTask.objects.filter(pk=request.POST.get("id")).update(done=True)
        return redirect("task_list")
    qs = FollowUpTask.objects.filter(done=False).select_related("customer", "specialist", "complaint")
    if request.GET.get("mine"):
        qs = qs.filter(assigned_to=request.user)
    return render(request, "crm/task_list.html", {"page": _page(request, qs)})


@login_required
def import_view(request):
    if not (request.user.is_superuser or getattr(getattr(request.user, "profile", None), "role", "") in ("manager", "supervisor")):
        return HttpResponseForbidden("فقط مدیر یا سرپرست می‌تواند داده وارد کند.")
    form = ImportForm(request.POST or None, request.FILES or None)
    results = None
    if request.method == "POST" and form.is_valid():
        results = import_workbook(form.cleaned_data["file"].read())
        if not results:
            messages.warning(request, "هیچ برگه‌ی شناخته‌شده‌ای در این فایل پیدا نشد.")
    return render(request, "crm/import.html", {"form": form, "results": results})


# ---------------------------------------------------------------- ingest API (for the VoIP uploader inside the office)

@csrf_exempt
@require_POST
def api_ingest_call(request):
    """POST multipart: audio=<file>, kind, agent (username), customer_phone, specialist_phone, note.
    Header: Authorization: Bearer <INGEST_TOKEN>."""
    token = settings.INGEST_TOKEN
    given = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token or not hmac.compare_digest(token, given):
        return JsonResponse({"error": "unauthorized"}, status=401)
    f = request.FILES.get("audio")
    if not f:
        return JsonResponse({"error": "audio missing"}, status=400)
    kind = request.POST.get("kind") or "other"
    if kind not in dict(Call.KINDS):
        kind = "other"
    from django.contrib.auth import get_user_model
    agent = get_user_model().objects.filter(username=request.POST.get("agent") or "").first()
    call = Call.objects.create(kind=kind, agent=agent, audio=f, note=request.POST.get("note", ""),
                               customer=_find_customer(request.POST.get("customer_phone")),
                               specialist=_find_specialist(request.POST.get("specialist_phone")))
    return JsonResponse({"id": call.pk, "status": call.status})


@login_required
def media_audio(request, pk):
    """Voices are served only to logged-in staff, never as public static files."""
    call = get_object_or_404(Call, pk=pk)
    if not call.audio:
        raise Http404
    from django.http import FileResponse
    return FileResponse(call.audio.open("rb"))


@login_required
def availability(request):
    """Daily employment sheet: one select per active specialist (replaces the «اشتغال» sheet)."""
    from .models import Availability
    d = parse_date(request.GET.get("date") or request.POST.get("date")) or timezone.localdate()
    sps = list(Specialist.objects.filter(active=True).order_by("gender", "name"))
    if request.method == "POST":
        n = 0
        for sp in sps:
            st = request.POST.get(f"s_{sp.pk}")
            if st:
                Availability.objects.update_or_create(specialist=sp, date=d, defaults={"state": st})
                n += 1
            else:
                Availability.objects.filter(specialist=sp, date=d).delete()
        messages.success(request, f"وضعیت {n} متخصص برای {fmt_date(d)} ذخیره شد.")
        return redirect(f"{request.path}?date={fmt_date(d)}")
    current = {a.specialist_id: a.state for a in Availability.objects.filter(date=d)}
    rows = [(sp, current.get(sp.pk, "")) for sp in sps]
    return render(request, "crm/availability.html", {"rows": rows, "date": d, "states": Availability.STATES,
                                                     "prev": fmt_date(d - dt.timedelta(days=1)), "next": fmt_date(d + dt.timedelta(days=1))})
