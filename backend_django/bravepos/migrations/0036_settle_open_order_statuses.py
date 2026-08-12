from django.db import migrations


def settle_open_statuses(apps, schema_editor):
    """Move every un-cancelled bill to "completed".

    ``new`` and ``preparing`` only ever meant "the kitchen has not finished
    this yet" — they were columns on the Order Hub kanban, which is switched
    off because nobody used it.  With no screen left to tap, a bill parked in
    either state would sit there forever, so the rows are settled to the state
    they would have reached.

    Financially inert: every report already asks ``exclude(status='cancel')``
    rather than ``filter(status='completed')``, precisely because a paid bill
    counts whatever column it sits in.  Shift cash and the Peak consolidation
    likewise branch only on cancelled-ness.  So this changes what the app
    *displays* (chiefly "outstanding" on the customer detail, which becomes
    empty) and not one satang of any total.

    ``cancel`` is deliberately untouched: those bills are voided, their
    ``voided_by``/``voided_at`` stamps are real, and un-voiding them here would
    silently add their money back to days that have already been filed.
    """
    Order = apps.get_model('bravepos', 'Order')
    moved = Order.objects.filter(status__in=('new', 'preparing')).update(
        status='completed',
    )
    if moved:
        print(f"  settled {moved} open order(s) to completed")


class Migration(migrations.Migration):

    dependencies = [
        ('bravepos', '0035_branch_pos_id_unique'),
    ]

    operations = [
        # No reverse: which of these were "new" and which "preparing" is not
        # recoverable once merged, and nothing downstream reads the difference.
        migrations.RunPython(settle_open_statuses, migrations.RunPython.noop),
    ]
