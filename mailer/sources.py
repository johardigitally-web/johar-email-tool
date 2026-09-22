"""Where subscribers come from.

Two ways in: the webshop, and a CSV. Both funnel through `db.upsert_subscriber`,
which is the only place consent is ever written, so the rule that an import can
add people but never resurrect an unsubscribe is enforced once rather than in
every importer.

The rule worth repeating: an address is not consent. Shopify knows 404 customers
and only 248 of them ticked the box. The other 156 gave their address to get a
sofa delivered.
"""
import csv
import io
import json
import urllib.error
import urllib.parse
import urllib.request

from . import config, db


class SourceError(Exception):
    pass


# --- Shopify ------------------------------------------------------------------

def _shopify_token():
    if not (config.SHOPIFY_STORE_DOMAIN and config.SHOPIFY_CLIENT_ID and config.SHOPIFY_SECRET):
        raise SourceError("Shopify is not configured (SHOPIFY_* missing from .env)")
    body = json.dumps({
        "client_id": config.SHOPIFY_CLIENT_ID,
        "client_secret": config.SHOPIFY_SECRET,
        "grant_type": "client_credentials",
    }).encode()
    req = urllib.request.Request(
        "https://%s/admin/oauth/access_token" % config.SHOPIFY_STORE_DOMAIN,
        data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())["access_token"]
    except urllib.error.HTTPError as e:
        raise SourceError("Shopify rejected the app credentials (HTTP %s)" % e.code)


def _shopify_gql(tok, query, variables=None):
    req = urllib.request.Request(
        "https://%s/admin/api/%s/graphql.json" % (
            config.SHOPIFY_STORE_DOMAIN, config.SHOPIFY_API_VERSION),
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={"X-Shopify-Access-Token": tok, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as r:
        payload = json.loads(r.read())
    if payload.get("errors"):
        raise SourceError(str(payload["errors"])[:200])
    return payload["data"]


CUSTOMERS_QUERY = """query($c:String){customers(first:250,after:$c){
  pageInfo{hasNextPage endCursor}
  nodes{ email firstName lastName numberOfOrders
    emailMarketingConsent{ marketingState consentUpdatedAt } }}}"""


def import_shopify(conn):
    """Pull the webshop's newsletter subscribers.

    Only Shopify's own SUBSCRIBED state becomes consent here. NOT_SUBSCRIBED and
    UNSUBSCRIBED both arrive as `never`, so they are stored and countable but can
    never be mailed.
    """
    tok = _shopify_token()
    tally = {"created": 0, "updated": 0, "kept_unsubscribed": 0, "unchanged": 0, "seen": 0}
    cursor = None
    while True:
        data = _shopify_gql(tok, CUSTOMERS_QUERY, {"c": cursor})
        block = data["customers"]
        for node in block["nodes"]:
            tally["seen"] += 1
            block_consent = node.get("emailMarketingConsent") or {}
            state = (block_consent.get("marketingState") or "").upper()
            # Shopify's own record of when they opted in. Ours would only ever
            # be the date the import ran, which tells nobody anything.
            signed_up = (block_consent.get("consentUpdatedAt") or "")[:10]
            name = " ".join(x for x in (node.get("firstName"), node.get("lastName")) if x)
            try:
                orders = int(node.get("numberOfOrders") or 0)
            except (TypeError, ValueError):
                orders = 0
            outcome, _ = db.upsert_subscriber(
                conn, node.get("email"), name=name.strip(),
                consent=db.YES if state == "SUBSCRIBED" else db.NEVER,
                source="shopify_customer" if orders else "shopify",
                signed_up_at=signed_up)
            if outcome in tally:
                tally[outcome] += 1
        if not block["pageInfo"]["hasNextPage"]:
            break
        cursor = block["pageInfo"]["endCursor"]
    conn.commit()
    return tally


# --- CSV ----------------------------------------------------------------------

def import_csv(conn, text, assume_consent=False):
    """Import a pasted or uploaded CSV.

    `assume_consent` is off by default and is the one switch in this tool that
    can create consent out of thin air. It exists because a list exported from
    somewhere else is a real situation, and it is deliberately a conscious tick
    rather than the default, because the person ticking it is asserting that
    those people really did opt in.

    Accepts a header row with `email` and optionally `name`/`naam`, or a bare
    list of one address per line.
    """
    tally = {"created": 0, "updated": 0, "kept_unsubscribed": 0,
             "unchanged": 0, "skipped": 0, "seen": 0}
    text = (text or "").strip()
    if not text:
        return tally

    sample = text.splitlines()[0].lower()
    has_header = "email" in sample or "e-mail" in sample

    if has_header:
        reader = csv.DictReader(io.StringIO(text))
        rows = []
        for r in reader:
            low = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
            rows.append((low.get("email") or low.get("e-mail") or "",
                         low.get("name") or low.get("naam") or ""))
    else:
        rows = []
        for line in text.splitlines():
            parts = [p.strip() for p in line.replace(";", ",").split(",")]
            if parts and parts[0]:
                rows.append((parts[0], parts[1] if len(parts) > 1 else ""))

    for email, name in rows:
        tally["seen"] += 1
        if "@" not in email:
            tally["skipped"] += 1
            continue
        outcome, _ = db.upsert_subscriber(
            conn, email, name=name,
            consent=db.YES if assume_consent else db.NEVER,
            source="import")
        if outcome in tally:
            tally[outcome] += 1
    conn.commit()
    return tally
