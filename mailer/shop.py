"""Reading product details off the webshop.

Paste a product link, get its photo, name and price. Typing a price by hand into
a mailing that goes to hundreds of people is the kind of mistake you only find
out about from a customer.

Uses the storefront's **public** JSON (`/products/<handle>.json`), not the Admin
API. The tool does hold Shopify admin credentials for importing subscribers, and
this deliberately does not reuse them: looking up a product to put in an email
needs no more privilege than a shopper opening the page, so it gets no more.

Only links on the shop's own domains are followed. Nothing here should be
persuadable into fetching an arbitrary address.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

#: Cheap in-process cache. The composer re-renders the preview on every pause in
#: typing, and without this each of those would be three requests to the shop.
_CACHE = {}
_TTL = 600      # seconds
_TIMEOUT = 6    # seconds; the composer must not hang on a slow shop


def _shop_hosts():
    """The hosts a product link is allowed to be on."""
    return set(config.shop_hosts())


def handle_of(url):
    """The product handle in a shop URL, or None if it is not one.

    Refuses anything off the shop's own domains, so a pasted link cannot make
    the server fetch somewhere it should not.
    """
    try:
        parts = urllib.parse.urlparse((url or "").strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    if parts.hostname is None or parts.hostname.lower() not in _shop_hosts():
        return None
    segments = [s for s in parts.path.split("/") if s]
    # /products/<handle>, and also /collections/<x>/products/<handle>
    if "products" not in segments:
        return None
    i = segments.index("products")
    if i + 1 >= len(segments):
        return None
    handle = segments[i + 1]
    return handle[:-5] if handle.endswith(".json") else handle


def _money(raw):
    """Shopify gives '1495.00'. The house style is 'EUR 1.495', no cents.

    Whole euros because these are furniture prices and the Klaviyo templates
    rounded the same way; a trailing ,00 on every line is noise.
    """
    try:
        cents = int(round(float(raw)))
    except (TypeError, ValueError):
        return ""
    return "EUR " + "{:,}".format(cents).replace(",", ".")


def _fetch(handle):
    url = "%s/products/%s.json" % (config.SHOP_URL,
                                   urllib.parse.quote(handle))
    req = urllib.request.Request(url, headers={
        "User-Agent": "newsletter-tool/1.0",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def lookup(url):
    """What to show for a pasted product link, or None if it cannot be read.

    None rather than an exception: a shop that is slow, or a link with a typo,
    must not stop somebody writing the rest of their email.
    """
    handle = handle_of(url)
    if not handle:
        return None

    hit = _CACHE.get(handle)
    if hit and (time.time() - hit[0]) < _TTL:
        return hit[1]

    try:
        data = _fetch(handle)
        p = data["product"]
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
        return None

    variants = p.get("variants") or [{}]
    first = variants[0] if variants else {}
    images = p.get("images") or []
    image = ""
    if p.get("image") and p["image"].get("src"):
        image = p["image"]["src"]
    elif images and images[0].get("src"):
        image = images[0]["src"]

    was = _money(first.get("compare_at_price"))
    now = _money(first.get("price"))
    out = {
        "title": (p.get("title") or "").strip(),
        "price": now,
        # Only a real markdown, not the same number printed twice.
        "was": was if (was and was != now) else "",
        "image": image,
        "url": "%s/products/%s" % (config.SHOP_URL, handle),
    }
    _CACHE[handle] = (time.time(), out)
    return out


def lookup_all(urls):
    """Resolve several links, dropping the ones that cannot be read.

    Returns a list of (url, product-or-None) so the composer can say which link
    failed rather than silently showing fewer products than were pasted.
    """
    out = []
    for u in urls:
        if (u or "").strip():
            out.append((u, lookup(u)))
    return out
