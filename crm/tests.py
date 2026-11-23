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
from crm.models import (Availability, Call, ChecklistItem, Complaint, Customer, DailyCheck, FollowUpTask, LostCustomer, Order,
                        Penalty, PenaltyType, RecurringTask, Rules, ServiceType, Specialist, SpecialistComplaint, Survey,
                        normalize_phone)


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
            {"item": c.title, "done": [True, False, "partial"][i % 3], "evidence": "", "note": ""} for i, c in enumerate(ChecklistItem.call_items("survey"))],
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
        self.assertEqual(call.checklist_score, Decimal("55.0"))  # 3 done + 3 partial of 9, + automatic banned-words check passed
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
        # default tiers: home VIP, first order, churned, ..., home normal (then earliest start)
        self.assertEqual(q[0], o_first)
        self.assertEqual([q[1], q[2]], [o_plain, o_loyal])
        vip = ServiceType.objects.get(name="نظافت ویژه منزل (VIP)")
        o_vip = Order.objects.create(customer=loyal, service=vip, start=start + dt.timedelta(hours=10))
        churn = Customer.objects.create(name="چرن", phone="09123333333", legacy_order_count=5,
                                        legacy_first_order=dt.date(2024, 1, 1), legacy_last_order=dt.date(2025, 1, 1))
        o_churn = Order.objects.create(customer=churn, service=self.home, start=start + dt.timedelta(hours=11))
        q = rules.dispatch_queue()
        self.assertEqual(q[0], o_vip)
        self.assertEqual(q[1], o_first)
        self.assertEqual(q[2], o_churn)
        self.assertEqual(q[2].tier_label, "مشتری چرن‌شده / ناراضی قبلی")
        r = Rules.get()
        r.dispatch_tiers = "first_order,home_vip"
        r.save()
        self.assertEqual(rules.dispatch_queue()[0], o_first)
        self.assertEqual(rules.dispatch_queue()[1], o_vip)
        o_vip.delete()
        o_churn.delete()
        self.cust.blocked_by_specialists.add(self.sp2)
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
        # customer cancelled after assignment counts as a worked day; worker fault does not
        t = timezone.make_aware(dt.datetime.combine(sat + dt.timedelta(days=4), dt.time(9)))
        Order.objects.create(customer=self.cust, service=self.home, start=t, status=Order.CANCELLED, specialist=self.sp2,
                             cancelled_after_assign=True)
        t = timezone.make_aware(dt.datetime.combine(sat + dt.timedelta(days=5), dt.time(9)))
        Order.objects.create(customer=self.cust, service=self.home, start=t, status=Order.CANCELLED, specialist=self.sp2,
                             worker_fault=True)
        rows = {r["specialist"]: r for r in rules.weekly_commission(sat)}
        self.assertEqual(rows[self.sp2]["day_count"], 5)
        self.assertTrue(rows[self.sp]["eligible"])
        self.assertEqual(len(rows[self.sp]["bonus_days"]), 2)
        self.assertEqual(rows[self.sp]["saving_rial"], 2 * 1_000_000)  # 20% of 10M halved, twice
        self.assertFalse(rows[self.sp2]["eligible"])
        self.assertEqual(rows[self.sp2]["needs"], 1)
        # fixed 8-hour periodic orders keep the normal commission
        Order.objects.filter(specialist=self.sp, start__date=sat + dt.timedelta(days=6)).update(is_periodic=True, hours=8)
        rows = {r["specialist"]: r for r in rules.weekly_commission(sat)}
        self.assertEqual(len(rows[self.sp]["bonus_orders"]), 1)
        self.assertEqual(rows[self.sp]["excluded_fixed"], 1)
        g = rules.thursday_groups(sat)
        self.assertIn(self.sp, [x[0] for x in g["both"]])
        self.assertIn(self.sp2, [x[0] for x in g["friday"]] + [x[0] for x in g["if_thursday"]] + [x[0] for x in g["both"]])

    def test_pages_render(self):
        Order.objects.create(customer=self.cust, service=self.home, start=timezone.now() + dt.timedelta(hours=5))
        for name in ["dashboard", "call_list", "call_upload", "order_new", "dispatch", "survey_list", "survey_new",
                     "complaint_list", "pro_complaint_list", "ticket_list", "ticket_new", "report_commission",
                     "report_capacity", "report_agents", "report_specialists", "task_list", "import", "availability",
                     "daily_lists", "lost_list", "penalty_list", "complaint_new", "report_vip", "report_top", "settings"]:
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


