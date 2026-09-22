"""A discount code that belongs to one person, made in Shopify at send time.

WHY THIS EXISTS
One shared code in a marketing email is a code on a coupon site by the weekend,
and an email that calls it "uw persoonlijke code" while everyone has the same
one is simply untrue. Klaviyo generated a code per recipient; this does the
same, through Shopify's own discounts API.

WHAT IT MAKES
A percentage discount, single use, valid from now until 48 hours from now, one
per customer, not combinable. The code reads ELA10-XXXXXX so it is obviously
ours in the Shopify admin and in a support call.

IF SHOPIFY IS UNREACHABLE
Nothing is sent. An email built around a code, carrying a code that does not
work, is worse than an email that arrives an hour late: the customer types it
in, it fails, and they are now annoyed at a shop that offered them something.
The runner puts the message back in the queue and tries again on the next run.
"""
import json
import os
import re
import secrets
import string
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from . import config

#: The word a step's offer code carries when it should be made per recipient.
#: Anything else in that box is used as it is, so a shared code stays possible.
AUTO = "AUTO"

#: What a preview shows. Not a real code, and it never reaches a customer.
SAMPLE = "ELA10-XXXXXX"

#: Full-price products only, which is the shop's standing rule: one discount
#: per product, never a code on top of a van/nu price. It is the same
#: collection your own shared code is scoped to, so the two behave alike and
#: `scripts/sync_sale_tags.py` keeps both honest when prices change.
#: Empty means every product, which is almost certainly not what anybody wants.
COLLECTION = os.environ.get(
    "DISCOUNT_COLLECTION", "").strip()

PREFIX = "ELA%d-"
PERCENT = 0.10
HOURS = 48

#: A discount the email promises, in the two shapes this shop writes them:
#: "20% korting" in a sentence, and "-20%" in the badge. Deliberately narrow.
#: A bare "%" would also match "100% katoen" and "0% rente", and inferring a
#: discount from the word for cotton is how you end up creating a 100% off code.
_PROMISE = re.compile(r"(\d{1,2})\s*%\s*korting|-\s*(\d{1,2})\s*%", re.I)


def promised(text):
    """Every discount percentage this email promises, as whole numbers.

    More than one means the email contradicts itself. The caller's job is then
    to send nothing, not to pick one.
    """
    out = set()
    for a, b in _PROMISE.findall(text or ""):
        value = int(a or b)
        if 1 <= value <= 99:
            out.add(value)
    return out


def rate_for(text, fallback=PERCENT):
    """(rate, why). rate is None when the email cannot be trusted to say."""
    seen = promised(text)
    if len(seen) > 1:
        return None, "promises %s" % " and ".join(
            "%d%%" % v for v in sorted(seen))
    if not seen:
        return fallback, ""
    return seen.pop() / 100.0, ""

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"     # no I, O, 0, 1

MUTATION = """
mutation($basic: DiscountCodeBasicInput!) {
  discountCodeBasicCreate(basicCodeDiscount: $basic) {
    codeDiscountNode { id }
    userErrors { field message }
  }
}
"""


def configured():
    return bool(config.SHOPIFY_STORE_DOMAIN)


def _code(percent=PERCENT):
    # The percentage is in the code itself. In a support call somebody reads
    # out "E L A twenty dash", and that is the whole answer to "how much off?".
    return (PREFIX % round(percent * 100)) + "".join(
        secrets.choice(_ALPHABET) for _ in range(6))


def _graphql(query, variables):
    from . import sources
    url = "https://%s/admin/api/%s/graphql.json" % (
        config.SHOPIFY_STORE_DOMAIN, config.SHOPIFY_API_VERSION)
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "X-Shopify-Access-Token": sources._shopify_token(),
        "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def create(email, percent=PERCENT, hours=HOURS):
    """A fresh single-use code, or None if Shopify would not make one.

    None rather than an exception: the caller's job is to hold the email back,
    not to crash a run because one discount could not be created.
    """
    if not configured():
        return None
    now = datetime.now(timezone.utc)
    code = _code(percent)
    basic = {
        # The per cent sign is doubled because it is a format character here,
        # and this line threw every time it ran until a test called it.
        "title": "Ela mailer %d%%%% (%s)" % (round(percent * 100),
                                             (email or "")[:60]),
        "code": code,
        "startsAt": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "endsAt": (now + timedelta(hours=hours)).isoformat(
            timespec="seconds").replace("+00:00", "Z"),
        "customerSelection": {"all": True},
        "customerGets": {
            "value": {"percentage": percent},
            "items": ({"collections": {"add": [COLLECTION]}} if COLLECTION
                      else {"all": True}),
        },
        # One use, by one person. Both, because "all customers" plus a usage
        # limit of one is what makes a shared link pointless the moment the
        # person it was meant for has used it.
        "usageLimit": 1,
        "appliesOncePerCustomer": True,
        "combinesWith": {"orderDiscounts": False, "productDiscounts": False,
                         "shippingDiscounts": False},
    }
    try:
        out = _graphql(MUTATION, {"basic": basic})
    except (urllib.error.URLError, OSError, ValueError):
        return None
    node = (((out or {}).get("data") or {}).get("discountCodeBasicCreate") or {})
    if node.get("userErrors"):
        return None
    if not node.get("codeDiscountNode"):
        return None
    return code


#: Shopify's `code:` search is a FUZZY text match, not an exact one. Searching
#: for SAVE10-PL6TX9 returns WELCOME10 as well, because it contains "10". I
#: deleted the live wishlist discount that way once; it was recreated within a
#: minute, but the lesson is in the code now instead of in my memory: nothing
#: here deletes what a search returned without checking the code itself first.
FIND = """
query($q: String!) {
  codeDiscountNodes(first: 20, query: $q) {
    nodes { id codeDiscount { ... on DiscountCodeBasic {
      codes(first: 5) { nodes { code } } } } }
  }
}
"""

DELETE = """
mutation($id: ID!) {
  discountCodeDelete(id: $id) {
    deletedCodeDiscountId
    userErrors { field message }
  }
}
"""


def delete(code):
    """Remove one code, matched exactly. Returns how many were deleted."""
    code = (code or "").strip()
    if not code:
        return 0
    try:
        out = _graphql(FIND, {"q": "code:%s" % code})
    except (urllib.error.URLError, OSError, ValueError):
        return 0
    gone = 0
    for node in (((out or {}).get("data") or {})
                 .get("codeDiscountNodes", {}).get("nodes", [])):
        codes = [c["code"] for c in
                 ((node.get("codeDiscount") or {}).get("codes") or {}).get("nodes", [])]
        if codes != [code]:          # anything else the search dragged in
            continue
        _graphql(DELETE, {"id": node["id"]})
        gone += 1
    return gone
