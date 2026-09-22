"""Reading the mail that comes back, and taking dead addresses off the list.

WHY THIS IS NOT OPTIONAL
Sending to an address that no longer exists is the single loudest signal a mail
provider has that a sender is careless. Do it for a few weeks and Gmail stops
delivering to the inbox, then stops accepting at all, and the damage lands on
the order confirmations too, because they leave from the same domain. Every
paid-for service does this for you, which is exactly why it is the piece that
had to be built when we chose to depend on nobody.

HOW A BOUNCE FINDS ITS WAY BACK
Every message goes out with an envelope sender of

    bounce+<token>@bounces.yourshop.example

where the token is the same one already in that message's unsubscribe link and
open pixel. So a returned email identifies the exact person and the exact
message, without matching on names or reading anything a bounce says about
itself. Postfix drops whatever comes back into one mailbox and this reads it.

HARD AND SOFT, AND WHY THE DIFFERENCE MATTERS
A 5.x.x status means the address is gone: unsubscribe it, permanently. A 4.x.x
means the far side was busy, out of space or greylisting: it says nothing about
the address and is counted, not acted on. Treating a full mailbox as a dead one
loses real customers, and it is a mistake you cannot see afterwards.
"""
import email
import email.policy
import os
import re

from . import db

#: Where Postfix leaves what comes back.
MAILDIR = os.environ.get("BOUNCE_MAILDIR", "/var/lib/mailer/Maildir")

#: bounce+TOKEN@... , which is how the returned mail names the person.
TOKEN = re.compile(r"bounce\+([A-Za-z0-9_\-]{6,64})@", re.I)

#: The machine-readable verdict inside a delivery status notification.
STATUS = re.compile(r"^Status:\s*([245])\.\d+\.\d+", re.I | re.M)

#: What a server says when it has no machine-readable part. Only used when the
#: DSN gave us nothing, and deliberately short: guessing from prose is how a
#: temporary problem gets read as a permanent one.
HARD_WORDS = re.compile(
    r"user unknown|no such user|does not exist|recipient rejected|"
    r"unrouteable address|mailbox unavailable|invalid recipient|"
    r"address rejected|no mailbox here", re.I)


def _verdict(raw):
    """('hard'|'soft'|'', why). The empty verdict means "this is not a bounce"."""
    hit = STATUS.search(raw)
    if hit:
        kind = "hard" if hit.group(1) == "5" else "soft"
        line = raw[hit.start():hit.start() + 120].splitlines()[0]
        return kind, line.strip()
    if "Delivery Status Notification" in raw or "Mail Delivery" in raw:
        if HARD_WORDS.search(raw):
            found = HARD_WORDS.search(raw)
            return "hard", raw[max(0, found.start() - 40):found.end() + 40].strip()[:120]
        return "soft", "returned, no status given"
    return "", ""


def _token_of(msg, raw):
    """Which message came back. The envelope sender first, because it is ours
    and cannot be reworded by the server that rejected it."""
    for header in ("X-Original-To", "To", "Delivered-To", "Original-Recipient",
                   "Final-Recipient"):
        hit = TOKEN.search(str(msg.get(header, "")))
        if hit:
            return hit.group(1)
    hit = TOKEN.search(raw)
    return hit.group(1) if hit else ""


def _who(conn, token):
    """The address a token was sent to, from either kind of message."""
    row = conn.execute(
        "SELECT to_email FROM (SELECT token, to_email FROM send"
        " UNION ALL SELECT token, to_email FROM flow_send) WHERE token = ?",
        (token,)).fetchone()
    return (row["to_email"] if row else "").strip().lower()


def scan(conn, maildir=None, limit=500):
    """Read what came back, act on it, and delete what has been dealt with.

    Deleting rather than keeping: the reason lives on the subscriber as
    `bounce_reason`, which is where anybody would look, and a maildir that only
    grows is a disk that eventually fills.
    """
    folder = os.path.join(maildir or MAILDIR, "new")
    out = {"read": 0, "hard": 0, "soft": 0, "unknown": 0, "not_ours": 0,
           "unreadable": 0}
    try:
        names = sorted(os.listdir(folder))[:limit]
    except OSError:
        out["error"] = "no mailbox at %s" % folder
        return out

    for name in names:
        if name.startswith("."):
            continue
        path = os.path.join(folder, name)
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            # Counted, LOUDLY. This is the exact failure that made a completely
            # blind scanner print the same line as a healthy one for two weeks.
            out["unreadable"] = out.get("unreadable", 0) + 1
            out["error"] = "cannot read %s: %s" % (name, exc.strerror or exc)
            continue
        out["read"] += 1
        raw = data.decode("utf-8", "replace")
        msg = email.message_from_string(raw, policy=email.policy.default)

        kind, why = _verdict(raw)
        token = _token_of(msg, raw)
        email_addr = _who(conn, token) if token else ""

        if not kind or not email_addr:
            # Something we cannot act on: a reply, an auto-responder, a report.
            # Moved aside rather than deleted, so it can still be looked at, and
            # rather than left in place, because `new/` is read in name order
            # with a window: a few hundred out-of-office replies at the front
            # would otherwise stop bounce processing for ever.
            out["unknown" if kind else "not_ours"] += 1
            _set_aside(path, maildir or MAILDIR, name)
            continue

        if kind == "hard":
            sub = conn.execute("SELECT id FROM subscriber WHERE email = ?",
                               (email_addr,)).fetchone()
            if sub is not None:
                db.mark_bounced(conn, sub["id"], why[:250])
            # And out of any sequence they were in, immediately.
            conn.execute("UPDATE flow SET stopped = 'bounced'"
                         " WHERE email = ? AND stopped = ''", (email_addr,))
            out["hard"] += 1
        else:
            out["soft"] += 1
        conn.execute("UPDATE send SET error = ? WHERE token = ?", (why[:250], token))
        conn.execute("UPDATE flow_send SET error = ? WHERE token = ?",
                     (why[:250], token))
        try:
            os.unlink(path)
        except OSError:
            pass
    conn.commit()
    return out


def _set_aside(path, root, name):
    """Move something that is not a bounce into review/, out of the window."""
    review = os.path.join(root, "review")
    try:
        os.makedirs(review, exist_ok=True)
        os.replace(path, os.path.join(review, name))
    except OSError:
        pass


def held_for_review(maildir=None):
    """How much arrived that we could not classify. Worth a number on a screen:
    a rising pile here means real replies are going unread."""
    try:
        return len(os.listdir(os.path.join(maildir or MAILDIR, "review")))
    except OSError:
        return 0


def waiting(maildir=None):
    """How many returned emails are sitting unread. For the setup screen."""
    try:
        return len(os.listdir(os.path.join(maildir or MAILDIR, "new")))
    except OSError:
        return 0