class ChecklistScopeTests(TestCase):
    def setUp(self):
        call_command("seed_defaults", verbosity=0)

    def test_final_dialogue_only_for_order_calls(self):
        from crm.analysis import build_prompt
        from crm.defaults import FINAL_GROUP
        order_call = Call(kind="order", transcript="x")
        survey_call = Call(kind="survey", transcript="x")
        self.assertIn(FINAL_GROUP, build_prompt(order_call))
        self.assertIn("تسویه هزینه با متخصص", build_prompt(order_call))
        self.assertNotIn(FINAL_GROUP, build_prompt(survey_call))
        self.assertEqual(len(ChecklistItem.call_items("order")), 21)
        self.assertEqual(len(ChecklistItem.call_items("survey")), 9)
        self.assertNotIn("سلام، معرفی خود و شرکت", [c.title for c in ChecklistItem.call_items("order")])

    def test_seed_is_idempotent(self):
        before = (ChecklistItem.objects.count(), PenaltyType.objects.count(), RecurringTask.objects.count())
        call_command("seed_defaults", verbosity=0)
        self.assertEqual(before, (ChecklistItem.objects.count(), PenaltyType.objects.count(), RecurringTask.objects.count()))
        self.assertGreaterEqual(before[1], 17)
        self.assertGreaterEqual(before[2], 25)



