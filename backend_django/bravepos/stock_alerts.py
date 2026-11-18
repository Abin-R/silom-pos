"""Alert Bot Testing on each branch's second receipt/issue per Bangkok day.

Record each creation in the document transaction. Process daily row locks in
a separate transaction after commit, outside branch/product business locks.
This avoids a lock-order inversion with automatic promotional stock-outs.
"""
from datetime import datetime, time, timedelta
import logging

from django.db import transaction
from django.utils import timezone

from . import seatalk
from .models import StockDocument, StockDocumentAlertDay, StockDocumentAlertEvent

log = logging.getLogger(__name__)

# Bot Testing, also used by the connected shop backoffice. Not a credential.
BOT_TESTING_GROUP_ID = "MzM0NzQ2MjIyOTM5"


def record_document(document, using="default"):
    """Register one durable creation event; do not lock a shared daily row."""
    zone = timezone.get_default_timezone()
    day = timezone.localtime(document.created_at, zone).date()
    StockDocumentAlertEvent.objects.using(using).get_or_create(
        document_id=document.pk,
        defaults={"branch_id": document.branch_id, "document_type": document.type,
                  "day": day, "created_at": document.created_at},
    )
    transaction.on_commit(
        lambda: process_day(document.branch_id, document.type, day, using), using=using,
    )


def process_day(branch_id, document_type, day, using="default"):
    """Process pending creations once; failures cannot roll back a receipt.

    A later creation retries unprocessed events for the same branch/type/day.
    Chat delivery itself keeps the existing best-effort SeaTalk behavior.
    """
    try:
        with transaction.atomic(using=using):
            rows = StockDocumentAlertDay.objects.using(using)
            daily, created = rows.get_or_create(
                branch_id=branch_id, document_type=document_type, day=day,
            )
            daily = rows.select_for_update().get(pk=daily.pk)
            events = StockDocumentAlertEvent.objects.using(using).filter(
                branch_id=branch_id, document_type=document_type, day=day,
            )
            if created:
                # Count pre-rollout/imported documents, excluding every durable
                # event so simultaneous commits cannot be counted twice.
                zone = timezone.get_default_timezone()
                start = datetime.combine(day, time.min, tzinfo=zone)
                end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
                daily.document_count = StockDocument.objects.using(using).filter(
                    branch_id=branch_id, type=document_type,
                    created_at__gte=start, created_at__lt=end,
                ).exclude(pk__in=events.values("document_id")).count()
            for event in events.filter(processed=False).order_by("created_at", "pk"):
                daily.document_count += 1
                if daily.document_count == 2 and daily.triggered_at is None:
                    daily.triggered_at = event.created_at
                    document_id = event.document_id
                    transaction.on_commit(
                        lambda pk=document_id: notify_document(pk, day, using), using=using,
                    )
                event.processed = True
                event.save(update_fields=["processed"], using=using)
            daily.save(update_fields=["document_count", "triggered_at"], using=using)
    except Exception:
        log.exception("Daily stock document alert processing failed for %s/%s/%s",
                      branch_id, document_type, day)


def build_alert(document, day):
    kind = "stock-in" if document.type == "in" else "stock-out"
    when = timezone.localtime(document.created_at, timezone.get_default_timezone())
    return "\n".join([
        f"BravePOS: Second {kind} document today",
        f"Branch: {document.branch.name}",
        f"Shop date: {day.isoformat()}",
        f"Document: {document.document_no}",
        f"Staff: {document.created_by or '—'}",
        f"Time: {when:%d %b %Y %H:%M} ({timezone.get_default_timezone_name()})",
        f"Type: {document.reason or '—'}",
        f"Total: ฿{document.total:,.2f}",
        "Daily document count: 2",
    ])


def notify_document(document_id, day, using="default"):
    """Use the existing background sender; failures cannot undo a receipt."""
    try:
        document = StockDocument.objects.using(using).select_related("branch").get(pk=document_id)
        if not seatalk.is_configured():
            log.warning("Stock document alert not sent: SeaTalk is not configured")
            return
        seatalk.send_group_text_async(BOT_TESTING_GROUP_ID, build_alert(document, day))
    except Exception:
        log.exception("Stock document alert failed for %s", document_id)
