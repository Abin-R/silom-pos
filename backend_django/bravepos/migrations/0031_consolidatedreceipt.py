# Depends on 0030, the last migration on main — NOT on the 0031/0032 in this
# working tree, which are someone else's uncommitted work in progress. Chaining
# onto those would make this migration unappliable anywhere they haven't landed,
# production included.
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('bravepos', '0030_pos_full_tax_invoice'),
    ]

    operations = [
        migrations.CreateModel(
            name='ConsolidatedReceipt',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('date', models.DateField(db_index=True)),
                ('peak_queue_id', models.CharField(blank=True, default='', max_length=128)),
                ('response', models.JSONField(blank=True, null=True)),
                ('needs_reissue', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('branch', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='consolidated_receipts', to='bravepos.branch')),
                ('orders', models.ManyToManyField(blank=True, related_name='consolidated_receipts', to='bravepos.order')),
            ],
            options={
                'ordering': ['-date'],
                'unique_together': {('branch', 'date')},
            },
        ),
    ]
