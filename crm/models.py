"""Data model of Serviq CRM.

One place for what today lives in the old CRM, the HCM and the operations sheets: customers, specialists
(cleaning staff), orders, calls (voices) and their analysis, surveys, complaints, tickets and daily availability.
"""
from __future__ import annotations

import re

from django.conf import settings
from django.db import models
from django.utils import timezone

PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize_phone(value) -> str:
    """'9121234567', '۰۹۱۲...', '+98 912...', 9121234567.0 -> '09121234567'. Landlines are kept as digits."""
    if value is None:
        return ""
    s = str(value).translate(PERSIAN_DIGITS).strip()
    if s.endswith(".0"):
        s = s[:-2]
    s = re.sub(r"\D", "", s)
    if s.startswith("0098"):
        s = "0" + s[4:]
    elif s.startswith("98") and len(s) == 12:
        s = "0" + s[2:]
    if len(s) == 10 and s.startswith("9"):
        s = "0" + s
    return s


class TimeStamped(models.Model):
    created_at = models.DateTimeField("ایجاد", auto_now_add=True)
    updated_at = models.DateTimeField("به‌روزرسانی", auto_now=True)

    class Meta:
        abstract = True


# ---------------------------------------------------------------- settings & reference data

class Rules(models.Model):
    """Business rules, editable from admin. Defaults are placeholders to be confirmed in the deployment meeting."""
    complaint_avg_below = models.DecimalField("شکایت اگر میانگین نظرسنجی کمتر از", max_digits=3, decimal_places=2, default=4)
    complaint_item_at_most = models.PositiveSmallIntegerField("شکایت اگر یکی از معیارها حداکثر", default=2)
    commission_full_days = models.PositiveSmallIntegerField("روزهای کاری لازم در هفته برای نیم‌بها", default=5)
    commission_bonus_from_day = models.PositiveSmallIntegerField("نیم‌بها از روز کاری شماره", default=6)
    commission_bonus_factor = models.DecimalField("ضریب پورسانت روزهای جایزه", max_digits=3, decimal_places=2, default=0.5,
                                                  help_text="۰٫۵ یعنی پورسانت نصف می‌شود")
    commission_rate_app = models.DecimalField("درصد پورسانت (پایان کار از اپ)", max_digits=4, decimal_places=1, default=20)
    commission_rate_staff = models.DecimalField("درصد پورسانت (پایان کار توسط کارشناس)", max_digits=4, decimal_places=1, default=30)
    loyal_min_orders = models.PositiveSmallIntegerField("مشتری وفادار: حداقل تعداد سفارش انجام‌شده", default=3)
    loyal_window_days = models.PositiveSmallIntegerField("مشتری وفادار: در چند روز گذشته", default=180)
    ticket_sla_minutes = models.PositiveIntegerField("حداکثر زمان اولین پاسخ تیکت (دقیقه)", default=30)
    vip_min_score = models.DecimalField("حداقل امتیاز نیروی VIP", max_digits=3, decimal_places=2, default=4.7)
    capacity_shortage_hours = models.PositiveIntegerField("هشدار نیازمند نیرو اگر ساعت آزاد کمتر از", default=8)
    # dispatch (سند شاخص‌های اولویت‌بندی پخش سفارشات)
    dispatch_tiers = models.CharField("ترتیب اولویت پخش کار", max_length=300,
                                      default="home_vip,first_order,churned,work_vip,common_vip,home,work,common,other",
                                      help_text="کدها به ترتیب اولویت، با ویرگول")
    churn_inactive_days = models.PositiveIntegerField("مشتری چرن‌شده: روزهای بدون سفارش", default=60)
    churn_min_orders = models.PositiveSmallIntegerField("مشتری چرن‌شده: حداقل سفارش‌های قبلی", default=2)
    churn_on_unhappy = models.BooleanField("مشتری با نارضایتی در آخرین نظرسنجی هم چرن‌شده/در خطر حساب شود", default=True)
    first_order_min_score = models.DecimalField("حداقل امتیاز نیرو برای سفارش اول مشتری", max_digits=3, decimal_places=2, default=4.5)
    temp_order_cancel_hours = models.PositiveIntegerField("سفارش موقت: اگر تا چند ساعت قبل نیرو نداشت لغو شود", default=24)
    # VIP (سند دیالوگ متخصصین VIP)
    vip_window = models.PositiveSmallIntegerField("VIP: تعداد آخرین سفارش‌های نظرسنجی‌شده", default=100)
    vip_min_count = models.PositiveSmallIntegerField("VIP: حداقل تعداد نظرسنجی برای قضاوت", default=30)
    vip_avg_min = models.DecimalField("VIP: حداقل میانگین امتیاز", max_digits=3, decimal_places=2, default=4.75)
    vip_single_min = models.DecimalField("VIP: هیچ امتیازی کمتر از", max_digits=3, decimal_places=2, default=4)
    # commission (دستورالعمل نیم‌بها)
    commission_exclude_fixed_8h = models.BooleanField("سفارش‌های ثابت ۸ ساعته (دوره‌ای) مشمول نیم‌بها نیستند", default=True)
    commission_count_customer_cancel = models.BooleanField("لغو مشتری بعد از تخصیص، روز کاری نیرو حساب شود", default=True)
    # order dialogue
    meal_min_hours = models.DecimalField("وعده غذا از چند ساعت", max_digits=3, decimal_places=1, default=6)
    meal_inclusive = models.BooleanField("وعده غذا شامل خود همان ساعت هم می‌شود (۶ ساعت و بیشتر)", default=True)
    # discipline (دستورالعمل انضباطی / جدول جریمه‌ها)
    late_grace_minutes = models.PositiveSmallIntegerField("تأخیر مجاز (دقیقه)", default=15)
    late_step_minutes = models.PositiveSmallIntegerField("جریمه تأخیر به ازای هر چند دقیقه", default=30)
    late_step_amount = models.PositiveIntegerField("مبلغ جریمه هر پله تأخیر (تومان)", default=100000)
    # call quality
    banned_words = models.TextField("کلمات و عبارات ممنوع کارشناس", blank=True,
                                    default="نمیشه، نمی‌شه، نمی‌تونیم، نمیتونیم، نمی‌تونم، نمیتونم، نداریم، نمی‌دونم، نمیدونم، باید صبر کنید، آره، هان، "
                                            "اشتباه از شماست، مهندس، حاجی، آبجی، اوکی، اوکیه، حله، اجازه بدید از مسئولم پیگیری کنم، "
                                            "همکارم اینو بهتون نگفته بود",
                                    help_text="با ویرگول جدا کنید. در جمله‌های کارشناس به‌صورت دقیق جستجو می‌شوند.")
    # top specialists of the month (سند متخصصان برتر ماه)
    top_w_free = models.DecimalField("متخصص برتر: وزن هر کار آزاد", max_digits=5, decimal_places=2, default=1)
    top_w_favorite = models.DecimalField("متخصص برتر: وزن هر کار مورد علاقه", max_digits=5, decimal_places=2, default=2)
    top_w_survey = models.DecimalField("متخصص برتر: وزن میانگین نظرسنجی (به ازای هر ۰٫۱ بالای ۴)", max_digits=5, decimal_places=2, default=2)
    top_w_complaint = models.DecimalField("متخصص برتر: کسر به ازای هر شکایت تأییدشده", max_digits=5, decimal_places=2, default=5)
    top_prizes = models.CharField("جوایز ۱۰ نفر برتر (تومان)", max_length=200,
                                  default="5000000,4000000,3000000,2000000,1500000,1000000,800000,700000,600000,500000")
    top_growth_prize = models.PositiveIntegerField("جایزه بیشترین رشد (تومان)", default=1000000)
    seed_version = models.PositiveSmallIntegerField(default=0, editable=False)

    TIER_LABELS = {"home_vip": "منزل VIP", "first_order": "اولین سفارش مشتری", "churned": "مشتری چرن‌شده / ناراضی قبلی",
                   "work_vip": "محل کار VIP", "common_vip": "مشاعات VIP", "home": "منزل عادی", "work": "محل کار عادی",
                   "common": "مشاعات عادی", "other": "سایر خدمات"}

    def tiers(self):
        out = [t.strip() for t in self.dispatch_tiers.split(",") if t.strip() in self.TIER_LABELS]
        return out + [t for t in self.TIER_LABELS if t not in out]

    def banned_list(self):
        import re as _re
        return [w.strip() for w in _re.split(r"[,،\n]", self.banned_words or "") if w.strip()]

    def meal_applies(self, hours) -> bool:
        h = float(hours or 0)
        return h >= float(self.meal_min_hours) if self.meal_inclusive else h > float(self.meal_min_hours)

    def late_penalty(self, minutes: int) -> int:
        over = int(minutes) - self.late_grace_minutes
        if over <= 0:
            return 0
        steps = -(-int(minutes) // self.late_step_minutes)  # started steps of the whole delay
        return steps * self.late_step_amount

    class Meta:
        verbose_name = verbose_name_plural = "قوانین و تنظیمات"

    def __str__(self):
        return "قوانین سیستم"

    @classmethod
    def get(cls) -> "Rules":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class ChecklistItem(models.Model):
    """Editable lists: the 7-point call checklist, specialist fault reasons, survey criteria, specialist complaint reasons."""
    CALL = "call"
    FAULT = "fault"
    SURVEY = "survey"
    PRO_COMPLAINT = "pro_complaint"
    KINDS = [
        (CALL, "چک‌لیست مکالمه کارشناس"),
        (FAULT, "دلایل شکایت مشتری از متخصص"),
        (SURVEY, "معیارهای نظرسنجی"),
        (PRO_COMPLAINT, "دلایل شکایت متخصص"),
    ]
    kind = models.CharField("نوع", max_length=20, choices=KINDS)
    title = models.CharField("عنوان", max_length=200)
    guide = models.TextField("راهنما برای تحلیل‌گر", blank=True, help_text="یعنی چه؟ متن دستورالعمل، شرط اجرا، مثال خوب و بد. هوش مصنوعی از این متن استفاده می‌کند.")
    group = models.CharField("گروه", max_length=80, blank=True, help_text="فقط برای چک‌لیست مکالمه؛ مثلاً «دیالوگ نهایی هماهنگی سفارش»")
    call_kinds = models.CharField("فقط برای این نوع تماس‌ها", max_length=200, blank=True,
                                  help_text="کدهای نوع تماس با ویرگول، مثلاً order,new_customer. خالی = همه‌ی تماس‌ها")
    order = models.PositiveSmallIntegerField("ترتیب", default=0)
    active = models.BooleanField("فعال", default=True)

    class Meta:
        verbose_name = "آیتم فهرست"
        verbose_name_plural = "فهرست‌ها (چک‌لیست، معیارها، دلایل)"
        ordering = ["kind", "order", "id"]

    def __str__(self):
        return self.title

    @classmethod
    def of(cls, kind):
        return list(cls.objects.filter(kind=kind, active=True))

    @classmethod
    def call_items(cls, call_kind: str):
        """Call checklist items that apply to this kind of call."""
        out = []
        for c in cls.of(cls.CALL):
            kinds = [k.strip() for k in c.call_kinds.split(",") if k.strip()]
            if not kinds or call_kind in kinds:
                out.append(c)
        return out

    def applies_to(self, call_kind: str) -> bool:
        kinds = [k.strip() for k in self.call_kinds.split(",") if k.strip()]
        return not kinds or call_kind in kinds


class ServiceType(models.Model):
    name = models.CharField("نام خدمت", max_length=120, unique=True)
    category = models.CharField("گروه", max_length=20, choices=[("home", "منزل"), ("common", "مشاعات"), ("work", "محل کار"),
                                                               ("other", "سایر")], default="other")
    is_vip = models.BooleanField("VIP", default=False)
    active = models.BooleanField("فعال", default=True)

    class Meta:
        verbose_name = "نوع خدمت"
        verbose_name_plural = "انواع خدمات"
        ordering = ["name"]

    def __str__(self):
        return self.name


class CannedResponse(models.Model):
    """Ready-made answers for frequent specialist questions."""
    category = models.CharField("دسته", max_length=80, blank=True)
    title = models.CharField("عنوان", max_length=200)
    text = models.TextField("متن")
    active = models.BooleanField("فعال", default=True)

    class Meta:
        verbose_name = "پاسخ آماده"
        verbose_name_plural = "پاسخ‌های آماده"
        ordering = ["category", "title"]

    def __str__(self):
        return self.title


# ---------------------------------------------------------------- people

class Customer(TimeStamped):
    name = models.CharField("نام", max_length=200)
    phone = models.CharField("موبایل", max_length=20, unique=True)
    kind = models.CharField("نوع", max_length=20, choices=[("person", "شخصی"), ("company", "شرکتی"), ("building", "ساختمان/مجتمع")],
                            default="person")
    source = models.CharField("نحوه آشنایی", max_length=80, blank=True)
    district = models.CharField("محله/منطقه", max_length=120, blank=True)
    address = models.TextField("آدرس", blank=True)
    preferred_gender = models.CharField("ترجیح جنسیت نیرو", max_length=10, choices=[("", "فرقی ندارد"), ("m", "آقا"), ("f", "خانم")],
                                        blank=True)
    favorite_specialists = models.ManyToManyField("Specialist", blank=True, related_name="fans", verbose_name="نیروهای مورد علاقه")
    blocked_specialists = models.ManyToManyField("Specialist", blank=True, related_name="blocked_by", verbose_name="نیروهای لیست سیاه")
    notes = models.TextField("یادداشت", blank=True)
    legacy_id = models.CharField("شناسه در CRM قدیم", max_length=60, blank=True)
    blocked = models.BooleanField("بلاک (ثبت سفارش ممنوع)", default=False)
    block_reason = models.CharField("علت بلاک", max_length=200, blank=True)
    debt = models.PositiveBigIntegerField("بدهی تسویه‌نشده (تومان)", default=0)
    risky = models.BooleanField("اعزام نیرو مشکل‌ساز است", default=False,
                                help_text="طبق سوابق، اعزام نیرو برای این مشتری احتمال مشکل دارد")
    blocked_by_specialists = models.ManyToManyField("Specialist", blank=True, related_name="blocked_customers",
                                                    verbose_name="متخصصینی که این مشتری را بلاک کرده‌اند")
    legacy_order_count = models.PositiveIntegerField("تعداد سفارش در CRM قدیم", default=0)
    legacy_first_order = models.DateField("اولین سفارش در CRM قدیم", null=True, blank=True)
    legacy_last_order = models.DateField("آخرین سفارش در CRM قدیم", null=True, blank=True)

    class Meta:
        verbose_name = "مشتری"
        verbose_name_plural = "مشتریان"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.phone})"

    def save(self, *a, **kw):
        self.phone = normalize_phone(self.phone)
        super().save(*a, **kw)

    def done_orders(self):
        return self.orders.filter(status=Order.DONE)

    @property
    def is_loyal(self) -> bool:
        r = Rules.get()
        since = timezone.now() - timezone.timedelta(days=r.loyal_window_days)
        recent = self.done_orders().filter(start__gte=since).count()
        if recent >= r.loyal_min_orders:
            return True
        # History before this system lives in the old CRM's customer report.
        legacy_recent = self.legacy_last_order and self.legacy_last_order >= since.date()
        return bool(legacy_recent and self.legacy_order_count + recent >= r.loyal_min_orders)


