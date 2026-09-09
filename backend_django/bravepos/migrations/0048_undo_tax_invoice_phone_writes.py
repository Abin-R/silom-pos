"""Give back the phone numbers the tax-invoice form took.

Issuing a full tax invoice PATCHed the buyer's details onto the customer so the
next invoice would prefill, and the phone box rode along with them.  The number
on an invoice is the buyer's billing contact for that document — usually a
company switchboard — so what it wrote over was the customer's own number.

Four customers ended up holding a company landline.  Three had no number before
and simply gained one that is not theirs.  The fourth, and the reason this is a
repair rather than a tidy-up, had a real mobile:

    เบส   +66624101030  ->  026625644     (2026-08-21, Vibhavadi Rangsit)

It happened twice that morning, to two different customers, with the same
landline written over both — after which they looked like one person sharing a
number, and 0045 merged them.  One customer was deleted by that merge.

So each number is put back where the audit log still holds it, and cleared
where it does not: a number that was never the customer's is worse than no
number, because the CRM keys a loyalty membership on it and the till offers it
as that person's contact.

``AuditLog`` is the source rather than a hardcoded list, so this repairs
whatever the form actually did on each deployment rather than what it did on
ours.  Where the old number has since been taken by somebody else the row is
cleared instead — restoring it would collide with the live customer holding it,
and the audit log keeps the number either way.

The tax-invoice form no longer writes ``phone`` at all, so this cannot recur.
The number still prints and reprints: it lives on ``Order.pos_tax_invoice``,
which is where a document's own details belong.
"""
from django.db import migrations


def undo_the_writes(apps, schema_editor):
    from bravepos.customers import undo_tax_invoice_phone_writes

    undo_tax_invoice_phone_writes(
        apps.get_model("bravepos", "Customer"),
        apps.get_model("bravepos", "AuditLog"),
    )


def leave_as_found(apps, schema_editor):
    """Nothing to undo.

    Putting the landlines back would mean re-applying an edit the shop has
    since decided was wrong, and the audit log holds every number involved if
    anyone needs to look.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("bravepos", "0047_customer_phone_unique"),
    ]

    operations = [
        migrations.RunPython(undo_the_writes, leave_as_found),
    ]
