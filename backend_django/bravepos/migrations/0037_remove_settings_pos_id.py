from django.db import migrations, models


def carry_pos_id_to_branch(apps, schema_editor):
    """Don't drop an RD machine number that only exists on Settings.

    The shop-wide value was what every receipt printed, so if some branch was
    relying on it and has no number of its own, park it on that branch rather
    than losing it.  Only fills branches that are blank, and only when exactly
    one branch exists — with several branches there is no way to tell which one
    the shop-wide number belonged to, and guessing is how the wrong number got
    printed in the first place.
    """
    Settings = apps.get_model('bravepos', 'Settings')
    Branch = apps.get_model('bravepos', 'Branch')
    row = Settings.objects.first()
    value = (row.pos_id or '').strip() if row else ''
    if not value:
        return
    if Branch.objects.exclude(pos_id='').filter(pos_id=value).exists():
        return  # already held by the branch it belongs to
    blanks = list(Branch.objects.filter(pos_id=''))
    if len(blanks) == 1:
        blanks[0].pos_id = value
        blanks[0].save(update_fields=['pos_id'])


def restore_settings_pos_id(apps, schema_editor):
    """Re-adding the column leaves it blank; the numbers live on Branch now."""


class Migration(migrations.Migration):

    dependencies = [
        ('bravepos', '0036_settle_open_order_statuses'),
    ]

    operations = [
        migrations.RunPython(carry_pos_id_to_branch, restore_settings_pos_id),
        migrations.RemoveField(
            model_name='settings',
            name='pos_id',
        ),
    ]
