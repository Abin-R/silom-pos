from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = "backoffice"

urlpatterns = [
    # Auth — Django's built-in LoginView/LogoutView. No signup; admins are
    # provisioned via `manage.py createsuperuser`.
    path(
        "login/",
        auth_views.LoginView.as_view(
            template_name="backoffice/login.html",
            redirect_authenticated_user=True,
        ),
        name="login",
    ),
    path(
        "logout/",
        auth_views.LogoutView.as_view(next_page="backoffice:login"),
        name="logout",
    ),

    # The design system's stylesheet. Served by Django, not `staticfiles` —
    # see `views.backoffice_css` for why. Unauthenticated so the login page
    # can load it.
    path("app.css", views.backoffice_css, name="app_css"),

    # Product photos as separate cacheable resources rather than base64
    # inlined into the catalogue markup — see `views.product_image`.
    path("products/<uuid:product_id>/image", views.product_image,
         name="product_image"),

    path("", views.dashboard, name="home"),
    path("dashboard", views.dashboard, name="dashboard"),
    path("transactions", views.transactions, name="transactions"),
    path("transactions/export", views.transactions_export, name="transactions_export"),
    path("receipt/<str:order_number>/print", views.receipt_print, name="receipt_print"),

    # Sales reports
    path("report/daily", views.report_daily, name="report_daily"),
    # `daily/export` must precede `daily/<date_str>` so it isn't captured as a date.
    path("report/daily/export", views.report_daily_export, name="report_daily_export"),
    path("report/daily/<str:date_str>", views.report_daily_detail, name="report_daily_detail"),
    path("report/daily/<str:date_str>/export", views.report_daily_detail_export, name="report_daily_detail_export"),
    path("report/sell", views.report_sell, name="report_sell"),
    path("report/sell/export", views.report_sell_export, name="report_sell_export"),
    path("report/sku", views.report_sku, name="report_sku"),
    path("report/tax", views.report_tax, name="report_tax"),
    path("report/tax/export", views.report_tax_export, name="report_tax_export"),
    path("report/sku/export", views.report_sku_export, name="report_sku_export"),

    # Inventory
    path("inventory", views.inventory_summary, name="inventory"),
    path("inventory/export", views.inventory_export, name="inventory_export"),
    path("inventory/stock-movement-export", views.stock_movement_export,
         name="stock_movement_export"),

    # Products
    path("products", views.product_list, name="product_list"),
    path("products/new", views.product_new, name="product_new"),
    path("products/sync", views.product_sync, name="product_sync"),
    path("products/bulk-add", views.product_bulk_add, name="product_bulk_add"),
    path("products/bulk-edit", views.product_bulk_edit, name="product_bulk_edit"),
    path("products/<uuid:product_id>", views.product_detail, name="product_detail"),
    path("products/<uuid:product_id>/archive", views.product_archive, name="product_archive"),
    path("products/<uuid:product_id>/restore", views.product_restore, name="product_restore"),

    # Categories
    path("categories", views.category_list, name="category_list"),
    path("categories/new", views.category_new, name="category_new"),
    path("categories/<uuid:category_id>", views.category_detail, name="category_detail"),
    path("categories/<uuid:category_id>/delete", views.category_delete, name="category_delete"),

    # Units
    path("units", views.unit_list, name="unit_list"),
    path("units/new", views.unit_new, name="unit_new"),
    path("units/create-ajax", views.unit_create_ajax, name="unit_create_ajax"),
    path("units/<uuid:unit_id>", views.unit_detail, name="unit_detail"),
    path("units/<uuid:unit_id>/delete", views.unit_delete, name="unit_delete"),

    # Staff (POS PIN logins)
    path("staff", views.staff_list, name="staff_list"),
    path("staff/new", views.staff_new, name="staff_new"),
    path("staff/<uuid:staff_id>", views.staff_detail, name="staff_detail"),
    path("staff/<uuid:staff_id>/delete", views.staff_delete, name="staff_delete"),

    # Backoffice users (web logins — username or email + password)
    path("users", views.user_list, name="user_list"),
    path("users/new", views.user_new, name="user_new"),
    path("users/<uuid:staff_id>", views.user_detail, name="user_detail"),
    path("users/<uuid:staff_id>/reset-password", views.user_reset_password, name="user_reset_password"),
    path("users/<uuid:staff_id>/revoke", views.user_delete, name="user_delete"),

    # Customers
    path("customers", views.customer_list, name="customer_list"),
    path("customers/<uuid:customer_id>", views.customer_detail, name="customer_detail"),

    # Audit log
    path("audit", views.audit_log, name="audit_log"),
    path("audit/export", views.audit_log_export, name="audit_log_export"),

    # Shops & Branches
    path("branches", views.branch_list, name="branch_list"),
    path("branches/new", views.branch_new, name="branch_new"),
    path("branches/<uuid:branch_id>", views.branch_detail, name="branch_detail"),

    # Shop-level settings (singleton)
    path("setting/shop", views.shop_settings, name="shop_settings"),
]
