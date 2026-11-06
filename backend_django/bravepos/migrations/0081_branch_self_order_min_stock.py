from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bravepos', '0080_selforder_hold_expires_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='branch',
            name='self_order_min_stock',
            field=models.PositiveIntegerField(default=0),
        ),
    ]
