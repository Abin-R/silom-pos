"""Backfill branch_id on existing rows.

When per-branch scoping was added in 0004, every business row needs a branch.
We pick the first active Branch (creating "EmQuartier" if none exists yet) and
attach all unscoped rows to it.  This is safe because, before this migration,
the entire DB represented a single physical shop.
"""
from django.db import migrations


SCOPED_MODELS = ["Category", "Product", "Customer", "Order", "ParkedOrder", "StockMovement", "Shift"]


def backfill(apps, schema_editor):
    Branch = apps.get_model("bravepos", "Branch")
    default_branch = Branch.objects.filter(active=True).order_by("created_at").first()
    if default_branch is None:
        default_branch, _ = Branch.objects.get_or_create(
            name="EmQuartier",
            defaults={"code": "EMQ", "active": True},
        )

    for model_name in SCOPED_MODELS:
        Model = apps.get_model("bravepos", model_name)
        Model.objects.filter(branch__isnull=True).update(branch=default_branch)


def noop(apps, schema_editor):
    # Reversing is destructive (data loss).  Make it a no-op so `migrate` back
    # to 0004 just leaves the data in place.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("bravepos", "0004_branch_scoping"),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
