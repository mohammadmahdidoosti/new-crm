from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import User

from .models import (Availability, Call, CannedResponse, ChecklistItem, Complaint, Customer, FollowUpTask, Order, Rules,
                     ServiceType, Specialist, SpecialistComplaint, StaffProfile, Survey, Ticket, TicketMessage)

admin.site.site_header = "مدیریت CRM سرویک"
admin.site.site_title = "CRM سرویک"
admin.site.index_title = "تنظیمات و داده‌ها"


class ProfileInline(admin.StackedInline):
    model = StaffProfile
    can_delete = False


admin.site.unregister(User)


@admin.register(User)
class StaffAdmin(UserAdmin):
    inlines = [ProfileInline]
    list_display = ("username", "first_name", "last_name", "is_active", "is_staff")


@admin.register(Rules)
class RulesAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return not Rules.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ChecklistItem)
class ChecklistAdmin(admin.ModelAdmin):
    list_display = ("title", "kind", "group", "call_kinds", "order", "active")
    list_filter = ("kind", "group", "active")
    list_editable = ("order", "active")


admin.site.register(ServiceType, list_display=("name", "category", "is_vip", "active"), list_editable=("is_vip", "active"))
admin.site.register(CannedResponse, list_display=("title", "category", "active"), search_fields=("title", "text"))


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "kind", "source", "created_at")
    search_fields = ("name", "phone")
    list_filter = ("kind",)
    autocomplete_fields = ("favorite_specialists", "blocked_specialists")


@admin.register(Specialist)
class SpecialistAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "gender", "worker_type", "score", "active")
    list_filter = ("active", "gender", "worker_type", "does_home", "does_common", "does_work")
    search_fields = ("name", "phone", "personnel_code")


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("tracking_code", "customer", "service", "start", "status", "specialist")
    list_filter = ("status", "service", "is_periodic")
    search_fields = ("tracking_code", "customer__name", "customer__phone")
    autocomplete_fields = ("customer", "specialist")
    date_hierarchy = "start"


admin.site.register(Availability, list_display=("specialist", "date", "state"), list_filter=("state", "date"),
                    autocomplete_fields=("specialist",))


@admin.register(Call)
class CallAdmin(admin.ModelAdmin):
    list_display = ("id", "kind", "agent", "customer", "specialist", "status", "checklist_score", "tone_score", "created_at")
    list_filter = ("kind", "status")
    search_fields = ("transcript", "customer__name", "specialist__name")
    autocomplete_fields = ("customer", "specialist", "order")


@admin.register(Survey)
class SurveyAdmin(admin.ModelAdmin):
    list_display = ("customer", "specialist", "call_date", "average", "result", "confirmed", "legacy")
    list_filter = ("result", "confirmed", "legacy")
    search_fields = ("customer__name", "customer__phone", "specialist__name")
    autocomplete_fields = ("customer", "specialist", "order")


@admin.register(Complaint)
class ComplaintAdmin(admin.ModelAdmin):
    list_display = ("specialist", "reason_text", "score", "status", "auto_created", "created_at")
    list_filter = ("status", "auto_created", "legacy", "reason")
    search_fields = ("specialist__name", "customer__name", "description")
    autocomplete_fields = ("customer", "specialist", "order")


@admin.register(SpecialistComplaint)
class SpecialistComplaintAdmin(admin.ModelAdmin):
    list_display = ("specialist", "reason_text", "status", "confirmed", "created_at")
    list_filter = ("status", "confirmed", "legacy")
    search_fields = ("specialist__name", "description")
    autocomplete_fields = ("customer", "specialist", "order")


class TicketMessageInline(admin.TabularInline):
    model = TicketMessage
    extra = 0


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ("title", "specialist", "category", "status", "created_at", "first_response_at")
    list_filter = ("status", "category")
    inlines = [TicketMessageInline]
    autocomplete_fields = ("specialist",)


admin.site.register(FollowUpTask, list_display=("title", "assigned_to", "due", "done"), list_filter=("done",))
