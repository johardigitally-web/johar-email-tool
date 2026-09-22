"""Composing and sending.

Six guards, because the failure that matters here is mailing real people by
mistake and there is no undo on a sent email:

1. SENDING_ENABLED is separate from whether SMTP works. Getting the mailbox
   working must not by itself be the thing that mails 300 people.
2. Nothing sends on a trigger or a timer. A person presses a button. There is no
   scheduler in this tool on purpose.
3. Recipients are frozen into `send` rows before the first message leaves, so a
   crash or a pause resumes instead of restarting.
4. A unique index refuses a duplicate even if this code asks for one.
5. DAILY_CAP bounds a runaway to one day of sending.
6. A campaign will not send at all unless PUBLIC_URL is a real host, because a
   newsletter whose unsubscribe link points at somebody's laptop is both illegal
   and a fast route to being marked as spam.

Consent is re-checked at the moment of sending, not only when the queue was
built. The queue is frozen; consent is not, and consent wins.
"""
import hashlib
import hmac
import json
import html as _html
import re
import smtplib
import time
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

from . import config, db, discounts, layouts

#: What a campaign can be aimed at. A short fixed list rather than a rule
#: builder: each of these answers a question the shop actually asks, and a
#: free-form segment editor is how the wrong people get mailed at 2am.
#: Who a campaign goes to.
#:
#: Built on `spent` and `last_order_at`, which come from the CRM, NOT on
#: `source` or `created`. The earlier versions used those and both silently
#: became wrong the moment the CRM import ran: "never bought anything" keyed off
#: source strings that no longer existed and reported 3.607 people when the true
#: answer was 326, and "recent" measured when the ROW was created, so after an
#: import that added five thousand rows in one afternoon it meant everybody.
#:
#: An audience that quietly means "everyone" is the most dangerous object in
#: this tool, so these are ordered by how recently somebody bought, which is
#: what actually predicts whether they open rather than complain.
AUDIENCES = [
    ("all", "Everyone who opted in"),
    ("buyers", "Bought from us at some point"),
    ("buyers_12m", "Bought in the last 12 months"),
    ("buyers_24m", "Bought in the last 2 years"),
    ("lapsed", "Bought, but not in over 3 years"),
    ("never_bought", "Opted in, never bought anything"),
    ("engaged", "Opened or clicked in the last 6 months"),
    ("gone_quiet", "Mailed 3 times or more, never opened"),
]

#: What each one is for, in the words somebody choosing needs rather than the
#: rule it is built from. Shown on the Lists screen.
AUDIENCE_NOTES = {
    "all": "Everyone who may be mailed at all. The widest possible send, and "
           "the one to be most careful with.",
    "buyers": "Anyone who has ever spent money. The strongest group to write "
              "to: an existing customer relationship stands on its own.",
    "buyers_12m": "The warm-up audience. Most likely to open, least likely to "
                  "bounce, most likely to remember you. Start here.",
    "buyers_24m": "Recent enough to remember the showroom.",
    "lapsed": "Worth reaching, worth reaching last, and worth a different "
              "message. Some of these addresses are years old and will bounce.",
    "never_bought": "On the list but never bought. No customer relationship to "
                    "lean on, so this group rests entirely on their opt-in.",
    "engaged": "Showed interest recently. The safest send there is, and the "
               "group to fall back on if a mail provider starts throttling.",
    "gone_quiet": "Mailed repeatedly and has never opened one. This is not a "
                  "group to write to. It is the group to STOP writing to: "
                  "people who never open are the main reason email starts "
                  "landing in spam for everybody else.",
}

#: What counts as recent interest. Six months is roughly two of this shop's
#: repeat cycles, whose median gap between orders is 73 days.
ENGAGED_MONTHS = 6

#: How many unanswered emails before somebody counts as gone quiet. Three,
#: because one can be a bad subject line and two can be a bad fortnight.
QUIET_AFTER = 3


def audience_sql(name):
    """(where clause, params). Every branch sits on the same consent floor, so no
    audience can reach somebody who opted out however the filter is written."""
    base = "consent = ? AND bounced = 0 AND email <> ''"
    params = [db.YES]
    if name == "buyers":
        return base + " AND spent > 0", params
    if name == "buyers_12m":
        return base + " AND last_order_at >= date('now','-12 months')", params
    if name == "buyers_24m":
        return base + " AND last_order_at >= date('now','-24 months')", params
    if name == "lapsed":
        # Bought, but long enough ago that they may not remember the shop. Worth
        # reaching, worth reaching LAST, and worth a different message.
        return (base + " AND spent > 0 AND last_order_at <> ''"
                       " AND last_order_at < date('now','-36 months')"), params
    if name == "never_bought":
        return base + " AND spent <= 0", params
    if name == "engaged":
        return (base + " AND (last_opened >= date('now','-%d months')"
                       " OR last_clicked >= date('now','-%d months'))"
                % (ENGAGED_MONTHS, ENGAGED_MONTHS)), params
    if name == "gone_quiet":
        # Never opened, not merely quiet lately: somebody with one open two
        # years ago at least chose to look once.
        return (base + " AND sent_count >= %d AND last_opened IS NULL"
                       " AND last_clicked IS NULL" % QUIET_AFTER), params
    return base, params


