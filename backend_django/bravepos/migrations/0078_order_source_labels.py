# Order.source values renamed: "table" -> "counter", "kiosk" -> "self_order".

from django.db import migrations, models

RENAMES = {"table": "counter", "kiosk": "self_order"}


def forwards(apps, schema_editor):
    Order = apps.get_model("bravepos", "Order")
    for old, new in RENAMES.items():
        Order.objects.filter(source=old).update(source=new)


def backwards(apps, schema_editor):
    Order = apps.get_model("bravepos", "Order")
    for old, new in RENAMES.items():
        Order.objects.filter(source=new).update(source=old)


class Migration(migrations.Migration):

    dependencies = [
        ("bravepos", "0077_branch_queue_prefix"),
    ]

    operations = [
        migrations.AlterField(
            model_name="order",
            name="source",
            field=models.CharField(
                choices=[
                    ("counter", "Counter"),
                    ("delivery", "Delivery"),
                    ("self_order", "Self Order"),
                    ("other", "Other"),
                ],
                default="counter",
                max_length=16,
            ),
        ),
        migrations.RunPython(forwards, backwards),
    ]