class Specialist(TimeStamped):
    TYPES = [("normal", "عادی"), ("vip_all", "VIP همه کار"), ("vip_cw", "VIP مشاعات و محل کار"), ("trainee", "کارآموز")]
    name = models.CharField("نام و نام خانوادگی", max_length=200)
    phone = models.CharField("موبایل", max_length=20, unique=True)
    national_id = models.CharField("کد ملی", max_length=12, blank=True)
    personnel_code = models.CharField("کد پرسنلی", max_length=30, blank=True)
    gender = models.CharField("جنسیت", max_length=1, choices=[("m", "آقا"), ("f", "خانم")])
    worker_type = models.CharField("نوع نیرو", max_length=10, choices=TYPES, default="normal")
    level = models.CharField("سطح", max_length=5, choices=[("A", "A"), ("A+", "A+")], blank=True)
    active = models.BooleanField("فعال", default=True)
    does_home = models.BooleanField("نظافت منزل", default=False)
    does_common = models.BooleanField("مشاعات", default=False)
    does_work = models.BooleanField("محل کار", default=False)
    does_hospitality = models.BooleanField("پذیرایی/مهمان‌دار", default=False)
    smoker = models.BooleanField("سیگاری", default=False)
    ok_with_pets = models.BooleanField("با حیوان خانگی مشکلی ندارد", default=True)
    residence = models.CharField("محل اقامت", max_length=120, blank=True)
    work_area = models.CharField("محدوده کاری", max_length=200, blank=True)
    score = models.DecimalField("امتیاز", max_digits=3, decimal_places=2, null=True, blank=True)
    hired_on = models.DateField("تاریخ استخدام", null=True, blank=True)
    traits = models.TextField("خصوصیات و نکات رفتاری", blank=True)
    legacy_id = models.CharField("شناسه در HCM", max_length=60, blank=True)

    class Meta:
        verbose_name = "متخصص"
        verbose_name_plural = "متخصصین"
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *a, **kw):
        self.phone = normalize_phone(self.phone)
        super().save(*a, **kw)

    @property
    def is_vip(self):
        return self.worker_type in ("vip_all", "vip_cw")


