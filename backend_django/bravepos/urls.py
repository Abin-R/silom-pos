"""Brave POS API endpoints — same URL surface as the legacy FastAPI backend
so the existing frontend doesn't need to change a single fetch URL."""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

# Register the same ViewSets through two routers — one with the trailing slash,
# one without — so both `/api/categories` (what the frontend sends) and
# `/api/categories/` (what browsers / 3rd-party tools sometimes send) work.
# DRF's DefaultRouter doesn't natively support an optional slash, so this is
# the cleanest way to expose both forms.
def _register(r):
    r.register(r'categories', views.CategoryViewSet, basename='category')
    r.register(r'products', views.ProductViewSet, basename='product')
    r.register(r'customers', views.CustomerViewSet, basename='customer')
    r.register(r'branches', views.BranchViewSet, basename='branch')
    r.register(r'shift-categories', views.DrawerCategoryViewSet, basename='shift-category')

router = DefaultRouter()          # /api/categories/
router_no_slash = DefaultRouter(trailing_slash='')  # /api/categories
_register(router)
# basename collisions are fine — Django routes are matched by path, not by name.
_register(router_no_slash)

urlpatterns = [
    # Healthcheck
    path('', views.api_root),

    # Auth — new email/password flow with branch-scoped sessions
    path('auth/login', views.auth_login),
    path('auth/logout', views.auth_logout),
    path('auth/me', views.auth_me),
    path('auth/switch-branch', views.auth_switch_branch),
    # PIN-pad login (current frontend flow): pick branch → pick user → enter PIN.
    path('auth/branch-users', views.auth_branch_users),
    path('auth/pin-login', views.auth_pin_login),
    # Legacy PIN endpoint — keep for backwards compat until everyone migrates.
    path('auth/verify-pin', views.verify_pin),

    # Settings — single document, GET + PUT.
    path('settings', views.settings_view),

    # Orders
    path('orders', views.orders_list_create),
    path('orders/<uuid:order_id>/status', views.order_update_status),

    # Parked orders
    path('parked-orders', views.parked_orders_list_create),
    path('parked-orders/<uuid:pid>', views.parked_orders_delete),

    # Stock movements
    path('stock-movements', views.stock_movements),

    # Stock documents (multi-line stock-in / stock-out)
    path('stock-documents', views.stock_documents),
    path('stock-documents/<uuid:doc_id>', views.stock_document_detail),

    # Shifts
    path('shifts/current', views.shift_current),
    path('shifts/open', views.shift_open),
    path('shifts/movement', views.shift_movement),
    path('shifts/close', views.shift_close),
    path('shifts/<uuid:shift_id>/summary', views.shift_summary),
    path('shifts', views.shifts_list),

    # Customer stats (manual route — keeps `/customers/` ViewSet clean)
    path('customers/<uuid:customer_id>/stats', views.customer_stats),

    # Dashboard
    path('dashboard', views.dashboard),
    path('dashboard/channels', views.dashboard_channels),

    # Beam payment
    path('beam/charge', views.beam_charge_create),
    path('beam/charge/<str:charge_id>', views.beam_charge_status),

    # Printer
    path('printers/detect', views.printer_detect),
    path('printers/status', views.printer_status),
    path('print-test', views.print_test),
    path('print-receipt/<uuid:order_id>', views.print_receipt),

    # Demo data
    path('seed', views.seed_data),

    # ViewSet-backed CRUD (categories, products, customers) — both URL forms.
    path('', include(router.urls)),
    path('', include(router_no_slash.urls)),
]
