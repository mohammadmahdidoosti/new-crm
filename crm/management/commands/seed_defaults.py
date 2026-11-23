"""Create default rules, lists and service types (idempotent). Defaults are placeholders to confirm with the team."""
from django.core.management.base import BaseCommand

from crm.defaults import FINAL_DIALOGUE, FINAL_GROUP, GENERAL_GROUP
from crm import defaults_v2 as V2
from crm.models import CannedResponse, ChecklistItem, PenaltyType, RecurringTask, Rules, ServiceType

CALL = [
    ("سلام، معرفی خود و شرکت", "کارشناس در ابتدای تماس سلام می‌کند و خود و سرویک را معرفی می‌کند."),
    ("ثبت دقیق اطلاعات مشتری", "نام، شماره تماس و آدرس را می‌پرسد یا تأیید می‌کند."),
    ("نیازسنجی کامل", "نوع خدمت، متراژ/ساعت، حیوان خانگی، ترجیح جنسیت نیرو و نکات خاص را می‌پرسد."),
    ("اعلام شفاف قیمت و شرایط", "قیمت، مدت، شرایط لغو، تجدید نظافت و اینکه وسایل نظافت با مشتری است را روشن می‌گوید."),
    ("مشاوره و پیشنهاد مناسب", "بر اساس نیاز مشتری خدمت، ساعت یا نیروی مناسب (VIP، نیروی مورد علاقه) را پیشنهاد می‌دهد."),
    ("تکرار و تأیید نهایی", "در پایان تاریخ، ساعت، آدرس و خدمت را تکرار و تأیید می‌کند."),
    ("پایان محترمانه", "می‌پرسد سؤال دیگری هست یا نه و محترمانه خداحافظی می‌کند."),
]
FAULTS = ["کیفیت پایین (متخصص)", "رفتار نامناسب (متخصص)", "تأخیر (متخصص)", "تعجیل (متخصص)", "سرعت عمل پایین(متخصص)",
          "آراستگی ظاهری (متخصص)", "غیبت (متخصص)"]
PRO = ["رفتار نامناسب (مشتری)", "عدم وسایل نظافتی مناسب (مشتری)", "ثبت اشتباه سفارش (عملیات)", "عدم حضور (مشتری)",
       "عدم پرداخت هزینه (مشتری)", "مشکلات شخصی متخصصین", "اضافه کار اجباری (مشتری)", "عدم پاسخگویی و حل مشکل (عملیات)",
       "رفتار نامناسب (عملیات)"]
SURVEY = ["نظافت نیرو", "سرعت عمل", "کیفیت کار", "ادب و رفتار نیرو", "روند ثبت سفارش و پاسخگویی", "امتیاز کارشناس به نیرو",
          "پوشش متخصص"]
SERVICES = [("نظافت منزل", "home", False), ("نظافت ویژه منزل (VIP)", "home", True), ("نظافت محل کار", "work", False),
            ("نظافت ویژه محل کار (VIP)", "work", True), ("نظافت محل کار (فاکتور رسمی)", "work", False),
            ("مشاعات ساعتی (راه پله)", "common", False), ("نظافت ویژه مشاعات ساعتی (VIP)", "common", True),
            ("اعزام نیروی اثاث کش", "other", False), ("پذیرایی و اعزام مهمان دار", "other", False)]


class Command(BaseCommand):
    help = "Seed default rules and lists"

    def handle(self, *args, **opts):
        Rules.get()
        n = 0
        for kind, items in ((ChecklistItem.CALL, CALL), (ChecklistItem.FAULT, [(t, "") for t in FAULTS]),
                            (ChecklistItem.PRO_COMPLAINT, [(t, "") for t in PRO]), (ChecklistItem.SURVEY, [(t, "") for t in SURVEY])):
            if ChecklistItem.objects.filter(kind=kind).exists():
                continue
            for i, (title, guide) in enumerate(items):
                ChecklistItem.objects.create(kind=kind, title=title, guide=guide, order=i)
                n += 1
        if not ChecklistItem.objects.filter(kind=ChecklistItem.CALL, group=FINAL_GROUP).exists():
            ChecklistItem.objects.filter(kind=ChecklistItem.CALL, group="").update(group=GENERAL_GROUP)
            for i, (title, guide) in enumerate(FINAL_DIALOGUE):
                ChecklistItem.objects.create(kind=ChecklistItem.CALL, title=title, guide=guide, group=FINAL_GROUP,
                                             call_kinds="order", order=20 + i)
                n += 1
        for name, cat, vip in SERVICES:
            ServiceType.objects.get_or_create(name=name, defaults={"category": cat, "is_vip": vip})
        if not CannedResponse.objects.exists():
            CannedResponse.objects.create(category="امور پشتیبانی و پخش کار", title="نمونه: درخواست کار",
                                          text="سلام، درخواستتان ثبت شد. به محض وجود کار مناسب در محدوده‌ی شما، در اپلیکیشن برایتان ارسال می‌شود.")
            CannedResponse.objects.create(category="پشتیبانی اپلیکیشن", title="نمونه: ثبت شروع و پایان کار",
                                          text="سلام، برای ثبت شروع و پایان کار، وارد بخش «کارهای من» شوید و روی سفارش امروز دکمه‌ی شروع/پایان را بزنید.")
        rules = Rules.get()
        if rules.seed_version < 2:
            n += self.seed_v2()
            rules.seed_version = 2
            rules.save(update_fields=["seed_version"])
        self.stdout.write(self.style.SUCCESS(f"defaults ready ({n} items added)"))

    def seed_v2(self) -> int:
        """Checklists per call type, penalties and recurring duties from Serviq's instructions."""
        n = 0
        CI = ChecklistItem
        CI.objects.filter(kind=CI.CALL, title__in=V2.V1_TO_ORDER).update(group=V2.ORDER, call_kinds="order")
        CI.objects.filter(kind=CI.CALL, title__in=V2.V1_RETIRE).update(active=False)
        CI.objects.filter(kind=CI.CALL, title=V2.MEAL_OLD).update(title=V2.MEAL_TITLE, guide=V2.MEAL_GUIDE)
        for i, (group, kinds, title, guide) in enumerate(V2.CHECKLISTS):
            _, created = CI.objects.get_or_create(kind=CI.CALL, title=title,
                                                  defaults={"group": group, "call_kinds": kinds, "guide": guide, "order": i})
            n += created
        for i, (title, amount, unit, note, faults) in enumerate(V2.PENALTIES):
            _, created = PenaltyType.objects.get_or_create(title=title, defaults={"amount": amount, "unit": unit, "note": note,
                                                                                 "fault_titles": faults, "order": i})
            n += created
        for i, (title, place, days, link) in enumerate(V2.TASKS):
            _, created = RecurringTask.objects.get_or_create(title=title, defaults={"place": place, "weekdays": days,
                                                                                   "link": link, "order": i})
            n += created
        return n
