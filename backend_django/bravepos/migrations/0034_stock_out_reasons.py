import uuid

import django.db.models.deletion
from django.db import migrations, models


def seed_reasons(apps, schema_editor):
    """Give every existing branch the default reason list.

    Branches created *after* this migration are seeded by the post_save signal
    in ``bravepos.signals`` instead, so the two paths must stay in step — both
    read ``DEFAULT_STOCK_OUT_REASONS`` from models.py rather than keeping their
    own copy.
    """
    from bravepos.models import DEFAULT_STOCK_OUT_REASONS

    Branch = apps.get_model("bravepos", "Branch")
    StockOutReason = apps.get_model("bravepos", "StockOutReason")
    rows = [
        StockOutReason(
            branch=branch, name=name, name_th=name_th, sort_order=i,
        )
        for branch in Branch.objects.all()
        for i, (name, name_th) in enumerate(DEFAULT_STOCK_OUT_REASONS)
    ]
    StockOutReason.objects.bulk_create(rows)


def unseed_reasons(apps, schema_editor):
    apps.get_model("bravepos", "StockOutReason").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("bravepos", "0033_merge_0031_consolidatedreceipt_0032_product_par_level"),
    ]

    operations = [
        migrations.CreateModel(
            name="StockOutReason",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=120)),
                ("name_th", models.CharField(blank=True, default="", max_length=120)),
                ("sort_order", models.IntegerField(default=0)),
                ("active", models.BooleanField(default=True)),
                ("branch", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="stock_out_reasons", to="bravepos.branch",
                )),
            ],
            options={
                "ordering": ["sort_order", "name"],
            },
        ),
        migrations.AddIndex(
            model_name="stockoutreason",
            index=models.Index(fields=["branch"], name="bravepos_st_branch__6f8aab_idx"),
        ),
        migrations.AddField(
            model_name="stockdocument",
            name="reason",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.RunPython(seed_reasons, unseed_reasons),
    ]
