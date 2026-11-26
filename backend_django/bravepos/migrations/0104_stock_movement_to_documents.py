"""The till's Stock Movement tab, filed as Stock In / Stock Out documents.

The tab (``POST /api/stock-movements``) changed stock with Stock In, Stock Out
or Adjust and wrote only a ledger row numbered ``SM<yymmddHHMMSS>`` — no
document, so none of it showed on the Stock In or Stock Out lists.  The tab is
gone; this files each past entry that moved stock as a one-line document of
type "Stock Movement":

* Stock In / Stock Out — the same type, the same quantity;
* Adjust — the ledger row holds the *new* stock, not the change, so the change
  comes from the audit row the same save wrote (stock from → to).  Up is a
  Stock In, down a Stock Out.  An adjust that left stock as it was (the number
  typed matched what was on hand) moved nothing and is not filed.

Stock, the ledger rows and the audit log are left alone: the stock moved when
the entry was saved, and past-date inventory reads the audit log, so it counts
these changes exactly as before.  Historical models, so no signal fires — no
audit row, no repeat-stock alert.
"""
from datetime import timedelta

from django.db import migrations

STOCK_MOVEMENT_TYPE = "Stock Movement"
AUDIT_WINDOW = timedelta(seconds=5)


def _adjust_change(AuditLog, alias, mv, used):
    """``(before, after)`` for an Adjust entry, or None if stock didn't change.

    ``used`` holds the audit rows already matched: Save pressed twice writes two
    entries but changes stock once, so the second must not reuse the first's."""
    rows = (
        AuditLog.objects.using(alias)
        .filter(model="Product", object_id=str(mv.product_id), action="update",
                at__gte=mv.created_at - AUDIT_WINDOW, at__lte=mv.created_at + AUDIT_WINDOW,
                changes__has_key="stock")
        .exclude(pk__in=used)
        .values_list("pk", "changes", "at")
    )
    best = None
    for pk, changes, at in rows:
        stock = (changes or {}).get("stock")
        if not isinstance(stock, dict):
            continue
        try:
            before, after = int(stock.get("from") or 0), int(stock.get("to") or 0)
        except (TypeError, ValueError):
            continue
        if after != mv.qty:
            continue
        gap = abs((at - mv.created_at).total_seconds())
        if best is None or gap < best[0]:
            best = (gap, before, after, pk)
    if best is None:
        return None
    used.add(best[3])
    if best[1] == best[2]:
        return None
    return best[1], best[2]


def forwards(apps, schema_editor):
    alias = schema_editor.connection.alias
    StockMovement = apps.get_model("bravepos", "StockMovement")
    StockDocument = apps.get_model("bravepos", "StockDocument")
    StockDocumentItem = apps.get_model("bravepos", "StockDocumentItem")
    AuditLog = apps.get_model("bravepos", "AuditLog")

    # Two entries saved in the same second share an SM number, so a filed one
    # is recognised by number, product and time together.
    filed = set(
        StockDocumentItem.objects.using(alias)
        .filter(document__reason=STOCK_MOVEMENT_TYPE, document__document_no__startswith="SM")
        .values_list("document__document_no", "product_id", "document__created_at")
    )
    moves = (
        StockMovement.objects.using(alias)
        .filter(document_no__startswith="SM", type__in=("in", "out", "adjust"))
        .select_related("product", "staff")
        .order_by("created_at", "pk")
    )
    used = set()
    for mv in moves.iterator():
        if (mv.document_no, mv.product_id, mv.created_at) in filed:
            continue
        before = after = 0
        if mv.type == "adjust":
            change = _adjust_change(AuditLog, alias, mv, used)
            if change is None:
                continue
            before, after = change
            doc_type, qty = ("in" if after > before else "out"), abs(after - before)
        else:
            doc_type, qty = mv.type, mv.qty
            if qty <= 0:
                continue
        doc = StockDocument.objects.using(alias).create(
            branch_id=mv.branch_id, type=doc_type, document_no=mv.document_no,
            reason=STOCK_MOVEMENT_TYPE,
            created_by=mv.created_by or (mv.staff.name if mv.staff_id else ""),
        )
        StockDocument.objects.using(alias).filter(pk=doc.pk).update(created_at=mv.created_at)
        StockDocumentItem.objects.using(alias).create(
            document=doc, product_id=mv.product_id,
            barcode=mv.product.barcode or "",
            product_name=mv.product_name or mv.product.name,
            qty=qty, price=0, discount=0, total=0,
            before_qty=before, reconcile_qty=after,
        )


def backwards(apps, schema_editor):
    StockDocument = apps.get_model("bravepos", "StockDocument")
    StockDocument.objects.using(schema_editor.connection.alias).filter(
        reason=STOCK_MOVEMENT_TYPE, document_no__startswith="SM").delete()


class Migration(migrations.Migration):

    dependencies = [("bravepos", "0103_inventory_snapshot")]

    operations = [migrations.RunPython(forwards, backwards)]
