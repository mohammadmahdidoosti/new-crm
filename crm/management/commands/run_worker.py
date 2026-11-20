"""Background worker: transcribes and analyzes queued calls. Runs next to the web process (see start.sh)."""
import logging
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections, transaction
from django.utils import timezone

from crm.analysis import process_call
from crm.models import Call

log = logging.getLogger("crm.worker")
MAX_ATTEMPTS = 3


def claim():
    with transaction.atomic():
        call = (Call.objects.select_for_update(skip_locked=True)
                .filter(status__in=["queued"], attempts__lt=MAX_ATTEMPTS).order_by("created_at").first())
        if call:
            call.attempts += 1
            call.status = "transcribing"
            call.save(update_fields=["attempts", "status"])
        return call


def reset_stuck():
    """Calls left mid-way by a restart go back to the queue."""
    cutoff = timezone.now() - timezone.timedelta(minutes=15)
    Call.objects.filter(status__in=["transcribing", "analyzing"], updated_at__lt=cutoff).update(status="queued")


class Command(BaseCommand):
    help = "Process queued calls forever"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **opts):
        log.info("worker started")
        last_reset = 0
        while True:
            if not opts["once"]:  # long-running: drop stale DB connections between jobs
                close_old_connections()
            if time.time() - last_reset > 300:
                reset_stuck()
                last_reset = time.time()
            call = claim()
            if not call:
                if opts["once"]:
                    return
                time.sleep(5)
                continue
            try:
                process_call(call)
                log.info("call %s done", call.pk)
            except Exception as e:
                log.exception("call %s failed", call.pk)
                call.refresh_from_db()
                call.error = str(e)[:1000]
                call.status = "queued" if call.attempts < MAX_ATTEMPTS else "failed"
                call.save(update_fields=["error", "status"])
                if not opts["once"]:
                    time.sleep(10)
            if opts["once"]:
                return
