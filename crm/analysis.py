"""Turn a call transcript into a structured review, and turn that review into drafts the staff confirm.

Nothing here changes a final record on its own: surveys and specialist complaints extracted from voices are saved as
unconfirmed drafts; a person confirms them in the panel (semi-automatic).
"""
from __future__ import annotations

import json
import re
import logging

from django.db import transaction
from django.utils import timezone

from . import ai
from .defaults_v2 import BANNED_TITLE, GENERAL
from .models import Call, ChecklistItem, Complaint, DailyCheck, Rules, SpecialistComplaint, Survey

log = logging.getLogger(__name__)

AUTO_NOTE = "نوع تماس خودکار تشخیص داده شد"

SYSTEM = """تو ارزیاب کیفیت مکالمات تیم پشتیبانی «سرویک» هستی؛ شرکتی که نیروی نظافت (منزل، مشاعات، محل کار) اعزام می‌کند.
متن پیاده‌شده‌ی یک تماس تلفنی را می‌گیری و فقط یک شیء JSON معتبر برمی‌گردانی، بدون هیچ متن دیگری.
منصف و دقیق باش؛ فقط بر اساس چیزی که در متن هست قضاوت کن.
برای evidence فقط یک تکه‌ی کوتاه از متن (حداکثر ۱۲ کلمه) بیاور و note را در یک جمله‌ی کوتاه بنویس؛ اگر مورد موضوعیت ندارد evidence را خالی بگذار.
برای هر مورد چک‌لیست سخت‌گیر باش: done=true فقط وقتی همه‌ی اجزای آن مورد انجام شده؛ اگر بخشی انجام شده و بخشی نه
(مثلاً سلام کرد ولی نام شرکت را نگفت) مقدار "partial" بگذار و در note بگو چه چیزی جا افتاد؛ اگر انجام نشد false.
اگر موردی در این نوع تماس موضوعیت ندارد مقدار done را null بگذار. همه‌ی متن‌ها فارسی باشد.
خط‌هایی که با «سیستم:» شروع می‌شوند پیام خودکار تلفن‌اند و جزو صحبت کارشناس حساب نمی‌شوند.
اگر در این تماس سفارش تازه‌ای ثبت نشد (مثلاً مشتری فقط برای پیگیری یا اطمینان از سفارشی که قبلاً ثبت شده زنگ زد)،
همه‌ی موارد گروه «دیالوگ نهایی هماهنگی سفارش» را null بگذار، چون آن دیالوگ هنگام ثبت سفارش گفته می‌شود."""

COMMON_SCHEMA = """{
  "summary": "خلاصه‌ی دو تا چهار جمله‌ای تماس",
  "checklist": [{"item": "عنوان دقیق مورد از فهرست", "done": true|"partial"|false|null, "evidence": "حداکثر ۱۲ کلمه از متن", "note": "یک جمله"}],
  "tone": {"score": 1-5, "note": "ارزیابی لحن و برخورد کارشناس", "issues": ["حداکثر ۳ جمله‌ی نامناسب کارشناس، کوتاه؛ اگر نبود خالی"]},
  "advice": {"score": 1-5 یا null, "note": "کیفیت مشاوره و راهنمایی مشتری"},
  "other_party_sentiment": "positive|neutral|negative",
  "suggested_actions": ["اقدام پیشنهادی بعدی"],
  "extracted": { ... }
}"""

EXTRACT = {
    "order": """"extracted": {"service": "نوع خدمت", "date": "تاریخ گفته‌شده", "time": "ساعت", "hours": عدد یا null,
  "address": "", "district": "", "gender_preference": "آقا|خانم|فرقی ندارد|null", "has_pets": true|false|null,
  "special_requests": "", "price_quoted": "", "customer_name": "", "order_registered": true|false,
  "conditions_rejected": ["عنوان موارد دیالوگ نهایی که مشتری صریحاً نپذیرفت؛ اگر همه را پذیرفت یا مطرح نشد خالی"]}""",
    "survey": """"extracted": {"answered": true|false, "scores": {SURVEY_KEYS}, "result": "full|good|medium|unhappy|no_answer|unaware",
  "comment": "حرف‌های مشتری درباره‌ی نیرو و خدمت", "specialist_faults": ["فقط از فهرست: FAULTS"]}
برای scores هر معیاری که مشتری نمره داد را ۱ تا ۵ بگذار و اگر نپرسید یا جواب نداد null.""",
    "recall": """"extracted": {"resolved": true|false, "customer_request": "", "compensation": "مثلاً کد تخفیف ۱۰۰ هزار تومانی یا تجدید نظافت یا هیچ",
  "outcome": "نتیجه‌ی تماس در یک جمله، مناسب برای ستون اقدام/نتیجه پیگیری"}""",
    "pro_complaint": """"extracted": {"reason": "فقط یکی از: PRO_REASONS", "description": "شرح شکایت متخصص",
  "requested_action": "", "amount": عدد تومان یا null, "customer_or_order_hint": "نام مشتری یا کد سفارش اگر گفته شد"}""",
    "new_customer": """"extracted": {"customer_name": "", "service": "", "order_placed": true|false, "reason_not_ordered": "استعلام|هزینه بالا|عدم ظرفیت|عدم تطابق نیرو|رد سفارش|سایر|null",
  "follow_up_needed": true|false}""",
}
EXTRACT["unhappy"] = """"extracted": {"complaint_summary": "موضوع شکایت در یک جمله", "specialist_faults": ["فقط از فهرست: FAULTS"],
  "customer_request": "", "followup_time_promised": "زمانی که برای اعلام نتیجه گفته شد، یا null"}"""
