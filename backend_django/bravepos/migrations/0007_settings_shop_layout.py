"""Adds the four shop-info fields the backoffice Shop page exposes:
address_line_1 / address_line_2 (replaces the single free-form blob on
receipts), company_name (legal entity printed above the address), and
currency (default "THB", surfaced in the SilomPOS-style dropdown)."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bravepos', '0006_branch_logo_url'),
    ]

    operations = [
        migrations.AddField(
            model_name='settings',
            name='address_line_1',
            field=models.CharField(blank=True, default='', max_length=200),
        ),
        migrations.AddField(
            model_name='settings',
            name='address_line_2',
            field=models.CharField(blank=True, default='', max_length=200),
        ),
        migrations.AddField(
            model_name='settings',
            name='company_name',
            field=models.CharField(blank=True, default='', max_length=200),
        ),
        migrations.AddField(
            model_name='settings',
            name='currency',
            field=models.CharField(default='THB', max_length=8),
        ),
    ]
