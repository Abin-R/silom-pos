from django.db import migrations


# Keep migration data independent of future edits to the application defaults.
DEFAULT_NAMES = (
    ("Shop Supply", "รับสินค้าจากร้าน"),
    ("Entry Correction", "แก้ไขการบันทึกข้อมูล"),
    ("Branch Transfer", "รับโอนระหว่างสาขา"),
)


def fill_blank_thai_names(apps, schema_editor):
    StockInReason = apps.get_model("bravepos", "StockInReason")
    rows = StockInReason.objects.using(schema_editor.connection.alias)
    for name, name_th in DEFAULT_NAMES:
        rows.filter(name=name, name_th="").update(name_th=name_th)


class Migration(migrations.Migration):
    dependencies = [("bravepos", "0093_stock_in_reasons")]

    operations = [
        # Do not clear Thai names on reverse: admins may edit them after rollout.
        migrations.RunPython(fill_blank_thai_names, migrations.RunPython.noop),
    ]
