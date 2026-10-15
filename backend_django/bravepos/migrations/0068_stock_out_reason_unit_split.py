"""Add "Unit Split" to every branch's stock-out reasons.

New branches pick it up from ``DEFAULT_STOCK_OUT_REASONS`` through the
post_save signal; this covers the branches that already exist.  It reuses
0040's reseed with nothing to retire, so the defaults keep their order and any
reason an admin added stays below them.
"""
from __future__ import annotations

import importlib

from django.db import migrations


def add_unit_split(apps, schema_editor):
    from bravepos.models import DEFAULT_STOCK_OUT_REASONS

    recode = importlib.import_module("bravepos.migrations.0040_stock_out_reason_codes")
    recode._reseed(apps, DEFAULT_STOCK_OUT_REASONS, [])


def remove_unit_split(apps, schema_editor):
    # Saved documents snapshot the reason text, so history keeps the wording.
    StockOutReason = apps.get_model("bravepos", "StockOutReason")
    StockOutReason.objects.filter(name="Unit Split").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("bravepos", "0067_product_sale_windows"),
    ]

    operations = [
        migrations.RunPython(add_unit_split, remove_unit_split),
    ]
