from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('bravepos', '0063_discount_free_category'),
    ]

    operations = [
        migrations.AlterField(
            model_name='staff',
            name='role',
            field=models.CharField(choices=[('admin', 'Admin'), ('cashier', 'Cashier'), ('viewer', 'Viewer'), ('packer', 'Packer')], default='cashier', max_length=16),
        ),
    ]
