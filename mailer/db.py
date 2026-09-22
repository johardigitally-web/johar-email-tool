"""SQLite storage.

One file, no server, no migrations framework. `init()` is idempotent and runs on
every start, so upgrading is copying the new code over the old and restarting.

Three tables, and the shape of them is the whole design:

  subscriber      one email address and whether we may mail it
  campaign        one newsletter: what it says and who it is for
  send            one campaign to one person, and what they did with it

`send` is also the queue. A row exists before anything leaves, which is what
makes sending resumable rather than restartable: the loop takes rows where sent
is 0, so pressing the button again continues where it stopped instead of mailing
everybody a second time. The UNIQUE(campaign_id, subscriber_id) index means the
database refuses a duplicate even when the code asks for one.
"""
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from . import config

# Consent states. NEVER is the resting state for an address we happen to know
# about; UNSUBSCRIBED is a one-way door that no import may reopen.
NEVER = "never"
YES = "subscribed"
NO = "unsubscribed"

DRAFT = "draft"
SENDING = "sending"
PAUSED = "paused"
SENT = "sent"

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriber (
    id            INTEGER PRIMARY KEY,
    email         TEXT NOT NULL COLLATE NOCASE,
    name          TEXT NOT NULL DEFAULT '',
    consent       TEXT NOT NULL DEFAULT 'never',
    source        TEXT NOT NULL DEFAULT '',
    consent_at    TEXT,
    unsubscribed_at TEXT,
    unsubscribed_by TEXT NOT NULL DEFAULT '',
    bounced       INTEGER NOT NULL DEFAULT 0,
    bounce_reason TEXT NOT NULL DEFAULT '',
    token         TEXT NOT NULL UNIQUE,
    created       TEXT NOT NULL,
    last_sent     TEXT,
    -- On the person, not just on the message. "How did that campaign do" and
    -- "should this person be on the next one" are different questions, and
    -- only the first one could be answered before.
    last_opened   TEXT,
    last_clicked  TEXT,
    sent_count    INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS subscriber_email ON subscriber(email COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS subscriber_consent ON subscriber(consent);

CREATE TABLE IF NOT EXISTS campaign (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    subject    TEXT NOT NULL DEFAULT '',
    preheader  TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL DEFAULT '',
    audience   TEXT NOT NULL DEFAULT 'all',
    status     TEXT NOT NULL DEFAULT 'draft',
    created    TEXT NOT NULL,
    started    TEXT,
    finished   TEXT
);

-- A saved filter, given a name, so a selection you can describe on the
-- subscribers screen becomes a thing you can send to. Stores the FILTER, not
-- the people: a list of frozen ids would keep mailing somebody who unsubscribed
-- the day after it was saved.
CREATE TABLE IF NOT EXISTS segment (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL,
    filters  TEXT NOT NULL DEFAULT '{}',
    -- The audience the screen was showing when the list was saved. Without it
    -- a list saved while looking at "bought in the last 2 years" quietly meant
    -- everybody, and the count on the button said otherwise.
    audience TEXT NOT NULL DEFAULT '',
    created  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS segment_name ON segment(name COLLATE NOCASE);

-- When something last ran. Small enough not to deserve a table of its own and
-- important enough that "have we done this yet today" must survive a restart.
CREATE TABLE IF NOT EXISTS ran (
    job  TEXT PRIMARY KEY,
    at   TEXT NOT NULL
);

-- One abandoned checkout, and how far through the reminder sequence it is.
-- Separate from `campaign`/`send` because the two behave differently in the one
-- way that matters: a campaign is aimed by a person at a moment they choose,
-- this fires on its own, and everything about it therefore needs its own set of
-- brakes rather than borrowing the campaign ones.
CREATE TABLE IF NOT EXISTS flow (
    id            INTEGER PRIMARY KEY,
    checkout_id   TEXT NOT NULL,
    email         TEXT NOT NULL,
    name          TEXT NOT NULL DEFAULT '',
    recovery_url  TEXT NOT NULL DEFAULT '',
    total         REAL NOT NULL DEFAULT 0,
    items         TEXT NOT NULL DEFAULT '[]',
    abandoned_at  TEXT NOT NULL,
    step          INTEGER NOT NULL DEFAULT 0,
    last_step_at  TEXT,
    stopped       TEXT NOT NULL DEFAULT '',
    created       TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS flow_checkout ON flow(checkout_id);

-- One reminder to one person. The unique index is the guard: the database
-- refuses a second copy of step 2 even if the runner is started twice.
CREATE TABLE IF NOT EXISTS flow_send (
    id        INTEGER PRIMARY KEY,
    flow_id   INTEGER NOT NULL REFERENCES flow(id) ON DELETE CASCADE,
    step      INTEGER NOT NULL,
    to_email  TEXT NOT NULL,
    sent_at   TEXT,
    error     TEXT NOT NULL DEFAULT '',
    token     TEXT NOT NULL UNIQUE
);
CREATE UNIQUE INDEX IF NOT EXISTS flow_send_once ON flow_send(flow_id, step);

CREATE TABLE IF NOT EXISTS send (
    id            INTEGER PRIMARY KEY,
    campaign_id   INTEGER NOT NULL REFERENCES campaign(id) ON DELETE CASCADE,
    subscriber_id INTEGER NOT NULL REFERENCES subscriber(id) ON DELETE CASCADE,
    to_email      TEXT NOT NULL,
    sent          INTEGER NOT NULL DEFAULT 0,
    sent_at       TEXT,
    error         TEXT NOT NULL DEFAULT '',
    opened        TEXT,
    clicked       TEXT,
    token         TEXT NOT NULL UNIQUE
);
CREATE UNIQUE INDEX IF NOT EXISTS send_once ON send(campaign_id, subscriber_id);
CREATE INDEX IF NOT EXISTS send_pending ON send(campaign_id, sent);

-- An order we believe came out of an email. Written by attrib.match, which
-- compares who was emailed against who then bought, in the webshop AND in the
-- showroom. Not a guess about intent: the row records which email came before
-- it, how far before, and whether they clicked, so a weak match is visible as a
-- weak match rather than counted as revenue.
--
-- One row per order, forever: UNIQUE(source, order_ref) means running the match
-- twice cannot double the revenue, and an order already credited to the first
-- email is never re-credited to a later one.
CREATE TABLE IF NOT EXISTS attribution (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,             -- webshop | showroom
    order_ref   TEXT NOT NULL,
    email       TEXT NOT NULL,
    total       REAL NOT NULL DEFAULT 0,
    ordered_at  TEXT NOT NULL,
    campaign_id INTEGER,                   -- credited to a campaign...
    flow_id     INTEGER,                   -- ...or to a flow reminder
    step        INTEGER,
    touch       TEXT NOT NULL DEFAULT '',  -- clicked | opened | sent
    sent_at     TEXT NOT NULL DEFAULT '',
    hours       REAL NOT NULL DEFAULT 0,   -- between the email and the order
    matched_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS attribution_once
    ON attribution(source, order_ref);

-- A flow, as a row rather than as code. Everything that used to be a constant
-- in flows.py lives here so the sequence can be built, renamed, paused and
-- rewritten from the screen. `trigger` is the only part still fixed in code:
-- what starts a flow needs a query written against real data, so a new trigger
-- is a change to the program and a new flow is not.
CREATE TABLE IF NOT EXISTS flow_def (
    id      INTEGER PRIMARY KEY,
    key     TEXT NOT NULL UNIQUE,        -- what appears in the address bar
    name    TEXT NOT NULL,
    trigger TEXT NOT NULL,               -- checkout | browse | welcome | winback
    status  TEXT NOT NULL DEFAULT 'draft',   -- draft | live | paused
    days    INTEGER NOT NULL DEFAULT 365, -- only the win back trigger reads this
    note    TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL
);

-- One email in a sequence, with the wait before it. `pos` is 0-based and is
-- what `flow.step` counts against.
CREATE TABLE IF NOT EXISTS flow_step (
    id        INTEGER PRIMARY KEY,
    def_id    INTEGER NOT NULL REFERENCES flow_def(id) ON DELETE CASCADE,
    pos       INTEGER NOT NULL,
    hours     REAL NOT NULL DEFAULT 24,
    subject   TEXT NOT NULL DEFAULT '',
    preheader TEXT NOT NULL DEFAULT '',
    heading   TEXT NOT NULL DEFAULT '',
    subline   TEXT NOT NULL DEFAULT '',
    body      TEXT NOT NULL DEFAULT '',
    button    TEXT NOT NULL DEFAULT '',
    created   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS flow_step_pos ON flow_step(def_id, pos);

-- Somebody put something in the basket. Shopify's admin API cannot tell us
-- this: it knows about checkouts, which is one step later, and about orders,
-- which is two. So the shop reports it, exactly as Klaviyo's own script does,
-- and this is where it lands.
--
-- `source` is the whole point of having two flows: 'email' means the visit came
-- from one of our emails (we know who they are because they carried our own
-- token), 'shop' means they were signed in to their account. Nothing else is
-- recorded, and an event we cannot put a name to is thrown away rather than
-- stored.
CREATE TABLE IF NOT EXISTS cart_event (
    id          INTEGER PRIMARY KEY,
    email       TEXT NOT NULL,
    source      TEXT NOT NULL,
    token       TEXT NOT NULL DEFAULT '',
    cart_url    TEXT NOT NULL DEFAULT '',
    product_url TEXT NOT NULL DEFAULT '',
    total       REAL NOT NULL DEFAULT 0,
    items       TEXT NOT NULL DEFAULT '[]',
    seen        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cart_event_who ON cart_event(email, seen);

-- A template the owner built: an ordered list of blocks and the words they
-- typed as defaults. Drawn by the same section functions as the ten
-- built-in layouts, so nothing made here can look different from what the
-- shop already sends. `blocks` and `content` are JSON.
CREATE TABLE IF NOT EXISTS template (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    blurb     TEXT NOT NULL DEFAULT '',
    subject   TEXT NOT NULL DEFAULT '',
    preheader TEXT NOT NULL DEFAULT '',
    blocks    TEXT NOT NULL DEFAULT '[]',
    content   TEXT NOT NULL DEFAULT '{}',
    created   TEXT NOT NULL,
    updated   TEXT NOT NULL
);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def minutes_ago(minutes):
    """A stamp in the same shape as now(), for "has this happened lately"."""
    return (datetime.now(timezone.utc)
            - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def token():
    """Unguessable id for an unsubscribe link. A sequential one would let anybody
    unsubscribe the whole list by counting."""
    return secrets.token_urlsafe(12)


def connect():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # Without this SQLite ignores the REFERENCES clauses above and a deleted
    # campaign leaves its sends behind forever.
    conn.execute("PRAGMA foreign_keys = ON")
    # On a public host the open pixel and the unsubscribe link get written by
    # whoever happens to be reading their mail, at the same moment somebody is
    # working in the screen. Plain SQLite locks the whole file for a write and
    # the other request dies with "database is locked". WAL lets readers carry
    # on during a write; busy_timeout makes a second writer wait its turn
    # instead of failing.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


#: Columns added after the first release. SQLite has no "ADD COLUMN IF NOT
#: EXISTS", so this is checked against PRAGMA table_info on every start. Keeping
#: it here means upgrading is still "copy the new code over and restart".
_LATER_COLUMNS = [
    ("subscriber", "unsubscribed_by", "TEXT NOT NULL DEFAULT ''"),
    # Which layout the campaign was written in, and the words that were typed
    # into it, as JSON. `body` still holds the rendered HTML, so everything that
    # sends, previews or tests a campaign carries on reading exactly one field
    # and knows nothing about layouts. A campaign with an empty `layout` is one
    # written before this existed, and still opens in the old HTML editor.
    ("campaign", "layout", "TEXT NOT NULL DEFAULT ''"),
    ("campaign", "content", "TEXT NOT NULL DEFAULT ''"),
    ("segment", "audience", "TEXT NOT NULL DEFAULT ''"),
    ("subscriber", "last_opened", "TEXT"),
    ("subscriber", "last_clicked", "TEXT"),
    ("subscriber", "sent_count", "INTEGER NOT NULL DEFAULT 0"),
    # When they gave us the address. Distinct from `created`, which is only when
    # WE first saw it: importing 400 people does not mean 400 people signed up
    # today, and a column that claims they did is worse than no column.
    ("subscriber", "signed_up_at", "TEXT NOT NULL DEFAULT ''"),
    # What they have spent, copied from the CRM by `crm.sync`. Stored here so
    # the list renders from one database and still works when the CRM does not.
    ("subscriber", "spent", "REAL NOT NULL DEFAULT 0"),
    ("subscriber", "orders", "INTEGER NOT NULL DEFAULT 0"),
    ("subscriber", "first_order_at", "TEXT NOT NULL DEFAULT ''"),
    # How recently they bought. The audiences are built on this: recency
    # predicts whether somebody opens, and whether they remember the shop
    # well enough not to report it as spam.
    ("subscriber", "last_order_at", "TEXT NOT NULL DEFAULT ''"),
    # Which numbered sending list this person belongs to, 0 for none. A FIXED
    # membership, unlike the rule-based lists: the whole point is being able to
    # say afterwards exactly who was in list 7, which a filter re-evaluated at
    # send time cannot tell you.
    ("subscriber", "batch", "INTEGER NOT NULL DEFAULT 0"),
    # A reminder is an email like any other and has to be measurable like one.
    # Without these the open pixel and the click tracker had nowhere to write
    # for a flow message, so every reminder looked unread forever.
    ("flow_send", "opened", "TEXT"),
    ("flow_send", "clicked", "TEXT"),
    # Which flow definition this person is in. Everything written before flows
    # were editable belongs to the abandoned checkout sequence, which is what
    # the seeding below makes definition 1.
    ("flow", "def_id", "INTEGER NOT NULL DEFAULT 1"),
    # What they were looking at, for the browse trigger. The checkout trigger
    # uses `items` instead, which holds the whole basket.
    ("flow", "product_url", "TEXT NOT NULL DEFAULT ''"),
    # The cream strip under the heading. It carries a different promise in each
    # email of a sequence (delivery, then a reason to visit, then the code),
    # so it belongs to the step and not to the layout.
    ("flow_step", "hook", "TEXT NOT NULL DEFAULT ''"),
    # The orange box. Klaviyo's third cart email is built around one, holding a
    # discount code, and without these three the email loses the thing it is
    # for.
    ("flow_step", "offer_label", "TEXT NOT NULL DEFAULT ''"),
    ("flow_step", "offer_code", "TEXT NOT NULL DEFAULT ''"),
    ("flow_step", "offer_note", "TEXT NOT NULL DEFAULT ''"),
    # WHERE they clicked, not just that they did. One address, one webshop
    # button and one WhatsApp button in the same email are three different
    # intentions, and "12 clicks" cannot tell them apart. This is what makes
    # "they messaged us on WhatsApp because of this email" a countable thing.
    ("send", "clicked_url", "TEXT NOT NULL DEFAULT ''"),
    ("flow_send", "clicked_url", "TEXT NOT NULL DEFAULT ''"),
    # The discount code this one message carried. Kept so the sent copy on the
    # screen shows what the customer is holding, and so a code is never made
    # twice for one email.
    ("flow_send", "code", "TEXT NOT NULL DEFAULT ''"),
    ("send", "code", "TEXT NOT NULL DEFAULT ''"),
    # Why this row will never be sent. Empty means it is still to go. It exists
    # because "not sent" was doing two jobs: waiting its turn, and impossible.
    # A campaign counting the second kind as outstanding never finishes, and a
    # batch that takes the lowest ids first never gets past them.
    ("send", "skipped", "TEXT NOT NULL DEFAULT ''"),
    # Which run has taken this row. Set in one atomic UPDATE before anything is
    # handed to the mail server, so two runs cannot both take the same person.
    ("send", "claim", "TEXT NOT NULL DEFAULT ''"),
    ("send", "claimed_at", "TEXT NOT NULL DEFAULT ''"),
    # Why this row will never be sent. Empty means it is still to go. It exists
    # because "not sent" was doing two jobs: waiting its turn, and impossible.
    # A campaign counting the second kind as outstanding never finishes, and a
    # batch that takes the lowest ids first never gets past them.
    ("send", "skipped", "TEXT NOT NULL DEFAULT ''"),
    # Which run has taken this row. Set in one atomic UPDATE before anything is
    # handed to the mail server, so two runs cannot both take the same person.
    ("send", "claim", "TEXT NOT NULL DEFAULT ''"),
    ("send", "claimed_at", "TEXT NOT NULL DEFAULT ''"),
]


def init():
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        for table, column, decl in _LATER_COLUMNS:
            have = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
            if column in have:
                continue
            try:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s"
                             % (table, column, decl))
            except sqlite3.OperationalError as exc:
                # Several gunicorn workers start at once and all run this. Two
                # can both see the column missing and both try to add it, and
                # the loser crashes the worker, which takes the whole service
                # down. Looking before leaping is not enough between processes;
                # the only reliable check is doing it and forgiving the clash.
                if "duplicate column" not in str(exc).lower():
                    raise
        conn.commit()
        seed_flows(conn)
    finally:
        conn.close()


#: (hours after the previous email, subject, preheader, heading, subline,
#: cream strip, text under the basket, button, offer box or None).
#:
#: Waits are BETWEEN emails, which is how Klaviyo states them and how a person
#: says them out loud: two hours after they leave, twenty two hours after that,
#: then two days.
#:
#: The words are placeholders on purpose. They show the shape of a sequence
#: that works, and every one of them is meant to be rewritten on the flows
#: screen in the shop's own voice.
CART_FLOW = [
    (2, "Your order is still open",
     "Everything is saved. Continue where you left off.",
     "One step to go",
     "Hello %(naam)s, your basket has been saved.",
     "Your delivery promise goes here",
     "Placeholder text. Say what somebody needs to know before they finish: "
     "how long delivery takes, and how to reach you with a question.",
     "Finish your order", None),
    (22, "Not sure about size or material?",
     "Ask us before you decide. We answer quickly.",
     "Happy to help you choose",
     "Hello %(naam)s, ask us anything. We usually answer quickly.",
     "Your opening hours go here",
     "Placeholder text. This is the email for the question that stops people "
     "ordering. Answer it here, in your own words.",
     "Finish your order", None),
    (48, "10% off, valid for 48 hours",
     "Your personal code is in this email.",
     "10% off your basket",
     "Hello %(naam)s, this code is yours alone and expires in 48 hours.",
     "",
     "Placeholder text. Say what the code is for and when it runs out. The "
     "code itself is made for this one person as the email is sent.",
     "Check out with 10% off",
     ("Your personal code", "AUTO", "10% off, one use, valid for 48 hours")),
]

#: They put something in the basket and left. No email brought them here, so
#: they may not know the shop well: reassurance and an easy way to ask a
#: question do the work, not a discount.
BROWSE_FLOW = [
    (2, "Still thinking it over?",
     "We usually answer a question within minutes.",
     "Can we help you with anything?",
     "Hello %(naam)s, your choice is still waiting for you.",
     "Your delivery promise goes here",
     "Placeholder text. Somebody who fills a basket and leaves it usually has "
     "one question left. Answer that question here.",
     "View your basket", None),
    (48, "Come and see it for yourself",
     "Where to find us, and when.",
     "Prefer to see it first?",
     "You are welcome to come and look, with no obligation.",
     "Your opening hours go here",
     "Placeholder text. The second email invites them somewhere: a shop, a "
     "showroom, a call.",
     "View your basket", None),
]

#: The same moment, but these people came from one of our own emails. They know
#: the shop, so this gets to the point faster.
CART_EMAIL_FLOW = [
    (2, "You left something in your basket",
     "We saved it for you. Questions? Just ask.",
     "Your basket is saved",
     "Hello %(naam)s, you can continue where you left off.",
     "Your delivery promise goes here",
     "Placeholder text. These people came from one of your own emails, so they "
     "already know the shop. Keep it shorter than the other basket email.",
     "View your basket", None),
    (48, "Shall we think along with you?",
     "One question is often all it takes.",
     "Happy to think along",
     "Ask us about size, material or delivery. We answer quickly.",
     "Your opening hours go here",
     "Placeholder text. Say how somebody reaches you, and where they can see "
     "the product in person.",
     "View your basket", None),
]


#: After somebody buys. Waits are BETWEEN emails, so 30 days, then 30, then 30,
#: and the flow itself only starts 30 days after the order.
POST_PURCHASE_FLOW = [
    (0, "How is your purchase?",
     "We would like to know whether all is well",
     "Did everything arrive in good order?",
     "Hello %(naam)s, you chose us, and we are glad you did.",
     "Your support promise goes here",
     "Placeholder text. Ask how it went, and make it easy to say that "
     "something is wrong.\n"
     "\n"
     "Then ask for a review: this is the moment somebody is most willing to "
     "write one.",
     "Leave a review", None),

    (24 * 30, "What goes with it?",
     "A few things customers often add",
     "Make it complete",
     "Hello %(naam)s, this is what customers often choose alongside it.",
     "Your delivery promise goes here",
     "Placeholder text. Name the two or three things that genuinely belong "
     "with what they bought.\n"
     "\n"
     "A suggestion somebody can act on, not a catalogue.",
     "See the collection", None),

    (24 * 30, "10% off, as a thank you",
     "One use, valid for 30 days",
     "Thank you",
     "Hello %(naam)s, here is something to finish it off.",
     "",
     "Placeholder text. Three months after a purchase is a good moment to say "
     "thank you.\n"
     "\n"
     "The code below is made for this one person, can be used once and is "
     "valid for 30 days.",
     "See the collection",
     ("Your personal code", "AUTO", "10% off, one use, valid for 30 days")),
]


def _write_steps(conn, def_id, steps):
    for pos, row in enumerate(steps):
        hours, subject, pre, kop, subline, hook, body, button, offer = row
        label, code, note = offer or ("", "", "")
        conn.execute(
            "INSERT INTO flow_step (def_id, pos, hours, subject, preheader,"
            " heading, subline, hook, body, button, offer_label, offer_code,"
            " offer_note, created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (def_id, pos, hours, subject, pre, kop, subline, hook, body,
             button, label, code, note, now()))


def _seed_one(conn, key, name, trigger, status, note, steps):
    cur = conn.execute(
        "INSERT INTO flow_def (key, name, trigger, status, note, created)"
        " VALUES (?,?,?,?,?,?)", (key, name, trigger, status, note, now()))
    def_id = cur.lastrowid
    _write_steps(conn, def_id, steps)
    return def_id


# --- templates the owner built ----------------------------------------------

def templates(conn):
    return conn.execute("SELECT * FROM template ORDER BY name").fetchall()


def get_template(conn, tid):
    return conn.execute("SELECT * FROM template WHERE id = ?", (tid,)).fetchone()


def create_template(conn, name, blocks, content, subject="", preheader="",
                    blurb=""):
    cur = conn.execute(
        "INSERT INTO template (name, blurb, subject, preheader, blocks, content,"
        " created, updated) VALUES (?,?,?,?,?,?,?,?)",
        ((name or "Nieuw sjabloon").strip()[:80], (blurb or "")[:200],
         (subject or "")[:200], (preheader or "")[:200],
         json.dumps(list(blocks)), json.dumps(dict(content or {}), ensure_ascii=False),
         now(), now()))
    conn.commit()
    return cur.lastrowid


def update_template(conn, tid, **fields):
    """Only the columns named are touched. blocks and content are JSON."""
    cols, vals = [], []
    for key, value in fields.items():
        if key in ("blocks", "content"):
            value = json.dumps(value, ensure_ascii=False)
        cols.append("%s = ?" % key)
        vals.append(value)
    if not cols:
        return
    cols.append("updated = ?")
    vals.append(now())
    conn.execute("UPDATE template SET %s WHERE id = ?" % ", ".join(cols),
                 vals + [tid])
    conn.commit()


def delete_template(conn, tid):
    conn.execute("DELETE FROM template WHERE id = ?", (tid,))
    conn.commit()


def seed_flows(conn):
    """Put the starter sequences into the tables, once.

    Seeding them rather than leaving them in code is what makes them editable,
    and doing it only when the table is empty is what stops a deploy overwriting
    words somebody has since rewritten.
    """
    # Browse abandonment used to start when somebody opened a product from one
    # of our emails. It starts at add to cart instead, so the flow keeps its
    # name, its emails and its numbers, and changes only what starts it.
    # Written as a migration because the flow may already be live.
    conn.execute("UPDATE flow_def SET trigger = 'addtocart'"
                 " WHERE trigger = 'browse'")

    have = {r[0] for r in conn.execute("SELECT key FROM flow_def")}
    if "cart" not in have:
        _seed_one(conn, "cart", "Abandoned checkout", "checkout", "live",
                  "Klaviyo runs 'Abandoned cart' and 'Late checkout reminder' "
                  "as this one sequence, so nobody can be caught by both.",
                  CART_FLOW)
    if "browse" not in have:
        _seed_one(conn, "browse", "Browse abandonment", "addtocart", "live",
                  "Two emails, starting when somebody puts something in the "
                  "basket and does not buy.",
                  BROWSE_FLOW)
    if "cart-email" not in have:
        _seed_one(conn, "cart-email", "Add to cart from an email",
                  "addtocart_email", "live",
                  "For the traffic our own campaigns create: they came from an "
                  "email, filled a basket and left it. Kept apart from the "
                  "other one so nobody can be in both.",
                  CART_EMAIL_FLOW)
    if "post-purchase" not in have:
        # DRAFT, not live. A deploy must never start a sequence nobody has read.
        _seed_one(conn, "post-purchase", "After a purchase", "bought", "draft",
                  "Day 30 asks how it is and asks for a review, day 60 shows "
                  "what goes with it, day 90 says thank you with a personal "
                  "code. Only reaches purchases made from the day the flows "
                  "were switched on, never the back catalogue.",
                  POST_PURCHASE_FLOW)
        conn.execute("UPDATE flow_def SET days = 30 WHERE key = 'post-purchase'")
    conn.commit()


# --- subscribers --------------------------------------------------------------

def upsert_subscriber(conn, email, name="", consent=NEVER, source="",
                      signed_up_at=""):
    """Add an address, or update a known one.

    The only consent change this ever makes is NEVER -> YES. An unsubscribe is
    never undone, so re-running an import can add people and can never resurrect
    somebody who asked to be left alone.
    """
    email = (email or "").strip().lower()
    if not email:
        return None, "no_email"
    row = conn.execute("SELECT * FROM subscriber WHERE email = ?", (email,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO subscriber (email, name, consent, source, consent_at, token, created)"
            " VALUES (?,?,?,?,?,?,?)",
            (email, (name or "")[:150], consent, source,
             now() if consent == YES else None, token(), now()))
        if signed_up_at:
            conn.execute("UPDATE subscriber SET signed_up_at = ? WHERE email = ?",
                         (signed_up_at, email))
        return "created", None
    # Filling in a signup date we did not have is not a consent change, so it
    # happens before the guards below and for every state, unsubscribed included.
    if signed_up_at and not row["signed_up_at"]:
        conn.execute("UPDATE subscriber SET signed_up_at = ? WHERE id = ?",
                     (signed_up_at, row["id"]))
    if row["consent"] == NO:
        return "kept_unsubscribed", None
    if consent == YES and row["consent"] == NEVER:
        conn.execute(
            "UPDATE subscriber SET consent = ?, consent_at = COALESCE(consent_at, ?),"
            " source = CASE WHEN source = '' THEN ? ELSE source END,"
            " name = CASE WHEN name = '' THEN ? ELSE name END WHERE id = ?",
            (YES, now(), source, (name or "")[:150], row["id"]))
        return "updated", None
    return "unchanged", None


#: Who took the decision. This is the whole point of the column: an operator
#: slipping on a button is a mistake to undo, while a customer clicking the link
#: in their email is a decision to respect. Stored identically before, they were
#: indistinguishable, so neither could be reversed safely.
BY_SELF = "self"
BY_ADMIN = "admin"


def unsubscribe(conn, subscriber_id, by=BY_ADMIN):
    conn.execute(
        "UPDATE subscriber SET consent = ?, unsubscribed_at = ?, unsubscribed_by = ?"
        " WHERE id = ?", (NO, now(), by, subscriber_id))


def revoke_consent(conn, subscriber_id):
    """Back to 'never': we hold the address but may not mail it.

    Always allowed and never needs a warning. Taking consent away can only ever
    reduce who gets an email, so the worst case of a misclick here is that
    somebody stops receiving a newsletter they wanted.
    """
    conn.execute(
        "UPDATE subscriber SET consent = ?, consent_at = NULL, unsubscribed_at = NULL,"
        " unsubscribed_by = '' WHERE id = ?", (NEVER, subscriber_id))


def restore_consent(conn, subscriber_id, source=None):
    """Put somebody back on the list.

    The caller decides whether this is allowed; this function only writes. The
    rule lives in the view, because it depends on WHO unsubscribed them, and that
    is a judgement about people rather than about data.

    `source` is left alone unless the caller passes one. Undoing an operator's
    slip is not a new consent event - the consent still came from wherever it
    originally came from, and overwriting that to "admin_correction" would erase
    the provenance that answers "why may we mail this person at all". Only a
    genuine "they asked to rejoin" is a new event worth recording.
    """
    if source:
        conn.execute(
            "UPDATE subscriber SET consent = ?, consent_at = ?, unsubscribed_at = NULL,"
            " unsubscribed_by = '', source = ? WHERE id = ?",
            (YES, now(), source, subscriber_id))
    else:
        conn.execute(
            "UPDATE subscriber SET consent = ?, consent_at = COALESCE(consent_at, ?),"
            " unsubscribed_at = NULL, unsubscribed_by = '' WHERE id = ?",
            (YES, now(), subscriber_id))


def grant_consent(conn, subscriber_id, source="manual"):
    """Record that somebody has given permission, when the tool did not see it.

    The everyday case is real: they said so at the counter, filled in a paper
    form, or emailed asking to be added. The webshop never saw any of that.

    Unlike undoing an operator's slip, this IS a new consent event, so it does
    overwrite `source`. Saying it came from Shopify when it came from a
    conversation in the showroom would be a lie in the one field that answers
    "why are we allowed to mail this person".
    """
    conn.execute(
        "UPDATE subscriber SET consent = ?, consent_at = ?, source = ?,"
        " unsubscribed_at = NULL, unsubscribed_by = '' WHERE id = ?",
        (YES, now(), source, subscriber_id))


def clear_bounce(conn, subscriber_id):
    """Un-flag an address that bounced.

    Bounces are not all permanent: a full mailbox or a server having a bad
    afternoon both look the same as a dead address. Clearing it puts them back
    in the audience, so it is a deliberate act rather than something that
    expires on its own.
    """
    conn.execute(
        "UPDATE subscriber SET bounced = 0, bounce_reason = '' WHERE id = ?",
        (subscriber_id,))


def mark_bounced(conn, subscriber_id, reason=""):
    """A bounced address is never mailed again. Continuing to send to dead
    mailboxes is the quickest way to lose the sending domain."""
    conn.execute(
        "UPDATE subscriber SET bounced = 1, bounce_reason = ? WHERE id = ?",
        (reason[:200], subscriber_id))


# --- saved lists --------------------------------------------------------------

#: Everything a saved list can narrow by. One definition, used by the screen
#: that builds a filter and by the send that resolves it back into people, so
#: the two can never mean different things by the same saved list.
FILTER_KEYS = ("q", "state", "source", "spend", "year", "batch")


def subscriber_filter_sql(values):
    """(where fragment, params) for a set of filter values.

    Returns a fragment starting with AND, meant to be appended to a query that
    already has a WHERE. Nothing here touches consent: the caller supplies that
    floor, so a saved list can narrow an audience and can never widen it.
    """
    v = {k: str((values or {}).get(k) or "").strip() for k in FILTER_KEYS}
    sql, params = "", []
    if v["state"] in (YES, NO, NEVER):
        sql += " AND consent = ?"
        params.append(v["state"])
    if v["q"]:
        sql += " AND (email LIKE ? OR name LIKE ?)"
        params += ["%%%s%%" % v["q"], "%%%s%%" % v["q"]]
    if v["source"]:
        sql += " AND source = ?"
        params.append(v["source"])
    if v["spend"] == "yes":
        sql += " AND spent > 0"
    elif v["spend"] == "no":
        sql += " AND spent <= 0"
    elif v["spend"].isdigit():
        sql += " AND spent >= ?"
        params.append(float(v["spend"]))
    if v["batch"].isdigit():
        sql += " AND batch = ?"
        params.append(int(v["batch"]))
    if v["year"].isdigit():
        sql += (" AND substr(COALESCE(NULLIF(signed_up_at, ''), consent_at,"
                " created), 1, 4) = ?")
        params.append(v["year"])
    return sql, params


#: One a day, under a 300 daily cap with room to spare.
BATCH_SIZE = 275


def build_batches(conn, size=BATCH_SIZE):
    """Cut everyone who may be mailed into numbered lists of `size`.

    Two things make this different from the rule-based lists:

    * membership is FIXED and stored on the person. The point is being able to
      say, next month, exactly who was in list 7. A filter re-evaluated at send
      time cannot answer that.
    * it only numbers people who do not already have a number, so running it
      again after an import appends newcomers to the end instead of reshuffling
      lists that have already been sent to. Reshuffling would make every record
      of what was sent to whom a lie.

    Ordered newest-buyer-first, so list 1 is the most engaged and the warm-up
    starts on the people most likely to open rather than complain.
    """
    used = conn.execute(
        "SELECT COALESCE(MAX(batch), 0) FROM subscriber").fetchone()[0]
    room = conn.execute(
        "SELECT COUNT(*) FROM subscriber WHERE batch = ?", (used,)).fetchone()[0]
    # Fill up the last, partly-full list before opening a new one.
    number = used if (used and room < size) else used + 1
    space = size - room if (used and room < size) else size

    rows = conn.execute(
        "SELECT id FROM subscriber"
        " WHERE consent = ? AND bounced = 0 AND email <> '' AND batch = 0"
        " ORDER BY last_order_at DESC, spent DESC, id", (YES,)).fetchall()
    made = 0
    for row in rows:
        conn.execute("UPDATE subscriber SET batch = ? WHERE id = ?",
                     (number, row["id"]))
        made += 1
        space -= 1
        if space == 0:
            number += 1
            space = size
    conn.commit()

    # A saved list per number, so they appear wherever any other list does.
    highest = conn.execute(
        "SELECT COALESCE(MAX(batch), 0) FROM subscriber").fetchone()[0]
    for n in range(1, highest + 1):
        save_segment(conn, "Email list %s" % n, {"batch": str(n)})
    return {"assigned": made, "lists": highest}


def clear_batches(conn):
    conn.execute("UPDATE subscriber SET batch = 0")
    conn.execute("DELETE FROM segment WHERE name LIKE 'Email list %'")
    conn.commit()


def _natural_key(name):
    """Sort names the way somebody reads them, so 'Email list 2' comes before
    'Email list 10'.

    A plain alphabetical sort compares text, so it puts 10, 11 and 12 directly
    after 1, which defeats the entire point of numbering the lists in the order
    you are meant to send them.
    """
    import re
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", name or "")]


def segments(conn):
    rows = conn.execute("SELECT * FROM segment").fetchall()
    return sorted(rows, key=lambda r: _natural_key(r["name"]))


def segment(conn, sid):
    return conn.execute("SELECT * FROM segment WHERE id = ?", (sid,)).fetchone()


def due(conn, job, minutes):
    """Has `job` gone `minutes` without running? Records the run if so.

    Asking and recording in one call on purpose: two calls leave room for the
    job to fail between them and never be retried, or to be recorded twice by
    two workers and run neither time.
    """
    row = conn.execute("SELECT at FROM ran WHERE job = ?", (job,)).fetchone()
    if row is not None and row["at"] > minutes_ago(minutes):
        return False
    conn.execute("INSERT OR REPLACE INTO ran (job, at) VALUES (?, ?)",
                 (job, now()))
    conn.commit()
    return True


def save_segment(conn, name, filters, audience=""):
    import json
    name = (name or "").strip()[:80]
    if not name:
        return None
    kept = {k: str(filters.get(k) or "") for k in FILTER_KEYS
            if str(filters.get(k) or "").strip()}
    # A list built on another saved list would be a cycle waiting to happen and
    # means nothing anybody has asked for. The caller refuses it out loud; this
    # is the floor under that.
    audience = (audience or "").strip()[:40]
    if audience.startswith("list:"):
        audience = ""
    cur = conn.execute(
        "INSERT OR REPLACE INTO segment (id, name, filters, audience, created)"
        " VALUES ((SELECT id FROM segment WHERE name = ? COLLATE NOCASE),"
        " ?, ?, ?, ?)",
        (name, name, json.dumps(kept), audience, now()))
    conn.commit()
    return cur.lastrowid


def delete_segment(conn, sid):
    conn.execute("DELETE FROM segment WHERE id = ?", (sid,))
    conn.commit()


def stats(conn):
    def n(sql, *a):
        return conn.execute(sql, a).fetchone()[0]
    today = now()[:10]
    return {
        "total": n("SELECT COUNT(*) FROM subscriber"),
        "mailable": n("SELECT COUNT(*) FROM subscriber WHERE consent = ? AND bounced = 0", YES),
        "unsubscribed": n("SELECT COUNT(*) FROM subscriber WHERE consent = ?", NO),
        "never": n("SELECT COUNT(*) FROM subscriber WHERE consent = ?", NEVER),
        "bounced": n("SELECT COUNT(*) FROM subscriber WHERE bounced = 1"),
        "sent_today": n("SELECT COUNT(*) FROM send WHERE sent = 1 AND substr(sent_at,1,10) = ?", today),
    }


# --- campaigns ----------------------------------------------------------------

def campaign_counts(conn, campaign_id):
    row = conn.execute(
        "SELECT COUNT(*) AS total,"
        " SUM(sent) AS sent,"
        " SUM(CASE WHEN opened IS NOT NULL THEN 1 ELSE 0 END) AS opened,"
        " SUM(CASE WHEN clicked IS NOT NULL THEN 1 ELSE 0 END) AS clicked,"
        " SUM(CASE WHEN sent = 0 AND error <> '' THEN 1 ELSE 0 END) AS failed"
        " FROM send WHERE campaign_id = ?", (campaign_id,)).fetchone()
    return {k: (row[k] or 0) for k in ("total", "sent", "opened", "clicked", "failed")}
