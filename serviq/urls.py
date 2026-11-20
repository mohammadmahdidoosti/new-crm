from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

from crm import views as v

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", auth_views.LoginView.as_view(template_name="crm/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", v.dashboard, name="dashboard"),
    path("calls/", v.call_list, name="call_list"),
    path("calls/new/", v.call_upload, name="call_upload"),
    path("calls/<int:pk>/", v.call_detail, name="call_detail"),
    path("calls/<int:pk>/action/", v.call_action, name="call_action"),
    path("calls/<int:pk>/audio/", v.media_audio, name="call_audio"),
    path("orders/new/", v.order_new, name="order_new"),
    path("dispatch/", v.dispatch, name="dispatch"),
    path("dispatch/<int:pk>/assign/", v.assign, name="assign"),
    path("surveys/", v.survey_list, name="survey_list"),
    path("surveys/new/", v.survey_new, name="survey_new"),
    path("complaints/", v.complaint_list, name="complaint_list"),
    path("complaints/<int:pk>/", v.complaint_detail, name="complaint_detail"),
    path("pro-complaints/", v.pro_complaint_list, name="pro_complaint_list"),
    path("tickets/", v.ticket_list, name="ticket_list"),
    path("tickets/new/", v.ticket_new, name="ticket_new"),
    path("tickets/<int:pk>/", v.ticket_detail, name="ticket_detail"),
    path("reports/commission/", v.report_commission, name="report_commission"),
    path("reports/capacity/", v.report_capacity, name="report_capacity"),
    path("reports/agents/", v.report_agents, name="report_agents"),
    path("reports/specialists/", v.specialist_report, name="report_specialists"),
    path("tasks/", v.task_list, name="task_list"),
    path("availability/", v.availability, name="availability"),
    path("import/", v.import_view, name="import"),
    path("api/ingest/call/", v.api_ingest_call, name="api_ingest_call"),
]