class StaffProfile(models.Model):
    ROLES = [("agent", "کارشناس پشتیبانی"), ("supervisor", "سرپرست"), ("hr", "منابع انسانی/نگهداشت"), ("finance", "مالی"),
             ("manager", "مدیر")]
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField("نقش", max_length=20, choices=ROLES, default="agent")
    display_name = models.CharField("نام نمایشی", max_length=120, blank=True)

    class Meta:
        verbose_name = "پروفایل کارمند"
        verbose_name_plural = "پروفایل کارمندان"

    def __str__(self):
        return self.display_name or self.user.get_username()


def staff_name(user) -> str:
    if not user:
        return "-"
    prof = getattr(user, "profile", None)
    return (prof and prof.display_name) or user.get_full_name() or user.get_username()


# ---------------------------------------------------------------- orders & availability

class Order(TimeStamped):
    REGISTERED, ASSIGNED, DONE, CANCELLED = "registered", "assigned", "done", "cancelled"
    STATUSES = [(REGISTERED, "ثبت‌شده (بدون نیرو)"), (ASSIGNED, "نیرو تخصیص داده شد"), (DONE, "انجام شد"), (CANCELLED, "لغو شد")]
    tracking_code = models.CharField("کد پیگیری", max_length=40, blank=True, db_index=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="orders", verbose_name="مشتری")
    service = models.ForeignKey(ServiceType, on_delete=models.PROTECT, verbose_name="خدمت")
    start = models.DateTimeField("زمان شروع")
    hours = models.DecimalField("مدت (ساعت)", max_digits=4, decimal_places=1, default=4)
    gender_needed = models.CharField("جنسیت نیرو", max_length=1, choices=[("", "فرقی ندارد"), ("m", "آقا"), ("f", "خانم")], blank=True)
    address = models.TextField("آدرس", blank=True)
    district = models.CharField("محله", max_length=120, blank=True)
    has_pets = models.BooleanField("حیوان خانگی دارد", default=False)
    status = models.CharField("وضعیت", max_length=20, choices=STATUSES, default=REGISTERED, db_index=True)
    specialist = models.ForeignKey(Specialist, on_delete=models.SET_NULL, null=True, blank=True, related_name="orders",
                                   verbose_name="متخصص")
    price = models.PositiveBigIntegerField("مبلغ (ریال)", null=True, blank=True)
    closed_by_staff = models.BooleanField("پایان کار توسط کارشناس", default=False)
    requested_specific = models.BooleanField("مشتری نیروی مشخصی خواسته (کار مورد علاقه)", default=False)
    is_temporary = models.BooleanField("سفارش موقت", default=False)
    cancelled_after_assign = models.BooleanField("لغو مشتری بعد از تخصیص نیرو", default=False)
    worker_fault = models.BooleanField("انجام‌نشده به دلیل قصور یا غیبت نیرو", default=False)
    late_minutes = models.PositiveSmallIntegerField("دقیقه تأخیر نیرو", default=0)
    is_periodic = models.BooleanField("دوره‌ای", default=False)
    cancel_reason = models.CharField("دلیل لغو", max_length=200, blank=True)
    notes = models.TextField("توضیحات", blank=True)
    registered_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name="registered_orders", verbose_name="کارشناس ثبت")

    class Meta:
        verbose_name = "سفارش"
        verbose_name_plural = "سفارشات"
        ordering = ["-start"]

    def __str__(self):
        return f"{self.tracking_code or self.pk} - {self.customer.name}"

    @property
    def is_first_order(self) -> bool:
        c = self.customer
        if c.legacy_order_count and c.legacy_first_order and c.legacy_first_order < timezone.localtime(self.start).date():
            return False
        return not self.customer.orders.exclude(pk=self.pk).filter(start__lt=self.start).exclude(status=Order.CANCELLED).exists()

    @property
    def priority(self) -> int:
        """Higher = dispatch first. First orders and loyal customers first, VIP next, then the earliest start."""
        p = 0
        if self.is_first_order:
            p += 100
        if self.customer.is_loyal:
            p += 80
        if self.service.is_vip:
            p += 30
        return p

    @property
    def is_fixed_8h(self) -> bool:
        return self.is_periodic and float(self.hours or 0) >= 8

    def priority_reasons(self):
        out = []
        if self.is_first_order:
            out.append("سفارش اول")
        if self.customer.is_loyal:
            out.append("مشتری وفادار")
        if self.service.is_vip:
            out.append("VIP")
        return out


