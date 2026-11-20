"""Import Excel files from the old CRM, the HCM and the operations sheets.

Each importer recognizes its file by the column headers, so a staff member can upload any of the known files and the
system picks the right importer. Bank card / IBAN columns present in some exports are deliberately never read.
Re-importing the same file is safe: rows are matched on phone / tracking code and updated, not duplicated.
"""
from __future__ import annotations

import io
import re
from datetime import date
from dataclasses import dataclass, field

import openpyxl
from django.db import transaction

from .jalali import parse_date, parse_datetime
from .models import (ChecklistItem, Complaint, Customer, Order, ServiceType, Specialist, SpecialistComplaint, Survey,
                     normalize_phone)


def _norm(h) -> str:
    return re.sub(r"\s+", " ", str(h or "").replace("‌", " ").replace("\n", " ")).strip()


def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _bool(v) -> bool:
    return _s(v).lower() in ("true", "1", "بله", "دارد", "*", "yes")


@dataclass
class Result:
    kind: str = ""
    sheet: str = ""
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list = field(default_factory=list)

    def line(self):
        return f"{self.kind} ({self.sheet}): {self.created} جدید، {self.updated} به‌روز، {self.skipped} رد شده"


class Table:
    def __init__(self, ws):
        self.ws = ws
        rows = list(ws.iter_rows(values_only=True))
        # The header is the fullest text row near the top (some sheets have a title or weekday band above it).
        self.header_idx, self.headers, best = 0, [], 0
        for i, r in enumerate(rows[:6]):
            h = [_norm(c) for c in r]
            n = sum(1 for x in h if x and not re.fullmatch(r"[\d.]+", x))
            if n > best:
                self.header_idx, self.headers, best = i, h, n
        self.rows = rows[self.header_idx + 1:]

    def has(self, *names) -> bool:
        return all(n in self.headers for n in names)

    def records(self):
        for r in self.rows:
            if not any(c not in (None, "") for c in r):
                continue
            yield {h: r[i] if i < len(r) else None for i, h in enumerate(self.headers) if h}


# ---------------------------------------------------------------- helpers

def _service(name: str) -> ServiceType:
    name = name or "نامشخص"
    cat = "home" if "منزل" in name else "common" if ("مشاع" in name or "راه پله" in name) else "work" if "محل کار" in name else "other"
    st, _ = ServiceType.objects.get_or_create(name=name, defaults={"category": cat, "is_vip": "VIP" in name.upper()})
    return st


def _customer(name, phone) -> Customer | None:
    p = normalize_phone(phone)
    if not p:
        return None
    c, created = Customer.objects.get_or_create(phone=p, defaults={"name": _s(name) or "بی‌نام"})
    if not created and _s(name) and c.name in ("", "بی‌نام"):
        c.name = _s(name)
        c.save(update_fields=["name"])
    return c


def _specialist_by(name="", phone="") -> Specialist | None:
    p = normalize_phone(phone)
    if p:
        sp = Specialist.objects.filter(phone=p).first()
        if sp:
            return sp
    name = _s(name)
    if name:
        return Specialist.objects.filter(name=name).first()
    return None


def _gender(v) -> str:
    v = _s(v)
    return "f" if v in ("خانم", "زن", "f") else "m" if v in ("آقا", "مرد", "m") else ""


def _fault(title) -> ChecklistItem | None:
    title = _s(title)
    return ChecklistItem.objects.filter(kind=ChecklistItem.FAULT, title=title).first() if title else None


# ---------------------------------------------------------------- importers

