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

    path("", views.dashboard, name="home"),
    path("dashboard", views.dashboard, name="dashboard"),
    path("transactions", views.transactions, name="transactions"),

    # Sales reports
    path("report/daily", views.report_daily, name="report_daily"),
    path("report/daily/<str:date_str>", views.report_daily_detail, name="report_daily_detail"),
    path("report/sell", views.report_sell, name="report_sell"),
    path("report/sku", views.report_sku, name="report_sku"),

    # Inventory
    path("inventory", views.inventory_summary, name="inventory"),

    # Products
    path("products", views.product_list, name="product_list"),
    path("products/new", views.product_new, name="product_new"),
    path("products/bulk-add", views.product_bulk_add, name="product_bulk_add"),
    path("products/bulk-edit", views.product_bulk_edit, name="product_bulk_edit"),
    path("products/<uuid:product_id>", views.product_detail, name="product_detail"),

    # Shops & Branches
    path("branches", views.branch_list, name="branch_list"),
    path("branches/new", views.branch_new, name="branch_new"),
    path("branches/<uuid:branch_id>", views.branch_detail, name="branch_detail"),

    # Shop-level settings (singleton)
    path("setting/shop", views.shop_settings, name="shop_settings"),
]
