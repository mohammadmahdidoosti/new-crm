"""End-to-end tests with OpenRouter mocked. Run: DEBUG=1 python manage.py test crm"""
import datetime as dt
import json
import subprocess
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from crm import rules
from crm.jalali import fmt_date, parse_date, parse_datetime
from crm.models import (Availability, Call, ChecklistItem, Complaint, Customer, FollowUpTask, Order, Rules, ServiceType,
                        Specialist, SpecialistComplaint, Survey, normalize_phone)


def tiny_audio() -> bytes:
    with tempfile.TemporaryDirectory() as t:
        out = Path(t) / "a.ogg"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                        "-c:a", "libopus", str(out)], check=True)
        return out.read_bytes()


class FakeResp:
    def __init__(self, content):
        self._c = content

    def json(self):
        return {"choices": [{"message": {"content": self._c}}], "usage": {"cost": 0.0012}}


def fake_post_factory(analysis: dict):
    def fake_post(url, json=None, **kw):
        content = json["messages"][-1]["content"]
        if isinstance(content, list):  # audio -> transcript
            assert content[1]["type"] == "input_audio"
            return FakeResp("کارشناس: سلام، سرویک بفرمایید.\nطرف مقابل: سلام، برای نظرسنجی تماس گرفتید؟")
        return FakeResp(__import__("json").dumps(analysis, ensure_ascii=False))
    return fake_post


