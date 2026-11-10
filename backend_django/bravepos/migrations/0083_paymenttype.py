import uuid

import django.db.models.deletion
from django.db import migrations, models

# A snapshot, not an import of DEFAULT_PAYMENT_TYPES: this is what the till
# offered on the day it stopped being hardcoded.
TILL_LIST = [
    "EDC Kbank", "EDC Bangkok", "Brave Brand Co.,Ltd",
    "Thai Dot Com Pay", "คนละครึ่ง", "EDC SCB", "QR",
]


def seed(apps, schema_editor):
    """Every branch starts with the list its till already shows."""
    Branch = apps.get_model("bravepos", "Branch")
    PaymentType = apps.get_model("bravepos", "PaymentType")
    PaymentType.objects.bulk_create([
        PaymentType(branch=branch, name=name, sort_order=i)
        for branch in Branch.objects.all()
        for i, name in enumerate(TILL_LIST)
    ])


class Migration(migrations.Migration):

    dependencies = [
        ('bravepos', '0082_selforder_late_paid'),
    ]

    operations = [
        migrations.CreateModel(
            name='PaymentType',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=50)),
                ('sort_order', models.IntegerField(default=0)),
                ('active', models.BooleanField(default=True)),
                ('branch', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='payment_types', to='bravepos.branch')),
            ],
            options={
                'ordering': ['sort_order', 'name'],
            },
        ),
        migrations.AddConstraint(
            model_name='paymenttype',
            constraint=models.UniqueConstraint(fields=('branch', 'name'), name='uniq_payment_type_per_branch'),
        ),
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
