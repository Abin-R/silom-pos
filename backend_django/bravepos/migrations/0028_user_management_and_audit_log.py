"""Backoffice user management + audit log.

Schema: `Staff.username` / `.backoffice_access` / `.last_login_at`, and the
new `AuditLog` table.

Data: decide who keeps backoffice access. Until now *any* active Staff row
could sign into the backoffice with email + password, and
`staff_provisioning.ensure_branch_staff` gives every auto-created branch admin
the same well-known password (`admin1234`). That combination means the
backoffice is currently reachable by anyone who guesses a branch's generated
email. The backfill below closes it:

  * rows still on a shared provisioning default password → access denied
  * rows with an auto-generated `admin.<slug>@` / `cashier.<slug>@` email
    → access denied
  * everything else (i.e. real, human-created accounts) → access granted, so
    nobody currently using the backoffice is locked out by this migration.
"""
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone
import uuid


AUTO_EMAIL_PREFIXES = ("admin.", "cashier.")
AUTO_EMAIL_DOMAIN = "@rollingpinn.com"
DEFAULT_PASSWORDS = ("admin1234", "cashier1234")


def grant_backoffice_access(apps, schema_editor):
    from django.contrib.auth.hashers import check_password

    Staff = apps.get_model("bravepos", "Staff")
    for staff in Staff.objects.all().iterator():
        email = (staff.email or "").lower()
        auto_email = email.endswith(AUTO_EMAIL_DOMAIN) and email.startswith(
            AUTO_EMAIL_PREFIXES
        )
        shared_password = any(
            check_password(pw, staff.password_hash or "") for pw in DEFAULT_PASSWORDS
        )
        if auto_email or shared_password:
            continue
        Staff.objects.filter(pk=staff.pk).update(backoffice_access=True)


def revoke_backoffice_access(apps, schema_editor):
    """Reverse leaves the column populated — it is dropped by the schema
    operation that follows. Nothing to undo."""


class Migration(migrations.Migration):

    dependencies = [
        ("bravepos", "0027_blank_settings_pos_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="staff",
            name="username",
            field=models.CharField(
                blank=True, db_index=True, max_length=64, null=True, unique=True
            ),
        ),
        migrations.AddField(
            model_name="staff",
            name="backoffice_access",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="staff",
            name="last_login_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(grant_backoffice_access, revoke_backoffice_access),
        migrations.CreateModel(
            name="AuditLog",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("actor_label", models.CharField(blank=True, default="", max_length=200)),
                ("actor_role", models.CharField(blank=True, default="", max_length=32)),
                ("action", models.CharField(choices=[
                    ("create", "Created"), ("update", "Updated"), ("delete", "Deleted"),
                    ("login", "Signed in"), ("login_failed", "Sign-in failed"),
                    ("logout", "Signed out"), ("export", "Exported"),
                ], db_index=True, max_length=16)),
                ("model", models.CharField(blank=True, db_index=True, default="", max_length=64)),
                ("object_id", models.CharField(blank=True, default="", max_length=64)),
                ("object_label", models.CharField(blank=True, default="", max_length=250)),
                ("changes", models.JSONField(blank=True, default=dict)),
                ("source", models.CharField(blank=True, default="", max_length=16)),
                ("method", models.CharField(blank=True, default="", max_length=8)),
                ("path", models.CharField(blank=True, default="", max_length=300)),
                ("ip", models.GenericIPAddressField(blank=True, null=True)),
                ("user_agent", models.CharField(blank=True, default="", max_length=300)),
                ("note", models.CharField(blank=True, default="", max_length=300)),
                ("actor", models.ForeignKey(blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="audit_entries", to="bravepos.staff")),
                ("branch", models.ForeignKey(blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="audit_entries", to="bravepos.branch")),
            ],
            options={"ordering": ["-at"]},
        ),
        migrations.AddIndex(
            model_name="auditlog",
            index=models.Index(fields=["-at"], name="bravepos_au_at_0577a4_idx"),
        ),
        migrations.AddIndex(
            model_name="auditlog",
            index=models.Index(fields=["actor", "-at"], name="bravepos_au_actor_i_ffaee6_idx"),
        ),
        migrations.AddIndex(
            model_name="auditlog",
            index=models.Index(fields=["model", "-at"], name="bravepos_au_model_03db6e_idx"),
        ),
        migrations.AddIndex(
            model_name="auditlog",
            index=models.Index(fields=["action", "-at"], name="bravepos_au_action_136e3f_idx"),
        ),
    ]
