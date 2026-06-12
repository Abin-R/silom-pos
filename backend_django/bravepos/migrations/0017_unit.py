import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bravepos', '0016_stockdocumentitem_before_qty_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='Unit',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=64)),
                ('order', models.IntegerField(default=0)),
                ('active', models.BooleanField(default=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('branch', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='units', to='bravepos.branch')),
            ],
            options={
                'ordering': ['order', 'name'],
                'indexes': [models.Index(fields=['branch'], name='bravepos_un_branch__cd6ed3_idx')],
            },
        ),
    ]
