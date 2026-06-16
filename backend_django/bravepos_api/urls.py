"""Root URL config.  All Brave POS endpoints live under ``/api/`` so the
frontend's existing ``EXPO_PUBLIC_BACKEND_URL`` keeps working unchanged.

The server-rendered backoffice lives under ``/backoffice/``.

``/receipt/<order_number>/`` is the public landing page for the QR code
printed on every thermal receipt.  It is intentionally root-level (NOT
under ``/backoffice/``) so it isn't gated by Django's auth login."""
from django.urls import include, path
from django.http import HttpResponse

from backoffice import views as backoffice_views


def sentry_debug(request):
    """TEMPORARY — verifies Sentry captures backend errors. Raises on purpose;
    Sentry should log a ZeroDivisionError. Remove this route once confirmed."""
    1 / 0
    return HttpResponse('unreachable')


urlpatterns = [
    path('sentry-debug/', sentry_debug),
    path('api/', include('bravepos.urls')),
    path('backoffice/', include('backoffice.urls')),
    path('receipt/<str:order_number>/', backoffice_views.customer_receipt, name='customer_receipt'),
    path('receipt/<str:order_number>/tax-invoice/', backoffice_views.create_tax_invoice, name='create_tax_invoice'),
    path('receipt/<str:order_number>/tax-invoice/save/', backoffice_views.save_tax_invoice, name='save_tax_invoice'),
    path('receipt/<str:order_number>/tax-invoice/progress/', backoffice_views.tax_invoice_progress, name='tax_invoice_progress'),
    path('receipt/<str:order_number>/tax-invoice/process/', backoffice_views.tax_invoice_process, name='tax_invoice_process'),
]