EXTRACT["periodic"] = """"extracted": {"result": "confirmed|cancel|reschedule|no_answer|follow|other", "new_time": "اگر زمان عوض شد",
  "specialist_ok": "yes|no|null", "specialist_feedback": "نظر مشتری درباره‌ی متخصص ثابت", "note": ""}"""
EXTRACT["morning"] = """"extracted": {"result": "on_time|late|cannot|no_answer|replaced", "late_minutes": عدد یا 0,
  "eta": "زمان تقریبی رسیدن", "note": ""}"""
EXTRACT["satisfaction"] = EXTRACT["survey"]
EXTRACT["other"] = '"extracted": {}'


def build_prompt(call: Call) -> str:
    checklist = ChecklistItem.call_items(call.kind)
    lines, group = [], None
    for c in checklist:
        if (c.group or "عمومی") != group:
            group = c.group or "عمومی"
            lines.append(f"\n[{group}]")
        lines.append(f"- {c.title}" + (f": {c.guide}" if c.guide else ""))
    rules = Rules.get()
    meal = f"{rules.meal_min_hours:g} ساعت و بیشتر" if rules.meal_inclusive else f"بیشتر از {rules.meal_min_hours:g} ساعت"
    lines = [ln.replace("{MEAL}", meal) for ln in lines]
    extract = EXTRACT.get(call.kind, '"extracted": {}')
    survey_keys = ", ".join(f'"{c.title}": 1-5|null' for c in ChecklistItem.of(ChecklistItem.SURVEY))
    faults = "، ".join(c.title for c in ChecklistItem.of(ChecklistItem.FAULT))
    pro = "، ".join(c.title for c in ChecklistItem.of(ChecklistItem.PRO_COMPLAINT))
    extract = extract.replace("SURVEY_KEYS", survey_keys).replace("FAULTS", faults).replace("PRO_REASONS", pro)
    ctx = []
    if call.customer:
        ctx.append(f"مشتری: {call.customer.name}")
    if call.specialist:
        ctx.append(f"متخصص: {call.specialist.name}")
    if call.order:
        o = call.order
        ctx.append(f"سفارش: {o.service} در {timezone.localtime(o.start):%Y-%m-%d %H:%M}، {o.hours} ساعت"
                   + (f"، مبلغ {o.price:,} ریال" if o.price else "")
                   + (f"، متخصص {o.specialist.get_gender_display()}" if o.specialist else
                      (f"، نیروی {o.get_gender_needed_display()}" if o.gender_needed else "")))
    note = "\n".join(ln for ln in (call.note or "").splitlines() if not ln.startswith(AUTO_NOTE))
    if note.strip():
        ctx.append(f"یادداشت کارشناس: {note}")
    return (f"نوع تماس: {call.get_kind_display()}\n" + ("\n".join(ctx) + "\n" if ctx else "") +
            "\nچک‌لیست رفتار کارشناس در این نوع تماس (برای checklist همه‌ی موارد را به همین ترتیب بیاور و عنوان‌ها را دقیقاً همین‌طور بنویس؛"
            " اگر شرط یک مورد برقرار نیست، مثلاً سفارش کوتاه‌تر از حد یا نیروی آقا، done را null بگذار و در note بگو چرا):\n"
            + "\n".join(lines) +
            "\n\nقالب خروجی:\n" + COMMON_SCHEMA.replace('"extracted": { ... }', extract) +
            "\n\nمتن تماس:\n" + call.transcript)


def _int(v, lo=1, hi=5):
    try:
        x = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    return x if lo <= x <= hi else None


_FA_LETTER = r"[\u0600-\u06FF\u200c]"