def audience_order():
    """Newest buyers first.

    Which people a first send reaches matters more than how many. Reputation is
    built out of early engagement, so somebody who bought last month goes before
    somebody who bought in 2019 and may not remember the shop well enough to do
    anything but report it.
    """
    return " ORDER BY last_order_at DESC, spent DESC, id"


def audiences(conn):
    """The built-in audiences plus every saved list, for the dropdown."""
    out = list(AUDIENCES)
    for seg in db.segments(conn):
        out.append(("list:%s" % seg["id"], seg["name"]))
    return out


def audience_sql_for(conn, name):
    """Like audience_sql, but can also resolve a saved list.

    A saved list is stored as a FILTER, not as a set of people, so it is
    re-evaluated every time. Somebody who unsubscribes the day after the list
    was saved is gone from it, which a frozen list of ids could not manage.
    """
    if str(name).startswith("list:"):
        try:
            seg = db.segment(conn, int(str(name).split(":", 1)[1]))
        except (ValueError, TypeError):
            seg = None
        if seg is not None:
            import json
            try:
                filters = json.loads(seg["filters"] or "{}")
            except ValueError:
                filters = {}
            # The consent floor comes FIRST and is not negotiable. A saved list
            # can only ever narrow it, so no filter anybody saves can reach
            # somebody who opted out.
            extra, params = db.subscriber_filter_sql(filters)
            base = "consent = ? AND bounced = 0 AND email <> ''"
            # And the audience the screen was showing when it was saved. A list
            # saved while looking at "bought in the last 2 years" meant that,
            # and the count on the button said so.
            try:
                inner = (seg["audience"] or "").strip()
            except (KeyError, IndexError, TypeError):
                inner = ""
            if inner and not inner.startswith("list:"):
                a_where, a_params = audience_sql(inner)
                return ("%s%s AND (%s)" % (base, extra, a_where),
                        [db.YES] + params + a_params)
            return base + extra, [db.YES] + params
    return audience_sql(name)


def audience_count(conn, name):
    where, params = audience_sql_for(conn, name)
    return conn.execute("SELECT COUNT(*) FROM subscriber WHERE " + where, params).fetchone()[0]


def blocked_reason(conn, camp):
    """Why this campaign would not send. None means it may. Sends nothing."""
    if not config.SENDING_ENABLED:
        return "disabled"
    if not config.SMTP_HOST:
        return "no_smtp"
    if not (camp["subject"] or "").strip():
        return "no_subject"
    if not (camp["body"] or "").strip():
        return "no_body"
    if camp["status"] == db.SENT:
        return "already_sent"
    if not config.POSTAL_ADDRESS.strip():
        return "no_postal_address"
    if not config.public_url_is_reachable():
        return "public_url_is_local"
    return None


def sent_today(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM send WHERE sent = 1 AND substr(sent_at,1,10) = ?",
        (db.now()[:10],)).fetchone()[0]


def queue(conn, campaign_id, audience, limit=None):
    """Freeze the audience into send rows. Returns how many were added.

    Safe to call again: INSERT OR IGNORE plus the unique index means re-queueing
    after somebody new subscribes adds only the newcomer and can never give an
    earlier recipient a second copy.

    `limit` is what makes a warm-up possible: queue the 250 most recent buyers
    today, press it again tomorrow and get the next 250, because the ones
    already queued are skipped by the same unique index. No lists to maintain,
    and anybody who unsubscribes in between simply never comes up.
    """
    where, params = audience_sql_for(conn, audience)
    # Anyone already queued for this campaign is excluded HERE, not left to the
    # unique index to reject later. With a limit that distinction is the whole
    # feature: without it, "the next 250" would return the same 250 every time,
    # all of them already queued, and add nobody.
    sql = ("SELECT id, email FROM subscriber WHERE " + where +
           " AND id NOT IN (SELECT subscriber_id FROM send WHERE campaign_id = ?)")
    params = params + [campaign_id]
    sql += audience_order()
    if limit:
        sql += " LIMIT ?"
        params = params + [int(limit)]
    people = conn.execute(sql, params).fetchall()
    made = 0
    for person in people:
        cur = conn.execute(
            "INSERT OR IGNORE INTO send (campaign_id, subscriber_id, to_email, token)"
            " VALUES (?,?,?,?)",
            (campaign_id, person["id"], person["email"], db.token()))
        made += cur.rowcount
    conn.commit()
    return made


