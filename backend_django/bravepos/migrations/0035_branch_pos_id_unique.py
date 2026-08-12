from django.db import migrations, models


def blank_duplicate_pos_ids(apps, schema_editor):
    """Clear POS IDs that are shared by more than one branch.

    A POS ID is the Revenue Department's machine number for a single till, so a
    duplicate is always a paste error: one of the two branches is printing the
    other's number on its tax invoices.  The oldest branch holding the value
    keeps it (it is the one that was registered with it) and the others are
    blanked back to "not registered yet", which is exactly what a fresh branch
    looks like — they stop offering the full tax invoice until someone types
    the right number in the backoffice.

    Almost always a no-op: it only has anything to do on a database where the
    duplicate was entered before the constraint below existed.
    """
    Branch = apps.get_model('bravepos', 'Branch')
    seen = set()
    for branch in Branch.objects.exclude(pos_id='').order_by('created_at', 'id'):
        if branch.pos_id in seen:
            print(
                f"  branch {branch.name!r}: clearing duplicate POS ID "
                f"{branch.pos_id!r} (already used by an older branch)"
            )
            Branch.objects.filter(pk=branch.pk).update(pos_id='')
        else:
            seen.add(branch.pos_id)


class Migration(migrations.Migration):

    dependencies = [
        ('bravepos', '0034_stock_out_reasons'),
    ]

    operations = [
        migrations.RunPython(blank_duplicate_pos_ids, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='branch',
            constraint=models.UniqueConstraint(
                condition=models.Q(('pos_id', ''), _negated=True),
                fields=('pos_id',),
                name='branch_pos_id_unique_when_set',
            ),
        ),
    ]
