"""Context available to every backoffice page.

The rail carries live counts next to Catalogue, Branches and Users. Those are
chrome, not page data — asking each of ~30 views to supply them would mean
every new view remembering to, and a rail that silently loses a number when
one forgets. A processor keeps it in one place.

Three cheap COUNT queries, and only for signed-in backoffice pages: the API
and the public self-order pages render no template that uses them.
"""
from bravepos.models import Branch, Product, Settings, Staff


def nav(request):
    # The stylesheet's content hash, so `?v=` busts the browser cache the
    # moment a deploy changes it. Available signed out too — the login page
    # loads the same stylesheet.
    from .views import _css_payload

    try:
        _, css_version = _css_payload()
    except OSError:
        css_version = "0"

    context = {"css_version": css_version}

    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return context

    settings_row = Settings.objects.first()
    context.update({
        # The rail and the <title> both name the shop rather than the product.
        "shop_name": settings_row.shop_name if settings_row else "Brave POS",
        "nav_products": Product.objects.filter(active=True).count(),
        "nav_branches": Branch.objects.filter(active=True).count(),
        # Backoffice web logins, not till PINs — the same set the Users page
        # lists. `backoffice_access` is what separates the two tiers.
        "nav_users": Staff.objects.filter(backoffice_access=True).count(),
    })
    return context
