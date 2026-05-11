from django.apps import AppConfig


class BraveposConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'bravepos'
    # Explicit label so every table lands as `bravepos_<modelname>`.
    # This is the firewall against colliding with the other Django apps that
    # share the production Postgres database.
    label = 'bravepos'