class Availability(models.Model):
    """One row per specialist per day: what the employment report / HCM says about that day."""
    STATES = [("ready8", "آماده به کار ۸ ساعته"), ("ready4m", "آماده ۴ ساعته صبح"), ("ready4a", "آماده ۴ ساعته بعدازظهر"),
              ("leave", "مرخصی"), ("nonworking", "روز غیرکاری"), ("suspended", "تعلیق")]
    specialist = models.ForeignKey(Specialist, on_delete=models.CASCADE, related_name="availability", verbose_name="متخصص")
    date = models.DateField("تاریخ")
    state = models.CharField("وضعیت", max_length=12, choices=STATES)
    note = models.CharField("توضیح", max_length=200, blank=True)

    class Meta:
        verbose_name = "وضعیت روزانه متخصص"
        verbose_name_plural = "وضعیت روزانه متخصصین"
        unique_together = [("specialist", "date")]
        ordering = ["-date"]

    @property
    def free_hours(self):
        return {"ready8": 8, "ready4m": 4, "ready4a": 4}.get(self.state, 0)


# ---------------------------------------------------------------- calls, surveys, complaints

class Call(TimeStamped):
    KINDS = [
        ("auto", "تشخیص خودکار نوع تماس"),
        ("order", "ثبت سفارش / مشاوره مشتری"),
        ("survey", "نظرسنجی"),
        ("recall", "تماس مجدد با مشتری ناراضی"),
        ("pro_complaint", "شکایت متخصص"),
        ("unhappy", "دریافت شکایت / نارضایتی مشتری"),
        ("new_customer", "مشتری جدید"),
        ("periodic", "تأیید سفارش دوره‌ای"),
        ("morning", "تماس صبحگاهی با متخصص"),
        ("satisfaction", "سنجش خشنودی"),
        ("other", "سایر"),
    ]
    STATUSES = [("queued", "در صف"), ("transcribing", "در حال تبدیل به متن"), ("analyzing", "در حال تحلیل"), ("done", "آماده بررسی"),
                ("reviewed", "بررسی شد"), ("failed", "خطا")]
    kind = models.CharField("نوع مکالمه", max_length=20, choices=KINDS)
    agent = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="calls",
                              verbose_name="کارشناس")
    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name="calls", verbose_name="مشتری")
    specialist = models.ForeignKey(Specialist, on_delete=models.SET_NULL, null=True, blank=True, related_name="calls",
                                   verbose_name="متخصص")
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True, related_name="calls", verbose_name="سفارش")
    related_complaint = models.ForeignKey("Complaint", on_delete=models.SET_NULL, null=True, blank=True, related_name="recall_calls",
                                          verbose_name="شکایت مرتبط")
    audio = models.FileField("فایل صوتی", upload_to="calls/%Y/%m/", blank=True)
    note = models.TextField("یادداشت کارشناس", blank=True)
    status = models.CharField("وضعیت", max_length=20, choices=STATUSES, default="queued", db_index=True)
    error = models.TextField("خطا", blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    transcript = models.TextField("متن مکالمه", blank=True)
    analysis = models.JSONField("تحلیل", default=dict, blank=True)
    ai_cost = models.DecimalField("هزینه هوش مصنوعی (دلار)", max_digits=8, decimal_places=5, default=0)
    usage = models.JSONField("ریز مصرف هوش مصنوعی", default=list, blank=True)
    checklist_score = models.DecimalField("نمره چک‌لیست (از ۱۰۰)", max_digits=5, decimal_places=1, null=True, blank=True)
    tone_score = models.PositiveSmallIntegerField("نمره لحن (۱-۵)", null=True, blank=True)
    advice_score = models.PositiveSmallIntegerField("نمره مشاوره (۱-۵)", null=True, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="reviewed_calls", verbose_name="بررسی‌کننده")
    reviewed_at = models.DateTimeField("زمان بررسی", null=True, blank=True)
    review_note = models.TextField("بازخورد سرپرست", blank=True)

    class Meta:
        verbose_name = "مکالمه"
        verbose_name_plural = "مکالمات و وویس‌ها"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_kind_display()} #{self.pk}"


