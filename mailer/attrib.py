"""Did the email lead to a sale? Matching who was emailed against who bought.

WHY THIS IS NOT A PIXEL
-----------------------
Klaviyo answers this by putting a cookie on the click and reading it back at the
Shopify thank-you page. That works for a shop where the money arrives online.
Ela is the other kind: about eleven webshop orders in its whole history against
5.700 in the CRM, so a webshop-only conversion number would report roughly zero
forever and would be right about the wrong question. The customer who opens the
email at breakfast and buys something in the showroom on Saturday is the
normal case here, not the exception.

So this matches on the one thing both systems record: the email address. For
every order in the webshop AND every sale in the CRM, it looks for a message we
sent that address shortly before, and credits the most recent one.

WHAT THIS NUMBER IS, AND IS NOT
-------------------------------
It is "they were emailed, then they bought". It is not proof the email caused
it. Somebody who was coming to the showroom anyway will match too. That is why
every row keeps three things the screen shows rather than hides:

* `touch`   - clicked, opened, or only sent. A click before the order is strong
              evidence. "Sent" alone is barely evidence at all.
* `hours`   - how long between the email and the order.
* `source`  - webshop or showroom.

Judge the flow on clicks and the showroom total on trend, and do not quote
"sent" matches at anybody as revenue.

READ ONLY, BOTH ENDS. Shopify is read with the same token the flow uses. The CRM
is read through `crm`, whose Postgres role can SELECT two tables and write
nothing.
"""
import os
import urllib.error
from datetime import datetime, timedelta, timezone

from . import crm, db

#: How far back from an order to look for the email that preceded it. Klaviyo's
#: own default is five days for a click. Fourteen here because a bed is not an
#: impulse buy: the median Ela basket is EUR 995 and people sleep on it, often
#: literally. Long enough to catch a real decision, short enough that "we email
#: everybody monthly" does not quietly claim every sale in the shop.
WINDOW_DAYS = int(os.environ.get("ATTRIB_DAYS", "14") or 14)

WEBSHOP = "webshop"
SHOWROOM = "showroom"

#: Strength of the evidence, best first. Used to pick between two emails that
#: both landed before the order.
TOUCH_RANK = {"clicked": 3, "opened": 2, "sent": 1}

#: Every sale in the CRM with an email address on it, from a date. Same two
#: tables and the same "real order" rules as the spend column, so this agrees
#: with the CRM's own dashboard rather than inventing a second total.
SALES_SQL = """
    SELECT lower(trim(c.email)) AS email,
           s.id                 AS ref,
           s.created            AS created,
           s.total_override     AS total
      FROM core_customer c
      JOIN core_sale s ON s.customer_id = c.id
     WHERE coalesce(trim(c.email), '') <> ''
       AND s.kind = 'order'
       AND s.status <> 'cancelled'
       AND s.total_override IS NOT NULL
       AND s.created >= :since
"""


def _parse(stamp):
    """A stored timestamp as an aware datetime, or None.

    Dates arrive from three systems that each punctuate them differently, and a
    match that threw on one odd row would abandon every row after it.
    """
    if not stamp:
        return None
    try:
        out = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return out if out.tzinfo else out.replace(tzinfo=timezone.utc)


def _webshop_orders(since):
    """[(email, ref, total, ordered_at)] from Shopify, or None if unreachable."""
    from . import flows
    try:
        data = flows._shopify(
            "orders.json", status="any", limit=250, created_at_min=since,
            fields="id,name,email,contact_email,created_at,total_price")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    out = []
    for o in data.get("orders", []):
        email = (o.get("email") or o.get("contact_email") or "").strip().lower()
        if not email:
            continue
        out.append((email, str(o.get("name") or o.get("id")),
                    float(o.get("total_price") or 0), o.get("created_at") or ""))
    return out


def _showroom_orders(since):
    """[(email, ref, total, ordered_at)] from the CRM, or None if unreachable."""
    if not crm.configured():
        return None
    try:
        con = crm._connect()
    except Exception:
        return None
    try:
        out = []
        for email, ref, created, total in con.run(SALES_SQL, since=since):
            if not email:
                continue
            out.append((email, "crm-%s" % ref, float(total or 0),
                        created.isoformat() if created else ""))
        return out
    except Exception:
        return None
    finally:
        con.close()


def _touches(conn, floor):
    """{email: [what we sent them]} for everything sent since `floor`.

    Campaign messages and flow reminders in one list on purpose. An order after
    a reminder and an order after a newsletter are the same event to a customer,
    and which of the two came last is exactly the question being asked.
    """
    out = {}
    rows = conn.execute(
        "SELECT to_email, sent_at, opened, clicked, campaign_id AS cid,"
        "       NULL AS fid, NULL AS step"
        "  FROM send WHERE sent = 1 AND sent_at >= ?"
        " UNION ALL "
        "SELECT to_email, sent_at, opened, clicked, NULL, flow_id, step"
        "  FROM flow_send WHERE sent_at IS NOT NULL AND sent_at >= ?",
        (floor, floor)).fetchall()
    for r in rows:
        out.setdefault((r["to_email"] or "").strip().lower(), []).append(dict(r))
    return out