def import_crm_workers(t: Table, res: Result):
    """Old CRM: نیروها export."""
    for r in t.records():
        if _s(r.get("نوع مار بیزینسی نیرو")) not in ("", "سرویک"):
            res.skipped += 1
            continue
        phone = normalize_phone(r.get("شماره تلفن همراه"))
        if not phone:
            res.skipped += 1
            continue
        name = f"{_s(r.get('نام'))} {_s(r.get('نام خانوادگی'))}".strip()
        score = _num(r.get("امتیاز"))
        defaults = {"name": name, "personnel_code": _s(r.get("کد پرسنلی")), "national_id": _s(r.get("کد ملی")),
                    "active": _bool(r.get("وضعیت")), "legacy_id": _s(r.get("Id")),
                    "worker_type": "vip_all" if _bool(r.get("Vip")) else "normal",
                    "score": score if score else None, "residence": _s(r.get("شهر محل سکونت"))}
        sp, created = Specialist.objects.get_or_create(phone=phone, defaults={**defaults, "gender": "m"})
        if not created:
            for k, v in defaults.items():
                if v not in ("", None):
                    setattr(sp, k, v)
            sp.save()
        res.created += created
        res.updated += not created


def import_sheet_specialists(t: Table, res: Result):
    """Operations sheet: متخصصین."""
    types = {"vip همه کار": "vip_all", "vip": "vip_all", "vip مشاعات و محل کار": "vip_cw", "عادی": "normal"}
    for r in t.records():
        phone = normalize_phone(r.get("شماره تماس"))
        name = _s(r.get("نام و نام خانوادگی"))
        if not phone or not name:
            res.skipped += 1
            continue
        defaults = {
            "name": name, "gender": _gender(r.get("جنسیت")) or "m", "personnel_code": _s(r.get("کد پرسنلی")),
            "national_id": _s(r.get("کد ملی")), "does_home": _bool(r.get("نظافت منزل")), "does_common": _bool(r.get("مشاعات")),
            "does_work": _bool(r.get("محل کار")), "does_hospitality": _bool(r.get("پذیرایی و اعزام مهمان دار")),
            "smoker": _s(r.get("سیگاری/ غیر سیگاری") or r.get("سیگاری/غیر سیگاری")) == "سیگاری",
            "ok_with_pets": "نمی" in _s(r.get("حیوان خانگی")) or _s(r.get("حیوان خانگی")) == "",
            "worker_type": types.get(_s(r.get("نوع نیرو")).lower(), "normal"),
            "residence": _s(r.get("محل اقامت")), "work_area": _s(r.get("محدوده اقامت")),
            "active": _s(r.get("وضعیت")) == "فعال", "hired_on": parse_date(r.get("تاریخ استخدام")),
            "traits": _s(r.get("خصوصیات اخلاقی متخصص")),
        }
        score = _num(r.get("امتیاز"))
        if score and 0 < score <= 5:
            defaults["score"] = score
        sp, created = Specialist.objects.update_or_create(phone=phone, defaults=defaults)
        res.created += created
        res.updated += not created


def import_crm_orders(t: Table, res: Result):
    """Old CRM: سفارشات export."""
    status_map = [("لغو", Order.CANCELLED), ("انجام شد", Order.DONE), ("پایان", Order.DONE), ("اتمام", Order.DONE)]
    for r in t.records():
        cust = _customer(r.get("نام مشتری"), r.get("شماره تماس"))
        start = parse_datetime(r.get("تاریخ شروع"))
        code = _s(r.get("کد پیگیری"))
        if not cust or not start:
            res.skipped += 1
            continue
        st = _s(r.get("وضعیت سفارش"))
        status = next((v for k, v in status_map if k in st), None)
        names = [n.strip() for n in re.split(r"[,،\n]", _s(r.get("نیروها"))) if n.strip()]
        sp = _specialist_by(names[0]) if names else None
        if not status:
            status = Order.ASSIGNED if sp else Order.REGISTERED
        women, men = _num(r.get("تعداد کارگر خانم")) or 0, _num(r.get("تعداد کارگر آقا")) or 0
        defaults = {"customer": cust, "service": _service(_s(r.get("نام گروه"))), "start": start, "status": status,
                    "specialist": sp, "price": int(_num(r.get("هزینه کل")) or 0) or None,
                    "gender_needed": "f" if women and not men else "m" if men and not women else "",
                    "address": _s(r.get("آدرس")), "is_periodic": _s(r.get("سفارش دوره ای")) == "بله",
                    "cancel_reason": _s(r.get("دلیل لغو سفارش")), "notes": _s(r.get("توضیحات"))}
        if code:
            o, created = Order.objects.update_or_create(tracking_code=code, defaults=defaults)
        else:
            o, created = Order.objects.create(**defaults), True
        res.created += created
        res.updated += not created


