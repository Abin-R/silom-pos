"""Root URL config.  All Brave POS endpoints live under ``/api/`` so the
frontend's existing ``EXPO_PUBLIC_BACKEND_URL`` keeps working unchanged.

The server-rendered backoffice lives under ``/backoffice/``.

``/receipt/<order_number>/`` is the public landing page for the QR code
printed on every thermal receipt.  It is intentionally root-level (NOT
under ``/backoffice/``) so it isn't gated by Django's auth login."""
from django.urls import include, path

from backoffice import views as backoffice_views

urlpatterns = [
    path('api/', include('bravepos.urls')),
    path('backoffice/', include('backoffice.urls')),
    path('receipt/<str:order_number>/', backoffice_views.customer_receipt, name='customer_receipt'),
]