def agent_lines(transcript: str) -> list[str]:
    return [ln.split(":", 1)[1] for ln in (transcript or "").splitlines() if ln.strip().startswith("کارشناس") and ":" in ln]


def find_banned(transcript: str, words: list[str]) -> list[dict]:
    """Exact search of banned words in the agent's lines only (no AI, so no cost and no guessing)."""
    hits = []
    lines = agent_lines(transcript)
    for w in words:
        pat = re.compile(rf"(?<!{_FA_LETTER}){re.escape(w)}(?!{_FA_LETTER})")
        for ln in lines:
            if pat.search(ln):
                hits.append({"word": w, "line": ln.strip()[:160]})
                break
    return hits


def apply_analysis(call: Call, data: dict) -> None:
    """Store scores on the call and create the unconfirmed drafts for the human to confirm."""
    if call.kind != "other" and isinstance(data.get("checklist"), list):
        hits = find_banned(call.transcript, Rules.get().banned_list())
        data["banned"] = hits
        data["checklist"] = [c for c in data["checklist"] if not (isinstance(c, dict) and c.get("item") == BANNED_TITLE)]
        data["checklist"].append({"item": BANNED_TITLE, "group": GENERAL, "done": not hits,
                                  "evidence": "، ".join(h["word"] for h in hits), "note": "بررسی خودکار" if not hits else
                                  "کلمات ممنوع در جمله‌های کارشناس پیدا شد"})
    call.analysis = data
    items = [c for c in data.get("checklist") or [] if isinstance(c, dict) and c.get("done") is not None]
    credit = {True: 1.0, "partial": 0.5, False: 0.0}
    call.checklist_score = round(100 * sum(credit.get(c.get("done"), 0.0) for c in items) / len(items), 1) if items else None
    call.tone_score = _int((data.get("tone") or {}).get("score"))
    call.advice_score = _int((data.get("advice") or {}).get("score"))
    ex = data.get("extracted") or {}

    if call.kind in ("survey", "satisfaction") and ex.get("answered", True) and not call.surveys.exists():
        scores = {k: _int(v) for k, v in (ex.get("scores") or {}).items() if _int(v) is not None}
        result = ex.get("result") if ex.get("result") in dict(Survey.RESULTS) else ""
        s = Survey(call=call, order=call.order, customer=call.customer, specialist=call.specialist, agent=call.agent,
                   service_name=str(call.order.service) if call.order else "", scores=scores, result=result,
                   notes=ex.get("comment") or "", confirmed=False)
        s.compute_average()
        s.save()
    if call.kind == "unhappy" and not call.unhappy_complaints.exists():
        fault = ChecklistItem.objects.filter(kind=ChecklistItem.FAULT, title__in=ex.get("specialist_faults") or []).first()
        Complaint.objects.create(customer=call.customer, specialist=call.specialist, order=call.order, reason=fault,
                                 reason_text=fault.title if fault else (ex.get("complaint_summary") or "نارضایتی مشتری")[:200],
                                 description=(ex.get("complaint_summary") or "") + ("\nدرخواست مشتری: " + ex["customer_request"]
                                                                                   if ex.get("customer_request") else ""),
                                 status="open", agent=call.agent, auto_created=True, source="customer", source_call=call)
    if call.kind in ("periodic", "morning") and call.order_id and ex.get("result"):
        valid = dict(DailyCheck.RESULTS[call.kind])
        if ex["result"] in valid:
            late = int(ex.get("late_minutes") or 0) if str(ex.get("late_minutes") or "0").isdigit() else 0
            DailyCheck.objects.update_or_create(
                kind=call.kind, order=call.order, date=timezone.localdate(call.created_at),
                defaults={"result": ex["result"], "late_minutes": late, "user": call.agent,
                          "specialist_ok": ex.get("specialist_ok") if ex.get("specialist_ok") in ("yes", "no") else "",
                          "note": (ex.get("note") or ex.get("specialist_feedback") or "")[:300]})
    if call.kind == "pro_complaint" and not call.specialist_complaints.exists():
        reason = ChecklistItem.objects.filter(kind=ChecklistItem.PRO_COMPLAINT, title=ex.get("reason") or "").first()
        amount = ex.get("amount")
        SpecialistComplaint.objects.create(
            specialist=call.specialist, order=call.order, customer=call.customer, call=call, reason=reason,
            reason_text=(reason.title if reason else (ex.get("reason") or "")), description=ex.get("description") or "",
            amount=int(amount) if isinstance(amount, (int, float)) and amount > 0 else None, agent=call.agent, confirmed=False)


