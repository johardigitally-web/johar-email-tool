"""Settings, read from the environment or a .env file beside the app.

Everything has a default that is safe rather than convenient. In particular
SENDING_ENABLED is off, so a fresh checkout cannot mail anybody even if every
other detail happens to be correct.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv():
    """Read .env without a dependency. Ignores blanks, comments and junk lines."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


def _bool(key, default=False):
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "ja")


DB_PATH = os.environ.get("MAILER_DB", str(ROOT / "mailer.sqlite3"))

#: The password for the tool itself. It holds a mailing list and can send mail as
#: the company, so it is not left open even on a laptop.
PASSWORD = os.environ.get("MAILER_PASSWORD", "")
#: The default is a value anybody can read in this file, which is fine on a
#: laptop and useless anywhere else: knowing it means being able to forge a
#: session cookie and walk straight in.
DEV_SECRET = "dev-only-not-for-a-public-host"
SECRET_KEY = os.environ.get("MAILER_SECRET", DEV_SECRET)

# --- The shop this sends for --------------------------------------------------
#: Who the mail is from. Nothing below names a particular shop on purpose: this
#: tool is not tied to one, and a fresh checkout should read as nobody in
#: particular rather than as whoever ran it last.
SHOP_NAME = os.environ.get("SHOP_NAME", "Your Shop").strip()

#: The shop's public website. Where the logo links to, and where a button with
#: no url of its own sends the reader.
SHOP_URL = os.environ.get("SHOP_URL", "https://example.com").strip().rstrip("/")

#: A full https url to the logo, on a host a mail client can reach: an inbox
#: cannot read an image off this server's disk. Empty means no logo is drawn,
#: which is a plainer email rather than one with a broken image in it.
LOGO_URL = os.environ.get("LOGO_URL", "").strip()

#: International form, digits only, e.g. 31612345678. Empty means the WhatsApp
#: button is left out rather than pointing at nobody.
WHATSAPP_NUMBER = os.environ.get("WHATSAPP_NUMBER", "").strip()

# --- Sending ------------------------------------------------------------------
#: Separate from whether SMTP is configured, on purpose. Getting the mailbox
#: working must not by itself be what sends a newsletter to 300 people.
SENDING_ENABLED = _bool("SENDING_ENABLED", False)

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or 587)
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_STARTTLS = _bool("SMTP_STARTTLS", True)

#: Where a rejected message comes back to. Empty means "use the From address",
#: which is what it did before we had a server of our own, and which tells us
#: nothing about WHICH message bounced. With this set, every message leaves with
#: an envelope sender of bounce+<token>@<this>, so a returned email names the
#: exact person and the exact message.
BOUNCE_DOMAIN = os.environ.get("BOUNCE_DOMAIN", "").strip()


def bounce_address(token):
    if not BOUNCE_DOMAIN:
        return ""
    return "bounce+%s@%s" % (token, BOUNCE_DOMAIN)

#: The default follows SHOP_NAME so a first run already has a sensible From
#: line, but the address is a placeholder: mail only leaves once it is a mailbox
#: on a domain this sender is allowed to use.
FROM_EMAIL = os.environ.get("FROM_EMAIL", "%s <noreply@example.com>" % SHOP_NAME)
#: Where a reader's reply lands. Empty means replies go back to the From
#: address, which is right when that is a mailbox somebody reads.
REPLY_TO = os.environ.get("REPLY_TO", "")

#: How many go out per press of the send button, and how long to wait between
#: them. A relay that sees 300 messages arrive in four seconds treats the sender
#: differently from one that sees them trickle.
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "25") or 25)
SEND_PAUSE = float(os.environ.get("SEND_PAUSE", "0.4") or 0.4)

#: The ceiling on a mistake. A runaway loop costs one day of sending rather than
#: the domain's reputation, which also carries the webshop's order confirmations.
DAILY_CAP = int(os.environ.get("DAILY_CAP", "300") or 300)

#: Legally required in the footer of every marketing email, so it has no
#: default worth shipping. Enforced rather than optional: while it is empty the
#: tool refuses to send at all, rather than sending a footer that names nobody.
POSTAL_ADDRESS = os.environ.get("POSTAL_ADDRESS", "")
#: Both belong in the footer of a commercial email. An unidentifiable sender is
#: what a consumer authority acts on, and a trustmark asks for the registration
#: number. Either one left empty is simply left out of the footer, so an
#: unregistered sender still gets a legal email.
KVK = os.environ.get("KVK", "").strip()
PRIVACY_URL = os.environ.get("PRIVACY_URL", "").strip()

#: Where the unsubscribe link, the open pixel and the click tracker point. This
#: has to be a public address: a recipient's inbox cannot reach your laptop. Left
#: at localhost the tool still composes, imports and test-sends, but it refuses a
#: real campaign, because a newsletter whose unsubscribe link 404s is illegal as
#: well as rude.
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://127.0.0.1:5000").rstrip("/")

#: Where a click goes if the tracked url is missing or malformed. The shop's
#: front page is a better landing than an error, so that is the default.
FALLBACK_URL = os.environ.get("FALLBACK_URL", SHOP_URL)

