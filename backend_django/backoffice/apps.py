from django.apps import AppConfig


class BackofficeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "backoffice"
    label = "backoffice"

    def ready(self):
        # The stock user_logged_in receiver calls user.save(update_fields=["last_login"]),
        # but our Staff model has no last_login column — so login POSTs 500 after a
        # successful auth. Disconnect the receiver; per-session activity is already
        # tracked via BranchSession, which is a more useful audit trail anyway.
        from django.contrib.auth.signals import user_logged_in
        from django.contrib.auth.models import update_last_login
        user_logged_in.disconnect(update_last_login)
