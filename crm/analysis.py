"""Turn a call transcript into a structured review, and turn that review into drafts the staff confirm.

Nothing here changes a final record on its own: surveys and specialist complaints extracted from voices are saved as
unconfirmed drafts; a person confirms them in the panel (semi-automatic).
"""
from __future__ import annotations

import json
import logging

from django.db import transaction
from django.utils import timezone

from . import ai
from .models import Call, ChecklistItem, SpecialistComplaint, Survey

log = logging.getLogger(__name__)

SYSTEM = """تو ارزیاب کیفیت مکالمات تیم پشتیبانی «سرویک» هستی؛ شرکتی که نیروی نظافت (منزل، مشاعات، محل کار) اعزام می‌کند.
متن پیاده‌شده‌ی یک تماس تلفنی را می‌گیری و فقط یک شیء JSON معتبر برمی‌گردانی، بدون هیچ متن دیگری.
منصف و دقیق باش؛ فقط بر اساس چیزی که در متن هست قضاوت کن و برای هر قضاوت جمله‌ی شاهد کوتاه از متن بیاور.
اگر موردی در این نوع تماس موضوعیت ندارد مقدار done را null بگذار. همه‌ی متن‌ها فارسی باشد."""

COMMON_SCHEMA = """{
  "summary": "خلاصه‌ی دو تا چهار جمله‌ای تماس",
  "checklist": [{"item": "عنوان دقیق مورد از فهرست", "done": true|false|null, "evidence": "نقل کوتاه", "note": "توضیح کوتاه"}],
  "tone": {"score": 1-5, "note": "ارزیابی لحن و برخورد کارشناس", "issues": ["جمله‌ها یا رفتارهای نامناسب، اگر بود"]},
  "advice": {"score": 1-5 یا null, "note": "کیفیت مشاوره و راهنمایی مشتری"},
  "other_party_sentiment": "positive|neutral|negative",
  "suggested_actions": ["اقدام پیشنهادی بعدی"],
  "extracted": { ... }
}"""

EXTRACT = {
    "order": """"extracted": {"service": "نوع خدمت", "date": "تاریخ گفته‌شده", "time": "ساعت", "hours": عدد یا null,
  "address": "", "district": "", "gender_preference": "آقا|خانم|فرقی ندارد|null", "has_pets": true|false|null,
  "special_requests": "", "price_quoted": "", "customer_name": ""}""",
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
EXTRACT["satisfaction"] = EXTRACT["survey"]
EXTRACT["other"] = '"extracted": {}'


def build_prompt(call: Call) -> str:
    checklist = ChecklistItem.of(ChecklistItem.CALL)
    lines = [f"- {c.title}" + (f": {c.guide}" if c.guide else "") for c in checklist]
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
        ctx.append(f"سفارش: {call.order.service} در {timezone.localtime(call.order.start):%Y-%m-%d %H:%M}")
    if call.note:
        ctx.append(f"یادداشت کارشناس: {call.note}")
    return (f"نوع تماس: {call.get_kind_display()}\n" + ("\n".join(ctx) + "\n" if ctx else "") +
            "\nفهرست موارد هفت‌گانه‌ی رفتار کارشناس (برای checklist، عنوان‌ها را دقیقاً همین‌طور بنویس):\n" + "\n".join(lines) +
            "\n\nقالب خروجی:\n" + COMMON_SCHEMA.replace('"extracted": { ... }', extract) +
            "\n\nمتن تماس:\n" + call.transcript)


def _int(v, lo=1, hi=5):
    try:
        x = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    return x if lo <= x <= hi else None


def apply_analysis(call: Call, data: dict) -> None:
    """Store scores on the call and create the unconfirmed drafts for the human to confirm."""
    call.analysis = data
    items = [c for c in data.get("checklist") or [] if isinstance(c, dict) and c.get("done") is not None]
    call.checklist_score = round(100 * sum(1 for c in items if c.get("done")) / len(items), 1) if items else None
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
    if call.kind == "pro_complaint" and not call.specialist_complaints.exists():
        reason = ChecklistItem.objects.filter(kind=ChecklistItem.PRO_COMPLAINT, title=ex.get("reason") or "").first()
        amount = ex.get("amount")
        SpecialistComplaint.objects.create(
            specialist=call.specialist, order=call.order, customer=call.customer, call=call, reason=reason,
            reason_text=(reason.title if reason else (ex.get("reason") or "")), description=ex.get("description") or "",
            amount=int(amount) if isinstance(amount, (int, float)) and amount > 0 else None, agent=call.agent, confirmed=False)


def process_call(call: Call) -> None:
    """Full pipeline for one call. Raises on failure; the worker records the error."""
    if not call.transcript:
        if not call.audio:
            raise ai.AIError("فایل صوتی ندارد")
        call.status = "transcribing"
        call.save(update_fields=["status"])
        with call.audio.open("rb") as f:
            text, cost = ai.transcribe(f.read())
        call.transcript = text
        call.ai_cost += _dec(cost)
        call.save(update_fields=["transcript", "ai_cost"])
    call.status = "analyzing"
    call.save(update_fields=["status"])
    data, cost = ai.ask_json(SYSTEM, build_prompt(call))
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