class Survey(TimeStamped):
    RESULTS = [("full", "رضایت کامل"), ("good", "خوب"), ("medium", "متوسط"), ("unhappy", "ناراضی"), ("no_answer", "عدم پاسخگویی"),
               ("unaware", "عدم اطلاع از نحوه کار"), ("trainee", "کارآموزی")]
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True, related_name="surveys", verbose_name="سفارش")
    call = models.ForeignKey(Call, on_delete=models.SET_NULL, null=True, blank=True, related_name="surveys", verbose_name="مکالمه")
    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name="surveys",
                                 verbose_name="مشتری")
    specialist = models.ForeignKey(Specialist, on_delete=models.SET_NULL, null=True, blank=True, related_name="surveys",
                                   verbose_name="متخصص")
    agent = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="کارشناس تماس")
    service_name = models.CharField("نوع خدمت", max_length=120, blank=True)
    order_date = models.DateField("تاریخ سفارش", null=True, blank=True)
    call_date = models.DateField("تاریخ تماس", default=timezone.localdate)
    scores = models.JSONField("نمره‌ها", default=dict, blank=True, help_text='{"عنوان معیار": نمره}')
    average = models.DecimalField("میانگین", max_digits=3, decimal_places=2, null=True, blank=True)
    result = models.CharField("نتیجه تماس", max_length=20, choices=RESULTS, blank=True)
    notes = models.TextField("توضیحات", blank=True)
    confirmed = models.BooleanField("تأیید شده", default=True,
                                    help_text="نظرسنجی‌هایی که هوش مصنوعی از وویس درآورده تا تأیید کارشناس پیش‌نویس می‌مانند")
    legacy = models.BooleanField("وارد شده از شیت", default=False)

    class Meta:
        verbose_name = "نظرسنجی"
        verbose_name_plural = "نظرسنجی‌ها"
        ordering = ["-call_date", "-id"]

    def __str__(self):
        return f"نظرسنجی {self.customer or ''} - {self.call_date}"

    def compute_average(self):
        vals = []
        for v in (self.scores or {}).values():
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                pass
        self.average = round(sum(vals) / len(vals), 2) if vals else None
        return self.average


