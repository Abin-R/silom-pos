"""Adds the split address fields to Branch so the SilomPOS-style Shop page
can edit each branch independently. The legacy single `address` blob is
kept in sync on save."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bravepos', '0007_settings_shop_layout'),
    ]

    operations = [
        migrations.AddField(
            model_name='branch',
            name='address_line_1',
            field=models.CharField(blank=True, default='', max_length=200),
        ),
        migrations.AddField(
            model_name='branch',
            name='address_line_2',
            field=models.CharField(blank=True, default='', max_length=200),
        ),
    ]
