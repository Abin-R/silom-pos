"""Root URL config.  All Brave POS endpoints live under ``/api/`` so the
frontend's existing ``EXPO_PUBLIC_BACKEND_URL`` keeps working unchanged.

The server-rendered backoffice lives under ``/backoffice/``.

``/receipt/<order_number>/`` is the public landing page for the QR code
printed on every thermal receipt — issue a full tax invoice, or leave a
review.  It is intentionally root-level (NOT under ``/backoffice/``) so it
isn't gated by Django's auth login."""
from django.urls import include, path

from backoffice import views as backoffice_views
from bravepos import public_views

urlpatterns = [
    path('api/', include('bravepos.urls')),
    path('backoffice/', include('backoffice.urls')),

    # Customer self-ordering.  Root-level and unauthenticated for the same
    # reason as /receipt/ below: it is opened by a customer scanning a QR, who
    # has no login.  Authorisation for a specific cart is the opaque token in
    # the path.  See bravepos/public_views.py for the trust model.
    path('order/<uuid:branch_id>/', public_views.order_menu, name='selforder_menu'),
    path('order/<uuid:branch_id>/start/', public_views.order_start, name='selforder_start'),
    # Product images as separate cacheable resources rather than base64 inlined
    # into the menu HTML — see public_views.product_image.
    path('order/<uuid:branch_id>/img/<uuid:product_id>/', public_views.product_image,
         name='selforder_product_image'),
    path('order/s/<str:token>/', public_views.order_pay_page, name='selforder_pay_page'),
    path('order/s/<str:token>/pay/', public_views.order_pay, name='selforder_pay'),
    path('order/s/<str:token>/status/', public_views.order_status, name='selforder_status'),

    path('receipt/<str:order_number>/', backoffice_views.customer_receipt, name='customer_receipt'),
    path('receipt/<str:order_number>/tax-invoice/', backoffice_views.create_tax_invoice, name='create_tax_invoice'),
    path('receipt/<str:order_number>/tax-invoice/save/', backoffice_views.save_tax_invoice, name='save_tax_invoice'),
    path('receipt/<str:order_number>/tax-invoice/progress/', backoffice_views.tax_invoice_progress, name='tax_invoice_progress'),
    path('receipt/<str:order_number>/tax-invoice/process/', backoffice_views.tax_invoice_process, name='tax_invoice_process'),
]
