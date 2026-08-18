"""Both URL spellings work: ``/backoffice/branches`` and ``/backoffice/branches/``.

The URLconfs here are written in two styles.  ``bravepos.urls`` and most of
``backoffice.urls`` declare paths *without* a trailing slash, because the POS
app posts to ``/api/orders``; the auth views and the root-level customer pages
(``/order/<branch>/``, ``/receipt/<number>/``) declare them *with* one, because
that is how Django's own ``LoginView`` and every Django tutorial spell it.

Whichever spelling a page was declared in, the other one 404'd — nothing
reconciled the two.  Django ships half of that reconciliation as
``APPEND_SLASH``, but this project turned it off (see settings): its redirect
is a 301, and a 301 makes every browser re-send a POST as a GET, silently
dropping the form body.  Disabling it fixed the POST bug and left the 404s.

So do the whole job here instead:

* both directions — append a slash *or* strip one, whichever spelling the
  URLconf actually declares;
* 308 for POST/PUT/PATCH/DELETE, which is the one redirect status a client is
  required to replay with the same method and body — the reason APPEND_SLASH
  had to go stays fixed;
* only when routing itself missed.  A 404 raised *inside* a view (an order
  number nobody has) is a real 404: the other spelling would route to the same
  view, 404 again, and bounce the browser between the two forever.
"""
from __future__ import annotations

from django.http import HttpResponsePermanentRedirect
from django.urls import is_valid_path
from django.utils.encoding import escape_uri_path, iri_to_uri

#: Methods a 301 is safe for.  Everything else carries a body worth keeping.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


class _PermanentRedirectKeepingMethod(HttpResponsePermanentRedirect):
    """308: same as 301, minus the licence to turn the POST into a GET."""

    status_code = 308


class SlashTolerantMiddleware:
    """Redirect a 404 to the same URL with the trailing slash flipped."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if response.status_code != 404:
            return response
        target = self._other_spelling(request)
        if target is None:
            return response
        redirect = (HttpResponsePermanentRedirect if request.method in _SAFE_METHODS
                    else _PermanentRedirectKeepingMethod)
        return redirect(target)

    @staticmethod
    def _other_spelling(request):
        """The same URL with its trailing slash added/removed, or None.

        None whenever flipping the slash wouldn't help: at the site root, when
        the path already routes somewhere (so the 404 came from the view), or
        when the flipped path doesn't route either.
        """
        path_info = request.path_info
        if path_info in ("", "/"):
            return None
        urlconf = getattr(request, "urlconf", None)
        if is_valid_path(path_info, urlconf):
            return None
        had_slash = path_info.endswith("/")
        flipped = path_info[:-1] if had_slash else path_info + "/"
        if not is_valid_path(flipped, urlconf):
            return None
        # Rebuild from request.path, not path_info: under a script prefix the
        # two differ, and Location has to carry the prefix.  Relative on
        # purpose — behind Cloudflare an absolute URL rebuilt from the request
        # would come back out as http:// and cost a second round trip.
        path = request.path[:-1] if had_slash else request.path + "/"
        query = request.META.get("QUERY_STRING", "")
        return escape_uri_path(path) + (f"?{iri_to_uri(query)}" if query else "")
