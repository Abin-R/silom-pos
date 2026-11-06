from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bravepos', '0080_order_cancel_type_stock_returned'),
    ]

    operations = [
        migrations.AddField(
            model_name='selforder',
            name='hold_expires_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
