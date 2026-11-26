"""Who made a "Stock Movement" document, from the audit log.

Ledger rows from before 26 Oct 2026 carry no staff name, so the documents
0104 filed from them were blank.  The same save wrote an audit row for the
product's stock with the staff member as actor; its name is used.  Rows from
before the audit log began (17 Jun 2026) have none and stay blank.
"""
from datetime import timedelta

from django.db import migrations

STOCK_MOVEMENT_TYPE = "Stock Movement"
AUDIT_WINDOW = timedelta(seconds=5)


def forwards(apps, schema_editor):
    alias = schema_editor.connection.alias
    StockDocument = apps.get_model("bravepos", "StockDocument")
    StockDocumentItem = apps.get_model("bravepos", "StockDocumentItem")
    AuditLog = apps.get_model("bravepos", "AuditLog")
    docs = StockDocument.objects.using(alias).filter(
        reason=STOCK_MOVEMENT_TYPE, document_no__startswith="SM", created_by="")
    for doc in docs.iterator():
        line = StockDocumentItem.objects.using(alias).filter(document=doc).first()
        if line is None or line.product_id is None:
            continue
        rows = (
            AuditLog.objects.using(alias)
            .filter(model="Product", object_id=str(line.product_id), action="update",
                    at__gte=doc.created_at - AUDIT_WINDOW, at__lte=doc.created_at + AUDIT_WINDOW,
                    changes__has_key="stock")
            .exclude(actor_label="")
            .values_list("actor_label", "at")
        )
        best = min(rows, key=lambda r: abs((r[1] - doc.created_at).total_seconds()), default=None)
        if best is not None:
            StockDocument.objects.using(alias).filter(pk=doc.pk).update(created_by=best[0][:120])


class Migration(migrations.Migration):

    dependencies = [("bravepos", "0104_stock_movement_to_documents")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