# --- rendering ----------------------------------------------------------------

#: Words that mean the name on file is a business, not a person. A business
#: has no first name, and "Beste Handelsonderneming," is worse than no name.
_NOT_A_PERSON = ("b.v.", "bv", "n.v.", "nv", "vof", "v.o.f.", "handelsonderneming",
                 "holding", "beheer", "stichting", "b .v.")


def first_name(raw):
    """The name to greet somebody by, or "" when there is not one.

    Empty rather than a guess. "Beste klant" is a little flat; "Beste H." and
    "Beste Samira Azouagh" are both worse, and "Beste Handelsonderneming" is
    the sort of thing that gets forwarded to friends.
    """
    name = " ".join((raw or "").split())
    if not name or "@" in name or any(ch.isdigit() for ch in name):
        return ""
    low = name.lower()
    if any(w in low.split() or w in low for w in _NOT_A_PERSON):
        return ""
    # Automobielbedrijf, Schildersbedrijf, Transportbedrijf. The list cannot
    # name every one of them, and the ending is the whole word in Dutch.
    if low.split(" ")[0].endswith(("bedrijf", "handel", "groep")):
        return ""
    # "Azouagh, Samira" is an export talking, and the first name is after the
    # comma. Anything with two commas is a list, not a name.
    if name.count(",") == 1:
        after = name.split(",", 1)[1].strip()
        if after:
            name = after
    first = name.split(" ")[0].strip(".,;:'\"")
    # An initial is not a first name. Neither is a single letter that lost its
    # full stop on the way through a spreadsheet.
    if len(first) < 2 or not first[0].isalpha():
        return ""
    if not all(ch.isalpha() or ch in "-'" for ch in first):
        return ""
    # Case is repaired only when the whole word is one case, so that Özün,
    # McCarthy and van 't Hoff keep whatever they were given. title() on a
    # mixed-case word would turn McCarthy into Mccarthy.
    if first.isupper() or first.islower():
        first = first[:1].upper() + first[1:].lower()
    return first


def unsubscribe_url(tok):
    return "%s/u/%s" % (config.PUBLIC_URL, tok)


def _tagged(url, campaign, tok=""):
    """Add utm tags, so the visit is recognisable everywhere else.

    Our own click tracker knows somebody clicked. It cannot tell GA4, Shopify's
    own reports or Google Ads, and without these tags every one of them files an
    email visitor under "Direct" and the email looks like it did nothing. Tags
    go only on our own shop: putting them on a wa.me link is noise, and adding
    query strings to somebody else's site can break their page.

    An existing utm_source is left alone. If a link was built for an ad, that
    was deliberate.
    """
    parts = urlsplit(url)
    if not any(parts.netloc.endswith(h) for h in tag_hosts()):
        return url
    q = parse_qsl(parts.query, keep_blank_values=True)
    if any(k == "utm_source" for k, _v in q):
        return url
    q += [("utm_source", "email"), ("utm_medium", "email")]
    if campaign:
        q.append(("utm_campaign", campaign))
    # And our own token, which is what lets the shop tell us "this basket
    # belongs to the person you emailed" without guessing, without a cookie
    # from anybody else, and without trusting anything the page says. It is
    # per message and per address, so it cannot be forged or shared usefully.
    if tok:
        q.append(("ela", tok))
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(q), parts.fragment))


def tag_hosts():
    """Hosts that count as ours for utm tagging.

    Read each time rather than frozen at import, so a shop that configures its
    address after the module loads still gets its own links tagged.
    """
    return config.shop_hosts()


def _mailto_unsubscribe():
    """The address somebody can write to in order to get off the list.

    Falls back to the From address, because the header must carry a real one:
    an empty mailto is worse than no mailto, it looks like a broken opt-out.
    """
    from email.utils import parseaddr
    return (config.REPLY_TO or parseaddr(config.FROM_EMAIL)[1]
            or "postmaster@localhost")


def _list_host():
    """A stable identifier for this list, from the tool's own public address."""
    return (urlsplit(config.PUBLIC_URL).netloc or "localhost").lower()


