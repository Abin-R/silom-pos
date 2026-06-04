"""Adds a 4-digit PIN to Staff for the tablet PIN-pad login.

Existing rows are seeded so nobody gets locked out on first deploy:
admin → "1234", cashier → "0000" (matching the legacy /auth/verify-pin
behaviour the app used to ship with). Admins can change these via Django
admin / a follow-up settings screen.
"""
from django.contrib.auth.hashers import make_password
from django.db import migrations, models


DEFAULT_PINS = {"admin": "1234", "cashier": "0000"}


def seed_default_pins(apps, _schema_editor):
    Staff = apps.get_model("bravepos", "Staff")
    for staff in Staff.objects.all():
        if staff.pin_hash:
            continue
        plain = DEFAULT_PINS.get(staff.role, "0000")
        staff.pin_hash = make_password(plain)
        staff.save(update_fields=["pin_hash"])


def clear_pins(apps, _schema_editor):
    Staff = apps.get_model("bravepos", "Staff")
    Staff.objects.update(pin_hash="")


class Migration(migrations.Migration):

    dependencies = [
        ('bravepos', '0008_branch_address_lines'),
    ]

    operations = [
        migrations.AddField(
            model_name='staff',
            name='pin_hash',
            field=models.CharField(blank=True, default='', max_length=256),
        ),
        migrations.RunPython(seed_default_pins, clear_pins),
    ]
