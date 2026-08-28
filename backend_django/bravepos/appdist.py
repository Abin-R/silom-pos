"""PUBLIC, UNAUTHENTICATED views — the Android app install page.

Same shape, and the same reason, as ``public_views.py``: everything here is
reachable by anyone on the internet with no session, so it lives in its own
file rather than among the ``@login_required`` backoffice views, where one
forgotten decorator is a hole nobody notices.

Mounted at the URL root (``/app/``).  It *has* to be unauthenticated — the
device that needs the APK is normally a tablet being set up for the first
time, with no backoffice login on it and no Google account signed in.  Anyone
holding the link can install the till app, which is the point; the pages carry
no shop data, no credentials, and no way into one.

The APK bytes are not served from here.  ``models.AppRelease`` explains why
Google Drive keeps them and this only redirects.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from django.conf import settings as django_settings
from django.http import (
    Http404, HttpResponse, HttpResponseNotModified, HttpResponseRedirect,
)
from django.shortcuts import render

from .models import AppRelease


# ── Google Drive ────────────────────────────────────────────────────────────
#
# The obvious URL — ``drive.google.com/uc?export=download&id=…`` — does not
# return the file for a build this size.  Drive cannot virus-scan anything over
# ~100 MB, so it answers with an HTML interstitial asking the human to confirm.
# A tablet following that link gets a web page, not an APK.  ``confirm=t`` on
# the usercontent host is the answer to that prompt, given up front.
#
# This is an unofficial URL shape.  If Google changes it the button stops
# working, so ``preview_url`` below is kept as the manual fallback: the ordinary
# Drive page, where the download is one extra tap.
DRIVE_DOWNLOAD_URL = (
    "https://drive.usercontent.google.com/download"
    "?id={file_id}&export=download&confirm=t"
)
DRIVE_PREVIEW_URL = "https://drive.google.com/file/d/{file_id}/view"

# Every way Drive hands out a link to one file, plus a bare id pasted on its
# own — whichever of these someone copies out of the address bar should work.
_DRIVE_ID_PATTERNS = (
    re.compile(r"/file/d/([A-Za-z0-9_-]{10,})"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})"),
    re.compile(r"/d/([A-Za-z0-9_-]{10,})"),
    re.compile(r"^([A-Za-z0-9_-]{10,})$"),
)


def parse_drive_file_id(value: str) -> str:
    """Pull the file id out of a pasted Drive link, or "" if there isn't one.

    Taking the whole link rather than asking for the id keeps a copy-paste from
    the browser working — nobody should have to know which run of characters in
    a Drive URL is the id.
    """
    text = (value or "").strip()
    for pattern in _DRIVE_ID_PATTERNS:
        found = pattern.search(text)
        if found:
            return found.group(1)
    return ""


# An EAS build id is a plain UUID, and it turns up in three places someone
# might copy from: the artifact filename (``application-<uuid>.apk``), the
# expo.dev build page URL, and the build list.  Take any of them.
_BUILD_ID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def parse_build_id(value: str) -> str:
    found = _BUILD_ID_PATTERN.search(value or "")
    return found.group(0).lower() if found else ""


def download_url(release: AppRelease) -> str:
    return DRIVE_DOWNLOAD_URL.format(file_id=release.drive_file_id)


def preview_url(release: AppRelease) -> str:
    return DRIVE_PREVIEW_URL.format(file_id=release.drive_file_id)


def install_page_url() -> str:
    """Absolute URL of the install page — what the QR code encodes.

    Built from ``PUBLIC_BASE_URL`` rather than the request, for the same reason
    the self-order QR is: the URL has to be right for the device that scans it,
    which is not the device that rendered the page.
    """
    return f"{django_settings.BRAVEPOS['PUBLIC_BASE_URL']}/app/"


# ── Views ───────────────────────────────────────────────────────────────────
def app_install(request):
    """The install page: current build, download button, QR, older builds."""
    published = list(AppRelease.objects.filter(published=True))
    current = published[0] if published else None
    return render(request, "appdist/install.html", {
        "current": current,
        "older": published[1:],
        "page_url": install_page_url(),
        "preview_url": preview_url(current) if current else "",
        "qr_js_version": qr_js_version(),
    })


def app_download(request, token):
    """Hand the device off to Drive.

    A redirect, not a proxy — see ``models.AppRelease``.  An unpublished build
    404s rather than 403s: whoever has the link has no way to tell a withdrawn
    build from one that never existed, and neither answer helps them.
    """
    release = AppRelease.objects.filter(token=token, published=True).first()
    if release is None:
        raise Http404("No such build")

    response = HttpResponseRedirect(download_url(release))
    # Keep the token out of the Referer Google receives — the whole hop is
    # off-site, and the token is the only thing guarding the build.
    response["Referrer-Policy"] = "no-referrer"
    return response


# ── The QR library ──────────────────────────────────────────────────────────
#
# Vendored, and served from here rather than through ``staticfiles``: this
# project has no STATIC_ROOT pipeline (see ``backoffice.views.backoffice_css``,
# which serves the stylesheet the same way for the same reason).
#
# Generated in the browser rather than baked into a committed SVG so the code
# always encodes the URL this deployment actually answers on.  It is the same
# library the app uses for receipt QRs, so there is one QR implementation in
# the product rather than two.
_QR_JS_PATH = Path(__file__).resolve().parent / "vendor" / "qrcode.js"
_qr_js_payload = None


def _qr_js():
    global _qr_js_payload
    if _qr_js_payload is None:
        body = _QR_JS_PATH.read_bytes()
        _qr_js_payload = (body, hashlib.sha1(body).hexdigest()[:12])
    return _qr_js_payload


def app_qr_js(request):
    body, version = _qr_js()
    if request.headers.get("If-None-Match") == f'"{version}"':
        return HttpResponseNotModified()

    response = HttpResponse(body, content_type="application/javascript")
    response["ETag"] = f'"{version}"'
    # The file is vendored and never edited in place, so a version-stamped URL
    # really is immutable. The bare URL still has to stay revalidatable —
    # pos.rollingpinn.com is behind Cloudflare, and pinning it at the edge
    # against a file that could change on a future bump is how you get a stale
    # library nobody can clear.
    if request.GET.get("v") == version:
        response["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
        response["Cache-Control"] = "public, max-age=300"
    return response


def qr_js_version() -> str:
    return _qr_js()[1]