def click_signature(tok, url):
    """Proof that WE wrote this link.

    Keyed on the token as well as the address, so a signature lifted from one
    person's email cannot be replayed against another's. Truncated to 20 hex
    characters: enough that guessing is hopeless, short enough to keep the link
    from wrapping in a plain-text part.
    """
    mac = hmac.new(config.SECRET_KEY.encode("utf-8"),
                   ("%s|%s" % (tok, url)).encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()[:20]


def signed_click(tok, url, campaign=""):
    """The tracked, signed link that replaces one address in an email."""
    target = _tagged(url, campaign, tok)
    return "%s/c/%s?%s" % (config.PUBLIC_URL, tok,
                           urlencode({"u": target,
                                      "s": click_signature(tok, target)}))


def _rewrite_links(html, tok, campaign=""):
    """Send outbound links through the click tracker.

    The unsubscribe link is left alone deliberately: routing it through a
    redirect would log somebody asking to leave as an interested click, and it
    puts an extra hop on the one link that must never fail.
    """
    def repl(m):
        url = m.group(2)
        if url.startswith("#") or url.startswith("mailto:") or "/u/" in url:
            return m.group(0)
        # What is in the file is HTML, so the ampersands in it are escaped, and
        # they have to be turned back into ampersands before the address is
        # parsed. Without this, ?key=x&amp;discount=ELA10 was read as a
        # parameter literally called "amp;discount", and the shop never saw a
        # discount at all: the code was in the email and the button did not
        # apply it. Only "&amp;" is undone, not every entity, because a link
        # written by hand in an old campaign may contain "&copy=2" meaning
        # exactly that, and a full unescape would turn it into a copyright sign.
        url = url.replace("&amp;", "&")
        return "%s%s%s" % (m.group(1),
                           _html.escape(signed_click(tok, url, campaign), quote=True),
                           m.group(3))

    return re.sub(r'(href=["\'])(https?://[^"\']+)(["\'])', repl, html)


#: Why somebody is receiving this. A newsletter and a checkout reminder reach
#: people on entirely different grounds, and telling somebody they signed up for
#: a newsletter when they did not is both untrue and precisely the claim that
#: gets challenged.
REASON_NEWSLETTER = ("U ontvangt deze e-mail omdat u zich heeft aangemeld voor "
                     "onze nieuwsbrief.")
REASON_CHECKOUT = ("U ontvangt deze e-mail omdat u een bestelling bij ons bent "
                   "begonnen.")
#: The existing-customer basis, said out loud. Telecommunicatiewet 11.7 lid 3
#: permits email to somebody who has bought, on your own similar products, with
#: a way out every time. It does NOT permit claiming they subscribed.
REASON_CUSTOMER = ("U ontvangt deze e-mail omdat u eerder bij ons heeft gekocht. "
                   "Afmelden kan altijd, met \u00e9\u00e9n klik.")


def reason_for(sub):
    """Why THIS person is receiving THIS email, from what we actually know.

    Ordered by strength of claim. An explicit opt-in outranks the customer
    basis, and somebody we can say nothing about gets the customer wording
    rather than a signup we cannot prove: the point of this function is that no
    footer ever states something the database does not support.
    """
    if sub is None:
        return REASON_CUSTOMER
    try:
        source = (sub["source"] or "").strip().lower()
        spent = sub["spent"] or 0
    except (KeyError, IndexError, TypeError):
        return REASON_CUSTOMER
    if source in ("signup", "shopify", "form", "optin", "opt-in"):
        return REASON_NEWSLETTER
    if spent > 0 or source in ("customer", "crm", "shopify-buyer"):
        return REASON_CUSTOMER
    return REASON_CUSTOMER


def _footer_html(tok, reason=REASON_NEWSLETTER):
    """Postal address, why they are getting this, and the way out.

    Assembled here rather than in a layout, because all three are legally
    required and none of them may be a design decision. Styled to match the
    house footer so it sits inside the cream panel instead of looking bolted on
    underneath it.
    """
    # KvK and the privacy link are not decoration: Thuiswinkel asks for both,
    # and a commercial email without an identifiable sender is the thing the
    # ACM actually acts on.
    identity = config.POSTAL_ADDRESS.replace("\n", " &bull; ")
    if config.KVK:
        identity += " &bull; KvK %s" % config.KVK
    return (
        '<p style="margin:0 0 6px;font-family:%s;font-size:11px;color:%s;'
        'line-height:1.6;">%s</p>'
        '<p style="margin:0;font-family:%s;font-size:11px;color:%s;line-height:1.6;">'
        '%s<br>'
        '<a href="%s" style="color:%s;">Afmelden</a>'
        ' &bull; <a href="%s" style="color:%s;">Privacybeleid</a></p>'
    ) % (layouts.FONT, layouts.MUTED, identity,
         layouts.FONT, layouts.MUTED, reason,
         unsubscribe_url(tok), layouts.MUTED,
         config.PRIVACY_URL, layouts.MUTED)


def _preheader_html(preheader):
    """The grey line the inbox shows beside the subject.

    Two hidden blocks, not one. The second is a run of zero-width joiners, which
    stops the client filling the rest of that line with whatever text happens to
    come next in the message.
    """
    if not (preheader or "").strip():
        return ""
    hide = ("display:none;max-height:0;overflow:hidden;mso-hide:all;font-size:1px;"
            "line-height:1px;color:%s;opacity:0;" % layouts.CREAM)
    return ('<div style="%s">%s</div><div style="%s">%s</div>'
            % (hide, preheader, hide, "&nbsp;&zwnj;" * 6))


def utm_name(camp):
    """A utm_campaign value that a person will recognise in a report.

    Built from the campaign name rather than its number, because "nieuwsbrief-
    september" means something in GA4 six weeks later and "17" does not.
    """
    try:
        raw = camp["name"] or ""
    except (KeyError, IndexError, TypeError):
        raw = ""
    slug = re.sub(r"[^a-z0-9]+", "-", raw.strip().lower()).strip("-")
    return slug[:60]


def promise_text(camp):
    """The words of a campaign that a recipient reads, and nothing else.

    Not the rendered body: that carries a stylesheet, and a stylesheet is full
    of percentages. Searching it for a discount works today only because none
    of them happen to look like one.
    """
    parts = []
    for key in ("subject", "preheader"):
        try:
            parts.append(camp[key] or "")
        except (KeyError, IndexError, TypeError):
            pass
    try:
        raw = camp["content"] or ""
    except (KeyError, IndexError, TypeError):
        raw = ""
    try:
        values = json.loads(raw) if raw else {}
    except ValueError:
        values = {}
    if isinstance(values, dict):
        parts += [v for v in values.values() if isinstance(v, str)]
    return " ".join(parts)


def needs_code(camp):
    """Does this campaign carry a per-person discount code?"""
    try:
        return layouts.CODE_MARK in (camp["body"] or "")
    except (KeyError, IndexError, TypeError):
        return False


def render(camp, subscriber_name, tok, reason=REASON_NEWSLETTER, code=""):
    """The exact HTML and plain text one person receives."""
    # The first name only. "Beste Samira Azouagh," reads like a letter from a
    # bailiff, and nearly every name on the list is a full one.
    name = first_name(subscriber_name)
    # "Beste daar" is a word-for-word translation of "Hi there" and reads in
    # Dutch like nothing a shop would write. "Beste klant" is what a Dutch
    # shop actually says when it does not know the name, and one in ten of the
    # list is filed under initials and a surname, so this is 342 people.
    html = (camp["body"] or "").replace("{{naam}}", name or "klant")
    if layouts.CODE_MARK in html:
        # A preview or a test gets the shape of a code, never a real one: they
        # are rendered far more often than they are sent, and each real one is a
        # discount sitting in the shop.
        html = html.replace(layouts.CODE_MARK, code or discounts.SAMPLE)
    html = _rewrite_links(html, tok, utm_name(camp))

    footer = _footer_html(tok, reason)
    pixel = ('<img src="%s/p/%s.gif" width="1" height="1" alt="" style="display:none">'
             % (config.PUBLIC_URL, tok))

    # A layout says where the footer goes. A campaign written before layouts
    # existed has no marker, so it gets the footer appended, as it always did.
    # Either way it is present: the fallback is what makes that a guarantee
    # rather than something a layout has to remember.
    if layouts.FOOTER_MARK in html:
        body = html.replace(layouts.FOOTER_MARK, footer + pixel)
    else:
        body = html + (
            '<hr style="border:none;border-top:1px solid #e5e5e5;margin:32px 0 16px">'
            '<div>%s</div>' % footer) + pixel

    pre = _preheader_html(camp["preheader"])
    if layouts.PREHEADER_MARK in body:
        body = body.replace(layouts.PREHEADER_MARK, pre)
    else:
        body = pre + body

    # A plain-text part as well as HTML. A message carrying no text alternative is
    # a long-standing spam signal, and it is what a screen reader reads out.
    # <head> and <style> go first: their contents are text, not tags, so tag
    # stripping alone would read the CSS out loud.
    src = re.sub(r"(?is)<head\b.*?</head>", " ", html)
    src = re.sub(r"(?is)<style\b.*?</style>", " ", src)
    # Keep the links. Stripping tags threw every href away, so the text part
    # carried exactly one link, the unsubscribe, next to an HTML part full of
    # them. Filters read that difference, and a new sending domain cannot
    # afford to look like it is hiding where it sends people.
    src = re.sub(r'(?is)<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                 lambda m: "%s: %s" % (re.sub(r"<[^>]+>", " ", m.group(2)).strip()
                                       or "Link", m.group(1)), src)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</h1>|</h2>|</td>|</tr>", "\n", src)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()
    # Entities back into characters. This part is read, not parsed: left alone
    # it says "Bank &amp; Fauteuil", and that is what a screen reader reads out
    # and what anyone whose mail client prefers plain text sees.
    text = _html.unescape(text)
    text += "\n\n---\n%s\nAfmelden: %s" % (config.POSTAL_ADDRESS, unsubscribe_url(tok))
    return body, text


