"""Re-point every branch's stock-out reasons at the shop's spreadsheet codes.

The list shipped in 0034 was a guess; the shop reconciles stock-outs against a
sheet whose validation list is Expired / Tasting / Damaged (In-Store) /
Damaged (In-Transit) / Stock Transfer.  Two lists meant staff picked "Damaged"
in the app and then had to decide in-store vs in-transit again in the sheet, by
hand, from memory.

Saved documents are untouched: ``StockDocument.reason`` is a text snapshot, so
history keeps whatever wording it was recorded with — retiring a reason code
here can never rewrite a stock-out that already happened.

Only rows still spelled exactly as 0034 seeded them are retired.  A row an
admin renamed, added, or deactivated is a human decision and survives, sorted
below the new defaults.
"""
from __future__ import annotations

from django.db import migrations


def _reseed(apps, defaults, retire):
    """Replace the seeded defaults on every branch with ``defaults``.

    ``retire`` names the rows that count as untouched leftovers of the previous
    list; anything else on the branch is kept and pushed below the new block.
    """
    Branch = apps.get_model("bravepos", "Branch")
    StockOutReason = apps.get_model("bravepos", "StockOutReason")
    keep_names = [name for name, _th in defaults]

    for branch in Branch.objects.all():
        rows = StockOutReason.objects.filter(branch=branch)
        rows.filter(name__in=retire).exclude(name__in=keep_names).delete()

        for i, (name, name_th) in enumerate(defaults):
            row, created = StockOutReason.objects.get_or_create(
                branch=branch, name=name,
                defaults={"name_th": name_th, "sort_order": i},
            )
            if not created and row.sort_order != i:
                # A branch that already had this name keeps its own active flag
                # and Thai wording — only its place in the list is claimed.
                row.sort_order = i
                row.save(update_fields=["sort_order"])

        survivors = (
            StockOutReason.objects.filter(branch=branch)
            .exclude(name__in=keep_names).order_by("sort_order", "name")
        )
        for j, row in enumerate(survivors, start=len(defaults)):
            if row.sort_order != j:
                row.sort_order = j
                row.save(update_fields=["sort_order"])


def to_sheet_codes(apps, schema_editor):
    from bravepos.models import (
        DEFAULT_STOCK_OUT_REASONS, SUPERSEDED_STOCK_OUT_REASONS,
    )
    _reseed(apps, DEFAULT_STOCK_OUT_REASONS, SUPERSEDED_STOCK_OUT_REASONS)


def back_to_the_0034_codes(apps, schema_editor):
    from bravepos.models import DEFAULT_STOCK_OUT_REASONS

    old = [
        ("Waste / Expired", "ของเสีย / หมดอายุ"),
        ("Damaged", "สินค้าชำรุด"),
        ("Staff consumption", "พนักงานบริโภค"),
        ("Tasting / Sample", "ชิม / ตัวอย่าง"),
        ("Transfer to branch", "โอนไปสาขาอื่น"),
        ("Return to vendor", "คืนผู้ขาย"),
        ("Complimentary", "ของแถม / โปรโมชั่น"),
        ("Other", "อื่นๆ"),
    ]
    _reseed(apps, old, [name for name, _th in DEFAULT_STOCK_OUT_REASONS])


class Migration(migrations.Migration):

    dependencies = [
        ("bravepos", "0037_remove_settings_pos_id"),
    ]

    operations = [
        migrations.RunPython(to_sheet_codes, back_to_the_0034_codes),
    ]