#: A hard allowlist. While this is non-empty the tool will send to these
#: addresses and nothing else, whatever a campaign's audience says. It is not a
#: preference the interface can override: the check sits in send_one, below
#: every button and every API call.
#:
#: This is where "nothing to customers yet, tests to my own address only" gets
#: written down. Rather than remembered by whoever presses send, it is enforced
#: by the code. Empty the list to allow real sending again - a deliberate,
#: visible edit.
ALLOWED_RECIPIENTS = [a.strip().lower() for a in
                      os.environ.get("ALLOWED_RECIPIENTS", "").split(",") if a.strip()]


def recipient_allowed(email):
    if not ALLOWED_RECIPIENTS:
        return True
    return (email or "").strip().lower() in ALLOWED_RECIPIENTS

# --- Where subscribers come from ---------------------------------------------
SHOPIFY_STORE_DOMAIN = os.environ.get("SHOPIFY_STORE_DOMAIN", "")
SHOPIFY_CLIENT_ID = os.environ.get("SHOPIFY_CLIENT_ID", "")
SHOPIFY_SECRET = os.environ.get("SHOPIFY_SECRET", "")
SHOPIFY_API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2025-01")

#: Optional read-only connection to the CRM's Postgres, to pick up the customers
#: who ticked the newsletter box there. Left empty the CRM import is simply
#: unavailable rather than broken.
CRM_DSN = os.environ.get("CRM_DSN", "")

# --- automated flows ----------------------------------------------------------
#: The abandoned checkout reminders. A SECOND switch, deliberately not the same
#: one as SENDING_ENABLED: getting newsletters working must not by itself switch
#: on something that mails people without anybody pressing anything. Guard 2 of
#: this tool used to be "nothing sends on a trigger or a timer"; a flow breaks
#: that by definition, so it gets its own brake.
FLOWS_ENABLED = _bool("FLOWS_ENABLED", False)

#: A hard floor on how far back a reminder may reach, as YYYY-MM-DD. A checkout
#: abandoned before this date can NEVER be mailed about, whatever else is set.
#:
#: This is the guard that matters most on the day it is switched on. Without it,
#: turning flows on would immediately email everybody who abandoned a basket in
#: the last three months about a basket they have long forgotten. Empty means
#: nothing sends.
FLOWS_FROM = os.environ.get("FLOWS_FROM", "").strip()

#: Hours after the checkout was abandoned for each of the three reminders. The
#: same shape as the hosted flows this replaces, so moving over changes who
#: sends the mail and not what the customer experiences.
FLOW_STEPS = [2, 24, 72]

#: How many reminders may go out in a day, separate from the campaign cap. A
#: runaway here would be worse than a runaway campaign: nobody is watching.
FLOW_DAILY_CAP = int(os.environ.get("FLOW_DAILY_CAP", "100") or 100)


def flows_ready():
    """(ok, reason). Every condition that must hold before a reminder can go."""
    if not FLOWS_ENABLED:
        return False, "Flows are switched off (FLOWS_ENABLED)."
    if not FLOWS_FROM:
        return False, ("No start date set (FLOWS_FROM). Without one this would "
                       "reach back over every basket ever abandoned.")
    if not SENDING_ENABLED:
        return False, "Sending is switched off (SENDING_ENABLED)."
    if not SMTP_HOST:
        return False, "No mail server configured (SMTP_HOST)."
    if not public_url_is_reachable():
        return False, "PUBLIC_URL is not a real address."
    if not POSTAL_ADDRESS.strip():
        return False, "No postal address set, which the footer legally needs."
    return True, ""


def refuse_to_start():
    """The reason this must not run, or "" if it may.

    An empty password does not mean "no login needed", it means the login is
    switched off, and the tool holds a mailing list and can send mail as the
    company. On a laptop that is a convenience. On a public address it is an
    open door, so the service refuses to come up rather than coming up
    unprotected and hoping somebody reads the warning on the page.
    """
    if not public_url_is_reachable():
        return ""                      # a laptop, or a local test
    if not PASSWORD:
        return ("MAILER_PASSWORD is empty and PUBLIC_URL is a public address. "
                "The login would be switched off for anybody who found it.")
    if SECRET_KEY == DEV_SECRET:
        return ("MAILER_SECRET is still the value from the source. Anybody "
                "reading it could forge a session and walk in.")
    return ""


def public_url_is_reachable():
    """A localhost PUBLIC_URL means tracking links point at a machine no
    recipient can reach. True only when it looks like a real host."""
    u = PUBLIC_URL.lower()
    return not ("localhost" in u or "127.0.0.1" in u or u.startswith("http://0.0.0.0"))


def shop_hosts():
    """The hostnames that count as the shop's own, from SHOP_URL.

    Three modules need this and all three need it to be exact. A product link
    goes straight into a customer's email, so "contains our domain" is not good
    enough: yourshop.nl.example.com contains it too, and that is how a page
    would get the tool to send customers somewhere else.
    """
    from urllib.parse import urlsplit
    host = (urlsplit(SHOP_URL).netloc or "").lower().strip()
    if not host:
        return ()
    bare = host[4:] if host.startswith("www.") else host
    out = [bare, "www." + bare]
    if SHOPIFY_STORE_DOMAIN:
        out.append(SHOPIFY_STORE_DOMAIN.strip().lower())
    return tuple(out)