def _feedback_sender():
    """A short, stable token for this sender, for the Feedback-ID header.

    Google buckets complaints by this field, so it has to be the same on every
    send and different from anybody else using this tool. The shop name,
    reduced to letters and digits, does both.
    """
    return re.sub(r"[^A-Za-z0-9]+", "", config.SHOP_NAME).lower()[:20] or "sender"


def feedback_id(camp):
    """Who to blame for a complaint, in the shape Google asks for.

    campaign:audience:sender:vendor. Google buckets complaints by the third
    field and reports per first field, so a bad campaign or a bad audience can
    be identified and stopped instead of guessed at.
    """
    def field(key, fallback):
        try:
            value = camp[key]
        except (KeyError, IndexError, TypeError):
            return fallback
        return re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or ""))[:40] or fallback

    return "%s:%s:%s:newsletter" % (field("id", "flow"),
                                    field("audience", "all"),
                                    _feedback_sender())


def build_message(camp, to_email, html, text, tok):
    msg = EmailMessage()
    msg["Subject"] = camp["subject"]
    name, addr = parseaddr(config.FROM_EMAIL)
    msg["From"] = formataddr((name, addr)) if name else addr
    msg["To"] = to_email
    if config.REPLY_TO:
        msg["Reply-To"] = config.REPLY_TO
    # Gmail and Yahoo require one-click unsubscribe from bulk senders. Without
    # these the reader's only exit is the spam button, which hurts far more than
    # an unsubscribe does.
    msg["List-Unsubscribe"] = "<%s>, <mailto:%s?subject=unsubscribe>" % (
        unsubscribe_url(tok), _mailto_unsubscribe())
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    # What Microsoft and Google hand back when somebody presses Junk. Without
    # these a complaint is a number with no cause attached, so there is nothing
    # to pause. Free, and they have to be present BEFORE the send, not after.
    msg["List-Id"] = "%s <%s>" % (config.SHOP_NAME, _list_host())
    msg["Feedback-ID"] = feedback_id(camp)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def _smtp():
    server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20)
    if config.SMTP_STARTTLS:
        server.starttls()
    if config.SMTP_USER:
        server.login(config.SMTP_USER, config.SMTP_PASSWORD)
    return server


