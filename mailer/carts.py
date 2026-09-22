"""Baskets, as the shop reports them.

Shopify's admin API knows about checkouts and orders. It does not know that
somebody put a sofa in their basket and closed the tab, and that is the moment
two of the flows wait for. Klaviyo solves this with a script of its own on every
page; this is the same idea, kept as small as it can be.

WHO IS THIS PERSON, AND CAN THEY LIE ABOUT IT
---------------------------------------------
Anything a browser sends can be forged, so identity is never taken from the
message itself:

* `ela=<token>` is one of OUR OWN send tokens, unguessable and belonging to one
  message to one address. If it resolves, we know exactly who they are, and we
  know they got here from one of our emails. This is the strong path.
* `email=` is only honoured when the address is ALREADY a subscriber who has
  said yes. So the worst a forger can do is start a marketing sequence for
  somebody who had already agreed to receive marketing, in Dutch, capped and
  rate limited. Not nothing, but not a way to reach anybody new.

Everything else is dropped without a word.
"""
import json
import os
import re
from urllib.parse import urlsplit

from . import config, db

#: One event per person per quarter of an hour. A basket page that fires on
#: every keystroke, or somebody replaying the same request, must not be able to
#: fill the table or to keep resetting a sequence.
QUIET_MINUTES = 15

#: And a ceiling across everybody, per hour. This endpoint is public and what it
#: reports cannot be proved, so the question is not "can it be forged" (it can)
#: but "how much can a forger cost us". A real Saturday at this shop is a
#: handful of baskets an hour; anything above this is not customers. The events
#: over the line are dropped, not queued, and the refusal is counted so it is
#: visible rather than silent.
HOURLY_CEILING = int(os.environ.get("CART_HOURLY_CEILING", "40") or 40)

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _clean_items(raw):
    """Only the four fields an email needs, and nothing a page can smuggle in."""
    out = []
    for i in (raw or [])[:20]:
        if not isinstance(i, dict):
            continue
        out.append({
            "title": str(i.get("title") or "")[:120],
            "variant": str(i.get("variant") or "")[:80],
            "qty": _qty(i.get("qty")),
            "price": str(i.get("price") or "0")[:20],
        })
    return out


def _qty(raw):
    """A sane whole number. Anything else counts as one."""
    try:
        return max(1, min(99, int(float(str(raw if raw is not None else 1)
                                       .replace(",", ".")))))
    except (TypeError, ValueError):
        return 1


def _total(items):
    total = 0.0
    for i in items:
        try:
            total += float(str(i["price"]).replace(".", "").replace(",", ".")) * i["qty"]
        except (TypeError, ValueError):
            pass
    return round(total, 2)


#: Links we are willing to store. A basket link goes straight into an email, so
#: "contains our domain" is not good enough: yourshop.nl.example.com contains it
#: too, and that is how a page would get us to send customers somewhere else.
#: Read from the configured shop address each time, never frozen at import.


def _url(raw):
    """A link on our own shop, or nothing at all.

    Shopify's pixel hands over the product link RELATIVE ("/products/terra-x"),
    which the first version of this dropped, so every event arrived with an
    empty product. A path is ours by definition, so it gets the shop put in
    front of it; a full link still has to prove its host.
    """
    raw = str(raw or "")[:400]
    if raw.startswith("/") and not raw.startswith("//"):
        return config.SHOP_URL + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    if parts.scheme != "https" or parts.netloc.lower() not in config.shop_hosts():
        return ""
    return raw


#: What Shopify said about an address, kept for a while. The endpoint is public,
#: so without this anybody replaying requests would spend our Shopify API quota;
#: with it, one address costs one lookup per window no matter how often it is
#: tried. Misses are cached too, deliberately.
_LOOKUPS = {}
_LOOKUP_TTL = 15 * 60


def _shopify_customer(email):
    """{'consented': bool, 'buyer': bool, 'name': str} or None.

    None means "could not find out", and the caller treats it as a no: when the
    webshop is unreachable the safe answer to "may we email this person" is
    never yes.
    """
    import time
    hit = _LOOKUPS.get(email)
    if hit and (time.time() - hit[0]) < _LOOKUP_TTL:
        return hit[1]
    out = None
    try:
        from . import flows
        data = flows._shopify("customers/search.json", limit=1,
                              query="email:%s" % email,
                              fields="email,first_name,orders_count,"
                                     "email_marketing_consent")
        for c in data.get("customers", []):
            if (c.get("email") or "").strip().lower() != email:
                continue
            consent = (c.get("email_marketing_consent") or {})
            out = {
                "consented": consent.get("state") == "subscribed",
                "buyer": int(c.get("orders_count") or 0) > 0,
                "name": (c.get("first_name") or "").strip(),
            }
    except Exception:
        out = None
    _LOOKUPS[email] = (time.time(), out)
    return out


def record(conn, form):
    """Store one basket, or say why not. Returns (ok, why).

    `why` is for the tests and the log, never for the browser: an endpoint that
    explains itself to an anonymous caller is an endpoint that tells an attacker
    which addresses are on the list.
    """
    token = str(form.get("ela") or "")[:64]
    email, source = "", ""
    if token:
        hit = conn.execute(
            "SELECT to_email FROM (SELECT token, to_email FROM send"
            " UNION ALL SELECT token, to_email FROM flow_send) WHERE token = ?",
            (token,)).fetchone()
        if hit is not None:
            email, source = (hit["to_email"] or "").strip().lower(), "email"
    if not email:
        claimed = str(form.get("email") or "").strip().lower()[:200]
        if not EMAIL.match(claimed):
            return False, "no identity"
        known = conn.execute(
            "SELECT consent, bounced FROM subscriber WHERE email = ?",
            (claimed,)).fetchone()
        # An unsubscribe is absolute. No lookup, no second chance, whatever any
        # other system says about them.
        if known is not None and (known["consent"] == db.NO or known["bounced"]):
            return False, "unsubscribed"
        if known is None or known["consent"] != db.YES:
            # Not on our list yet. Ask Shopify whether we have a REAL basis:
            # marketing consent they gave Shopify themselves, or an order
            # history (the same existing-customer basis as the CRM import).
            # A cookie click is neither and does not appear here.
            who = _shopify_customer(claimed)
            if who is None or not (who["consented"] or who["buyer"]):
                return False, "not a subscriber"
            db.upsert_subscriber(
                conn, claimed, name=who["name"], consent=db.YES,
                source="shopify" if who["consented"] else "shopify-buyer")
        email, source, token = claimed, "shop", ""

    recent = conn.execute(
        "SELECT seen FROM cart_event WHERE email = ? ORDER BY seen DESC LIMIT 1",
        (email,)).fetchone()
    if recent is not None and recent["seen"] >= db.minutes_ago(QUIET_MINUTES):
        return False, "too soon"

    # The ceiling. Counted across everybody, because the per-person limit does
    # nothing against somebody working through a list of addresses.
    last_hour = conn.execute(
        "SELECT COUNT(*) FROM cart_event WHERE seen >= ?",
        (db.minutes_ago(60),)).fetchone()[0]
    if last_hour >= HOURLY_CEILING:
        return False, "hourly ceiling reached"

    items = _clean_items(form.get("items"))
    conn.execute(
        "INSERT INTO cart_event (email, source, token, cart_url, product_url,"
        " total, items, seen) VALUES (?,?,?,?,?,?,?,?)",
        (email, source, token, _url(form.get("cart_url")),
         _url(form.get("product_url")), _total(items),
         json.dumps(items, ensure_ascii=False), db.now()))
    conn.commit()
    return True, source