def import_sheet_surveys(t: Table, res: Result):
    """Operations sheet: نظر سنجی."""
    criteria = [c.title for c in ChecklistItem.of(ChecklistItem.SURVEY)]
    results = {label: code for code, label in Survey.RESULTS}
    for r in t.records():
        cust = _customer(r.get("نام مشتری"), r.get("شماره تماس"))
        call_date = parse_date(r.get("تاریخ تماس")) or parse_date(r.get("تاریخ سفارش"))
        if not cust or not call_date:
            res.skipped += 1
            continue
        scores = {}
        for c in criteria:
            v = _num(r.get(c))
            if v and 1 <= v <= 5:
                scores[c] = v
        sp = _specialist_by(r.get("نام نیرو"))
        order_date = parse_date(r.get("تاریخ سفارش"))
        if Survey.objects.filter(customer=cust, call_date=call_date, order_date=order_date, specialist=sp, legacy=True).exists():
            res.updated += 1
            continue
        s = Survey(customer=cust, specialist=sp, service_name=_s(r.get("نوع خدمت")), order_date=order_date, call_date=call_date,
                   scores=scores, result=results.get(_s(r.get("نتیجه تماس")), ""), notes=_s(r.get("توضیحات")),
                   confirmed=True, legacy=True)
        s.compute_average()
        s.save()
        res.created += 1


def import_sheet_complaints(t: Table, res: Result):
    """Operations sheet: شکایات از متخصصین."""
    for r in t.records():
        sp = _specialist_by(r.get("نام نیرو"), r.get("شماره تلفن نیرو"))
        reason = _s(r.get("علت شکایت مشتری"))
        if not sp and not reason:
            res.skipped += 1
            continue
        code = _s(r.get("کد پیگیری سفارش"))
        order = Order.objects.filter(tracking_code=code).first() if code else None
        follow = _s(r.get("واحد خشنودی مشتریان"))
        qc = _s(r.get("وضعیت ارجاع به واحد کنترل کیفیت"))
        status = "resolved" if (follow or "حل" in qc) else "referred" if "ارجاع" in qc else "open"
        called = parse_date(r.get("تاریخ تماس"))
        if status == "open" and called and (date.today() - called).days > 14:
            status = "resolved"  # old sheet rows without follow-up are history, not open work
        key = dict(specialist=sp, reason_text=reason, description=_s(r.get("توضیحات")), legacy=True)
        if Complaint.objects.filter(**key).exists():
            res.updated += 1
            continue
        c = Complaint.objects.create(**key, order=order, customer=order.customer if order else None, reason=_fault(reason),
                                     score=_num(r.get("امتیاز نظرسنجی")), status=status,
                                     resolution="\n".join(x for x in (follow, qc) if x),
                                     compensation=int(_num(r.get("مبلغ هزینه")) or 0) or None)
        d = parse_date(r.get("تاریخ تماس"))
        if d:
            Complaint.objects.filter(pk=c.pk).update(created_at=parse_datetime(r.get("تاریخ تماس")))
        res.created += 1