def classify_failure(exc):
    """(is_permanent, reason) for a failed send.

    The difference decides whether an address stays on the list. A 5xx is the
    receiving server saying this mailbox does not exist: send to it again next
    month and the one after, and the repeated hard bounces are what teaches
    Gmail to distrust the whole domain, including the webshop's order
    confirmations. A 4xx is "not now" - a full mailbox, a greylist, a temporary
    outage - and removing somebody for that would lose a real customer.

    Anything that is not an SMTP refusal at all (no route to host, TLS failure)
    is OUR problem, never theirs, and must not mark anybody.
    """
    refused = getattr(exc, "recipients", None)
    if refused:
        # SMTPRecipientsRefused: {address: (code, message)}
        for code, message in refused.values():
            text = message.decode("utf-8", "replace") if isinstance(
                message, bytes) else str(message)
            return (500 <= int(code) < 600), "%s %s" % (code, text)
    code = getattr(exc, "smtp_code", None)
    if code is not None:
        err = getattr(exc, "smtp_error", b"")
        text = err.decode("utf-8", "replace") if isinstance(err, bytes) else str(err)
        return (500 <= int(code) < 600), "%s %s" % (code, text)
    return False, str(exc)


def deliver(server, msg, tok):
    """Hand one message over, with a return address that names it.

    smtplib uses the From header as the envelope sender unless it is told
    otherwise. Telling it otherwise is the whole of bounce handling: what comes
    back arrives at bounce+<token>@, and the token says who and which message.
    """
    envelope = config.bounce_address(tok)
    if envelope:
        return server.send_message(msg, from_addr=envelope)
    return server.send_message(msg)