@override_settings(OPENROUTER_API_KEY="test-key", MEDIA_ROOT=tempfile.mkdtemp(),
                   STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
                             "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})
class FlowTests(TestCase):
    def setUp(self):
        call_command("seed_defaults", verbosity=0)
        self.user = User.objects.create_superuser("boss", password="pass-pass-123")
        self.client.force_login(self.user)
        self.sp = Specialist.objects.create(name="نیروی تست", phone="9120000001", gender="f", does_home=True, score=Decimal("4.8"),
                                            worker_type="vip_all")
        self.sp2 = Specialist.objects.create(name="نیروی دوم", phone="09120000002", gender="f", does_home=True, score=Decimal("4.2"))
        self.cust = Customer.objects.create(name="مشتری تست", phone="+98 912 000 0009")
        self.home = ServiceType.objects.get(name="نظافت منزل")

    def test_phone_and_dates(self):
        self.assertEqual(self.cust.phone, "09120000009")
        self.assertEqual(normalize_phone(9121234567.0), "09121234567")
        self.assertEqual(normalize_phone("۰۹۱۲۱۲۳۴۵۶۷"), "09121234567")
        self.assertEqual(parse_date("1405/8/29"), dt.date(2026, 11, 20))
        self.assertEqual(parse_date(dt.datetime(1404, 7, 13)), dt.date(2025, 10, 5))
        self.assertEqual(fmt_date(dt.date(2026, 11, 20)), "1405/08/29")
        self.assertEqual(timezone.localtime(parse_datetime("1405/9/2 9:0:0")).hour, 9)

    def test_survey_voice_to_complaint(self):
        faults = ChecklistItem.of(ChecklistItem.FAULT)
        crit = [c.title for c in ChecklistItem.of(ChecklistItem.SURVEY)]
        analysis = {"summary": "مشتری از تأخیر ناراضی بود", "checklist": [
            {"item": c.title, "done": [True, False, "partial"][i % 3], "evidence": "", "note": ""} for i, c in enumerate(ChecklistItem.of(ChecklistItem.CALL))],
            "tone": {"score": 4, "note": "مؤدب", "issues": []}, "advice": {"score": None, "note": ""},
            "extracted": {"answered": True, "scores": {crit[0]: 5, crit[1]: 2, crit[2]: 3}, "result": "unhappy",
                          "comment": "نیرو دیر رسید", "specialist_faults": [faults[2].title]}}
        r = self.client.post(reverse("call_upload"), {
            "kind": "survey", "files": SimpleUploadedFile("a.ogg", tiny_audio(), "audio/ogg"),
            "customer_phone": "09120000009", "specialist_phone": "09120000001"})
        call = Call.objects.get()
        self.assertRedirects(r, reverse("call_detail", args=[call.pk]))
        with mock.patch("crm.ai.requests.post", side_effect=fake_post_factory(analysis)):
            call_command("run_worker", once=True)
        call.refresh_from_db()
        self.assertEqual(call.status, "done", call.error)
        self.assertIn("سرویک", call.transcript)
        self.assertEqual(call.checklist_score, Decimal("57.1"))  # 3 done + 2 partial of 7
        self.assertEqual(call.tone_score, 4)
        s = Survey.objects.get(call=call)
        self.assertFalse(s.confirmed)
        self.assertEqual(s.specialist, self.sp)
        page = self.client.get(reverse("call_detail", args=[call.pk]))
        self.assertContains(page, "منتظر تأیید")
        data = {"action": "confirm_survey", "result": "unhappy", "notes": "دیر رسید", "fault": faults[2].pk}
        for c in ChecklistItem.of(ChecklistItem.SURVEY)[:3]:
            data[f"c_{c.pk}"] = s.scores[c.title]
        self.client.post(reverse("call_action", args=[call.pk]), data)
        s.refresh_from_db()
        self.assertTrue(s.confirmed)
        comp = Complaint.objects.get(survey=s)
        self.assertEqual(comp.reason, faults[2])
        self.assertTrue(comp.auto_created)
        self.assertTrue(FollowUpTask.objects.filter(complaint=comp, done=False).exists())
        # recall call closes the loop
        recall = {"summary": "دلجویی شد", "checklist": [], "tone": {"score": 5}, "advice": {"score": 4},
                  "extracted": {"resolved": True, "outcome": "کد تخفیف ارسال شد", "compensation": "کد تخفیف ۱۰۰ هزار تومانی"}}
        self.client.post(reverse("call_upload") + f"?complaint={comp.pk}", {
            "kind": "recall", "files": SimpleUploadedFile("b.ogg", tiny_audio(), "audio/ogg")})
        rc = Call.objects.get(kind="recall")
        self.assertEqual(rc.related_complaint, comp)
        with mock.patch("crm.ai.requests.post", side_effect=fake_post_factory(recall)):
            call_command("run_worker", once=True)
        self.client.post(reverse("call_action", args=[rc.pk]), {"action": "apply_recall", "outcome": "کد تخفیف ارسال شد", "resolved": "1"})
        comp.refresh_from_db()
        self.assertEqual(comp.status, "resolved")
        self.assertFalse(FollowUpTask.objects.filter(complaint=comp, done=False).exists())

    def test_good_survey_makes_no_complaint(self):
        s = Survey.objects.create(customer=self.cust, specialist=self.sp, scores={"کیفیت کار": 5, "سرعت عمل": 4})
        self.assertIsNone(rules.process_survey(s))
        s2 = Survey.objects.create(customer=self.cust, scores={}, result="no_answer")
        self.assertIsNone(rules.process_survey(s2))

    def test_pro_complaint_and_failure_retry(self):
        analysis = {"summary": "", "checklist": [], "tone": {"score": 3}, "advice": {},
                    "extracted": {"reason": "عدم پرداخت هزینه (مشتری)", "description": "مشتری پول نداد", "amount": 1500000}}
        call = Call.objects.create(kind="pro_complaint", agent=self.user, specialist=self.sp,
                                   audio=SimpleUploadedFile("c.ogg", tiny_audio(), "audio/ogg"))

        def boom(*a, **kw):
            raise ConnectionError("network down")
        with mock.patch("crm.ai.requests.post", side_effect=boom), mock.patch("crm.management.commands.run_worker.time.sleep"):
            call_command("run_worker", once=True)
        call.refresh_from_db()
        self.assertEqual(call.status, "queued")
        self.assertIn("network down", call.error)
        with mock.patch("crm.ai.requests.post", side_effect=fake_post_factory(analysis)):
            call_command("run_worker", once=True)
        pc = SpecialistComplaint.objects.get(call=call)
        self.assertFalse(pc.confirmed)
        self.assertEqual(pc.amount, 1500000)
        self.assertEqual(pc.reason.title, "عدم پرداخت هزینه (مشتری)")

    def test_dispatch_priority_and_suggestions(self):
        start = timezone.now() + dt.timedelta(hours=20)
        loyal = Customer.objects.create(name="وفادار", phone="09121111111", legacy_order_count=12,
                                        legacy_first_order=dt.date(2024, 1, 1), legacy_last_order=timezone.localdate())
        o_first = Order.objects.create(customer=self.cust, service=self.home, start=start + dt.timedelta(hours=2), gender_needed="f")
        o_loyal = Order.objects.create(customer=loyal, service=self.home, start=start, gender_needed="f")
        o_plain_cust = Customer.objects.create(name="عادی", phone="09122222222", legacy_order_count=1,
                                               legacy_first_order=dt.date(2024, 1, 1), legacy_last_order=dt.date(2024, 1, 1))
        o_plain = Order.objects.create(customer=o_plain_cust, service=self.home, start=start - dt.timedelta(hours=3))
        q = rules.dispatch_queue()
        self.assertEqual(q[0], o_first)
        self.assertEqual(q[1], o_loyal)
        self.assertEqual(q[-1], o_plain)
        self.cust.blocked_specialists.add(self.sp2)
        names = [s["specialist"] for s in rules.suggest_specialists(o_first)]
        self.assertEqual(names, [self.sp])
        Availability.objects.create(specialist=self.sp, date=timezone.localtime(o_loyal.start).date(), state="leave")
        self.assertNotIn(self.sp, [s["specialist"] for s in rules.suggest_specialists(o_loyal)])
        r = self.client.post(reverse("assign", args=[o_first.pk]), {"specialist": self.sp.pk})
        o_first.refresh_from_db()
        self.assertEqual(o_first.specialist, self.sp)
        self.assertEqual(o_first.status, Order.ASSIGNED)

    def test_weekly_commission(self):
        sat = rules.week_start(dt.date(2026, 11, 20))
        self.assertEqual(sat, dt.date(2026, 11, 14))
        self.assertEqual(sat.weekday(), 5)
        for i in range(7):
            t = timezone.make_aware(dt.datetime.combine(sat + dt.timedelta(days=i), dt.time(9)))
            Order.objects.create(customer=self.cust, service=self.home, start=t, status=Order.DONE, specialist=self.sp,
                                 price=10_000_000)
        for i in range(4):
            t = timezone.make_aware(dt.datetime.combine(sat + dt.timedelta(days=i), dt.time(9)))
            Order.objects.create(customer=self.cust, service=self.home, start=t, status=Order.DONE, specialist=self.sp2, price=1)
        rows = {r["specialist"]: r for r in rules.weekly_commission(sat)}
        self.assertTrue(rows[self.sp]["eligible"])
        self.assertEqual(len(rows[self.sp]["bonus_days"]), 2)
        self.assertEqual(rows[self.sp]["saving_rial"], 2 * 1_000_000)  # 20% of 10M halved, twice
        self.assertFalse(rows[self.sp2]["eligible"])
        self.assertEqual(rows[self.sp2]["needs"], 2)

    def test_pages_render(self):
        Order.objects.create(customer=self.cust, service=self.home, start=timezone.now() + dt.timedelta(hours=5))
        for name in ["dashboard", "call_list", "call_upload", "order_new", "dispatch", "survey_list", "survey_new",
                     "complaint_list", "pro_complaint_list", "ticket_list", "ticket_new", "report_commission",
                     "report_capacity", "report_agents", "report_specialists", "task_list", "import", "availability"]:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        self.assertEqual(self.client.get("/admin/crm/rules/").status_code, 200)

    def test_tickets(self):
        r = self.client.post(reverse("ticket_new"), {"category": "dispatch", "title": "درخواست کار", "specialist_phone": "09120000001",
                                                      "text": "سلام کار دارید؟"})
        from crm.models import Ticket
        t = Ticket.objects.get()
        with mock.patch("crm.ai.requests.post", side_effect=lambda *a, **k: FakeResp('{"ok": false, "issue": "لحن سرد"}')):
            self.client.post(reverse("ticket_detail", args=[t.pk]), {"action": "reply", "text": "نه"})
        t.refresh_from_db()
        self.assertEqual(t.status, "waiting_pro")
        self.assertIsNotNone(t.first_response_at)
        self.assertEqual(t.messages.last().tone_flag, "لحن سرد")

    def test_ingest_api(self):
        url = reverse("api_ingest_call")
        f = SimpleUploadedFile("x.ogg", tiny_audio(), "audio/ogg")
        self.assertEqual(self.client.post(url, {"audio": f}).status_code, 401)
        with override_settings(INGEST_TOKEN="secret-token"):
            f = SimpleUploadedFile("x.ogg", tiny_audio(), "audio/ogg")
            r = self.client.post(url, {"audio": f, "kind": "order", "customer_phone": "09120000009"},
                                 HTTP_AUTHORIZATION="Bearer secret-token")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Call.objects.get(pk=r.json()["id"]).customer, self.cust)

    def test_audio_requires_login(self):
        call = Call.objects.create(kind="order", audio=SimpleUploadedFile("c.ogg", b"x", "audio/ogg"))
        self.client.logout()
        self.assertEqual(self.client.get(reverse("call_audio", args=[call.pk])).status_code, 302)
