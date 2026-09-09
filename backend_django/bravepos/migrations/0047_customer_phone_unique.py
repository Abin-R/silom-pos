"""One phone number, one customer.

0045 folded the duplicates the branch-scoped book had accumulated, but nothing
stopped new ones.  The till warned about a number already on file and then
offered "Save anyway"; the tax-invoice buyer form never checked at all; and two
tills saving the same number in the same second both passed their own check,
because a check that runs a moment before the save cannot hold the gap.  Only
the database can settle a tie like that, which is what this is.

It matters because the phone number is not a note on a card any more.  The CRM
identifies a member by it and never renames an existing one, so a second
customer on a number it already knows rings up against the first one's
membership, under the first one's name — the points land on a customer nobody
is looking at, and the person who earned them is told they have none.

The constraint compares the stored characters, so it holds one number to one
customer when the number is typed the same way each time.  ``+66812345678`` and
``0812345678`` are one person to a cashier and two different values to the
index — see ``bravepos/customers.py`` for the normalising the till's own check
and ``merge_customers`` compare on, and why the stored number itself is left
exactly as it was keyed (``loyalty.member_for_customer`` hands it to the CRM
verbatim, where a tidied number reads as a new member and strands the real
one's points).

So the merge runs first, on the normalised key: an index cannot be built over
rows that already break it, and duplicates that slipped through "Save anyway"
since 0045 are still in the book.  It writes its own undo tape before touching
anything, exactly as 0045 did, and is a no-op on a book with nothing to fold.

    manage.py merge_customers                  # what would happen, changes nothing
    manage.py customer_merge_backup --restore  # put it all back

Restoring that tape brings back rows that share a number, which this refuses.
Migrate back to 0046 first — that drops the constraint — and the restore
command says so rather than failing with an IntegrityError.
"""
from django.db import migrations, models


def merge_by_phone(apps, schema_editor):
    """Fold anything that slipped through "Save anyway" since 0045."""
    from bravepos.customers import merge_duplicates_by_phone

    merge_duplicates_by_phone(
        apps.get_model("bravepos", "Customer"),
        apps.get_model("bravepos", "Order"),
        apps.get_model("bravepos", "ParkedOrder"),
        apps.get_model("bravepos", "CustomerMergeBackup"),
        note="migration 0047",
    )


def unmerge(apps, schema_editor):
    """Rolling back does not un-merge anybody.

    Same reasoning as 0045: a schema reversal is not the same decision as "that
    merge was wrong".  The shop may have traded since, and silently rewriting
    live customer data on the way past would be worse than leaving it.  The
    undo tape stays in ``CustomerMergeBackup`` and
    ``manage.py customer_merge_backup --restore`` is the deliberate way back.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("bravepos", "0046_customer_phone_nullable"),
    ]

    operations = [
        migrations.RunPython(merge_by_phone, unmerge),
        migrations.AlterField(
            model_name="customer",
            name="phone",
            field=models.CharField(
                blank=True, default=None, max_length=32, null=True, unique=True),
        ),
    ]