def send_one(conn, send_row, server=None, ignore_switch=False):
    """Send a single queued message. Returns (ok, reason).

    `ignore_switch` exists only for the test send: seeing the thing in a real
    inbox is how a broken link gets caught, and requiring the live switch for
    that would mean the first proper look at a newsletter is the moment everyone
    else also gets it.
    """
    sub = conn.execute("SELECT * FROM subscriber WHERE id = ?",
                       (send_row["subscriber_id"],)).fetchone()
    if send_row["sent"]:
        return False, "already_sent"
    if sub is None or sub["consent"] != db.YES or sub["bounced"]:
        # Terminal. They unsubscribed or their address died after being queued,
        # and no amount of pressing send again will change that.
        conn.execute("UPDATE send SET error = ?, skipped = ? WHERE id = ?",
                     ("not_mailable", "not_mailable", send_row["id"]))
        conn.commit()
        return False, "not_mailable"
    # The allowlist. Deliberately checked here, per message, rather than when a
    # campaign is queued: that way no route into this function can get round it,
    # including a queue built before the list was set.
    if not config.recipient_allowed(send_row["to_email"]):
        conn.execute("UPDATE send SET error = ? WHERE id = ?",
                     ("not_on_allowlist", send_row["id"]))
        conn.commit()
        return False, "not_on_allowlist"

    camp = conn.execute("SELECT * FROM campaign WHERE id = ?",
                        (send_row["campaign_id"],)).fetchone()

    # A campaign built around a discount code gets one made for this person.
    # If the shop will not make it, the message waits: an email whose code
    # fails at the checkout is worse than one that arrives an hour later.
    code = ""
    if needs_code(camp):
        code = send_row["code"] if "code" in send_row.keys() else ""
        if not code:
            # Worth what the email says it is worth. Read out of the finished
            # email rather than set anywhere, so the number in the code and the
            # number the customer read cannot drift apart.
            rate, why = discounts.rate_for(promise_text(camp))
            if rate is None:
                conn.execute("UPDATE send SET error = ? WHERE id = ?",
                             ("discount_unclear: %s" % why, send_row["id"]))
                conn.commit()
                return False, "discount_unclear"
            code = discounts.create(send_row["to_email"], percent=rate) or ""
        if not code:
            return False, "no_discount_code"
        conn.execute("UPDATE send SET code = ? WHERE id = ?",
                     (code, send_row["id"]))
        conn.commit()

    html, text = render(camp, sub["name"], send_row["token"],
                        reason=reason_for(sub), code=code)
    msg = build_message(camp, send_row["to_email"], html, text, send_row["token"])

    own = server is None
    try:
        if own:
            server = _smtp()
        deliver(server, msg, send_row["token"])
    except Exception as e:
        permanent, reason = classify_failure(e)
        conn.execute("UPDATE send SET error = ? WHERE id = ?",
                     (reason[:250], send_row["id"]))
        if permanent:
            # Terminal, so it stops counting as outstanding work: a campaign
            # waiting on addresses that will never accept mail never finishes.
            conn.execute("UPDATE send SET skipped = ? WHERE id = ?",
                         ("bounced", send_row["id"]))
            # A refusal the server calls permanent means this address does not
            # exist. Mailing it again on the next campaign is what turns a dead
            # address into a damaged sending reputation, so it comes off the
            # list now rather than after somebody notices.
            db.mark_bounced(conn, sub["id"], reason[:250])
        conn.commit()
        return False, "bounced" if permanent else "error"
    finally:
        if own and server is not None:
            try:
                server.quit()
            except Exception:
                pass

    stamp = db.now()
    conn.execute("UPDATE send SET sent = 1, sent_at = ?, error = '' WHERE id = ?",
                 (stamp, send_row["id"]))
    # The count as well as the date. "Mailed three times and never opened one"
    # is the question worth asking, and a date cannot answer it.
    conn.execute("UPDATE subscriber SET last_sent = ?,"
                 " sent_count = COALESCE(sent_count, 0) + 1 WHERE id = ?",
                 (stamp, sub["id"]))
    conn.commit()
    return True, "sent"


#: How long one run may hold a row before another may take it back. Longer
#: than any batch should take, short enough that a crash is not a lost evening.
CLAIM_MINUTES = 15


def pending_count(conn, campaign_id):
    """How many are genuinely still to go.

    NOT the same as "not sent": a row that can never be sent is not outstanding
    work, and counting it as such is what left campaigns stuck at 99 per cent
    for ever.
    """
    return conn.execute(
        "SELECT COUNT(*) FROM send WHERE campaign_id = ? AND sent = 0"
        " AND skipped = ''", (campaign_id,)).fetchone()[0]


