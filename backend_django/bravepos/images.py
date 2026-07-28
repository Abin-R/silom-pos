"""Product image hygiene.

A product image is not a file upload — there is no upload endpoint anywhere in
this project.  It is a base64 ``data:`` URI stored as a very long string in
``Product.image_url`` or ``Product.image_base64`` (both ``TextField``, both
unbounded).  Every client resizes before sending, so in principle they are
small; in practice nothing *enforced* that, and an unresized phone photo
(1917x998 PNG, 262 KB of base64) reached production.

That mattered because the customer self-order menu inlines every product image
into the HTML.  Three oversized rows made that page 648 KB and 8-13 s to load,
with a fast TTFB — the server was never slow, the document was just enormous.

Two independent defences, because either alone still has a hole:

  * ``normalize`` caps an image at *write* time, on both the DRF API and the
    backoffice form.  Client-side resizing is an optimisation, not a guarantee
    — the backoffice downscaler silently fell back to the full-size original on
    a decode error, and ``image_url`` is a free-text field anyone can paste a
    data URI into.  This is the backstop that actually holds.
  * ``selforder.menu_for`` stops inlining data URIs at all, emitting a URL to
    ``public_views.product_image`` instead.  Even at the cap, twenty images
    inlined into one document is a page nobody wants on 4G; as separate URLs
    the browser fetches them in parallel, lazily, and caches them across
    visits.

Hosted ``https://`` image URLs are left alone throughout — they are already
tens of bytes and already cacheable.  Most products use them; this module
exists for the ones that don't.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import re

from PIL import Image, ImageOps

# Longest edge, in pixels.  The image is only ever shown as a menu card or a
# POS grid thumbnail, so 512 is generous; the backoffice uploader already
# targets exactly this.
MAX_DIM = 512

JPEG_QUALITY = 82

# Hard ceiling on the stored string.  A 512px JPEG lands well under this, so
# hitting it means the image resisted the first re-encode (very noisy photo)
# and we drop quality until it fits.
MAX_CHARS = 96 * 1024

# Quality ladder for that second pass.  Stops at 50 — below it the artefacts
# are worse than a missing image.
_QUALITY_STEPS = (JPEG_QUALITY, 70, 60, 50)

_DATA_URI_RE = re.compile(r'^data:(image/[a-zA-Z0-9.+-]+)?;base64,', re.IGNORECASE)


def is_data_uri(value: str) -> bool:
    """True for an inline base64 image, False for '' or a hosted URL."""
    return bool(value) and bool(_DATA_URI_RE.match(value))


def decode(value: str) -> tuple[bytes, str] | None:
    """Raw bytes + MIME type for a data URI, or None if it isn't one / is junk.

    Used by the public image endpoint, which serves these bytes directly rather
    than making the browser parse a 200 KB string out of the HTML.
    """
    m = _DATA_URI_RE.match(value or '')
    if not m:
        return None
    try:
        raw = base64.b64decode(value[m.end():], validate=False)
    except (binascii.Error, ValueError):
        return None
    if not raw:
        return None
    return raw, (m.group(1) or 'image/jpeg')


def digest(value: str) -> str:
    """Short content hash, used to cache-bust the image URL.

    The URL carries this, so a re-uploaded image is a *different* URL and the
    old one can be cached hard (immutable, one year) without ever going stale.
    """
    return hashlib.sha256((value or '').encode('utf-8')).hexdigest()[:12]


def _to_jpeg(img: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=quality, optimize=True)
    return buf.getvalue()


def normalize(value: str) -> str:
    """Return an image field value that is safe to store.

    - '' and hosted URLs pass through untouched.
    - An already-small data URI passes through untouched, so re-saving a
      product doesn't recompress the same JPEG over and over — generation loss
      is cumulative and irreversible.
    - Anything larger is downscaled to ``MAX_DIM`` and re-encoded as JPEG.
    - Anything undecodable becomes '', because a corrupt image renders as a
      broken icon on the customer's phone and there is nothing to recover.
    """
    value = (value or '').strip()
    if not is_data_uri(value):
        return value

    decoded = decode(value)
    if decoded is None:
        return ''
    raw, _mime = decoded

    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        # Pillow raises a wide spread here (UnidentifiedImageError, OSError,
        # DecompressionBombError on a deliberately huge image). None of them
        # are recoverable, and none should 500 a product save.
        return ''

    if max(img.size) <= MAX_DIM and len(value) <= MAX_CHARS:
        return value

    try:
        # Phone photos carry rotation in EXIF; without this a portrait shot is
        # stored on its side, because the re-encode drops the EXIF that was
        # telling the browser to rotate it.
        img = ImageOps.exif_transpose(img)

        # JPEG has no alpha. Flattening onto white keeps transparent PNG logos
        # looking right — converting straight to RGB makes them black.
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGBA')
            flat = Image.new('RGB', img.size, (255, 255, 255))
            flat.paste(img, mask=img.split()[-1])
            img = flat
        elif img.mode != 'RGB':
            img = img.convert('RGB')

        img.thumbnail((MAX_DIM, MAX_DIM), Image.LANCZOS)

        for quality in _QUALITY_STEPS:
            out = f'data:image/jpeg;base64,{base64.b64encode(_to_jpeg(img, quality)).decode("ascii")}'
            if len(out) <= MAX_CHARS:
                return out
        return out
    except Exception:
        return ''