def _credit(touches, when):
    """The message that gets the credit for an order at `when`, or None.

    Best evidence wins, most recent breaks the tie. A click that happened AFTER
    the order does not count as a click: they were already a customer by then.
    """
    best = None
    for t in touches:
        sent = _parse(t["sent_at"])
        if sent is None or sent > when or when - sent > timedelta(days=WINDOW_DAYS):
            continue
        clicked, opened = _parse(t["clicked"]), _parse(t["opened"])
        if clicked is not None and clicked <= when:
            touch = "clicked"
        elif opened is not None and opened <= when:
            touch = "opened"
        else:
            touch = "sent"
        cand = (TOUCH_RANK[touch], sent)
        if best is None or cand > best[0]:
            best = (cand, touch, t)
    if best is None:
        return None
    (_rank, sent), touch, t = best
    return {
        "touch": touch,
        "sent_at": t["sent_at"],
        "hours": round((when - sent).total_seconds() / 3600.0, 1),
        "campaign_id": t["cid"], "flow_id": t["fid"], "step": t["step"],
    }


def match(conn, days=None):
    """Look for orders that followed an email, and record them.

    Safe to run as often as you like: an order already credited stays credited
    to the message it was credited to, because the unique index refuses a second
    row for it. Nothing here writes to Shopify or to the CRM.
    """
    days = days or WINDOW_DAYS
    floor = datetime.now(timezone.utc) - timedelta(days=days * 2)
    since = floor.date().isoformat()
    touches = _touches(conn, floor.replace(tzinfo=None).isoformat(timespec="seconds"))
    tally = {"webshop": 0, "showroom": 0, "value": 0.0, "unreachable": [],
             "note": ""}

    if not touches:
        tally["note"] = "Nothing has been emailed yet, so there is nothing to match."
        return tally

    for source, orders in ((WEBSHOP, _webshop_orders(since)),
                           (SHOWROOM, _showroom_orders(since))):
        if orders is None:
            # Say which half could not be read. A silent zero from the CRM would
            # read as "email sold nothing in the showroom", which is a very
            # different sentence.
            tally["unreachable"].append(source)
            continue
        for email, ref, total, ordered_at in orders:
            when = _parse(ordered_at)
            if when is None:
                continue
            credit = _credit(touches.get(email, []), when)
            if credit is None:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO attribution (source, order_ref, email,"
                " total, ordered_at, campaign_id, flow_id, step, touch, sent_at,"
                " hours, matched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (source, ref, email, total, ordered_at, credit["campaign_id"],
                 credit["flow_id"], credit["step"], credit["touch"],
                 credit["sent_at"], credit["hours"], db.now()))
            if cur.rowcount:
                tally[source] += 1
                tally["value"] += total
    conn.commit()
    return tally


# --- reading it back ----------------------------------------------------------

def for_campaign(conn, campaign_id):
    """{orders, value, clicked} for one campaign."""
    r = conn.execute(
        "SELECT COUNT(*) AS orders, COALESCE(SUM(total), 0) AS value,"
        " SUM(CASE WHEN touch = 'clicked' THEN 1 ELSE 0 END) AS clicked"
        " FROM attribution WHERE campaign_id = ?", (campaign_id,)).fetchone()
    return {"orders": r["orders"] or 0, "value": r["value"] or 0.0,
            "clicked": r["clicked"] or 0}


def by_step(conn):
    """{step: {orders, value, clicked}} for the flow reminders."""
    out = {}
    for r in conn.execute(
            "SELECT step, COUNT(*) AS orders, COALESCE(SUM(total), 0) AS value,"
            " SUM(CASE WHEN touch = 'clicked' THEN 1 ELSE 0 END) AS clicked"
            " FROM attribution WHERE flow_id IS NOT NULL"
            " GROUP BY step").fetchall():
        out[r["step"]] = {"orders": r["orders"], "value": r["value"] or 0.0,
                          "clicked": r["clicked"] or 0}
    return out


def recent(conn, limit=50):
    return conn.execute(
        "SELECT * FROM attribution ORDER BY ordered_at DESC LIMIT ?",
        (limit,)).fetchall()


def whatsapp(conn):
    """How many people an email pushed into a WhatsApp conversation.

    The WhatsApp button goes through the click tracker like any other link, so
    this needs no extra plumbing at all. It is counted separately because for
    this shop it is the most valuable click on the page: a WhatsApp message is a
    salesperson talking to a buyer, which is how most of these beds get sold.
    """
    like = "clicked_url LIKE '%wa.me%' OR clicked_url LIKE '%whatsapp%'"
    return conn.execute(
        "SELECT COUNT(*) FROM (SELECT clicked_url FROM send WHERE " + like +
        " UNION ALL SELECT clicked_url FROM flow_send WHERE " + like + ")"
    ).fetchone()[0] or 0


def summary(conn):
    r = conn.execute(
        "SELECT COUNT(*) AS orders, COALESCE(SUM(total), 0) AS value,"
        " SUM(CASE WHEN touch = 'clicked' THEN 1 ELSE 0 END) AS clicked,"
        " SUM(CASE WHEN source = 'showroom' THEN 1 ELSE 0 END) AS showroom"
        " FROM attribution").fetchone()
    return {"orders": r["orders"] or 0, "value": r["value"] or 0.0,
            "clicked": r["clicked"] or 0, "showroom": r["showroom"] or 0,
            "whatsapp": whatsapp(conn), "window": WINDOW_DAYS}