def import_sheet_pro_complaints(t: Table, res: Result):
    """Operations sheet: شکایات متخصصان."""
    for r in t.records():
        sp = _specialist_by(r.get("نام نیرو"), r.get("شماره تلفن نیرو"))
        reason = _s(r.get("علت شکایت متخصص"))
        if not sp and not reason:
            res.skipped += 1
            continue
        key = dict(specialist=sp, reason_text=reason, description=_s(r.get("توضیحات")), legacy=True)
        if SpecialistComplaint.objects.filter(**key).exists():
            res.updated += 1
            continue
        resolution = _s(r.get("نگهداشت"))
        item = ChecklistItem.objects.filter(kind=ChecklistItem.PRO_COMPLAINT, title=reason).first()
        c = SpecialistComplaint.objects.create(**key, reason=item, resolution=resolution,
                                               status="resolved" if resolution else "open",
                                               amount=int(_num(r.get("مبلغ هزینه")) or 0) or None)
        dtv = parse_datetime(r.get("تاریخ تماس"))
        if dtv:
            SpecialistComplaint.objects.filter(pk=c.pk).update(created_at=dtv)
        res.created += 1


def import_crm_customer_report(t: Table, res: Result):
    """Old CRM: گزارشات مشتریان (order count, first/last order) — gives history for «first order» and «loyal»."""
    for r in t.records():
        phone = normalize_phone(r.get("شماره همراه"))
        if not phone:
            res.skipped += 1
            continue
        c, created = Customer.objects.get_or_create(phone=phone, defaults={"name": _s(r.get("نام مشتری")) or "بی‌نام"})
        c.legacy_order_count = int(_num(r.get("تعداد سفارش")) or 0)
        c.legacy_first_order = parse_date(r.get("تاریخ اولین سفارش"))
        c.legacy_last_order = parse_date(r.get("تاریخ آخرین سفارش"))
        if _s(r.get("نام مشتری")) and c.name == "بی‌نام":
            c.name = _s(r.get("نام مشتری"))
        c.source = c.source or _s(r.get("نحوه آشنایی"))
        c.save()
        res.created += created
        res.updated += not created


IMPORTERS = [
    ("گزارش مشتریان CRM", ("شماره همراه", "تعداد سفارش", "تاریخ اولین سفارش"), import_crm_customer_report),
    ("نیروهای CRM", ("کد پرسنلی", "شماره تلفن همراه", "نوع مار بیزینسی نیرو"), import_crm_workers),
    ("سفارشات CRM", ("کد پیگیری", "نام گروه", "تاریخ شروع", "نام مشتری"), import_crm_orders),
    ("متخصصین (شیت)", ("نام و نام خانوادگی", "نوع نیرو", "محل اقامت", "تعداد کل کار"), import_sheet_specialists),
    ("نظرسنجی (شیت)", ("کارشناس تماس", "نام نیرو", "نتیجه تماس", "میانگین امتیاز"), import_sheet_surveys),
    ("شکایات از متخصصین (شیت)", ("علت شکایت مشتری", "نام نیرو"), import_sheet_complaints),
    ("شکایات متخصصان (شیت)", ("علت شکایت متخصص", "نام نیرو"), import_sheet_pro_complaints),
]
# Specialists must exist before surveys/complaints can be linked to them.
ORDER = ["گزارش مشتریان CRM", "نیروهای CRM", "متخصصین (شیت)", "سفارشات CRM", "نظرسنجی (شیت)", "شکایات از متخصصین (شیت)", "شکایات متخصصان (شیت)"]


def import_workbook(data: bytes) -> list[Result]:
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    found = []
    for ws in wb.worksheets:
        t = Table(ws)
        for label, needed, fn in IMPORTERS:
            if t.has(*needed):
                found.append((ORDER.index(label), label, ws.title, t, fn))
                break
    results = []
    for _, label, sheet, t, fn in sorted(found, key=lambda x: x[0]):
        res = Result(kind=label, sheet=sheet)
        try:
            with transaction.atomic():
                fn(t, res)
        except Exception as e:  # report the failing sheet and continue with the others
            res.errors.append(str(e)[:300])
        results.append(res)
    return results
