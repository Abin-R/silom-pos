"""PUBLIC, UNAUTHENTICATED views — the legal pages.

Same shape, and the same reason, as ``public_views.py`` and ``appdist.py``:
reachable by anyone on the internet with no session, so it lives in its own
file rather than among the ``@login_required`` backoffice views, where one
forgotten decorator is a hole nobody notices.

There is exactly one page here and it is static.  It exists because Google
Play will not let an app through App content review without a publicly
reachable privacy policy URL, and because that URL has to keep working for as
long as the listing does — a link to a doc in someone's Drive would not.

The page carries no shop data, reads no models, and takes no input.
"""
from django.shortcuts import render


def privacy_policy(request):
    """The privacy policy.  Static; see the module docstring."""
    return render(request, "legal/privacy.html")