class Complaint(TimeStamped):
    """Customer complaint about a specialist (sheet: شکایات از متخصصین)."""
    STATUSES = [("open", "باز"), ("recall", "در انتظار تماس مجدد"), ("referred", "ارجاع به کنترل کیفیت/نگهداشت"),
                ("resolved", "حل و فصل شد")]
    survey = models.OneToOneField(Survey, on_delete=models.SET_NULL, null=True, blank=True, related_name="complaint",
                                  verbose_name="نظرسنجی")
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True, related_name="complaints", verbose_name="سفارش")
    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name="complaints",
                                 verbose_name="مشتری")
    specialist = models.ForeignKey(Specialist, on_delete=models.SET_NULL, null=True, blank=True, related_name="complaints",
                                   verbose_name="متخصص")
    reason = models.ForeignKey(ChecklistItem, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="علت شکایت",
                               limit_choices_to={"kind": ChecklistItem.FAULT})
    reason_text = models.CharField("علت (متن)", max_length=200, blank=True)
    score = models.DecimalField("امتیاز نظرسنجی", max_digits=3, decimal_places=2, null=True, blank=True)
    description = models.TextField("توضیحات", blank=True)
    status = models.CharField("وضعیت", max_length=20, choices=STATUSES, default="recall", db_index=True)
    resolution = models.TextField("اقدام / نتیجه پیگیری", blank=True)
    compensation = models.PositiveBigIntegerField("مبلغ جبران (تومان)", null=True, blank=True)
    agent = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="کارشناس پیگیری")
    auto_created = models.BooleanField("ایجاد خودکار", default=False)
    legacy = models.BooleanField("وارد شده از شیت", default=False)
    source = models.CharField("منبع", max_length=10, choices=[("customer", "مشتری"), ("staff", "کارشناس عملیات")], default="customer")
    specialist_statement = models.TextField("اظهارات متخصص (بدون قضاوت)", blank=True)
    fault = models.CharField("نتیجه بررسی", max_length=12, blank=True,
                             choices=[("", "در حال بررسی"), ("confirmed", "قصور متخصص احراز شد"), ("not", "قصوری احراز نشد")])
    compensation_type = models.CharField("جبران برای مشتری", max_length=12, blank=True,
                                         choices=[("", "-"), ("discount", "کد تخفیف"), ("reclean", "تجدید نظافت"), ("none", "توضیح قوانین")])
    refer_retention = models.BooleanField("ارجاع به واحد نگهداشت", default=False)
    source_call = models.ForeignKey("Call", on_delete=models.SET_NULL, null=True, blank=True, related_name="unhappy_complaints",
                                    verbose_name="مکالمه‌ی ثبت شکایت")

    class Meta:
        verbose_name = "شکایت از متخصص"
        verbose_name_plural = "شکایات از متخصصین"
        ordering = ["-created_at"]

    def __str__(self):
        return f"شکایت از {self.specialist or '-'}"


