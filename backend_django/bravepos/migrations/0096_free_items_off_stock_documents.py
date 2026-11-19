"""Free items no longer leave stock on a "Free item" stock-out document.

Since this change a sale's free items leave stock the way its sold lines do:
a stock decrement plus a stock-history line on the order number.  This turns
the documents written before it into the same shape — each document's
history lines move onto its order number (which is how a void finds them to
put stock back), and the document itself goes.  Stock levels do not change:
the decrement already happened when the sale was saved.
"""
from django.db import migrations

FREE_ITEM_REASON = "Free item"


def forwards(apps, schema_editor):
    alias = schema_editor.connection.alias
    StockDocument = apps.get_model("bravepos", "StockDocument")
    StockMovement = apps.get_model("bravepos", "StockMovement")
    docs = StockDocument.objects.using(alias).filter(type="out", reason=FREE_ITEM_REASON)
    for doc in docs.iterator():
        if not doc.ref_no:
            continue  # no order to attach it to; leave it as it is
        moves = StockMovement.objects.using(alias).filter(
            branch_id=doc.branch_id, type="out", document_no=doc.document_no,
            note__startswith=FREE_ITEM_REASON,
        )
        moves.filter(created_by="").update(created_by=doc.created_by or "")
        moves.update(document_no=doc.ref_no)
        doc.delete()  # its items go with it


class Migration(migrations.Migration):

    dependencies = [("bravepos", "0095_stock_document_daily_alerts")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