@override_settings(STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
                             "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})
class PhaseATests(TestCase):
    def setUp(self):
        call_command("seed_defaults", verbosity=0)
        self.user = User.objects.create_superuser("boss", password="pass-pass-123")
        self.client.force_login(self.user)
        self.home = ServiceType.objects.get(name="نظافت منزل")
        self.cust = Customer.objects.create(name="مشتری", phone="09120000009", address="تهران", source="سایت")
        self.sp = Specialist.objects.create(name="نیرو", phone="09120000001", gender="f", does_home=True, score=Decimal("4.8"))

    def test_banned_words_found_only_in_agent_lines(self):
        from crm.analysis import find_banned
        t = "کارشناس: سلام، آره حتماً، فقط اون روز نمیشه.\nطرف مقابل: اوکی ممنون"
        words = [h["word"] for h in find_banned(t, Rules.get().banned_list())]
        self.assertIn("آره", words)
        self.assertIn("نمیشه", words)
        self.assertNotIn("اوکی", words)

    def test_order_restrictions_block_and_override(self):
        self.cust.debt = 300000
        self.cust.save()
        data = {"customer_name": "مشتری", "customer_phone": "09120000009", "service": self.home.pk, "start": "1405/09/10 09:00",
                "hours": "4", "gender_needed": "", "address": "تهران"}
        self.client.post(reverse("order_new"), data)
        self.assertEqual(Order.objects.count(), 0)
        self.client.post(reverse("order_new"), {**data, "override": "1"})
        self.assertEqual(Order.objects.count(), 1)
        self.cust.debt = 0
        self.cust.blocked_by_specialists.add(self.sp)
        self.cust.save()
        self.assertTrue(any("بلاک" in p for p in rules.order_restrictions(self.cust, self.sp)))
        self.assertEqual(rules.order_restrictions(self.cust, None), [])

    def test_vip_review_and_penalties(self):
        r = Rules.get()
        r.vip_min_count = 3
        r.save()
        for i in range(4):
            Survey.objects.create(customer=self.cust, specialist=self.sp, scores={"کیفیت کار": 5}, average=Decimal("4.9"),
                                  call_date=dt.date(2026, 11, 1 + i))
        self.assertTrue(rules.vip_status(self.sp)["promote"])
        Survey.objects.create(customer=self.cust, specialist=self.sp, average=Decimal("3.5"), call_date=dt.date(2026, 11, 9))
        self.assertFalse(rules.vip_status(self.sp)["eligible"])
        # late penalty: 15 min grace, then 100k per started half hour
        self.assertEqual(r.late_penalty(10), 0)
        self.assertEqual(r.late_penalty(25), 100000)
        self.assertEqual(r.late_penalty(45), 200000)
        o = Order.objects.create(customer=self.cust, service=self.home, start=timezone.now(), specialist=self.sp, late_minutes=50)
        late = ChecklistItem.objects.get(kind=ChecklistItem.FAULT, title="تأخیر (متخصص)")
        c = Complaint.objects.create(specialist=self.sp, order=o, reason=late, reason_text=late.title)
        self.client.post(reverse("complaint_detail", args=[c.pk]), {"fault": "confirmed", "status": "open", "resolution": ""})
        p = Penalty.objects.get(complaint=c)
        self.assertEqual(p.amount, 200000)
        self.assertEqual(p.status, "proposed")
        self.client.post(reverse("penalty_list"), {"id": p.pk, "status": "approved", "amount": "200000", "note": ""})
        p.refresh_from_db()
        self.assertEqual(p.status, "approved")

    def test_coverage_top_and_lost(self):
        today = timezone.localdate()
        Order.objects.create(customer=self.cust, service=self.home, start=timezone.now(), specialist=self.sp, status=Order.DONE,
                             requested_specific=True)
        Order.objects.create(customer=self.cust, service=self.home, start=timezone.now(), specialist=self.sp, status=Order.DONE)
        self.client.post(reverse("lost_list"), {"date": "", "name": "x", "phone": "09121112222", "reason": "capacity"})
        self.assertEqual(LostCustomer.objects.count(), 1)
        cov = rules.coverage_rate(rules.month_start(today), today)
        self.assertEqual((cov["done"], cov["lost"], cov["rate"]), (2, 1, 66.7))
        top = rules.top_specialists(rules.month_start(today))
        self.assertEqual(top[0]["specialist"], self.sp)
        self.assertEqual((top[0]["free"], top[0]["fav"], top[0]["prize"]), (1, 1, 5000000))

    def test_settings_tabs_and_dispatch_order(self):
        for tab in ["rules", "dispatch", "call", "survey", "fault", "penalties", "tasks", "canned", "services", "users"]:
            self.assertEqual(self.client.get(reverse("settings") + f"?tab={tab}").status_code, 200, tab)
        tiers = ["first_order", "home_vip", "churned", "work_vip", "common_vip", "home", "work", "common", "other"]
        self.client.post(reverse("settings") + "?tab=dispatch", {f"t{i}": t for i, t in enumerate(tiers)})
        self.assertEqual(Rules.get().tiers()[0], "first_order")
        bad = {f"t{i}": "home" for i in range(9)}
        self.client.post(reverse("settings") + "?tab=dispatch", bad)
        self.assertEqual(Rules.get().tiers()[0], "first_order")

    def test_daily_board_checks(self):
        o = Order.objects.create(customer=self.cust, service=self.home, start=timezone.now(), specialist=self.sp, status=Order.ASSIGNED)
        self.client.post(reverse("daily_lists"), {"check_kind": "morning", "order": o.pk, "result": "late", "late": "40"})
        self.assertEqual(DailyCheck.objects.get(order=o).result, "late")
        o.refresh_from_db()
        self.assertEqual(o.late_minutes, 40)
        task = RecurringTask.objects.first()
        self.client.post(reverse("daily_lists"), {"task": task.pk})
        self.assertTrue(task.done.filter(user=self.user).exists())
        p = Order.objects.create(customer=self.cust, service=self.home, start=timezone.now() + dt.timedelta(days=1),
                                 specialist=self.sp, is_periodic=True)
        self.client.post(reverse("daily_lists"), {"check_kind": "periodic", "order": p.pk, "result": "confirmed",
                                                  "specialist_ok": "no", "note": "دیر می‌آید"})
        self.assertTrue(Complaint.objects.filter(order=p, specialist=self.sp).exists())

    def test_unhappy_and_periodic_calls_create_records(self):
        from crm.analysis import apply_analysis
        o = Order.objects.create(customer=self.cust, service=self.home, start=timezone.now(), specialist=self.sp, is_periodic=True)
        c1 = Call.objects.create(kind="unhappy", customer=self.cust, specialist=self.sp, order=o, transcript="کارشناس: سلام")
        apply_analysis(c1, {"checklist": [], "extracted": {"complaint_summary": "دیر رسید", "specialist_faults": ["تأخیر (متخصص)"]}})
        comp = c1.unhappy_complaints.get()
        self.assertEqual(comp.reason.title, "تأخیر (متخصص)")
        self.assertEqual(comp.status, "open")
        c2 = Call.objects.create(kind="periodic", customer=self.cust, order=o, transcript="کارشناس: سلام")
        apply_analysis(c2, {"checklist": [], "extracted": {"result": "reschedule", "specialist_ok": "yes", "note": "ساعت ۱۰"}})
        self.assertEqual(DailyCheck.objects.get(kind="periodic", order=o).result, "reschedule")
        c3 = Call.objects.create(kind="order", customer=self.cust, transcript="کارشناس: حله")
        apply_analysis(c3, {"checklist": [{"item": "x", "done": True}], "extracted": {"conditions_rejected": ["آماده بودن وسایل نظافتی"]}})
        self.assertEqual(c3.analysis["banned"][0]["word"], "حله")
        self.assertTrue(any("نپذیرفته" in p for p in rules.order_restrictions(self.cust, None, c3)))


@override_settings(OPENROUTER_API_KEY="test-key", MEDIA_ROOT=tempfile.mkdtemp())
class AutoKindTests(TestCase):
    def test_auto_kind_is_classified_then_analyzed(self):
        call_command("seed_defaults", verbosity=0)
        call = Call.objects.create(kind="auto", audio=SimpleUploadedFile("a.ogg", tiny_audio(), "audio/ogg"))
        answers = iter(["کارشناس: سلام، برای نظرسنجی تماس گرفتم", '{"kind": "survey", "reason": "نظرسنجی"}',
                        '{"summary": "x", "checklist": [], "tone": {"score": 4}, "advice": {}, "extracted": {"answered": false}}'])
        with mock.patch("crm.ai.requests.post", side_effect=lambda *a, **k: FakeResp(next(answers))):
            call_command("run_worker", once=True)
        call.refresh_from_db()
        self.assertEqual((call.kind, call.status), ("survey", "done"))
        self.assertIn("خودکار", call.note)


@override_settings(OPENROUTER_API_KEY="test-key", MEDIA_ROOT=tempfile.mkdtemp(),
                   STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
                             "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})
class CostTests(TestCase):
    def test_usage_logged_per_step_and_thinking_off_for_voice(self):
        call_command("seed_defaults", verbosity=0)
        call = Call.objects.create(kind="auto", audio=SimpleUploadedFile("a.ogg", tiny_audio(), "audio/ogg"))
        answers = iter(["کارشناس: سلام", '{"kind": "other", "reason": "-"}', '{"summary": "x", "checklist": []}'])
        bodies = []

        def post(url, json=None, **kw):
            bodies.append(json)
            return FakeResp(next(answers))
        with mock.patch("crm.ai.requests.post", side_effect=post):
            call_command("run_worker", once=True)
        call.refresh_from_db()
        self.assertEqual([u["step"] for u in call.usage], ["transcribe", "classify", "analyze"])
        self.assertEqual(bodies[0]["reasoning"], {"enabled": False})
        self.assertIn("seconds", call.usage[0])

    def test_lab_page_and_compare(self):
        from crm import lab
        self.assertGreater(lab.similarity("کارشناس: قیمت ۱۲۰۰ هزار", "کارشناس: قیمت ۱۲۰۰ هزار تومان"), 80)
        self.assertEqual(lab.numbers("پلاک ۱۰ و ۰۹۱۲"), {"10", "0912"})
        from django.contrib.auth.models import User
        u = User.objects.create_superuser("boss", "", "x")
        self.client.force_login(u)
        call = Call.objects.create(kind="order", transcript="کارشناس: قیمت ۱۶۰۰", status="done",
                                   usage=[{"step": "transcribe", "cost": 0.01, "seconds": 60}])
        lab.save({str(call.pk): {"lite": {"text": "کارشناس: قیمت ۱۶۰۰", "cost": 0.003, "usage": {"seconds": 60}}}})
        r = self.client.get(reverse("lab"))
        self.assertContains(r, "Flash-Lite")
        self.assertContains(r, "100٪")
        r = self.client.post(reverse("lab"), {"calls": str(call.pk), "action": "reprocess"})
        call.refresh_from_db()
        self.assertEqual(call.status, "done")  # no audio, so not reprocessed


class TranscriptShapeTests(TestCase):
    def test_label_alone_on_its_line_is_joined(self):
        from crm.ai import normalize_transcript
        from crm.analysis import find_banned
        raw = "سیستم:\nمکالمات شما ضبط می‌گردد.\nکارشناس:\nآره حله.\nادامه‌ی حرف\n**طرف مقابل:** باشه"
        t = normalize_transcript(raw)
        self.assertEqual(t.splitlines(), ["سیستم: مکالمات شما ضبط می‌گردد.", "کارشناس: آره حله. ادامه‌ی حرف",
                                          "طرف مقابل: باشه"])
        self.assertEqual({h["word"] for h in find_banned(t, ["آره", "حله", "باشه"])}, {"آره", "حله"})
