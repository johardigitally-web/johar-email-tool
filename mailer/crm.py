"""What a subscriber has actually spent, read out of the CRM.

Why not Shopify: the webshop knows about seven paying customers in total, so a
"total spend" column built from it would read EUR 0 for 398 of 404 people. The
money is in the CRM, which holds 5,771 orders and EUR 6.4m of history. Matching
the two by email address turns 172 of the 404 subscribers into people whose
spend you can see, averaging about EUR 1.180.

**Read only, and enforced by the database rather than by this file.** The
connection uses a dedicated Postgres role, `ela_mailer_ro`, which has SELECT on
exactly two tables and nothing else. It cannot write to the CRM even if the code
here were wrong, and it cannot read payments, notes or anything else it has no
business seeing. That is deliberate: the mailer and the CRM stay separate tools
that happen to share a machine, and this is the narrowest possible window
between them.

pg8000 rather than psycopg: pure Python, so there is no build step and no
compiled wheel to go missing on a Python upgrade.
"""
import urllib.parse

from . import config, db

#: One order, one customer, no proforma and nothing cancelled. `total_override`
#: is the exact grand total preserved from the legacy system, which is what the
#: CRM's own dashboard reports, so this column agrees with that screen rather
#: than inventing a second version of the truth.
SPEND_SQL = """
    SELECT lower(trim(c.email)) AS email,
           SUM(s.total_override) AS spent,
           COUNT(*)              AS orders,
           MIN(s.created)        AS first_order,
           MAX(s.created)        AS last_order
      FROM core_customer c
      JOIN core_sale s ON s.customer_id = c.id
     WHERE coalesce(trim(c.email), '') <> ''
       AND s.kind = 'order'
       AND s.status <> 'cancelled'
       AND s.total_override IS NOT NULL
     GROUP BY 1
"""


def configured():
    return bool((config.CRM_DSN or "").strip())


def _connect():
    """Open the read-only connection. Raises if pg8000 is not installed."""
    import pg8000.native

    u = urllib.parse.urlparse(config.CRM_DSN)
    return pg8000.native.Connection(
        user=urllib.parse.unquote(u.username or ""),
        password=urllib.parse.unquote(u.password or ""),
        host=u.hostname or "127.0.0.1",
        port=u.port or 5432,
        database=(u.path or "/").lstrip("/"),
        timeout=15,
    )


def spend_by_email():
    """{email: (spent, orders, first_order, last_order)} for every CRM customer
    who has bought something and has an email address on file.

    The LAST order date is what the audiences are built on: how recently
    somebody bought predicts whether they will open, and whether they will
    remember the shop well enough not to report it as spam.
    """
    con = _connect()
    try:
        out = {}
        for email, spent, orders, first, last in con.run(SPEND_SQL):
            if not email:
                continue
            out[email] = (float(spent or 0), int(orders or 0),
                          first.isoformat() if first else "",
                          last.isoformat() if last else "")
        return out
    finally:
        con.close()


#: Customers with an address on file. `newsletter` is the box they ticked at the
#: counter, and it is the ONLY thing here that becomes consent. `created` is when
#: they became a customer, which unlike Shopify's dates is real history going
#: back to 2013 rather than the date of a migration.
CUSTOMERS_SQL = """
    SELECT lower(trim(email)) AS email,
           -- The MOST RECENT name, not max(name), which is alphabetical and so
           -- picked whichever spelling happened to sort last. One person can be
           -- several customer records and the newest is the one they gave us
           -- most recently: a married name, a corrected spelling, a real name
           -- replacing the initials somebody typed at the counter in 2016.
           -- Among those, one we can actually greet somebody by comes first:
           -- a record saying "D. Mamak" is newer than one saying "Dondu Mamak"
           -- and is no use in "Beste ...".
           (array_agg(trim(name) ORDER BY (trim(name) ~ '^[[:alpha:]]{2,}') DESC,
                                          created DESC)
            FILTER (WHERE coalesce(trim(name), '') <> ''))[1] AS name,
           bool_or(newsletter) AS newsletter,
           min(created)       AS since
      FROM core_customer
     WHERE coalesce(trim(email), '') <> ''
     GROUP BY 1
"""


def customers():
    """[(email, name, newsletter, since)] for every CRM customer with an email.

    Grouped by address because one person can be several customer records, and
    two rows for one email would otherwise fight over the same subscriber.
    bool_or on the newsletter flag: if any of their records says they opted in,
    they opted in.
    """
    con = _connect()
    try:
        out = []
        for email, name, newsletter, since in con.run(CUSTOMERS_SQL):
            if not email:
                continue
            out.append((email, (name or "").strip(), bool(newsletter),
                        since.isoformat() if since else ""))
        return out
    finally:
        con.close()


def import_customers(conn):
    """Bring the CRM's customers in as subscribers.

    **An address is not consent, here as everywhere else.** Only the `newsletter`
    flag becomes `subscribed`; everybody else arrives as `never`, which means
    stored, countable and unmailable. That is the same rule the Shopify import
    follows, and `upsert_subscriber` enforces it regardless of what is passed.

    Brings the customer's real start date with them, which is the one thing the
    CRM has that Shopify does not: history back to 2013 instead of the date
    everything was migrated.
    """
    tally = {"seen": 0, "created": 0, "updated": 0, "kept_unsubscribed": 0,
             "unchanged": 0, "with_consent": 0}
    for email, name, newsletter, since in customers():
        tally["seen"] += 1
        if newsletter:
            tally["with_consent"] += 1
        outcome, _ = db.upsert_subscriber(
            conn, email, name=name,
            consent=db.YES if newsletter else db.NEVER,
            source="crm", signed_up_at=since)
        if outcome in tally:
            tally[outcome] += 1
    conn.commit()
    return tally


def sync(conn):
    """Copy CRM spend onto the subscribers we already have.

    Stored on our own rows rather than looked up when the page renders, so the
    list stays fast and, more importantly, so the screen still works when the
    CRM is down or the connection is misconfigured. A stale figure beats a
    page that will not load.

    Only ever writes to the mailer's own database. Nothing here can change the
    CRM, and the role it connects with could not do so anyway.
    """
    money = spend_by_email()
    matched = 0
    for row in conn.execute("SELECT id, email FROM subscriber").fetchall():
        found = money.get((row["email"] or "").lower())
        if not found:
            continue
        spent, orders, first, last = found
        conn.execute(
            "UPDATE subscriber SET spent = ?, orders = ?, first_order_at = ?,"
            " last_order_at = ? WHERE id = ?",
            (spent, orders, first, last, row["id"]))
        matched += 1
    conn.commit()
    return {"known_to_crm": len(money), "matched": matched}
