from django.db import migrations, models


def blank_placeholder_pos_id(apps, schema_editor):
    """Clear the legacy placeholder POS ID so the app shows nothing until the
    Revenue Department issues an approved machine number.  Only touches the
    old default ("001"); a real RD-issued value is left untouched."""
    Settings = apps.get_model('bravepos', 'Settings')
    Settings.objects.filter(pos_id='001').update(pos_id='')


def restore_placeholder_pos_id(apps, schema_editor):
    Settings = apps.get_model('bravepos', 'Settings')
    Settings.objects.filter(pos_id='').update(pos_id='001')


class Migration(migrations.Migration):

    dependencies = [
        ('bravepos', '0026_branch_self_order_enabled'),
    ]

    operations = [
        migrations.AlterField(
            model_name='settings',
            name='pos_id',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.RunPython(blank_placeholder_pos_id, restore_placeholder_pos_id),
    ]