def send_batch(conn, campaign_id, limit=None):
    """Send up to `limit` queued messages. Resumable: it only ever takes rows
    that have not gone yet, so pressing send again continues the same run."""
    camp = conn.execute("SELECT * FROM campaign WHERE id = ?", (campaign_id,)).fetchone()
    stop = blocked_reason(conn, camp)
    if stop:
        return {"sent": 0, "failed": 0, "stopped": stop}
    if camp["status"] != db.SENDING:
        return {"sent": 0, "failed": 0, "stopped": "not_sending"}

    limit = limit or config.BATCH_SIZE

    # Take the rows before sending any of them, in one statement. SQLite holds a
    # write lock for the length of an UPDATE, so two runs cannot come away with
    # the same person: the second sees them already claimed.
    #
    # A claim older than the lease is taken back, or a run that died halfway
    # would strand its batch for ever.
    claim = db.token()
    stale = db.minutes_ago(CLAIM_MINUTES)
    conn.execute(
        "UPDATE send SET claim = ?, claimed_at = ?"
        " WHERE id IN (SELECT id FROM send WHERE campaign_id = ? AND sent = 0"
        "   AND skipped = '' AND (claim = '' OR claimed_at < ?)"
        "   ORDER BY id LIMIT ?)",
        (claim, db.now(), campaign_id, stale, limit))
    conn.commit()
    pending = conn.execute(
        "SELECT * FROM send WHERE claim = ? ORDER BY id", (claim,)).fetchall()

    ok = bad = refused = 0
    stopped = ""
    server = None
    try:
        # One connection for the whole batch. Opening one per message is slow and
        # looks like a script to the receiving server.
        server = _smtp()
        for row in pending:
            if sent_today(conn) >= config.DAILY_CAP:
                stopped = "daily_cap"
                break
            good, why = send_one(conn, row, server=server)
            ok, bad = (ok + 1, bad) if good else (ok, bad + 1)
            if why == "not_on_allowlist":
                refused += 1
            if config.SEND_PAUSE:
                time.sleep(config.SEND_PAUSE)
    except Exception as e:
        stopped = "smtp_error: %s" % str(e)[:120]
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass

    # Hand back whatever this run did not manage to send, so the next one can
    # pick it up rather than waiting for the lease to expire.
    conn.execute("UPDATE send SET claim = '', claimed_at = ''"
                 " WHERE claim = ? AND sent = 0", (claim,))
    conn.commit()

    # Everybody the allowlist refused, and nothing else, means the run is not
    # going to make progress by being pressed again. Say so instead of letting
    # the page spin.
    if refused and refused == bad and not ok:
        stopped = stopped or "not_on_allowlist"

    left = pending_count(conn, campaign_id)
    # Finished means finished: every person the audience contains has had it,
    # not merely everybody queued so far. Marking it sent at the end of the
    # first batch is what left the documented twelve-day ramp with no day two,
    # because a sent campaign refuses to send again.
    unqueued = audience_count(conn, camp["audience"]) - conn.execute(
        "SELECT COUNT(*) FROM send WHERE campaign_id = ?",
        (campaign_id,)).fetchone()[0]
    if left == 0 and unqueued <= 0:
        conn.execute("UPDATE campaign SET status = ?, finished = ? WHERE id = ?",
                     (db.SENT, db.now(), campaign_id))
        conn.commit()
    return {"sent": ok, "failed": bad, "stopped": stopped}


def _touch_subscriber(conn, tok, column, stamp):
    """Put an open or a click on the person as well as on the message.

    By token, across both tables, because a token belongs either to a campaign
    message or to a flow reminder and the pixel has no way of knowing which.
    Always overwritten rather than written once: what matters here is the LAST
    time somebody showed interest, not the first.
    """
    conn.execute(
        "UPDATE subscriber SET %s = ? WHERE id IN ("
        "  SELECT subscriber_id FROM send WHERE token = ?"
        "  UNION SELECT s.id FROM subscriber s"
        "   JOIN flow f ON lower(f.email) = lower(s.email)"
        "   JOIN flow_send fs ON fs.flow_id = f.id WHERE fs.token = ?)" % column,
        (stamp, tok, tok))


def record_open(conn, tok):
    """A token belongs either to a campaign message or to a flow reminder, and
    the pixel has no way of knowing which. Writing to both is how a reminder
    stops being invisible; before this every flow email looked unread forever."""
    conn.execute("UPDATE send SET opened = ? WHERE token = ? AND opened IS NULL",
                 (db.now(), tok))
    conn.execute("UPDATE flow_send SET opened = ? WHERE token = ? AND opened IS NULL",
                 (db.now(), tok))
    _touch_subscriber(conn, tok, "last_opened", db.now())
    conn.commit()


def record_click(conn, tok, url=""):
    """A click also proves an open, which image blocking may have hidden.

    The destination is kept as well. One email carries a shop button and a
    WhatsApp button, and which of the two somebody pressed is the difference
    between a visit and a conversation.
    """
    stamp = db.now()
    url = (url or "")[:500]
    for table in ("send", "flow_send"):
        conn.execute(
            "UPDATE %s SET clicked_url = ? WHERE token = ? AND clicked_url = ''"
            % table, (url, tok))
    conn.execute("UPDATE send SET clicked = ? WHERE token = ? AND clicked IS NULL",
                 (stamp, tok))
    conn.execute("UPDATE send SET opened = ? WHERE token = ? AND opened IS NULL",
                 (stamp, tok))
    conn.execute("UPDATE flow_send SET clicked = ? WHERE token = ? AND clicked IS NULL",
                 (stamp, tok))
    # A click proves an open too, which image blocking may have hidden.
    _touch_subscriber(conn, tok, "last_clicked", stamp)
    _touch_subscriber(conn, tok, "last_opened", stamp)
    conn.execute("UPDATE flow_send SET opened = ? WHERE token = ? AND opened IS NULL",
                 (stamp, tok))
    conn.commit()