CLASSIFY_SYSTEM = """نوع یک تماس تلفنی شرکت خدمات نظافت «سرویک» را از روی متنش تشخیص بده. فقط JSON برگردان:
{"kind": "یکی از کدها", "reason": "یک جمله"}
کدها:
order = مشتری برای ثبت سفارش یا پرسیدن قیمت و شرایط تماس گرفته یا کارشناس سفارش ثبت می‌کند
survey = کارشناس بعد از انجام کار، از مشتری درباره‌ی کیفیت کار و نیرو نظر و نمره می‌پرسد
unhappy = مشتری از کیفیت کار یا نیرو گلایه و شکایت می‌کند
recall = کارشناس درباره‌ی شکایت قبلی مشتری دوباره تماس گرفته و نتیجه‌ی پیگیری را می‌گوید
pro_complaint = نیروی نظافت (متخصص) از مشتری یا شرکت گلایه یا درخواست دارد
morning = کارشناس صبح با نیرو تماس می‌گیرد تا از حضور و حرکتش به سمت محل کار مطمئن شود
periodic = کارشناس برای تأیید نوبت بعدی سفارش دوره‌ای با مشتری تماس گرفته
new_customer = تماس با مشتری جدیدی که ثبت‌نام کرده ولی سفارش نداده
other = هیچ‌کدام"""


def classify(call: Call) -> float:
    data, cost = ai.ask_json(CLASSIFY_SYSTEM, call.transcript[:6000], log=call.usage, step="classify")
    kind = data.get("kind")
    call.kind = kind if kind in dict(Call.KINDS) and kind != "auto" else "other"
    call.note = (call.note + "\n" if call.note else "") + f"{AUTO_NOTE}: {data.get('reason', '')}"
    call.save(update_fields=["kind", "note", "usage"])
    return cost


def process_call(call: Call) -> None:
    """Full pipeline for one call. Raises on failure; the worker records the error."""
    if not call.transcript:
        if not call.audio:
            raise ai.AIError("فایل صوتی ندارد")
        call.status = "transcribing"
        call.save(update_fields=["status"])
        with call.audio.open("rb") as f:
            text, cost = ai.transcribe(f.read(), log=call.usage)
        call.transcript = text
        call.ai_cost += _dec(cost)
        call.save(update_fields=["transcript", "ai_cost", "usage"])
    call.transcript = ai.normalize_transcript(call.transcript)
    call.status = "analyzing"
    call.save(update_fields=["status", "transcript"])
    if call.kind == "auto":
        call.ai_cost += _dec(classify(call))
    data, cost = ai.ask_json(SYSTEM, build_prompt(call), log=call.usage)
    with transaction.atomic():
        call.ai_cost += _dec(cost)
        apply_analysis(call, data)
        call.status = "done"
        call.error = ""
        call.save()


def _dec(x):
    from decimal import Decimal
    return Decimal(str(round(float(x or 0), 5)))


TICKET_SYSTEM = """تو کارشناس پشتیبانی مؤدب و دقیق «سرویک» هستی و به تیکت نیروهای نظافت جواب می‌دهی.
کوتاه، محترمانه و روشن بنویس. اگر پاسخ آماده‌ای مرتبط است از آن استفاده کن. قول یا مبلغی که در اطلاعات نیست نده؛
اگر نیاز به بررسی دارد بگو بررسی می‌شود. فقط متن پاسخ را بنویس."""

TONE_SYSTEM = """لحن پیام یک کارشناس پشتیبانی به یک نیروی نظافت را بسنج. فقط JSON برگردان:
{"ok": true|false, "issue": "اگر لحن تند، تحقیرآمیز، سرد یا غیرمحترمانه است، در یک جمله بگو چرا؛ وگرنه خالی"}"""


def draft_ticket_reply(ticket, canned) -> tuple[str, float]:
    history = "\n".join(("کارشناس: " if m.from_staff else "متخصص: ") + m.text for m in ticket.messages.all())
    ready = "\n".join(f"- {c.title}: {c.text}" for c in canned[:15])
    user = f"دسته: {ticket.get_category_display()}\nعنوان: {ticket.title}\n\nگفتگو:\n{history}\n\nپاسخ‌های آماده:\n{ready or '-'}"
    return ai.ask_text(TICKET_SYSTEM, user)


def check_tone(text: str) -> str:
    try:
        data, _ = ai.ask_json(TONE_SYSTEM, text)
    except Exception as e:  # tone check must never block sending a reply
        log.warning("tone check failed: %s", e)
        return ""
    return "" if data.get("ok", True) else str(data.get("issue") or "")[:300]


def dumps(d) -> str:
    return json.dumps(d, ensure_ascii=False, indent=2)