class SpecialistComplaint(TimeStamped):
    """Specialist's complaint about a customer or operations (sheet: شکایات متخصصان)."""
    STATUSES = [("open", "باز"), ("in_progress", "در حال پیگیری"), ("resolved", "حل شد")]
    specialist = models.ForeignKey(Specialist, on_delete=models.SET_NULL, null=True, blank=True, related_name="own_complaints",
                                   verbose_name="متخصص")
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="سفارش")
    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="مشتری")
    call = models.ForeignKey(Call, on_delete=models.SET_NULL, null=True, blank=True, related_name="specialist_complaints",
                             verbose_name="مکالمه")
    reason = models.ForeignKey(ChecklistItem, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="علت",
                               limit_choices_to={"kind": ChecklistItem.PRO_COMPLAINT})
    reason_text = models.CharField("علت (متن)", max_length=200, blank=True)
    description = models.TextField("شرح", blank=True)
    status = models.CharField("وضعیت", max_length=20, choices=STATUSES, default="open", db_index=True)
    resolution = models.TextField("نتیجه پیگیری", blank=True)
    amount = models.PositiveBigIntegerField("مبلغ (تومان)", null=True, blank=True)
    agent = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="کارشناس")
    confirmed = models.BooleanField("تأیید شده", default=True)
    legacy = models.BooleanField("وارد شده از شیت", default=False)

    class Meta:
        verbose_name = "شکایت متخصص"
        verbose_name_plural = "شکایات متخصصان"
        ordering = ["-created_at"]

    def __str__(self):
        return f"شکایت {self.specialist or '-'}"


# ---------------------------------------------------------------- tickets & tasks

class Ticket(TimeStamped):
    CATEGORIES = [("dispatch", "امور پشتیبانی و پخش کار"), ("finance", "امور مالی"), ("app", "پشتیبانی اپلیکیشن"),
                  ("settlement", "تسویه حساب"), ("other", "سایر")]
    STATUSES = [("waiting_staff", "در انتظار پاسخ کارشناس"), ("waiting_pro", "در انتظار پاسخ متخصص"), ("closed", "بسته شده")]
    specialist = models.ForeignKey(Specialist, on_delete=models.SET_NULL, null=True, blank=True, related_name="tickets",
                                   verbose_name="متخصص")
    category = models.CharField("دسته", max_length=20, choices=CATEGORIES, default="dispatch")
    title = models.CharField("عنوان", max_length=200)
    status = models.CharField("وضعیت", max_length=20, choices=STATUSES, default="waiting_staff", db_index=True)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="مسئول")
    first_response_at = models.DateTimeField("اولین پاسخ", null=True, blank=True)
    legacy_id = models.CharField("شناسه در CRM قدیم", max_length=60, blank=True)

    class Meta:
        verbose_name = "تیکت متخصص"
        verbose_name_plural = "تیکت‌های متخصصین"
        ordering = ["-created_at"]

    def __str__(self):
        return self.title

    @property
    def is_overdue(self) -> bool:
        if self.first_response_at or self.status == "closed":
            return False
        return timezone.now() - self.created_at > timezone.timedelta(minutes=Rules.get().ticket_sla_minutes)


class TicketMessage(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="messages")
    from_staff = models.BooleanField(default=False)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    text = models.TextField("متن")
    tone_flag = models.CharField("هشدار لحن", max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class FollowUpTask(TimeStamped):
    """Internal to-do created by the system or staff (call back an unhappy customer, find a specialist...)."""
    title = models.CharField("عنوان", max_length=250)
    details = models.TextField("جزئیات", blank=True)
    due = models.DateTimeField("مهلت", null=True, blank=True)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="مسئول")
    done = models.BooleanField("انجام شد", default=False, db_index=True)
    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="مشتری")
    specialist = models.ForeignKey(Specialist, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="متخصص")
    complaint = models.ForeignKey(Complaint, on_delete=models.SET_NULL, null=True, blank=True, related_name="tasks", verbose_name="شکایت")
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="سفارش")

    class Meta:
        verbose_name = "کار پیگیری"
        verbose_name_plural = "کارهای پیگیری"
        ordering = ["done", "due"]

    def __str__(self):
        return self.title


# ---------------------------------------------------------------- lost customers, penalties, daily work

class LostCustomer(TimeStamped):
    """Sheet «مشتریان از دست رفته»: requests that did not become an order (needed for the coverage rate)."""
    REASONS = [("inquiry", "استعلام"), ("capacity", "عدم ظرفیت"), ("price", "هزینه بالا"), ("rejected", "رد سفارش"),
               ("mismatch", "عدم تطابق نیرو"), ("restricted", "محدودیت ثبت سفارش"), ("conditions", "عدم پذیرش موارد هفت‌گانه"),
               ("other", "سایر")]
    date = models.DateField("تاریخ", default=timezone.localdate, db_index=True)
    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="مشتری")
    name = models.CharField("نام", max_length=200, blank=True)
    phone = models.CharField("موبایل", max_length=20, blank=True)
    source = models.CharField("نحوه آشنایی", max_length=80, blank=True)
    service = models.CharField("نوع خدمت", max_length=120, blank=True)
    gender_needed = models.CharField("جنسیت نیرو", max_length=10, blank=True)
    reason = models.CharField("علت", max_length=12, choices=REASONS)
    notes = models.TextField("توضیحات", blank=True)
    agent = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="پشتیبان")
    call = models.ForeignKey("Call", on_delete=models.SET_NULL, null=True, blank=True, verbose_name="مکالمه")
    legacy = models.BooleanField(default=False)

    class Meta:
        verbose_name = "مشتری از دست رفته"
        verbose_name_plural = "مشتریان از دست رفته"
        ordering = ["-date", "-id"]

    def save(self, *a, **kw):
        self.phone = normalize_phone(self.phone)
        super().save(*a, **kw)


