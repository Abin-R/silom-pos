"""Drop the Thai label 0068 gave "Unit Split" — the shop wants it English only.

Only rows still carrying the seeded wording are cleared, so a label an admin
typed in themselves survives.
"""
from __future__ import annotations

from django.db import migrations


def clear_thai(apps, schema_editor):
    StockOutReason = apps.get_model("bravepos", "StockOutReason")
    StockOutReason.objects.filter(name="Unit Split", name_th="แยกหน่วย").update(name_th="")


class Migration(migrations.Migration):

    dependencies = [
        ("bravepos", "0068_stock_out_reason_unit_split"),
    ]

    operations = [
        migrations.RunPython(clear_thai, migrations.RunPython.noop),
    ]
