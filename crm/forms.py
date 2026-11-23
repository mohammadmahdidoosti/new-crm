from django import forms

from .jalali import parse_date, parse_datetime
from .models import Call, ChecklistItem, Order, ServiceType, Specialist, Survey, Ticket


class MultiFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultiFileField(forms.FileField):
    def __init__(self, *a, **kw):
        kw.setdefault("widget", MultiFileInput(attrs={"accept": "audio/*,.ogg,.oga,.opus,.m4a,.mp3,.wav,.amr,.3gp,.wma"}))
        super().__init__(*a, **kw)

    def clean(self, data, initial=None):
        single = super().clean
        if isinstance(data, (list, tuple)):
            return [single(d, initial) for d in data]
        return [single(data, initial)] if data else []


class CallUploadForm(forms.Form):
    kind = forms.ChoiceField(label="نوع مکالمه", choices=Call.KINDS)
    files = MultiFileField(label="فایل(های) صوتی")
    customer_phone = forms.CharField(label="موبایل مشتری", required=False)
    specialist_phone = forms.CharField(label="موبایل یا نام متخصص", required=False)
    order_code = forms.CharField(label="کد پیگیری سفارش", required=False)
    note = forms.CharField(label="یادداشت", required=False, widget=forms.Textarea(attrs={"rows": 2}))


class JalaliDateField(forms.CharField):
    def to_python(self, value):
        value = super().to_python(value)
        if not value:
            return None
        d = parse_date(value)
        if not d:
            raise forms.ValidationError("تاریخ را به شکل ۱۴۰۵/۰۸/۲۹ وارد کنید")
        return d


class JalaliDateTimeField(forms.CharField):
    def to_python(self, value):
        value = super().to_python(value)
        if not value:
            return None
        d = parse_datetime(value)
        if not d:
            raise forms.ValidationError("زمان را به شکل ۱۴۰۵/۰۸/۲۹ ۰۹:۰۰ وارد کنید")
        return d


class OrderForm(forms.ModelForm):
    customer_name = forms.CharField(label="نام مشتری")
    customer_phone = forms.CharField(label="موبایل مشتری")
    start = JalaliDateTimeField(label="زمان شروع (۱۴۰۵/۰۸/۲۹ ۰۹:۰۰)")
    specialist = forms.ModelChoiceField(label="نیروی مورد علاقه / درخواستی مشتری (اختیاری)", required=False,
                                        queryset=Specialist.objects.filter(active=True))

    class Meta:
        model = Order
        fields = ["service", "start", "hours", "gender_needed", "district", "address", "has_pets", "is_periodic", "is_temporary",
                  "price", "notes", "tracking_code"]
        widgets = {"address": forms.Textarea(attrs={"rows": 2}), "notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["service"].queryset = ServiceType.objects.filter(active=True)


def survey_form_class():
    """Survey form with one 1–5 field per active criterion."""
    fields = {}
    for c in ChecklistItem.of(ChecklistItem.SURVEY):
        fields[f"c_{c.pk}"] = forms.TypedChoiceField(label=c.title, required=False, coerce=int, empty_value=None,
                                                     choices=[("", "-")] + [(i, i) for i in range(1, 6)])
    fields["result"] = forms.ChoiceField(label="نتیجه تماس", choices=[("", "-")] + Survey.RESULTS, required=False)
    fields["notes"] = forms.CharField(label="توضیحات", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    fields["fault"] = forms.ModelChoiceField(label="علت اصلی نارضایتی (اگر هست)", required=False,
                                             queryset=ChecklistItem.objects.filter(kind=ChecklistItem.FAULT, active=True))
    return type("SurveyForm", (forms.Form,), fields)


class NewSurveyForm(forms.Form):
    customer_phone = forms.CharField(label="موبایل مشتری")
    customer_name = forms.CharField(label="نام مشتری", required=False)
    specialist = forms.ModelChoiceField(label="متخصص", queryset=Specialist.objects.filter(active=True), required=False)
    order_code = forms.CharField(label="کد پیگیری سفارش", required=False)
    order_date = JalaliDateField(label="تاریخ سفارش", required=False)


class TicketForm(forms.ModelForm):
    specialist_phone = forms.CharField(label="موبایل یا نام متخصص")
    text = forms.CharField(label="متن درخواست متخصص", widget=forms.Textarea(attrs={"rows": 3}))

    class Meta:
        model = Ticket
        fields = ["category", "title"]


class ImportForm(forms.Form):
    file = forms.FileField(label="فایل اکسل (.xlsx)", widget=forms.ClearableFileInput(attrs={"accept": ".xlsx"}))