class PenaltyType(models.Model):
    UNITS = [("fixed", "مبلغ ثابت"), ("late", "طبق قانون تأخیر"), ("order", "معادل هزینه سفارش"), ("other", "طبق توضیحات")]
    title = models.CharField("مورد", max_length=200)
    amount = models.PositiveIntegerField("مبلغ (تومان)", default=0)
    unit = models.CharField("نوع محاسبه", max_length=8, choices=UNITS, default="fixed")
    note = models.CharField("توضیحات", max_length=250, blank=True)
    fault_titles = models.CharField("برای این علت‌های شکایت پیشنهاد شود", max_length=300, blank=True,
                                    help_text="عنوان دلایل شکایت با ویرگول؛ مثلاً «تأخیر (متخصص)»")
    active = models.BooleanField("فعال", default=True)
    order = models.PositiveSmallIntegerField("ترتیب", default=0)

    class Meta:
        verbose_name = "نوع جریمه"
        verbose_name_plural = "جدول جریمه‌ها"
        ordering = ["order", "id"]

    def __str__(self):
        return self.title


class Penalty(TimeStamped):
    STATUSES = [("proposed", "پیشنهاد سیستم"), ("approved", "تأیید شد"), ("cancelled", "لغو / بخشش")]
    specialist = models.ForeignKey(Specialist, on_delete=models.CASCADE, related_name="penalties", verbose_name="متخصص")
    ptype = models.ForeignKey(PenaltyType, on_delete=models.PROTECT, verbose_name="نوع جریمه")
    complaint = models.ForeignKey(Complaint, on_delete=models.SET_NULL, null=True, blank=True, related_name="penalties")
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True)
    amount = models.PositiveIntegerField("مبلغ (تومان)", default=0)
    status = models.CharField("وضعیت", max_length=10, choices=STATUSES, default="proposed", db_index=True)
    note = models.TextField("توضیحات", blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        verbose_name = "جریمه"
        verbose_name_plural = "جریمه‌ها"
        ordering = ["-created_at"]


class RecurringTask(models.Model):
    """Recurring duties from the responsibility matrix (شرح مسئولیت‌ها)."""
    DAYS = [(5, "شنبه"), (6, "یکشنبه"), (0, "دوشنبه"), (1, "سه‌شنبه"), (2, "چهارشنبه"), (3, "پنجشنبه"), (4, "جمعه")]
    title = models.CharField("کار", max_length=250)
    unit = models.CharField("واحد/زیرگروه", max_length=80, blank=True, default="عملیات - نظافت")
    place = models.CharField("محل ثبت", max_length=120, blank=True)
    weekdays = models.CharField("روزها", max_length=30, default="5,6,0,1,2,3,4",
                                help_text="شماره روزها: شنبه=5، یکشنبه=6، دوشنبه=0، سه‌شنبه=1، چهارشنبه=2، پنجشنبه=3، جمعه=4")
    link = models.CharField("صفحه مرتبط در سیستم", max_length=60, blank=True)
    assignees = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, verbose_name="مسئولان")
    active = models.BooleanField("فعال", default=True)
    order = models.PositiveSmallIntegerField("ترتیب", default=0)

    class Meta:
        verbose_name = "کار تکراری"
        verbose_name_plural = "کارهای تکراری (شرح وظایف)"
        ordering = ["order", "id"]

    def __str__(self):
        return self.title

    def due_on(self, d) -> bool:
        return str(d.weekday()) in [x.strip() for x in self.weekdays.split(",")]


class TaskDone(models.Model):
    task = models.ForeignKey(RecurringTask, on_delete=models.CASCADE, related_name="done")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    date = models.DateField(db_index=True)
    note = models.CharField(max_length=250, blank=True)
    done_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("task", "user", "date")]


class DailyCheck(TimeStamped):
    """Result of a routine call: morning call to a specialist, periodic-order confirmation."""
    KINDS = [("morning", "تماس صبحگاهی"), ("periodic", "تأیید سفارش دوره‌ای")]
    RESULTS = {
        "morning": [("on_time", "به‌موقع می‌رسد"), ("late", "با تأخیر می‌رسد"), ("cannot", "نمی‌تواند / خواب مانده"),
                    ("no_answer", "پاسخ نداد"), ("replaced", "نیروی جایگزین هماهنگ شد")],
        "periodic": [("confirmed", "سفارش پابرجاست"), ("cancel", "لغو سفارش"), ("reschedule", "تغییر زمان"),
                     ("no_answer", "عدم پاسخگویی"), ("follow", "نیاز به پیگیری مجدد"), ("other", "سایر")],
    }
    kind = models.CharField(max_length=10, choices=KINDS)
    date = models.DateField(db_index=True)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="checks")
    result = models.CharField("نتیجه", max_length=12)
    late_minutes = models.PositiveSmallIntegerField(default=0)
    specialist_ok = models.CharField("رضایت از متخصص ثابت", max_length=10, blank=True,
                                     choices=[("", "-"), ("yes", "راضی"), ("no", "ناراضی")])
    note = models.CharField("توضیح", max_length=300, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)

    class Meta:
        unique_together = [("kind", "order", "date")]

    def result_label(self):
        return dict(self.RESULTS.get(self.kind, [])).get(self.result, self.result)
