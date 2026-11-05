from django.db import migrations, models


def mark_past_refunds(apps, schema_editor):
    """Bills Beam was asked to refund are refunds; the rest stay blank."""
    Order = apps.get_model('bravepos', 'Order')
    Order.objects.filter(status='cancel').exclude(
        beam_refund_id='', beam_refund_error='',
    ).update(cancel_type='refund')


class Migration(migrations.Migration):

    dependencies = [
        ('bravepos', '0079_common_queue_replaces_queue_prefix'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='cancel_type',
            field=models.CharField(
                blank=True, choices=[('void', 'Void'), ('refund', 'Refund')],
                default='', max_length=8,
            ),
        ),
        migrations.AddField(
            model_name='order',
            name='stock_returned',
            field=models.BooleanField(default=False),
        ),
        migrations.AlterField(
            model_name='stockmovement',
            name='type',
            field=models.CharField(
                choices=[
                    ('in', 'Stock in'), ('out', 'Stock out'), ('adjust', 'Adjust'),
                    ('check', 'Check'), ('return', 'Returned from bill'),
                ],
                max_length=16,
            ),
        ),
        migrations.RunPython(mark_past_refunds, migrations.RunPython.noop),
    ]
