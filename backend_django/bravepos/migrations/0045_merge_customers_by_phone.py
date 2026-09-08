"""Fold customers who share a phone number into a single row.

The customer book was branch-scoped until now, so the same person registered
at two branches is two rows today: each holding half their visits, each signed
up to the CRM separately, and neither showing a cashier the whole customer.
Making the book shop-wide puts both rows in front of every till at once, so
they have to be reconciled — otherwise going global just means every branch
now sees the duplicates instead of only its own half.

This deletes rows and cannot be undone from the schema, so it writes an undo
tape into ``CustomerMergeBackup`` first, in this same transaction, before it
touches anything: every customer row as it stands, plus the previous owner of
every bill and held cart it moves.  Restoring the customer table alone would
not be enough — the moved bills would stay with the survivor.

    manage.py merge_customers                  # what would happen, changes nothing
    manage.py customer_merge_backup --restore  # put it all back
    manage.py customer_merge_backup --delete   # once you are satisfied

Bills are re-pointed, never rewritten: ``Order.customer_name`` keeps whatever
was printed on the receipt at the time.  Held carts are re-pointed too, since
``ParkedOrder.customer_id`` is a bare UUID that no foreign key would follow.

Matching is on phone alone and skips customers with no number on file — see
``bravepos/customers.py`` for why, and for how the surviving row is chosen.
"""
from django.db import migrations


def merge_by_phone(apps, schema_editor):
    from bravepos.customers import merge_duplicates_by_phone

    merge_duplicates_by_phone(
        apps.get_model("bravepos", "Customer"),
        apps.get_model("bravepos", "Order"),
        apps.get_model("bravepos", "ParkedOrder"),
        apps.get_model("bravepos", "CustomerMergeBackup"),
        note="migration 0045",
    )


def unmerge(apps, schema_editor):
    """Rolling the schema back does not un-merge anybody.

    Nothing here is undone automatically, because a migration reversal is not
    the same decision as "that merge was wrong": the shop may have registered
    customers since, and silently rewriting live data on the way past would be
    worse than leaving it. The undo tape stays in ``CustomerMergeBackup``, and
    ``manage.py customer_merge_backup --restore`` is the deliberate way back.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("bravepos", "0044_customer_merge_backup"),
    ]

    operations = [
        migrations.RunPython(merge_by_phone, unmerge),
    ]
