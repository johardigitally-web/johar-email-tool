"""The abandoned checkout reminders.

In Klaviyo this is one flow wearing two names: "Abandoned cart" and "Late
Checkout Reminder" are the same three emails, at 2, 24 and 72 hours. This
reproduces it, so moving over changes who sends the mail and not what the
customer receives.

WHY THIS ONE IS WORTH AUTOMATING AND BROWSE ABANDONMENT IS NOT
--------------------------------------------------------------
Somebody who reaches checkout has typed their address in themselves, so we know
who they are without any tracking at all, and every one of them arrives with an
address and a link back to the basket they filled. Browse abandonment needs to
identify an anonymous visitor, which is why the Klaviyo version of it sends
almost nothing.

THE PART THAT NEEDS CARE
------------------------
Guard 2 of this tool was "nothing sends on a trigger or a timer; a person
presses a button". A flow breaks that by definition, so it does not get to
borrow the campaign guards. It has its own:

* `FLOWS_ENABLED`, separate from `SENDING_ENABLED`.
* `FLOWS_FROM`, a hard date floor. On the day this is switched on it is the
  difference between reminding people about a basket from this morning and
  emailing everybody about baskets they abandoned three months ago.
* `FLOW_DAILY_CAP`, lower than the campaign cap, because nobody is watching.
* The `ALLOWED_RECIPIENTS` allowlist, checked in `send_one` per message as
  always, so this route cannot get round it either.
* One reminder per step per checkout, refused by a unique index rather than by
  this code remembering.
* Never to somebody who unsubscribed or bounced.
* Never after they have actually ordered.

CONSENT
-------
A reminder goes to somebody who reached your checkout and typed their address
in, whether or not they ever ticked a newsletter box. That is what Klaviyo does
today and what the soft opt-in for an existing customer relationship is for. It
is NOT the newsletter rule, so it is stated here rather than left to be
discovered: an unsubscribe is still absolute, and `FLOWS_NEED_CONSENT=1` in the
environment tightens it to opted-in people only if that is ever wanted.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import config, db, discounts, layouts, send as sender

#: Tighten to newsletter subscribers only. Off by default, matching Klaviyo.
NEEDS_CONSENT = (os.environ.get("FLOWS_NEED_CONSENT", "").strip().lower()
                 in ("1", "true", "yes", "on"))

STOP_ORDERED = "ordered"
STOP_UNSUBSCRIBED = "unsubscribed"
STOP_DONE = "finished"
#: Stopped because the same person reached something that matters more. Only
#: the checkout flow can do this, and only to a flow that is not itself a
#: checkout flow.
STOP_OVERTAKEN = "reached the checkout"


#: What can start a flow. This is the one part still fixed in code: a trigger
#: is a query against real data, so ADDING a trigger is a change to the program,
#: while adding a flow that uses one is not.
#:
#: `reason` is the line in the footer that says why they are getting this. A
#: checkout reminder and a newsletter reach somebody on entirely different
#: grounds and must not claim to be each other.
TRIGGERS = {
    "checkout": {
        "name": "They started a checkout and did not pay",
        "hint": "Read from Shopify. The only one that needs no newsletter "
                "consent: they typed their address into your checkout. It also "
                "outranks every other flow, so somebody who reaches your "
                "checkout hears about that and nothing else.",
        "consent": False, "stops_on_order": True, "reason": "checkout",
        "priority": 30,
    },
    "addtocart_email": {
        "name": "They came from one of our emails, put something in the basket "
                "and did not buy",
        "hint": "The visit carried our own token, so we know exactly who they "
                "are. This is the flow for the traffic your campaigns create.",
        "consent": True, "stops_on_order": True, "reason": "newsletter",
        "priority": 20,
    },
    "addtocart": {
        "name": "They put something in the basket and did not buy",
        "hint": "Signed-in customers who added something and left it there. "
                "People who arrived from one of our emails are deliberately "
                "NOT in this one; they have their own flow, so nobody can be "
                "caught by both.",
        "consent": True, "stops_on_order": True, "reason": "newsletter",
        "priority": 10,
    },
    "welcome": {
        "name": "Somebody new subscribes to the newsletter",
        "hint": "Fires once, for people who subscribe from now on. It never "
                "reaches back over the list you already have.",
        "consent": True, "stops_on_order": False, "reason": "newsletter",
        "priority": 1,
    },
    "bought": {
        "name": "Somebody bought from us",
        "hint": "Counts the showroom as well as the webshop, because it reads "
                "the CRM. Only purchases made from the day the flows were "
                "switched on: it can never reach back over the people who "
                "bought years ago.",
        "consent": True, "stops_on_order": True, "reason": "customer",
        "priority": 7,
        # A week, not three days. The runner goes every fifteen minutes, so
        # this only matters after an outage or after the flows have been off,
        # and somebody found a few days after their thirty day mark should
        # still get the sequence rather than silently never get it.
        "max_age_hours": 24 * 7,
    },
    "winback": {
        "name": "A customer has not bought for a while",
        "hint": "Uses what the CRM knows about their last order, so it counts "
                "the showroom too, not only the webshop.",
        "consent": True, "stops_on_order": True, "reason": "newsletter",
        "priority": 5,
    },
}

DRAFT, LIVE, PAUSED = "draft", "live", "paused"

#: Nothing older than this ever enters a flow, whatever FLOWS_FROM says. A
#: reminder about a basket from last week is not a reminder, it is a surprise.
MAX_AGE_HOURS = int(os.environ.get("FLOW_MAX_AGE_HOURS", "72") or 72)


def defs(conn):
    return conn.execute("SELECT * FROM flow_def ORDER BY id").fetchall()


def find(conn, key):
    return conn.execute("SELECT * FROM flow_def WHERE key = ?", (key,)).fetchone()


def steps_of(conn, def_id):
    return conn.execute(
        "SELECT * FROM flow_step WHERE def_id = ? ORDER BY pos", (def_id,)).fetchall()


def slug(name, taken=()):
    """A short address-bar name, unique against what already exists."""
    import re
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")[:24]
    base = base or "flow"
    key, n = base, 2
    while key in taken:
        key, n = "%s-%s" % (base, n), n + 1
    return key


def create(conn, name, trigger, days=365):
    """A new flow, as a draft, with one empty email in it.

    A draft rather than live: a sequence with no words in it should not be one
    restart away from sending.
    """
    if trigger not in TRIGGERS:
        raise ValueError("unknown trigger")
    key = slug(name, {d["key"] for d in defs(conn)})
    cur = conn.execute(
        "INSERT INTO flow_def (key, name, trigger, status, days, created)"
        " VALUES (?,?,?,?,?,?)",
        (key, (name or "Untitled flow").strip()[:80], trigger, DRAFT,
         max(1, int(days or 365)), db.now()))
    add_step(conn, cur.lastrowid)
    conn.commit()
    return key


def add_step(conn, def_id):
    """One more email at the end of a sequence."""
    pos = conn.execute("SELECT COUNT(*) FROM flow_step WHERE def_id = ?",
                       (def_id,)).fetchone()[0]
    conn.execute(
        "INSERT INTO flow_step (def_id, pos, hours, created) VALUES (?,?,?,?)",
        (def_id, pos, 2 if pos == 0 else 48, db.now()))
    conn.commit()
    return pos


def drop_step(conn, def_id, pos):
    """Remove one email and close the gap, so `pos` stays 0,1,2 with no holes.

    People already partway through keep their place by number, which is the
    honest outcome: deleting the second email of three means somebody who has
    had the first now gets what used to be the third.
    """
    conn.execute("DELETE FROM flow_step WHERE def_id = ? AND pos = ?", (def_id, pos))
    rows = conn.execute(
        "SELECT id FROM flow_step WHERE def_id = ? ORDER BY pos", (def_id,)).fetchall()
    for i, r in enumerate(rows):
        conn.execute("UPDATE flow_step SET pos = ? WHERE id = ?", (i, r["id"]))
    conn.commit()


def save_step(conn, def_id, pos, form):
    """Write what was typed. Everything is optional except the subject."""
    g = lambda k, n: (form.get(k) or "").strip()[:n]
    conn.execute(
        "UPDATE flow_step SET hours = ?, subject = ?, preheader = ?, heading = ?,"
        " subline = ?, hook = ?, body = ?, button = ?, offer_label = ?,"
        " offer_code = ?, offer_note = ? WHERE def_id = ? AND pos = ?",
        (_hours(form.get("hours")), g("subject", 200), g("preheader", 200),
         g("heading", 200), g("subline", 300), g("hook", 200),
         (form.get("body") or "").strip(), g("button", 60),
         g("offer_label", 60), g("offer_code", 40), g("offer_note", 80),
         def_id, pos))
    conn.commit()


def _hours(raw):
    try:
        return max(0.0, min(24 * 30.0, float(str(raw).replace(",", "."))))
    except (TypeError, ValueError):
        return 24.0


def set_status(conn, def_id, status):
    """Live, paused or back to draft.

    A flow with no subject on one of its emails cannot go live: the one thing
    worse than a reminder nobody asked for is a reminder with an empty subject
    line.
    """
    if status not in (DRAFT, LIVE, PAUSED):
        raise ValueError("unknown status")
    if status == LIVE:
        steps = steps_of(conn, def_id)
        if not steps or any(not (s["subject"] or "").strip() for s in steps):
            return False, "Every email needs a subject before this can go live."
    conn.execute("UPDATE flow_def SET status = ? WHERE id = ?", (status, def_id))
    conn.commit()
    return True, ""


def rename(conn, def_id, name, note="", days=None):
    conn.execute("UPDATE flow_def SET name = ?, note = ? WHERE id = ?",
                 ((name or "").strip()[:80] or "Untitled flow",
                  (note or "").strip()[:400], def_id))
    if days is not None:
        conn.execute("UPDATE flow_def SET days = ? WHERE id = ?",
                     (max(1, int(days or 365)), def_id))
    conn.commit()


# --- pulling them out of Shopify ---------------------------------------------

def _shopify(path, **params):
    from . import sources
    token = sources._shopify_token()
    url = "https://%s/admin/api/%s/%s?%s" % (
        config.SHOPIFY_STORE_DOMAIN, config.SHOPIFY_API_VERSION, path,
        urllib.parse.urlencode(params))
    req = urllib.request.Request(url, headers={"X-Shopify-Access-Token": token})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def poll(conn):
    """Look for people who have just done the thing a live flow waits for.

    Polling rather than webhooks or an on-site script. At this shop's volumes
    instant delivery buys nothing, and this has far fewer ways to fail: nothing
    public to secure, nothing to spoof, and a missed run heals itself on the
    next one.
    """
    tally = {"seen": 0, "added": 0, "too_old": 0, "no_email": 0,
             "already_running": 0, "skipped": ""}
    if not config.FLOWS_FROM:
        tally["skipped"] = "no start date"
        return tally
    for d in defs(conn):
        if d["status"] != LIVE:
            continue
        fn = {"checkout": _poll_checkout,
              "addtocart": _poll_addtocart,
              "addtocart_email": _poll_addtocart,
              "welcome": _poll_welcome,
              "bought": _poll_bought,
              "winback": _poll_winback}.get(d["trigger"])
        if fn is not None:
            fn(conn, d, tally)
    tally["bought"] = _sweep_bought(conn)
    conn.commit()
    return tally


def _sweep_bought(conn):
    """Take everyone who has bought since out of the flows they are waiting in.

    The check before each email already refuses to send to somebody who has
    bought, so this changes nothing about what goes out. What it changes is the
    screen: without it a customer who paid on Tuesday sits under "waiting" until
    their next email would have been due, which reads as though we are about to
    email them about a bed they already own.

    SQL only, deliberately. It runs over everybody waiting, so it must not cost
    one Shopify call per person; the webshop half of the question is asked at
    send time, one call for the one person about to be emailed.
    """
    stopped = 0
    for row in conn.execute(
            "SELECT f.id, f.email, f.abandoned_at FROM flow f"
            "  JOIN flow_def d ON d.id = f.def_id"
            " WHERE f.stopped = ''").fetchall():
        if not TRIGGERS.get(_trigger_of(conn, row["id"]), {}).get("stops_on_order"):
            continue
        day = (row["abandoned_at"] or "")[:10]
        sub = conn.execute("SELECT last_order_at FROM subscriber WHERE email = ?",
                           (row["email"],)).fetchone()
        bought = sub is not None and day and (sub["last_order_at"] or "")[:10] >= day
        if not bought:
            bought = conn.execute(
                "SELECT 1 FROM attribution WHERE email = ? AND ordered_at >= ?"
                " LIMIT 1", (row["email"], row["abandoned_at"])).fetchone() is not None
        if bought:
            stop(conn, row["id"], STOP_ORDERED)
            stopped += 1
    return stopped


def _trigger_of(conn, flow_id):
    row = conn.execute(
        "SELECT d.trigger FROM flow f JOIN flow_def d ON d.id = f.def_id"
        " WHERE f.id = ?", (flow_id,)).fetchone()
    return row["trigger"] if row is not None else ""


def needs_shop_pixel(conn, d):
    """True when this flow waits for something the shop has never reported.

    The two add-to-cart flows depend on one small pixel in Shopify. Without it
    they sit there marked live and do nothing at all, forever, which looks
    exactly like a flow with nobody in it. Better to say so on the screen.
    """
    if d["trigger"] not in ("addtocart", "addtocart_email"):
        return False
    return conn.execute("SELECT 1 FROM cart_event LIMIT 1").fetchone() is None


def priority(d):
    return TRIGGERS.get(d["trigger"], {}).get("priority", 0)


def _running(conn, email):
    """The flow this person is already in, if any."""
    return conn.execute(
        "SELECT f.*, d.trigger AS trig, d.key AS def_key FROM flow f"
        "  JOIN flow_def d ON d.id = f.def_id"
        " WHERE f.email = ? AND f.stopped = '' LIMIT 1", (email,)).fetchone()


def utc(stamp):
    """One timestamp shape for the whole flow engine: UTC, seconds, no offset.

    Shopify answers in the SHOP's timezone with the offset attached, e.g.
    2026-09-03T10:31:52+02:00. Cutting that to nineteen characters throws the
    offset away, and everything downstream then reads it as UTC, so every
    reminder ran two hours late in summer and one in winter. Convert, do not
    truncate.

    A stamp with no offset is taken as UTC, which is what everything WE write
    already is.
    """
    raw = str(stamp or "").strip()
    if not raw:
        return db.now()
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return db.now()
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


def _enter(conn, d, email, tally, *, name="", started="", total=0.0,
           items="[]", url="", checkout_id="", product_url=""):
    """Put one person into one flow, if that is the right flow for them.

    TWO RULES, and everything about not annoying people rests on them.

    **One person, one sequence.** Somebody who abandons twice in a week, or puts
    something in the basket while a reminder is already running, would otherwise
    be in two sequences at once and hear from us twice a day. The newer trigger
    is dropped, not queued.

    **The checkout outranks everything.** Somebody who reached your checkout has
    gone further than somebody who added to the basket, so if they are sitting
    in an add-to-cart flow that flow stops and the checkout reminder takes over.
    It never happens the other way round, and a checkout flow is never replaced
    by another checkout flow.
    """
    email = (email or "").strip().lower()
    if not email:
        tally["no_email"] += 1
        return
    started = utc(started)
    if started[:10] < config.FLOWS_FROM:
        tally["too_old"] += 1
        return
    # And an absolute ceiling, whatever the floor says. FLOWS_FROM is a date
    # somebody typed once and it appears on no screen: left at an old value, the
    # first run after switching the flows on emails everybody who abandoned a
    # basket in the meantime, all at once. Nobody wants a reminder about a
    # basket from a fortnight ago.
    # How late is too late depends on what triggered it. Three days is right
    # for a basket and wrong for a purchase: being found five days after the
    # thirty day mark is nothing, and dropping that person means the sequence
    # simply never runs for them.
    ceiling = TRIGGERS.get(d["trigger"], {}).get("max_age_hours", MAX_AGE_HOURS)
    if started < db.minutes_ago(60 * ceiling):
        tally["too_old"] += 1
        return

    # Every trigger except the checkout is marketing, and marketing needs a
    # subscriber. Refusing here rather than at send time keeps the screen honest:
    # a flow's "in it now" should be people who will actually hear from us.
    if TRIGGERS.get(d["trigger"], {}).get("consent") or NEEDS_CONSENT:
        who = conn.execute(
            "SELECT consent, bounced FROM subscriber WHERE email = ?",
            (email,)).fetchone()
        if who is None or who["consent"] != db.YES or who["bounced"]:
            tally["no_consent"] = tally.get("no_consent", 0) + 1
            return

    busy = _running(conn, email)
    if busy is not None:
        if priority(d) <= priority({"trigger": busy["trig"]}):
            tally["already_running"] += 1
            return
        stop(conn, busy["id"], STOP_OVERTAKEN)
        tally["overtaken"] = tally.get("overtaken", 0) + 1

    # `checkout_id` is unique, and that index is the only thing standing between
    # a poll that runs every hour and somebody who finished this sequence last
    # week being started on it again. Each trigger therefore hands in a key that
    # names the EVENT, not the person: one basket, one checkout, one signup, one
    # order they have not repeated. Get this wrong and a flow becomes a
    # subscription to itself.
    cur = conn.execute(
        "INSERT OR IGNORE INTO flow (def_id, checkout_id, email, name,"
        " recovery_url, product_url, total, items, abandoned_at, created)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (d["id"], checkout_id or "%s-%s" % (d["key"], email),
         email, name or "", url, product_url,
         float(total or 0), items, started, db.now()))
    if not cur.rowcount:
        return
    tally["added"] += 1

    # And if they have already bought since, they are done before they start.
    # Checking here as well as before each send costs one lookup per new person
    # and means a poll that runs while somebody is paying cannot put them in a
    # sequence about the thing they just bought.
    if TRIGGERS.get(d["trigger"], {}).get("stops_on_order"):
        row = conn.execute("SELECT * FROM flow WHERE id = ?",
                           (cur.lastrowid,)).fetchone()
        try:
            already = has_bought(conn, row)
        except ShopUnreachable:
            # Let them in. The same question is asked again, properly, before
            # any email actually goes out.
            already = False
        if already:
            stop(conn, cur.lastrowid, STOP_ORDERED)
            tally["added"] -= 1
            tally["ordered"] = tally.get("ordered", 0) + 1


def _poll_checkout(conn, d, tally):
    """Abandoned checkouts, from Shopify."""
    since = max(config.FLOWS_FROM,
                (datetime.now(timezone.utc) - timedelta(days=14)).date().isoformat())
    data = _shopify("checkouts.json", limit=250, created_at_min=since)
    for co in data.get("checkouts", []):
        tally["seen"] += 1
        items = [{"title": li.get("title") or "",
                  "variant": li.get("variant_title") or "",
                  "qty": li.get("quantity") or 1,
                  "price": li.get("price") or "0"}
                 for li in (co.get("line_items") or [])]
        _enter(conn, d, co.get("email"), tally,
               name=((co.get("customer") or {}).get("first_name") or "").strip(),
               started=co.get("created_at") or "",
               total=co.get("total_price") or 0,
               items=json.dumps(items, ensure_ascii=False),
               url=co.get("abandoned_checkout_url") or "",
               checkout_id=str(co.get("id")))


def _poll_addtocart(conn, d, tally):
    """People who put something in the basket and did not buy.

    Shopify's admin API cannot answer this. It knows about checkouts, which is
    one step further on, and about orders, which is two. So the shop reports the
    event itself, exactly as Klaviyo's own script does, and `cart_event` is
    where it lands.

    The two flows split on `source`, and they are mutually exclusive by
    construction: a visit that carried one of our email tokens is 'email' and
    can only ever start the email flow; a signed-in customer who arrived any
    other way is 'shop' and can only ever start this one. Nobody can be caught
    by both, and it is not a rule anybody has to remember, it is which query
    finds them.
    """
    want = "email" if d["trigger"] == "addtocart_email" else "shop"
    floor = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT * FROM cart_event WHERE source = ? AND seen >= ?"
        " ORDER BY seen", (want, floor)).fetchall()
    for r in rows:
        tally["seen"] += 1
        person = conn.execute("SELECT name FROM subscriber WHERE email = ?",
                              (r["email"],)).fetchone()
        _enter(conn, d, r["email"], tally,
               name=person["name"] if person else "",
               started=r["seen"], total=r["total"], items=r["items"] or "[]",
               url=r["cart_url"] or "", product_url=r["product_url"] or "",
               # One basket, one sequence: the event id, so a second poll over
               # the same event adds nobody, and a NEW basket next month does.
               checkout_id="%s-%s" % (d["key"], r["id"]))


def _poll_welcome(conn, d, tally):
    """People who have just subscribed.

    Never reaches back over the list we already have: FLOWS_FROM is the floor
    for every trigger, so switching this on does not welcome five thousand
    people who have been customers for ten years.
    """
    rows = conn.execute(
        "SELECT email, name, created FROM subscriber"
        " WHERE consent = ? AND bounced = 0 AND created >= ?"
        " ORDER BY created DESC LIMIT 500",
        (db.YES, config.FLOWS_FROM)).fetchall()
    for r in rows:
        tally["seen"] += 1
        _enter(conn, d, r["email"], tally, name=r["name"], started=r["created"],
               checkout_id="%s-%s" % (d["key"], r["email"]))


def _poll_bought(conn, d, tally):
    """People whose purchase was `days` ago, and who bought AFTER FLOWS_FROM.

    That second condition is the whole design. Without it, switching this on
    would enter every customer who has ever bought, because everybody's last
    order is more than thirty days ago, and five hundred of them would receive
    the same email within the quarter hour.

    The trigger moment is the order date plus the wait, not now, so somebody
    who bought thirty five days ago is five days into the sequence rather than
    at the start of it. Anything older than MAX_AGE_HOURS past that point is
    refused by _enter, which is what keeps a restart from replaying history.
    """
    days = max(1, d["days"])
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    rows = conn.execute(
        "SELECT email, name, last_order_at FROM subscriber"
        " WHERE consent = ? AND bounced = 0 AND spent > 0"
        "   AND last_order_at <> ''"
        "   AND substr(last_order_at,1,10) <= ?"
        "   AND substr(last_order_at,1,10) >= ?"
        " ORDER BY last_order_at DESC LIMIT 500",
        (db.YES, cutoff, config.FLOWS_FROM)).fetchall()
    for r in rows:
        tally["seen"] += 1
        started = (datetime.fromisoformat(r["last_order_at"][:10])
                   + timedelta(days=days)).isoformat() + "Z"
        _enter(conn, d, r["email"], tally, name=r["name"], started=started,
               checkout_id="%s-%s-%s" % (d["key"], r["email"],
                                         r["last_order_at"][:10]))


def _poll_winback(conn, d, tally):
    """Customers who bought once and have been quiet since.

    Reads `last_order_at`, which comes from the CRM, so this counts the showroom
    as well as the webshop. A win back that emails somebody who bought a bed in
    the shop last week would be worse than not having one.
    """
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=max(1, d["days"]))).date().isoformat()
    rows = conn.execute(
        "SELECT email, name, last_order_at FROM subscriber"
        " WHERE consent = ? AND bounced = 0 AND spent > 0"
        "   AND last_order_at <> '' AND substr(last_order_at,1,10) <= ?"
        " ORDER BY spent DESC LIMIT 500", (db.YES, cutoff)).fetchall()
    for r in rows:
        tally["seen"] += 1
        # The trigger moment is now, not the old order: a sequence dated two
        # years ago would fire all of its steps at once.
        _enter(conn, d, r["email"], tally, name=r["name"], started=db.now(),
               checkout_id="%s-%s-%s" % (d["key"], r["email"], r["last_order_at"][:10]))


class ShopUnreachable(Exception):
    """The webshop could not be asked. Not an answer, and never a purchase."""


def has_bought(conn, row):
    """Have they bought since this flow started? Then it stops.

    Three places are asked, because this shop takes money in three ways and
    getting a "your basket is waiting" email after paying is the single most
    annoying thing a shop can do:

    * Shopify, for a webshop order.
    * The CRM's last order date, which counts the showroom and WhatsApp.
    * Anything the Results screen has already matched to this person.

    Any one of them saying yes is enough.
    """
    email = (row["email"] or "").strip().lower()
    since = row["abandoned_at"] or ""
    if _has_ordered(email, since):
        return True
    day = since[:10]
    sub = conn.execute(
        "SELECT last_order_at FROM subscriber WHERE email = ?", (email,)).fetchone()
    if sub is not None and (sub["last_order_at"] or "")[:10] >= day > "":
        return True
    seen = conn.execute(
        "SELECT 1 FROM attribution WHERE email = ? AND ordered_at >= ? LIMIT 1",
        (email, since)).fetchone()
    return seen is not None


def _has_ordered(email, since):
    """Did they actually order after abandoning? Then there is nothing to remind
    them about, and sending anyway is the single most annoying thing a shop can
    do to somebody who has just given it money."""
    try:
        data = _shopify("orders.json", status="any", limit=50,
                        created_at_min=since, email=email)
    except (urllib.error.URLError, OSError, ValueError):
        # Cannot tell, so do not send. But raising rather than answering "yes":
        # answering yes made the CALLER write stopped='ordered' permanently, so
        # a five second outage cancelled somebody's whole sequence and then
        # showed up on the screen as a recovered sale. Holding the message back
        # costs an hour. Recording a sale that never happened costs the number
        # the entire system exists to produce.
        raise ShopUnreachable("Shopify did not answer")
    return bool(data.get("orders"))


# --- deciding what is due -----------------------------------------------------

def due(conn, now=None):
    """Reminders that should go out at this moment.

    The wait before each email is read from the flow itself, so changing 24
    hours to 48 in the screen changes what is due on the next run. Only live
    flows: pausing one leaves everybody exactly where they are, and starts them
    moving again when it goes live.
    """
    now = now or datetime.now(timezone.utc)
    out = []
    for row in conn.execute(
            "SELECT f.*, s.hours AS wait, d.status AS def_status"
            "  FROM flow f"
            "  JOIN flow_def d ON d.id = f.def_id"
            "  JOIN flow_step s ON s.def_id = f.def_id AND s.pos = f.step"
            " WHERE f.stopped = '' AND d.status = ?", (LIVE,)).fetchall():
        try:
            started = datetime.fromisoformat(utc(row["abandoned_at"]))
        except ValueError:
            continue
        # Each email waits from the one before it, not from the trigger, so the
        # numbers on the screen read the way a person says them out loud.
        if row["step"] and row["last_step_at"]:
            try:
                started = datetime.fromisoformat(utc(row["last_step_at"]))
            except ValueError:
                pass
        if now >= started + timedelta(hours=row["wait"]):
            out.append(row)
    return out


def stop(conn, flow_id, why):
    conn.execute("UPDATE flow SET stopped = ? WHERE id = ?", (why, flow_id))
    conn.commit()


# --- sending them -------------------------------------------------------------

def _line_total(item):
    """Quantity times price, whole euros, as the shop writes them.

    Whole euros because that is what the Klaviyo template does
    (`|floatformat:0`), and because a cent in a reminder email invites somebody
    to check the arithmetic rather than press the button.
    """
    try:
        raw = str(item.get("price") or "0").replace(".", "").replace(",", ".")
        total = float(raw) * int(item.get("qty") or 1)
    except (TypeError, ValueError):
        return str(item.get("price") or "")
    return "{:,.0f}".format(total).replace(",", ".")


def _products_for(flow_row):
    """The product they were looking at, as the email should draw it.

    Only the browse trigger has one. Read from the shop's public product JSON,
    so it needs no credentials, and a shop that is slow or a link with a typo
    produces no product block rather than no email.
    """
    url = ""
    try:
        url = flow_row["product_url"] or ""
    except (KeyError, IndexError, TypeError):
        url = ""
    if not url:
        return None
    from . import shop
    p = shop.lookup(url)
    return [p] if p else None


def _campaign_like(step_row, flow_row, english=False, products=None,
                   code_for=""):
    """A flow email pretends to be a campaign for rendering.

    `sender.render` takes a campaign row and produces the exact HTML and text
    somebody receives, complete with the footer, the postal address and the
    unsubscribe link. Reusing it means a reminder cannot quietly be missing the
    things every message must carry.

    `english` is for the preview only and never for a real send. An owner who
    does not read the language the shop writes in cannot approve an email they
    cannot read. Only text WE shipped has a translation; anything typed in the
    screen stays as it was typed, visibly, rather than being quietly
    mistranslated.
    """
    def w(dutch):
        return (layouts.english(dutch) or dutch) if english else dutch

    values = dict(layouts.defaults("cart"))
    if english:
        values = layouts.to_english(values)

    # The basket, as rows with a name, a quantity and a line total, which is
    # how Klaviyo's own template draws it. It used to be a line of plain text
    # per item, which said the same thing and looked like a receipt.
    basket = []
    for i in json.loads(flow_row["items"] or "[]"):
        title = i.get("title") or ""
        if i.get("variant"):
            title += " (%s)" % i["variant"]
        basket.append({"title": title, "qty": i.get("qty") or 1,
                       "price": "EUR %s" % _line_total(i), "image": "",
                       "url": ""})
    body = w(step_row["body"] or "")
    subline = w(step_row["subline"] or "")
    if "%(naam)s" in subline:
        subline = subline % {"naam": "{{naam}}"}
    values.update({
        "subline": subline,
        "kop": w(step_row["heading"] or ""),
        # What they left behind first, then whatever the email adds underneath.
        # That is the order Klaviyo's own templates use: the basket, and then
        # "liever eerst zien en voelen?".
        "tekst": "",
        "tekst_na": body,
        "knop_link": (flow_row["recovery_url"] or flow_row["product_url"]
                      or config.FALLBACK_URL),
    })
    values["hook"] = w(step_row["hook"] or "")
    # The orange box, and the discount code in it. An empty code means no box,
    # which is how the first two emails of the sequence have none.
    values["actie_label"] = w(step_row["offer_label"] or "")
    values["actie_tekst"] = w(step_row["offer_note"] or "")
    # AUTO means a code made for this one person at send time. A preview shows
    # the shape of one instead, because previewing an email must not create a
    # real discount in the shop.
    code = (step_row["offer_code"] or "").strip()
    if code == discounts.AUTO:
        code = code_for or discounts.SAMPLE
    values["actie_code"] = code
    # A code in the email is worth nothing if pressing the button does not
    # apply it. Shopify reads ?discount= on a checkout link.
    if code and values["knop_link"] and "discount=" not in values["knop_link"]:
        values["knop_link"] += ("&" if "?" in values["knop_link"] else "?") + \
            "discount=" + code
    if (step_row["button"] or "").strip():
        # knop_TEKST. The layout has no field called "knop", so writing to that
        # threw the button text away silently and left the default in place.
        values["knop_tekst"] = w(step_row["button"])
    return {
        "subject": w(step_row["subject"] or ""),
        "preheader": w(step_row["preheader"] or ""),
        "body": layouts.render("cart", values, products or basket or None),
    }


#: A basket to show when nobody is in the flow yet. Named and priced like a
#: real order, because a preview full of "Lorem ipsum" tells you nothing about
#: whether a long product name breaks the layout.
SAMPLE = {
    "email": "example@example.com",
    "name": "Alex",
    "total": 1290.0,
    "recovery_url": config.SHOP_URL + "/cart/example",
    "product_url": config.SHOP_URL + "/products/example",
    "items": json.dumps([
        {"title": "Example three seat sofa", "variant": "Beige",
         "qty": 1, "price": "995"},
        {"title": "Example pillow", "variant": "", "qty": 2,
         "price": "147,50"},
    ]),
}


#: Every box of a step that the recipient reads, which is where a promise of
#: a discount can be written and therefore where it has to be looked for.
_PROMISE_FIELDS = ("subject", "preheader", "heading", "subline", "hook", "body",
                   "button", "offer_label", "offer_note")


def _promise_text(step_row):
    return " ".join((step_row[k] or "") for k in _PROMISE_FIELDS)


def preview(conn, d, pos, english=False):
    """(subject, html) for one email, exactly as it would be received.

    Uses a real person waiting in this flow if there is one, so the preview
    shows the products and prices a customer would actually see, and falls back
    to a sample when nobody is in it. Writes nothing and sends nothing: the
    token is the literal word "voorbeeld", so the links and the open pixel in a
    preview point at nothing that is being counted.
    """
    step_row = conn.execute(
        "SELECT * FROM flow_step WHERE def_id = ? AND pos = ?",
        (d["id"], pos)).fetchone()
    if step_row is None:
        return None, None
    row = conn.execute(
        "SELECT * FROM flow WHERE def_id = ? AND stopped = ''"
        " ORDER BY abandoned_at DESC LIMIT 1", (d["id"],)).fetchone()
    if row is None:
        # A browse email carries the one thing they looked at, not a basket,
        # so the stand-in has to be shaped like the real thing or the preview
        # shows a layout this flow never sends.
        row = dict(SAMPLE)
        if d["trigger"] != "checkout":
            row["items"] = json.dumps([{"title": "Example three seat sofa",
                                        "variant": "Beige", "qty": 1,
                                        "price": "995"}])
            row["recovery_url"] = ""
    camp = _campaign_like(step_row, row, english=english,
                          products=_products_for(row))
    reason = (sender.REASON_CHECKOUT
              if TRIGGERS[d["trigger"]]["reason"] == "checkout"
              else sender.REASON_NEWSLETTER)
    html, _text = sender.render(camp, row["name"], "voorbeeld", reason=reason)
    if english:
        html = layouts.english_html(html)
    return camp["subject"], html


def run(conn, now=None):
    """Send whatever is due, across every live flow.

    Returns what it did and, importantly, what it did not do and why. Nothing
    here trusts itself: the daily cap, the allowlist, the consent rule and the
    "have they already bought" check are all re-read per message rather than
    decided once at the top.
    """
    ok, reason = config.flows_ready()
    if not ok:
        return {"blocked": reason, "sent": 0, "skipped": 0, "stopped": 0}

    sent_today = conn.execute(
        "SELECT COUNT(*) FROM flow_send WHERE sent_at IS NOT NULL"
        " AND substr(sent_at,1,10) = ?", (db.now()[:10],)).fetchone()[0]

    result = {"blocked": "", "sent": 0, "skipped": 0, "stopped": 0,
              "capped": False}
    for row in due(conn, now):
        if sent_today + result["sent"] >= config.FLOW_DAILY_CAP:
            result["capped"] = True
            break

        d = conn.execute("SELECT * FROM flow_def WHERE id = ?",
                         (row["def_id"],)).fetchone()
        if d is None:
            continue
        rules = TRIGGERS.get(d["trigger"], TRIGGERS["checkout"])
        step_row = conn.execute(
            "SELECT * FROM flow_step WHERE def_id = ? AND pos = ?",
            (d["id"], row["step"])).fetchone()
        if step_row is None:                      # the email was deleted
            stop(conn, row["id"], STOP_DONE)
            continue

        person = conn.execute(
            "SELECT * FROM subscriber WHERE email = ?", (row["email"],)).fetchone()
        if person is not None and (person["consent"] == db.NO or person["bounced"]):
            stop(conn, row["id"], STOP_UNSUBSCRIBED)
            result["stopped"] += 1
            continue
        # A checkout reminder goes to anybody who typed their address into the
        # checkout, which is what the existing-customer soft opt-in is for. Every
        # other trigger is marketing and needs a real subscriber.
        if (NEEDS_CONSENT or rules["consent"]) and (
                person is None or person["consent"] != db.YES):
            stop(conn, row["id"], STOP_UNSUBSCRIBED)
            result["stopped"] += 1
            continue
        # Anything at all from us in the last 24 hours, campaign or reminder,
        # and this waits. Two of our emails landing on one person in one day is
        # how a shop trains somebody to unsubscribe, and the sender of the other
        # one has no idea this exists.
        # Its OWN earlier steps are excluded: a sequence may run at 2 and 24
        # hours, so step 2 can be 22 hours after step 1 and a blanket rule would
        # block every flow from ever getting past its first email.
        recent = conn.execute(
            "SELECT MAX(t) FROM (SELECT sent_at t FROM send WHERE to_email = ?"
            "  AND sent = 1 UNION ALL"
            " SELECT sent_at t FROM flow_send WHERE to_email = ?"
            "  AND sent_at IS NOT NULL AND flow_id <> ?)",
            (row["email"], row["email"], row["id"])).fetchone()[0]
        if recent and recent > (datetime.now(timezone.utc)
                                - timedelta(hours=24)).isoformat(timespec="seconds"):
            result["skipped"] += 1
            continue

        if rules["stops_on_order"]:
            try:
                bought = has_bought(conn, row)
            except ShopUnreachable:
                # Try again next run. Nothing is written, nothing is counted as
                # a recovery, and the person keeps their place in the sequence.
                result["skipped"] += 1
                result["unreachable"] = result.get("unreachable", 0) + 1
                continue
            if bought:
                stop(conn, row["id"], STOP_ORDERED)
                result["stopped"] += 1
                continue

        step = row["step"]
        token = db.token()
        cur = conn.execute(
            "INSERT OR IGNORE INTO flow_send (flow_id, step, to_email, token)"
            " VALUES (?,?,?,?)", (row["id"], step, row["email"], token))
        if not cur.rowcount:
            # The unique index refused it, which is the point of the index. But
            # refused means "a row already exists", NOT "it already went": a
            # message that failed on a busy mail server, or was held back by the
            # allowlist, leaves a row with no sent_at. Without this the index
            # would then block that person's step for ever, and they would sit
            # in the flow marked "waiting" until somebody noticed.
            waiting = conn.execute(
                "SELECT token, sent_at FROM flow_send WHERE flow_id = ? AND step = ?",
                (row["id"], step)).fetchone()
            if waiting is None or waiting["sent_at"] is not None:
                result["skipped"] += 1
                continue
            token = waiting["token"]      # try again, same row, same token
        conn.commit()

        # A code, if this step carries one. If Shopify will not make it the
        # message waits: an email built around a discount, carrying a code that
        # does not work, is worse than one that arrives an hour late.
        code = ""
        if (step_row["offer_code"] or "").strip() == discounts.AUTO:
            code = conn.execute(
                "SELECT code FROM flow_send WHERE flow_id = ? AND step = ?",
                (row["id"], step)).fetchone()["code"]
            if not code:
                # Read out of the words of this step, which is what the person
                # will actually read. A step that says 20% in the heading and
                # 10% in the orange box sends nothing at all.
                rate, _why = discounts.rate_for(_promise_text(step_row))
                if rate is None:
                    conn.execute(
                        "DELETE FROM flow_send WHERE flow_id = ? AND step = ?",
                        (row["id"], step))
                    conn.commit()
                    result["skipped"] += 1
                    continue
                code = discounts.create(row["email"], percent=rate) or ""
            if not code:
                # Take the queue row out again, or the unique index would refuse
                # every later attempt and this person would never get the email.
                conn.execute("DELETE FROM flow_send WHERE flow_id = ? AND step = ?",
                             (row["id"], step))
                conn.commit()
                result["skipped"] += 1
                continue
            conn.execute("UPDATE flow_send SET code = ? WHERE flow_id = ?"
                         " AND step = ?", (code, row["id"], step))
            conn.commit()

        camp = _campaign_like(step_row, row, products=_products_for(row),
                              code_for=code)
        html, text = sender.render(
            camp, row["name"], token,
            reason={"checkout": sender.REASON_CHECKOUT,
                    "customer": sender.REASON_CUSTOMER,
                }.get(rules["reason"], sender.REASON_NEWSLETTER))
        if not config.recipient_allowed(row["email"]):
            conn.execute("UPDATE flow_send SET error = ? WHERE flow_id = ? AND step = ?",
                         ("not_on_allowlist", row["id"], step))
            conn.commit()
            result["skipped"] += 1
            continue

        msg = sender.build_message(camp, row["email"], html, text, token)
        server = None
        try:
            server = sender._smtp()
            sender.deliver(server, msg, token)
            conn.execute(
                "UPDATE flow_send SET sent_at = ? WHERE flow_id = ? AND step = ?",
                (db.now(), row["id"], step))
            # A reminder is still an email to that person. Without this it did
            # not count towards "mailed three times and never opened one", and
            # somebody could be sent a whole sequence while still reading as
            # never contacted.
            conn.execute(
                "UPDATE subscriber SET last_sent = ?,"
                " sent_count = COALESCE(sent_count, 0) + 1"
                " WHERE lower(email) = lower(?)", (db.now(), row["email"]))
            conn.execute(
                "UPDATE flow SET step = ?, last_step_at = ? WHERE id = ?",
                (step + 1, db.now(), row["id"]))
            n = conn.execute("SELECT COUNT(*) FROM flow_step WHERE def_id = ?",
                             (d["id"],)).fetchone()[0]
            if step + 1 >= n:
                conn.execute("UPDATE flow SET stopped = ? WHERE id = ?",
                             (STOP_DONE, row["id"]))
            result["sent"] += 1
        except Exception as exc:                       # noqa: BLE001
            permanent, why = sender.classify_failure(exc)
            conn.execute(
                "UPDATE flow_send SET error = ? WHERE flow_id = ? AND step = ?",
                (why[:250], row["id"], step))
            if permanent and person is not None:
                db.mark_bounced(conn, person["id"], why[:250])
                stop(conn, row["id"], STOP_UNSUBSCRIBED)
            result["skipped"] += 1
        finally:
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    pass
        conn.commit()
    return result


# --- what the screens read ----------------------------------------------------

def steps(conn, d):
    """Per step: received, opened, clicked, failed, and still waiting.

    The Klaviyo flow view, which is the thing worth copying about it: a sequence
    you cannot see the performance of, step by step, is a sequence you cannot
    improve. A single total tells you nothing about whether it is the second
    email doing the work or the third annoying people.
    """
    out = []
    for st in steps_of(conn, d["id"]):
        row = conn.execute(
            "SELECT SUM(CASE WHEN fs.sent_at IS NOT NULL THEN 1 ELSE 0 END) AS sent,"
            " SUM(CASE WHEN fs.opened IS NOT NULL THEN 1 ELSE 0 END) AS opened,"
            " SUM(CASE WHEN fs.clicked IS NOT NULL THEN 1 ELSE 0 END) AS clicked,"
            " SUM(CASE WHEN fs.error <> '' THEN 1 ELSE 0 END) AS failed"
            " FROM flow_send fs JOIN flow f ON f.id = fs.flow_id"
            " WHERE f.def_id = ? AND fs.step = ?", (d["id"], st["pos"])).fetchone()
        waiting = conn.execute(
            "SELECT COUNT(*) FROM flow WHERE def_id = ? AND stopped = '' AND step = ?",
            (d["id"], st["pos"])).fetchone()[0]
        out.append({
            "number": st["pos"] + 1, "pos": st["pos"], "hours": st["hours"],
            "subject": st["subject"] or "(no subject yet)",
            "sent": row["sent"] or 0, "opened": row["opened"] or 0,
            "clicked": row["clicked"] or 0, "failed": row["failed"] or 0,
            "waiting": waiting,
        })
    return out


def skipped(conn, d):
    """Who the sequence stopped, and why. The other half of the Klaviyo view:
    knowing somebody was deliberately left out is as useful as knowing somebody
    was mailed, and it is the answer to "why did this person not get it"."""
    return conn.execute(
        "SELECT email, total, stopped, abandoned_at FROM flow"
        " WHERE def_id = ? AND stopped <> '' AND stopped <> ?"
        " ORDER BY abandoned_at DESC LIMIT 50", (d["id"], STOP_DONE)).fetchall()


def sent_messages(conn, d, limit=300):
    """Every message this flow has sent, newest first.

    The answer to "which emails did this flow actually send, and to whom". Each
    row can be opened, so a customer asking "what did you send me" is one click
    rather than a guess.
    """
    return conn.execute(
        "SELECT fs.*, f.email AS person, f.name AS person_name, s.subject"
        "  FROM flow_send fs"
        "  JOIN flow f ON f.id = fs.flow_id"
        "  LEFT JOIN flow_step s ON s.def_id = f.def_id AND s.pos = fs.step"
        " WHERE f.def_id = ? ORDER BY COALESCE(fs.sent_at, '') DESC, fs.id DESC"
        " LIMIT ?", (d["id"], limit)).fetchall()


def summary(conn, d):
    """What the screen needs to show about one flow."""
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    return {
        "waiting": q("SELECT COUNT(*) FROM flow WHERE def_id = ? AND stopped = ''",
                     d["id"]),
        "sent": q("SELECT COUNT(*) FROM flow_send fs JOIN flow f ON f.id = fs.flow_id"
                  " WHERE f.def_id = ? AND fs.sent_at IS NOT NULL", d["id"]),
        "recovered": q("SELECT COUNT(*) FROM flow WHERE def_id = ? AND stopped = ?",
                       d["id"], STOP_ORDERED),
        "finished": q("SELECT COUNT(*) FROM flow WHERE def_id = ? AND stopped = ?",
                      d["id"], STOP_DONE),
        "value": q("SELECT COALESCE(SUM(total), 0) FROM flow"
                   " WHERE def_id = ? AND stopped = ''", d["id"]),
    }


def catalog(conn):
    """The list for the flows screen, with live numbers on each."""
    out = []
    for d in defs(conn):
        row = dict(d)
        row["emails"] = conn.execute(
            "SELECT COUNT(*) FROM flow_step WHERE def_id = ?", (d["id"],)).fetchone()[0]
        row.update(summary(conn, d))
        money = conn.execute(
            "SELECT COUNT(*) AS orders, COALESCE(SUM(a.total), 0) AS value"
            "  FROM attribution a JOIN flow f ON f.id = a.flow_id"
            " WHERE f.def_id = ?", (d["id"],)).fetchone()
        row["orders"] = money["orders"] or 0
        row["revenue"] = money["value"] or 0.0
        row["trigger_name"] = TRIGGERS.get(d["trigger"], {}).get("name", d["trigger"])
        out.append(row)
    return out
