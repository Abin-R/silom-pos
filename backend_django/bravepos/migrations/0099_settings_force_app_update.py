from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bravepos', '0098_production_lead_time_and_delivery_dates'),
    ]

    operations = [
        migrations.AddField(
            model_name='settings',
            name='force_app_update',
            field=models.BooleanField(default=True),
        ),
    ]
