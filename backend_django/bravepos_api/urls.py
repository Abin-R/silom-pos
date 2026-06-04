"""Root URL config.  All Brave POS endpoints live under ``/api/`` so the
frontend's existing ``EXPO_PUBLIC_BACKEND_URL`` keeps working unchanged.

The server-rendered backoffice lives under ``/backoffice/``."""
from django.urls import include, path

urlpatterns = [
    path('api/', include('bravepos.urls')),
    path('backoffice/', include('backoffice.urls')),
]
