"""A small standalone newsletter tool.

Standalone on purpose. It holds its own SQLite file, its own login and its own
sending switch, so nothing here can reach another system's database or its
customers, and a mistake in one cannot take the other down.

Run it with run.bat, or:
    .venv\\Scripts\\python app.py

The three routes at the bottom (/p, /c, /u) are public: they are opened from
inside somebody's inbox and have no session. Everything else needs the password.
"""
import functools
import hmac
import json
import os

from flask import (Flask, g, redirect, request, render_template,
                   session, url_for, send_file, abort)

from urllib.parse import quote, urlsplit

from mailer import (attrib, blocks, carts, config, crm, db, flows, layouts,
                    send as sender, shop, sources)

app = Flask(__name__)
app.secret_key = config.SECRET_KEY

def _custom_layout(key):
    """A template the owner built, in the shape the composer expects.

    Its own connection, opened and closed here: this is called from inside
    layouts.get(), which has no idea about request context and must keep
    working from the send loop and the cron runner as well as from a page.
    """
    try:
        tid = int(key[len(layouts.CUSTOM_PREFIX):])
    except (TypeError, ValueError):
        return None
    c = db.connect()
    try:
        row = db.get_template(c, tid)
    finally:
        c.close()
    if row is None:
        return None
    try:
        blocks = json.loads(row["blocks"] or "[]")
        content = json.loads(row["content"] or "{}")
    except ValueError:
        blocks, content = [], {}
    fields = [(k, label, kind, hint, content.get(k, default))
              for k, label, kind, hint, default in layouts.fields_for_blocks(blocks)]
    return {"key": key, "name": row["name"], "blurb": row["blurb"] or "Eigen sjabloon",
            "subject": row["subject"], "preheader": row["preheader"],
            "fields": fields, "blocks": blocks, "custom": True, "id": tid}


layouts.CUSTOM = _custom_layout


def _all_layouts():
    """Built-in first, then the owner's own, for every screen that offers a
    choice of design."""
    own = []
    for row in db.templates(conn()):
        spec = _custom_layout(layouts.CUSTOM_PREFIX + str(row["id"]))
        if spec:
            own.append(spec)
    return list(layouts.LAYOUTS) + own


# Before anything else. A tool that holds five thousand addresses and can send
# mail as the company does not get to start unprotected on a public address.
_refuse = config.refuse_to_start()
if _refuse:
    raise SystemExit("REFUSING TO START: %s" % _refuse)

#: The blocked_reason codes in the words a person reading the screen needs.
#: One map, used by both the page and the JSON endpoint, so the two can never
#: explain the same refusal differently.
REASONS = {
    "disabled": "Sending is switched off (SENDING_ENABLED in .env).",
    "no_smtp": "No SMTP configured (SMTP_HOST).",
    "no_subject": "Give it a subject first.",
    "no_body": "The message is still empty.",
    "already_sent": "This campaign has already been sent.",
    "no_postal_address": "No postal address set. It is legally required in the footer.",
    "public_url_is_local": "PUBLIC_URL is a local address, so the unsubscribe link would not work.",
    "not_sending": "The campaign is not set to sending.",
    "daily_cap": "daily cap reached",
    "not_on_allowlist": "blocked: not on ALLOWED_RECIPIENTS",
}

#: A 1x1 transparent GIF, inlined so the open pixel cannot break by somebody
#: tidying up a static folder.
PIXEL = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04"
         b"\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D"
         b"\x01\x00;")


def conn():
    if "db" not in g:
        g.db = db.connect()
    return g.db


@app.teardown_appcontext
def _close(_exc):
    c = g.pop("db", None)
    if c is not None:
        c.close()


def login_required(view):
    @functools.wraps(view)
    def wrapped(*a, **kw):
        # An empty password means the tool is running unprotected. That is fine
        # on a laptop and dangerous anywhere else, so the pages say so rather
        # than silently allowing it.
        if config.PASSWORD and not session.get("ok"):
            return redirect(url_for("login", next=request.path))
        return view(*a, **kw)
    return wrapped


def _static_version():
    """A number that changes whenever a static file does.

    Flask tells browsers to cache /static for twelve hours. Without this, a
    deploy that fixes a broken stylesheet or script leaves the broken copy in
    somebody's browser until that expires, which makes a real fix look like it
    did nothing. Computed once at startup, because these files only change when
    the service is restarted anyway.
    """
    newest = 0
    folder = os.path.join(app.root_path, "static")
    try:
        names = os.listdir(folder)
    except OSError:
        return "0"
    for name in names:
        try:
            newest = max(newest, os.path.getmtime(os.path.join(folder, name)))
        except OSError:
            pass
    return str(int(newest))


STATIC_VERSION = _static_version()


@app.context_processor
def _globals():
    # How many things still stand between here and a real send. Shown as one
    # chip in the header rather than three banners repeated on every page:
    # a warning you see everywhere is a warning you stop reading.
    blockers = sum(1 for ok in (
        bool(config.SMTP_HOST),
        bool(config.POSTAL_ADDRESS.strip()),
        config.public_url_is_reachable(),
        config.SENDING_ENABLED,
    ) if not ok)
    return {
        "cfg": config,
        "unprotected": not config.PASSWORD,
        "public_ok": config.public_url_is_reachable(),
        "blockers": blockers,
        "locked_to": config.ALLOWED_RECIPIENTS,
        "REASONS": REASONS,
        "sv": STATIC_VERSION,
    }


def _safe_next(target):
    """Only ever come back to a path on this site.

    `next` arrives in the query string, so taken at face value the login page is
    an open redirect: a link that really is on our own domain but lands on
    somebody else's. That is the exact shape used to make a phishing page look
    legitimate, and it matters now the tool is reachable from the internet.
    """
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("index")


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        # compare_digest rather than ==, which returns as soon as two characters
        # differ and so leaks the password one character at a time to anybody
        # timing the replies.
        given = request.form.get("password") or ""
        if hmac.compare_digest(given, config.PASSWORD):
            session["ok"] = True
            return redirect(_safe_next(request.args.get("next")))
        error = "Wrong password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --- campaigns ----------------------------------------------------------------

@app.context_processor
def _shop():
    """The shop's own name and reply address, on every screen.

    A context processor rather than passing them into thirty render_template
    calls: they are chrome, every page wants them, and forgetting one would
    leave a page with a blank company name and no way to tell why.
    """
    return {"shop_name": config.SHOP_NAME, "reply_to": config.REPLY_TO}


@app.route("/")
@login_required
def index():
    rows = conn().execute(
        "SELECT c.*,"
        " (SELECT COUNT(*) FROM send s WHERE s.campaign_id = c.id) AS n_total,"
        " (SELECT COUNT(*) FROM send s WHERE s.campaign_id = c.id AND s.sent = 1) AS n_sent,"
        " (SELECT COUNT(*) FROM send s WHERE s.campaign_id = c.id AND s.opened IS NOT NULL) AS n_open"
        " FROM campaign c ORDER BY c.created DESC").fetchall()
    return render_template("index.html", campaigns=rows, stats=db.stats(conn()),
                           names=dict(sender.audiences(conn())))


def _compose(form, layout_key):
    """Work out (layout, content, body) from a submitted composer form.

    The HTML body is generated here and stored, rather than assembled at send
    time, so everything downstream - the send loop, the preview, the test button
    - keeps reading one field and needs to know nothing about layouts.

    An unknown or empty layout means a campaign written before layouts existed.
    Those keep their hand-written HTML and their old editor, because rewriting
    somebody's finished email into a shape it was never authored in is a good
    way to change what it says.
    """
    if layouts.get(layout_key) is None:
        return "", "", (form.get("body") or "")
    values = layouts.collect(layout_key, form)
    # Resolved here, at save time, and baked into the stored HTML. The send loop
    # must never depend on the webshop being reachable, and the price that goes
    # out should be the one that was on screen when it was approved.
    products = [p for _u, p in shop.lookup_all(layouts.product_urls(values)) if p]
    return layout_key, json.dumps(values, ensure_ascii=False), layouts.render(
        layout_key, values, products)


def _values_of(camp):
    """The words that were typed, as a dict. Falls back to the layout defaults.

    Stored content that will not parse is treated as absent rather than fatal:
    the composer opening with default text beats the page refusing to open.
    """
    if camp is None:
        return {}
    try:
        stored = json.loads(camp["content"] or "{}")
    except (ValueError, TypeError):
        stored = {}
    values = layouts.defaults(camp["layout"])
    if isinstance(stored, dict):
        values.update({k: v for k, v in stored.items() if isinstance(v, str)})
    return values


def _product_report(values, camp=None):
    """What each pasted product link resolved to, and whether prices have moved.

    Two things the composer has to say out loud. A link that cannot be read just
    produces no row in the preview, which leaves somebody hunting for why. And
    the price is baked into the stored email at save time, so if the webshop has
    changed it since, the campaign would send the old one without a word.
    """
    report, stale = {}, False
    body = (camp["body"] if camp else "") or ""
    for key in layouts.PRODUCT_KEYS:
        url = ((values or {}).get(key) or "").strip()
        if not url:
            continue
        found = shop.lookup(url)
        if found is None:
            report[key] = {"ok": False,
                           "text": "Could not read this one. Check it is a "
                                   "product link from %s." % config.SHOP_NAME}
            continue
        report[key] = {"ok": True,
                       "text": "%s  ·  %s" % (found["title"], found["price"])}
        if body and found["price"] and found["price"] not in body:
            stale = True
    return report, stale


@app.route("/campaign/new", methods=["GET", "POST"])
@login_required
def campaign_new():
    """Pick a layout, then fill in the words.

    A blank textarea asking for HTML is where this tool lost people. The choice
    on this screen is between four finished designs, and what follows it is a
    set of labelled boxes, never markup.
    """
    if request.method == "POST":
        layout, content, body = _compose(request.form, request.form.get("layout") or "")
        cur = conn().execute(
            "INSERT INTO campaign (name, subject, preheader, body, audience,"
            " created, layout, content) VALUES (?,?,?,?,?,?,?,?)",
            ((request.form.get("name") or "Nieuwe campagne")[:150],
             (request.form.get("subject") or "")[:200],
             (request.form.get("preheader") or "")[:200],
             body,
             (request.form.get("audience") or "all")[:40], db.now(),
             layout, content))
        conn().commit()
        return redirect(url_for("campaign", cid=cur.lastrowid))

    chosen = layouts.get(request.args.get("start") or "")
    # Arrived from the Lists screen with a list already picked. Validated
    # against the real list of audiences, so a made-up value in the URL cannot
    # aim a campaign at something that does not exist.
    picked = (request.args.get("audience") or "").strip()
    if picked not in dict(sender.audiences(conn())):
        picked = ""
    return render_template("campaign.html", camp=None, picked=picked,
                           audiences=sender.audiences(conn()),
                           stats=db.stats(conn()), layout_choices=_all_layouts(),
                           snippets=blocks.SNIPPETS, layout=chosen,
                           values=layouts.defaults(chosen["key"]) if chosen else {},
                           products={}, products_stale=False)


@app.route("/campaign/<int:cid>", methods=["GET", "POST"])
@login_required
def campaign(cid):
    camp = conn().execute("SELECT * FROM campaign WHERE id = ?", (cid,)).fetchone()
    if camp is None:
        abort(404)
    editable = camp["status"] in (db.DRAFT, db.PAUSED)

    if request.method == "POST" and editable:
        # The layout is taken from the stored campaign, never from the form. It
        # is decided when the campaign is created, and letting a posted field
        # change it would reinterpret the words in a shape they were not
        # written for.
        layout, content, body = _compose(request.form, camp["layout"])
        conn().execute(
            "UPDATE campaign SET name = ?, subject = ?, preheader = ?, body = ?,"
            " audience = ?, layout = ?, content = ? WHERE id = ?",
            ((request.form.get("name") or camp["name"])[:150],
             (request.form.get("subject") or "")[:200],
             (request.form.get("preheader") or "")[:200],
             body,
             (request.form.get("audience") or "all")[:40], layout, content, cid))
        conn().commit()
        return redirect(url_for("campaign", cid=cid))

    recent = conn().execute(
        "SELECT * FROM send WHERE campaign_id = ? ORDER BY sent_at DESC, id DESC LIMIT 25",
        (cid,)).fetchall()
    values = _values_of(camp)
    products, products_stale = _product_report(values, camp)
    return render_template(
        "campaign.html", camp=camp, editable=editable, picked="",
        audiences=sender.audiences(conn()),
        counts=db.campaign_counts(conn(), cid),
        reach=sender.audience_count(conn(), camp["audience"]),
        blocked=sender.blocked_reason(conn(), camp),
        stats=db.stats(conn()), recent=recent, snippets=blocks.SNIPPETS,
        layout_choices=_all_layouts(), layout=layouts.get(camp["layout"]),
        values=values,
        products=products, products_stale=products_stale,
        msg=session.pop("msg", ""))


@app.route("/campaign/<int:cid>/preview")
@login_required
def campaign_preview(cid):
    camp = conn().execute("SELECT * FROM campaign WHERE id = ?", (cid,)).fetchone()
    if camp is None:
        abort(404)
    html, _text = sender.render(camp, "Sam", "voorbeeld")
    return html


@app.route("/campaign/<int:cid>/test", methods=["POST"])
@login_required
def campaign_test(cid):
    """One copy to a chosen address. The only send that ignores SENDING_ENABLED,
    because seeing it in a real inbox is how a broken link gets caught, and
    requiring the live switch for that would mean the first proper look at a
    newsletter is the moment everybody else gets it too."""
    to = (request.form.get("to") or "").strip()
    if "@" not in to:
        session["msg"] = "That is not a valid test address."
        return redirect(url_for("campaign", cid=cid))
    if not config.SMTP_HOST:
        session["msg"] = "No SMTP configured, so no test mail either."
        return redirect(url_for("campaign", cid=cid))

    # A test must leave no trace. The old version ran upsert_subscriber with
    # consent=YES, so sending yourself a preview quietly signed you up, created a
    # counted send row, and your own open went into the campaign's open rate.
    # Nothing here is written to the database: the message is built from a
    # throwaway token and handed straight to SMTP.
    camp = conn().execute("SELECT * FROM campaign WHERE id = ?", (cid,)).fetchone()
    if camp is None:
        abort(404)
    if not config.recipient_allowed(to):
        session["msg"] = ("%s is not on ALLOWED_RECIPIENTS, so nothing was sent. "
                          "Allowed right now: %s"
                          % (to, ", ".join(config.ALLOWED_RECIPIENTS)))
        return redirect(url_for("campaign", cid=cid))
    tok = "test-" + db.token()
    html, text = sender.render(camp, "Sam", tok)
    msg = sender.build_message(camp, to, html, text, tok)
    try:
        server = sender._smtp()
        try:
            server.send_message(msg)
        finally:
            try:
                server.quit()
            except Exception:
                pass
        session["msg"] = "Test sent to %s. Nothing was added to your list." % to
    except Exception as e:
        session["msg"] = "Test failed: %s" % str(e)[:160]
    return redirect(url_for("campaign", cid=cid))


@app.route("/campaign/<int:cid>/queue", methods=["POST"])
@login_required
def campaign_queue(cid):
    camp = conn().execute("SELECT * FROM campaign WHERE id = ?", (cid,)).fetchone()
    # A batch size turns this into a warm-up. Press it for 25 today and 50
    # tomorrow and it takes the NEXT 50, newest buyers first, because anyone
    # already queued is skipped. A domain with no sending history that opens at
    # full volume gets throttled, and the people you reach first are what teach
    # Gmail whether your mail is wanted.
    raw = (request.form.get("batch") or "").strip()
    try:
        limit = int(raw) if raw else None
    except ValueError:
        limit = None
    if limit is not None and limit < 1:
        limit = None
    made = sender.queue(conn(), cid, camp["audience"], limit=limit)
    total = sender.audience_count(conn(), camp["audience"])
    queued = conn().execute(
        "SELECT COUNT(*) FROM send WHERE campaign_id = ?", (cid,)).fetchone()[0]
    session["msg"] = ("%s added. %s of %s in this audience are now queued."
                      % (made, queued, total))
    return redirect(url_for("campaign", cid=cid))


@app.route("/campaign/<int:cid>/send", methods=["POST"])
@login_required
def campaign_send(cid):
    camp = conn().execute("SELECT * FROM campaign WHERE id = ?", (cid,)).fetchone()
    if camp["status"] in (db.DRAFT, db.PAUSED):
        n = conn().execute("SELECT COUNT(*) FROM send WHERE campaign_id = ?", (cid,)).fetchone()[0]
        if not n:
            sender.queue(conn(), cid, camp["audience"])
        conn().execute(
            "UPDATE campaign SET status = ?, started = COALESCE(started, ?) WHERE id = ?",
            (db.SENDING, db.now(), cid))
        conn().commit()
    result = sender.send_batch(conn(), cid)
    session["msg"] = ("%s sent, %s failed%s." % (
        result["sent"], result["failed"],
        (", stopped: " + result["stopped"]) if result["stopped"] else ""))
    return redirect(url_for("campaign", cid=cid))


@app.route("/campaign/<int:cid>/pause", methods=["POST"])
@login_required
def campaign_pause(cid):
    conn().execute("UPDATE campaign SET status = ? WHERE id = ? AND status = ?",
                   (db.PAUSED, cid, db.SENDING))
    conn().commit()
    session["msg"] = "Campaign paused."
    return redirect(url_for("campaign", cid=cid))


# --- json endpoints the interface talks to ------------------------------------

def _preview_reason(audience):
    """The footer line the first person in this audience will really get.

    Every email states why it was sent, reason_for() picks which of the three
    from what is known about that person, and the preview used to pass nobody
    at all and get the newsletter line every time. A campaign aimed at past
    buyers sends the existing-customer line, so the one sentence in the email
    that has to be true was the one sentence not being previewed.
    """
    name = (audience or "").strip()
    if not name or name not in dict(sender.audiences(conn())):
        return sender.REASON_CUSTOMER
    where, params = sender.audience_sql_for(conn(), name)
    row = conn().execute(
        "SELECT * FROM subscriber WHERE " + where + sender.audience_order()
        + " LIMIT 1", params).fetchone()
    # Nobody in it yet is not a reason to show the wrong line. The customer
    # wording is the one that claims least.
    return sender.reason_for(row) if row is not None else sender.REASON_CUSTOMER


@app.route("/api/preview", methods=["POST"])
@login_required
def api_preview():
    """Render what the message will look like, for the live pane in the composer.

    Uses a throwaway token, so the preview's links and pixel point nowhere real
    and looking at your own draft never counts as an open.
    """
    data = request.get_json(silent=True) or {}
    layout_key = data.get("layout") or ""
    # A reading aid, never a sending mode: what goes to a customer is always the
    # Dutch. Only text we shipped is translated, so an edited box stays visibly
    # Dutch rather than quietly wrong.
    as_english = bool(data.get("english"))
    preheader = data.get("preheader") or ""
    subject = data.get("subject") or ""
    if layouts.get(layout_key) is not None:
        # Rendered by the same function that renders the real thing, so what is
        # on screen cannot drift from what would be sent.
        raw = data.get("values")
        values = ({k: v for k, v in raw.items() if isinstance(v, str)}
                  if isinstance(raw, dict) else {})
        if as_english:
            values = layouts.to_english(values)
        products = [p for _u, p in shop.lookup_all(layouts.product_urls(values)) if p]
        body = layouts.render(layout_key, values, products)
    else:
        body = data.get("body") or ""
    if as_english:
        preheader = layouts.english(preheader) or preheader
        subject = layouts.english(subject) or subject
    fake = {"body": body, "preheader": preheader, "subject": ""}
    html, _text = sender.render(fake, "Sam", "voorbeeld",
                                reason=_preview_reason(data.get("audience")))
    if as_english:
        html = layouts.english_html(html)
    # A header rather than a JSON envelope: the pane wants HTML and has wanted
    # HTML since it was written, and changing that shape to carry one extra
    # string would mean rewriting the one piece of this that already works.
    # Percent-encoded because a header may not carry anything but latin-1, and
    # this is Dutch.
    return html, 200, {
        "Content-Type": "text/html; charset=utf-8",
        "X-Preview-Subject": quote(subject or ""),
    }


@app.route("/api/audience/<name>")
@login_required
def api_audience(name):
    """How many people this audience means, right now."""
    if name not in dict(sender.audiences(conn())):
        return {"count": 0, "error": "unknown audience"}, 400
    return {"count": sender.audience_count(conn(), name)}


@app.route("/api/campaign/<int:cid>/send", methods=["POST"])
@login_required
def api_send(cid):
    """Send one batch and report where the whole run has got to.

    The page calls this repeatedly until `done`, which is what turned "press the
    button twelve times for 300 people" into one button and a progress bar. The
    batching itself is unchanged: each call is still bounded, still resumable,
    and still stops at the daily cap.
    """
    camp = conn().execute("SELECT * FROM campaign WHERE id = ?", (cid,)).fetchone()
    if camp is None:
        return {"error": "campagne bestaat niet"}, 404

    stop = sender.blocked_reason(conn(), camp)
    if stop:
        return {"error": REASONS.get(stop, stop)}

    if camp["status"] in (db.DRAFT, db.PAUSED):
        n = conn().execute("SELECT COUNT(*) FROM send WHERE campaign_id = ?",
                           (cid,)).fetchone()[0]
        if not n:
            sender.queue(conn(), cid, camp["audience"])
        conn().execute(
            "UPDATE campaign SET status = ?, started = COALESCE(started, ?) WHERE id = ?",
            (db.SENDING, db.now(), cid))
        conn().commit()

    result = sender.send_batch(conn(), cid)
    counts = db.campaign_counts(conn(), cid)
    # The same count the sender uses. Two different definitions of "left" is
    # how a progress bar sits at 99 per cent while the run believes it is done.
    left = sender.pending_count(conn(), cid)
    return {
        "sent": result["sent"], "failed": counts["failed"],
        "sent_total": counts["sent"], "queued": counts["total"],
        "remaining": left, "done": left == 0,
        "stopped": REASONS.get(result["stopped"], result["stopped"]),
    }


@app.route("/campaign/<int:cid>/duplicate", methods=["POST"])
@login_required
def campaign_duplicate(cid):
    """Last month's newsletter as this month's starting point.

    Copies the words and the audience, never the send history: the copy starts
    as a draft with nobody queued, so duplicating can never re-mail the people
    who got the original.
    """
    src = conn().execute("SELECT * FROM campaign WHERE id = ?", (cid,)).fetchone()
    if src is None:
        abort(404)
    cur = conn().execute(
        "INSERT INTO campaign (name, subject, preheader, body, audience, created)"
        " VALUES (?,?,?,?,?,?)",
        (("Copy of " + src["name"])[:150], src["subject"], src["preheader"],
         src["body"], src["audience"], db.now()))
    conn().commit()
    session["msg"] = "Copied. This is a fresh draft with nobody queued."
    return redirect(url_for("campaign", cid=cur.lastrowid))


@app.route("/setup")
@login_required
def setup():
    """Everything that is missing, in one place, with what to do about it.

    Replaces the three warning banners that used to sit on top of every page.
    They were the same three every time, which is how a warning becomes
    wallpaper.
    """
    checks = [
        {"ok": bool(config.PASSWORD), "must": False,
         "name": "Password on this tool",
         "why": "It holds a mailing list and can send as the company.",
         "fix": "MAILER_PASSWORD=something in .env",
         "note": "Fine to skip on this laptop. Never skip it on a server."},
        {"ok": bool(config.SMTP_HOST), "must": True,
         "name": "Mail server",
         "why": "Without it nothing can be sent, not even a test.",
         "fix": "SMTP_HOST, SMTP_USER and SMTP_PASSWORD in .env",
         "note": "Use the same relay the CRM uses."},
        {"ok": bool(config.POSTAL_ADDRESS.strip()), "must": True,
         "name": "Postal address",
         "why": "Legally required in the footer of every marketing email.",
         "fix": "POSTAL_ADDRESS in .env", "note": ""},
        {"ok": config.public_url_is_reachable(), "must": True,
         "name": "Public address",
         "why": "Unsubscribe links and open tracking have to be reachable from "
                "somebody's inbox. A laptop is not.",
         "fix": "PUBLIC_URL=https://your-host in .env",
         "note": "Everything except the final send works without this."},
        {"ok": config.SENDING_ENABLED, "must": True,
         "name": "Sending switched on",
         "why": "The main switch. Off by default so a working mailbox is never "
                "by itself the thing that mails everyone.",
         "fix": "SENDING_ENABLED=true in .env",
         "note": "Turn this on last, after a test has landed."},
        {"ok": not config.ALLOWED_RECIPIENTS, "must": False,
         "name": "Recipient allowlist is off",
         "why": "While it is set, only those addresses can ever receive anything.",
         "fix": "Empty ALLOWED_RECIPIENTS in .env to mail real subscribers",
         "note": "Currently: " + (", ".join(config.ALLOWED_RECIPIENTS) or "not set")},
    ]
    return render_template("setup.html", checks=checks, stats=db.stats(conn()))


def _keep(req):
    """Carry the page and filter through a POST, so acting on row 340 does not
    dump you back at the top of the list."""
    out = {}
    for k in ("page", "per", "sort", "dir", "audience") + FILTER_KEYS:
        v = req.form.get(k) or req.args.get(k)
        if v:
            out[k] = v
    return out


# --- subscribers --------------------------------------------------------------

#: Offered in the rows-per-page control. A fixed 100 was fine for 400 people and
#: is 58 pages of clicking for 5.778.
PAGE_SIZES = (50, 100, 250, 500)


def _subscriber_filter(args):
    """The WHERE clause behind the subscribers list.

    Shared with the bulk action deliberately. If the two built their own
    queries, "unsubscribe all 249 matching this filter" could act on a different
    set of people than the one on screen, and nobody would find out until
    customers quietly stopped receiving anything.
    """
#: Everything the list can be narrowed by. Defined in db.py because a SAVED list
#: is one of these stored on disk, and the screen that builds a filter and the
#: send that resolves it back into people have to agree about what the words
#: mean. Adding a filter in one place and forgetting another is how "select all
#: matching" ends up meaning something different from what is on screen.
FILTER_KEYS = db.FILTER_KEYS

#: Sortable columns, mapped to SQL. A whitelist rather than the column name off
#: the query string: ORDER BY cannot be parameterised, so anything else here
#: would be handing the database a string from a URL.
SORTS = {
    "email": "email COLLATE NOCASE",
    "name": "name COLLATE NOCASE",
    "consent": "consent",
    "source": "source",
    "signed_up": "COALESCE(NULLIF(signed_up_at, ''), consent_at, created)",
    "spent": "spent",
    "last_mailed": "COALESCE(last_sent, '')",
}
DEFAULT_SORT = ("signed_up", "desc")


def _subscriber_filter(args):
    """The WHERE clause behind the subscribers list.

    Shared with the bulk action deliberately. If the two built their own
    queries, "unsubscribe all 249 matching this filter" could act on a different
    set of people than the one on screen, and nobody would find out until
    customers quietly stopped receiving anything.
    """
    f = {k: (args.get(k) or "").strip() for k in FILTER_KEYS}
    extra, params = db.subscriber_filter_sql(f)
    # A spend value that is not one of the offered options is not a filter, and
    # showing it as active would claim the list is narrowed when it is not.
    if f["spend"] and f["spend"] not in ("yes", "no") and not f["spend"].isdigit():
        f["spend"] = ""
    return " WHERE 1=1" + extra, params, f


def _sort_sql(args):
    """(order clause, column, direction). Falls back to newest first."""
    col = (args.get("sort") or "").strip()
    direction = "asc" if (args.get("dir") or "").strip() == "asc" else "desc"
    if col not in SORTS:
        col, direction = DEFAULT_SORT
    # id last so the order is total: without it, rows sharing a value come back
    # in whatever order SQLite feels like and pagination can repeat or skip one.
    return (" ORDER BY %s %s, id DESC" % (SORTS[col], direction.upper()),
            col, direction)


def _with_audience(where, params, args):
    """Narrow a subscriber query to one audience, if the screen asked for one.

    Applied to the LIST view and to the bulk action through the same helper,
    because "select all matching" while looking at an audience has to mean that
    audience. Two code paths would eventually mean two different sets of people.
    """
    name = (args.get("audience") or "").strip()
    if not name:
        return where, params, ""
    a_where, a_params = sender.audience_sql_for(conn(), name)
    return where + " AND (" + a_where + ")", params + a_params, name


@app.route("/flows")
@login_required
def flows_screen():
    """Every flow, one card each. The sequence is one click in."""
    ok, why = config.flows_ready()
    return render_template("flows.html", catalog=flows.catalog(conn()),
                           triggers=flows.TRIGGERS, blocked="" if ok else why,
                           stats=db.stats(conn()), msg=session.pop("msg", ""))


@app.route("/flows/new", methods=["POST"])
@login_required
def flow_new():
    """A new flow, as a draft with one empty email in it."""
    try:
        key = flows.create(conn(), request.form.get("name"),
                           request.form.get("trigger") or "",
                           request.form.get("days") or 365)
    except ValueError:
        session["msg"] = "Pick what starts the flow."
        return redirect(url_for("flows_screen"))
    session["msg"] = ("Created as a draft. Write the emails, then set it live."
                      " Nothing sends while it is a draft.")
    return redirect(url_for("flow_detail", key=key))


def _flow_or_404(key):
    d = flows.find(conn(), key)
    if d is None:
        abort(404)
    return d


@app.route("/flows/<key>")
@login_required
def flow_detail(key):
    d = _flow_or_404(key)
    ok, why = config.flows_ready()
    rows = conn().execute(
        "SELECT * FROM flow WHERE def_id = ? ORDER BY abandoned_at DESC LIMIT 100",
        (d["id"],)).fetchall()
    return render_template("flow.html", f=d, rows=rows,
                           trigger=flows.TRIGGERS.get(d["trigger"], {}),
                           no_events=flows.needs_shop_pixel(conn(), d),
                           blocked="" if ok else why,
                           s=flows.summary(conn(), d),
                           steps_rows=flows.steps(conn(), d),
                           money=attrib.by_step(conn()),
                           skipped_rows=flows.skipped(conn(), d),
                           stats=db.stats(conn()), msg=session.pop("msg", ""))


@app.route("/flows/<key>/status", methods=["POST"])
@login_required
def flow_status(key):
    """Live, paused, or back to draft.

    Pausing leaves everybody exactly where they are in the sequence and simply
    stops the clock, so switching it back on continues rather than restarts.
    """
    d = _flow_or_404(key)
    ok, why = flows.set_status(conn(), d["id"], request.form.get("status") or "")
    session["msg"] = why if not ok else {
        flows.LIVE: "Live. It will send at the next run, and only what is due.",
        flows.PAUSED: "Paused. Nobody moves on until you set it live again.",
        flows.DRAFT: "Back to a draft.",
    }.get(request.form.get("status"), "")
    return redirect(url_for("flow_detail", key=key))


@app.route("/flows/<key>/rename", methods=["POST"])
@login_required
def flow_rename(key):
    d = _flow_or_404(key)
    flows.rename(conn(), d["id"], request.form.get("name"),
                 request.form.get("note"), request.form.get("days"))
    return redirect(url_for("flow_detail", key=key))


@app.route("/flows/<key>/step/add", methods=["POST"])
@login_required
def flow_step_add(key):
    d = _flow_or_404(key)
    pos = flows.add_step(conn(), d["id"])
    return redirect(url_for("flow_step_edit", key=key, pos=pos))


@app.route("/flows/<key>/step/<int:pos>/delete", methods=["POST"])
@login_required
def flow_step_delete(key, pos):
    d = _flow_or_404(key)
    flows.drop_step(conn(), d["id"], pos)
    session["msg"] = "Email removed."
    return redirect(url_for("flow_detail", key=key))


@app.route("/flows/<key>/step/<int:pos>", methods=["GET", "POST"])
@login_required
def flow_step_edit(key, pos):
    """Write one email of a sequence.

    The same shape as the campaign composer on purpose: the words on the left,
    the real email on the right. Learning two editors to write one shop's email
    is one editor too many.
    """
    d = _flow_or_404(key)
    step = conn().execute("SELECT * FROM flow_step WHERE def_id = ? AND pos = ?",
                          (d["id"], pos)).fetchone()
    if step is None:
        abort(404)
    if request.method == "POST":
        flows.save_step(conn(), d["id"], pos, request.form)
        session["msg"] = "Saved."
        return redirect(url_for("flow_detail", key=key) + "#email%s" % (pos + 1))
    return render_template("flow_step.html", f=d, step=step, pos=pos,
                           n=len(flows.steps_of(conn(), d["id"])),
                           stats=db.stats(conn()), msg=session.pop("msg", ""))


@app.route("/flows/<key>/sent")
@login_required
def flow_sent(key):
    """Every message this flow has sent, and to whom."""
    d = _flow_or_404(key)
    return render_template("flow_sent.html", f=d,
                           rows=flows.sent_messages(conn(), d),
                           stats=db.stats(conn()), msg=session.pop("msg", ""))


@app.route("/flows/<key>/preview/<int:step>")
@login_required
def flows_preview(key, step):
    """See an email before anybody receives it.

    `?english=1` shows the English reading of the text we shipped, for the same
    reason the composer has it: approving Dutch you cannot read is not
    approving anything.
    """
    d = _flow_or_404(key)
    _subject, html = flows.preview(conn(), d, step - 1,
                                   english=bool(request.args.get("english")))
    if html is None:
        abort(404)
    return html


@app.route("/flows/<key>/message/<int:sid>")
@login_required
def flow_message(key, sid):
    """One sent message, as that person received it."""
    d = _flow_or_404(key)
    row = conn().execute(
        "SELECT fs.*, f.def_id FROM flow_send fs JOIN flow f ON f.id = fs.flow_id"
        " WHERE fs.id = ? AND f.def_id = ?", (sid, d["id"])).fetchone()
    if row is None:
        abort(404)
    _subject, html = flows.preview(conn(), d, row["step"])
    return html


@app.route("/flows/<key>/poll", methods=["POST"])
@login_required
def flows_poll(key):
    """Look for people the trigger applies to. Records only, never sends."""
    _flow_or_404(key)
    try:
        out = flows.poll(conn())
    except Exception as exc:                           # noqa: BLE001
        session["msg"] = "Could not check: %s" % str(exc)[:140]
        return redirect(url_for("flow_detail", key=key))
    if out.get("skipped"):
        session["msg"] = "Nothing checked: %s." % out["skipped"]
    else:
        session["msg"] = (
            "%s looked at, %s new. %s were before the start date and can never "
            "be mailed, %s are already in a flow."
            % (out.get("seen", 0), out.get("added", 0), out.get("too_old", 0),
               out.get("already_running", 0)))
    return redirect(url_for("flow_detail", key=key))


@app.route("/flows/<key>/run", methods=["POST"])
@login_required
def flows_run(key):
    """Send whatever is due. Every guard still applies."""
    _flow_or_404(key)
    out = flows.run(conn())
    if out.get("blocked"):
        session["msg"] = "Nothing sent. %s" % out["blocked"]
    else:
        session["msg"] = (
            "%s emails sent, %s stopped (ordered or unsubscribed), %s skipped."
            % (out["sent"], out["stopped"], out["skipped"])
            + (" Daily cap reached." if out.get("capped") else ""))
    return redirect(url_for("flow_detail", key=key))


# --- templates ----------------------------------------------------------------

@app.route("/templates")
@login_required
def templates_screen():
    """Her own designs, next to the ten that came with the tool."""
    return render_template("templates.html", own=db.templates(conn()),
                           built_in=layouts.LAYOUTS, stats=db.stats(conn()),
                           msg=session.pop("msg", ""))


@app.route("/templates/new", methods=["POST"])
@login_required
def template_new():
    """Start from a built-in layout, one of her own, or from nothing.

    Starting from something is the normal case: a template is nearly always
    "that one, but with a quote and without the picture", and a blank page is
    where people give up.
    """
    start = (request.form.get("start") or "").strip()
    name = (request.form.get("name") or "").strip()
    src = layouts.get(start)
    if src is not None:
        blocks = list(src.get("blocks") or layouts.blocks_of(start))
        content = layouts.defaults(start)
        subject, preheader = src["subject"], src["preheader"]
        name = name or ("%s (kopie)" % src["name"])
    else:
        blocks = ["hero", "text", "button", "usp_line"]
        content = {}
        subject = preheader = ""
        name = name or "Nieuw sjabloon"
    tid = db.create_template(conn(), name, blocks, content, subject, preheader)
    session["msg"] = "Made. Change the blocks and the words, then save."
    return redirect(url_for("template_edit", tid=tid))


def _template_or_404(tid):
    row = db.get_template(conn(), tid)
    if row is None:
        abort(404)
    return row


@app.route("/templates/<int:tid>", methods=["GET", "POST"])
@login_required
def template_edit(tid):
    """The words on the left, the real email on the right.

    Same shape as the composer on purpose, down to the element ids, so the
    same script drives the live preview. The block order is saved on every
    block change and the words are saved on Save; the preview reads the saved
    order and the live words, which is why moving a block re-renders at once.
    """
    row = _template_or_404(tid)
    key = layouts.CUSTOM_PREFIX + str(tid)
    spec = layouts.get(key)
    if request.method == "POST":
        _save_template(row, request.form)
        session["msg"] = "Saved."
        return redirect(url_for("template_edit", tid=tid))
    values = {f[0]: f[4] for f in spec["fields"]}
    return render_template("template_edit.html", t=row, spec=spec, values=values,
                           blocks=[(k, layouts.BLOCKS[k]) for k in spec["blocks"]
                                   if k in layouts.BLOCKS],
                           palette=[(k, layouts.BLOCKS[k]) for k in layouts.BLOCK_ORDER],
                           stats=db.stats(conn()), msg=session.pop("msg", ""))


def _blocks(row):
    try:
        return [b for b in json.loads(row["blocks"] or "[]") if b in layouts.BLOCKS]
    except ValueError:
        return []


def _content(row):
    try:
        return dict(json.loads(row["content"] or "{}"))
    except ValueError:
        return {}


def _words(row, form):
    """What is in the boxes right now, on top of what was already stored.

    Merged rather than replaced so that removing a block does not throw away
    what was written in it: put the block back and the words return. The cost
    is a few unused keys in a JSON column, which nothing reads.
    """
    key = layouts.CUSTOM_PREFIX + str(row["id"])
    spec = layouts.get(key) or {}
    # Only boxes that were actually submitted. layouts.collect() answers "" for
    # a field that is not in the form, which is right for the composer, where
    # every box is always on screen, and wrong here: one request that arrives
    # without the boxes would empty the whole template.
    typed = {f[0]: form.get("f_" + f[0]) for f in spec.get("fields", [])
             if ("f_" + f[0]) in form}
    return {**_content(row), **typed}


def _save_template(row, form, **extra):
    """Everything on the screen, every time, whichever button was pressed.

    All four buttons submit the same form, so all four save the same things.
    Otherwise moving a block would quietly put back the subject you had just
    rewritten, and you would blame the tool without ever knowing why.
    """
    def field(name, limit):
        value = form.get(name)
        return (row[name] if value is None else value)[:limit]

    db.update_template(conn(), row["id"], name=field("name", 80) or row["name"],
                       subject=field("subject", 200),
                       preheader=field("preheader", 200),
                       blurb=field("blurb", 200),
                       content=_words(row, form), **extra)


@app.route("/templates/<int:tid>/block/add", methods=["POST"])
@login_required
def template_block_add(tid):
    row = _template_or_404(tid)
    key = (request.form.get("block") or "").strip()
    if key not in layouts.BLOCKS:
        abort(400)
    blocks = _blocks(row) + [key]
    _save_template(row, request.form, blocks=blocks)
    return redirect(url_for("template_edit", tid=tid) + "#block%s" % (len(blocks) - 1))


@app.route("/templates/<int:tid>/block/<int:pos>/<action>", methods=["POST"])
@login_required
def template_block_move(tid, pos, action):
    """Up, down, or gone. Server-side on purpose: no drag library to break."""
    row = _template_or_404(tid)
    blocks = _blocks(row)
    if not 0 <= pos < len(blocks):
        abort(404)
    if action == "remove":
        blocks.pop(pos)
    elif action == "up" and pos > 0:
        blocks[pos - 1], blocks[pos] = blocks[pos], blocks[pos - 1]
    elif action == "down" and pos < len(blocks) - 1:
        blocks[pos + 1], blocks[pos] = blocks[pos], blocks[pos + 1]
    else:
        abort(400)
    _save_template(row, request.form, blocks=blocks)
    return redirect(url_for("template_edit", tid=tid))


@app.route("/templates/<int:tid>/delete", methods=["POST"])
@login_required
def template_delete(tid):
    """Campaigns already written with it keep their rendered body, because the
    HTML was baked in when they were saved. Only the starting point goes."""
    _template_or_404(tid)
    db.delete_template(conn(), tid)
    session["msg"] = "Template removed. Campaigns already made with it are unchanged."
    return redirect(url_for("templates_screen"))


@app.route("/results")
@login_required
def results():
    """Who bought after being emailed. The question every other screen leads to.

    Deliberately its own page rather than a number on the campaign list: the
    honest version of this needs a paragraph of context about what a match is
    and is not, and that does not fit in a table cell.
    """
    camps = conn().execute(
        "SELECT c.id, c.name,"
        " (SELECT COUNT(*) FROM send s WHERE s.campaign_id = c.id AND s.sent = 1) AS n_sent,"
        " (SELECT COUNT(*) FROM send s WHERE s.campaign_id = c.id AND s.opened IS NOT NULL) AS n_open,"
        " (SELECT COUNT(*) FROM send s WHERE s.campaign_id = c.id AND s.clicked IS NOT NULL) AS n_click,"
        " (SELECT COUNT(*) FROM attribution a WHERE a.campaign_id = c.id) AS orders,"
        " (SELECT COALESCE(SUM(a.total), 0) FROM attribution a WHERE a.campaign_id = c.id) AS value"
        " FROM campaign c WHERE EXISTS (SELECT 1 FROM send s WHERE s.campaign_id = c.id)"
        " ORDER BY c.created DESC").fetchall()
    return render_template("results.html", a=attrib.summary(conn()),
                           rows=attrib.recent(conn()), camps=camps,
                           names={c["id"]: c["name"] for c in camps},
                           stats=db.stats(conn()), msg=session.pop("msg", ""))


@app.route("/results/match", methods=["POST"])
@login_required
def results_match():
    """Look for orders that followed an email. Reads two systems, writes to
    neither of them."""
    try:
        out = attrib.match(conn())
    except Exception as exc:                           # noqa: BLE001
        session["msg"] = "Could not check for orders: %s" % str(exc)[:140]
        return redirect(url_for("results"))
    if out.get("note"):
        session["msg"] = out["note"]
    else:
        session["msg"] = ("%s new orders matched, EUR %.0f. (%s from the webshop,"
                          " %s from the showroom.)"
                          % (out["webshop"] + out["showroom"], out["value"],
                             out["webshop"], out["showroom"]))
    if out.get("unreachable"):
        session["msg"] += (" Could not read: %s, so anything sold there is not"
                           " counted yet." % ", ".join(out["unreachable"]))
    return redirect(url_for("results"))


@app.route("/lists")
@login_required
def lists():
    """Every audience in one place, with who is in it a click away.

    The audiences used to exist only inside a dropdown on a campaign, which
    meant the only way to find out who was in one was to aim a campaign at it.
    """
    # What has already gone to each list. One query for all of them rather than
    # one per row: with 21 numbered lists that is the difference between one
    # query and twenty-one.
    history = {}
    for camp in conn().execute(
            "SELECT c.id, c.name, c.audience, c.status, c.started, c.created,"
            " (SELECT COUNT(*) FROM send s WHERE s.campaign_id = c.id AND s.sent = 1)"
            " AS sent FROM campaign c"
            " ORDER BY COALESCE(c.started, c.created) DESC").fetchall():
        # Only what actually went out. A draft aimed at a list has not been sent
        # to it, and listing it under "already sent to it" would be a lie that
        # matters: the whole point of this column is knowing what a list has
        # already received before you send it something else.
        if camp["sent"]:
            history.setdefault(camp["audience"], []).append(camp)

    rows = []
    for key, label in sender.audiences(conn()):
        went = history.get(key, [])
        rows.append({
            "key": key,
            "name": label,
            "note": sender.AUDIENCE_NOTES.get(key, ""),
            "count": sender.audience_count(conn(), key),
            "saved": key.startswith("list:"),
            "id": key.split(":", 1)[1] if key.startswith("list:") else "",
            "campaigns": went,
            "last": went[0] if went else None,
        })
    unnumbered = conn().execute(
        "SELECT COUNT(*) FROM subscriber WHERE consent = ? AND bounced = 0"
        " AND email <> '' AND batch = 0", (db.YES,)).fetchone()[0]
    return render_template("lists.html", rows=rows, stats=db.stats(conn()),
                           unnumbered=unnumbered, batch_size=db.BATCH_SIZE,
                           msg=session.pop("msg", ""))


@app.route("/subscribers")
@login_required
def subscribers():
    where, params, filters = _subscriber_filter(request.args)
    where, params, audience = _with_audience(where, params, request.args)
    sql = "SELECT * FROM subscriber" + where
    total = conn().execute("SELECT COUNT(*) FROM subscriber" + where,
                           params).fetchone()[0]
    try:
        per = int(request.args.get("per") or 100)
    except ValueError:
        per = 100
    if per not in PAGE_SIZES:
        per = 100
    try:
        page = max(1, int(request.args.get("page") or 1))
    except ValueError:
        page = 1
    pages = max(1, (total + per - 1) // per)
    page = min(page, pages)
    order, sort_col, sort_dir = _sort_sql(request.args)
    rows = conn().execute(sql + order + " LIMIT ? OFFSET ?",
                          params + [per, (page - 1) * per]).fetchall()
    # The values actually present, so the column filters offer real choices
    # rather than a fixed list that goes stale as imports add new sources.
    sources = [r[0] for r in conn().execute(
        "SELECT DISTINCT source FROM subscriber WHERE source <> '' ORDER BY 1")]
    years = [r[0] for r in conn().execute(
        "SELECT DISTINCT substr(COALESCE(NULLIF(signed_up_at,''), consent_at,"
        " created), 1, 4) y FROM subscriber WHERE y <> '' ORDER BY 1 DESC")]
    return render_template("subscribers.html", subs=rows, count=total,
                           stats=db.stats(conn()), f=filters,
                           sort=sort_col, dir=sort_dir,
                           sources=sources, years=years,
                           audience=audience,
                           audience_name=dict(sender.audiences(conn())).get(
                               audience, ""),
                           page=page, pages=pages, per=per,
                           page_sizes=PAGE_SIZES,
                           first=(0 if not total else (page - 1) * per + 1),
                           last=min(page * per, total),
                           msg=session.pop("msg", ""))


#: What a bulk action is allowed to do.
#:
#: `unsubscribe` and `revoke` only ever REDUCE who can be mailed, which is why
#: they are safe in bulk. `grant` is the other direction and was added on the
#: owner's explicit instruction (2026-09-01) after being told the concern: one
#: click on a selection is one claim about all of those people, and if a
#: specific address is ever challenged the answer has to be per person.
#:
#: The two things that keep it defensible are enforced below and neither may be
#: removed for convenience:
#:   1. it refuses without `they_opted_in=yes`, the same confirmation the
#:      single-row button demands, so nothing grants consent by accident;
#:   2. it can never touch somebody who unsubscribed. Granting is not a route
#:      around an unsubscribe, in bulk or otherwise.
#:
#: `restore` exists so the bulk unsubscribe is genuinely reversible rather than
#: only theoretically: "you can undo it row by row" is true for five people and
#: hollow for a hundred. It **only ever reverses unsubscribes made from this
#: screen**. Somebody who clicked the link in their own email stays put, because
#: in bulk you cannot honestly claim that seventy separate people each asked to
#: come back. That one stays a single-row decision with its own confirmation.
BULK_ACTIONS = ("unsubscribe", "revoke", "grant", "restore")


@app.route("/subscribers/bulk", methods=["POST"])
@login_required
def subscribers_bulk():
    """Unsubscribe or de-consent a selection, or everything matching a filter."""
    action = request.form.get("action") or ""
    if action not in BULK_ACTIONS:
        abort(400)
    # Same confirmation the single-row button demands. Without it this route
    # would be the one way to gain consent without stating that it was given.
    if action == "grant" and request.form.get("they_opted_in") != "yes":
        abort(400)

    if request.form.get("all_matching") == "1":
        # Re-run the list's own query rather than trusting a posted list of ids,
        # so what is acted on is what the filter says, whatever the browser sent.
        where, params, _filters = _subscriber_filter(request.form)
        where, params, _aud = _with_audience(where, params, request.form)
        ids = [r["id"] for r in
               conn().execute("SELECT id FROM subscriber" + where, params)]
    else:
        ids = [int(x) for x in request.form.getlist("ids") if x.isdigit()]

    # Where a grant came from, recorded on the row. Consent given because
    # somebody bought from the shop rests on a different footing from consent
    # somebody typed in, and in a year nobody will remember which was which
    # unless it is written down now.
    spend_filter = request.form.get("spend") or ""
    grant_source = ("customer"
                    if spend_filter == "yes" or spend_filter.isdigit()
                    else "manual")

    changed = already = protected = 0
    for sid in ids:
        row = conn().execute("SELECT * FROM subscriber WHERE id = ?",
                             (sid,)).fetchone()
        if row is None:
            continue
        if action == "unsubscribe":
            if row["consent"] == db.NO:
                already += 1
                continue
            # by=admin, so a slip on a hundred rows can still be undone one by
            # one. Recording these as "they asked" would be a lie and would make
            # them permanent.
            db.unsubscribe(conn(), sid, by=db.BY_ADMIN)
        elif action == "revoke":
            if row["consent"] != db.YES:
                already += 1
                continue
            db.revoke_consent(conn(), sid)
        elif action == "grant":
            if row["consent"] == db.NO:
                # The single choke point. Somebody who asked to leave is not
                # brought back by a selection that happened to include them.
                protected += 1
                continue
            if row["consent"] == db.YES:
                already += 1
                continue
            db.grant_consent(conn(), sid, source=grant_source)
        else:  # restore
            if row["consent"] != db.NO:
                already += 1
                continue
            if row["unsubscribed_by"] == db.BY_SELF:
                protected += 1
                continue
            # source=None: this corrects a record rather than being a new
            # signup, so where they originally came from is left alone.
            db.restore_consent(conn(), sid, source=None)
        changed += 1
    conn().commit()

    word = {"unsubscribe": "unsubscribed",
            "revoke": "had consent removed",
            "grant": "marked as subscribed",
            "restore": "put back on the list"}[action]
    # Said out loud rather than folded into "already": these are people whose
    # own decision was left standing, and that number should be visible.
    why = {"grant": "left untouched because they unsubscribed",
           "restore": "left alone because they unsubscribed themselves"}
    msg = "%s %s." % (changed, word)
    if already:
        msg += " %s already were." % already
    if protected:
        msg += " %s %s." % (protected, why[action])
    session["msg"] = msg
    return redirect(url_for("subscribers", **_keep(request)))


@app.route("/lists/build", methods=["POST"])
@login_required
def build_lists():
    """Cut everyone who may be mailed into numbered lists of 275.

    Under a 300 a day cap that is one list a day with room to spare, and the
    numbering is what lets you say afterwards exactly who was in which one.
    """
    try:
        size = int(request.form.get("size") or db.BATCH_SIZE)
    except ValueError:
        size = db.BATCH_SIZE
    size = max(1, min(size, 2000))
    result = db.build_batches(conn(), size=size)
    session["msg"] = (
        "%s people numbered. You now have %s lists of up to %s."
        % (result["assigned"], result["lists"], size))
    return redirect(url_for("lists"))


@app.route("/lists/rebuild", methods=["POST"])
@login_required
def rebuild_lists():
    """Throw the numbering away and start again.

    Only sensible before anything has been sent: renumbering afterwards makes
    every record of who received what misleading, so it says so and refuses
    quietly rather than pretending it is harmless.
    """
    sent = conn().execute("SELECT COUNT(*) FROM send WHERE sent = 1").fetchone()[0]
    if sent:
        session["msg"] = ("Not renumbering: %s emails have already gone out and "
                          "the numbers are the record of who received them." % sent)
        return redirect(url_for("lists"))
    db.clear_batches(conn())
    result = db.build_batches(conn())
    session["msg"] = "Renumbered into %s lists." % result["lists"]
    return redirect(url_for("lists"))


@app.route("/subscribers/list/save", methods=["POST"])
@login_required
def save_list():
    """Turn the filter now on screen into a named list.

    Stores the FILTER, not the people. A frozen set of ids would keep mailing
    somebody who unsubscribed the day after it was saved; a stored filter is
    re-evaluated every send, so the list looks after itself.
    """
    name = (request.form.get("name") or "").strip()
    if not name:
        session["msg"] = "Give the list a name."
        return redirect(url_for("subscribers", **_keep(request)))
    _where, _params, filters = _subscriber_filter(request.form)
    audience = (request.form.get("audience") or "").strip()
    if audience.startswith("list:"):
        session["msg"] = ("A list cannot be built on another saved list. "
                          "Pick a built-in audience, or clear it.")
        return redirect(url_for("subscribers", **_keep(request)))
    if audience and audience not in dict(sender.audiences(conn())):
        audience = ""
    if not any(filters.values()) and not audience:
        session["msg"] = ("That would save everybody, which is what "
                          '"Everyone who opted in" already is. Narrow it first.')
        return redirect(url_for("subscribers", **_keep(request)))
    db.save_segment(conn(), name, filters, audience)
    session["msg"] = ('Saved as "%s". It is now in the Audience list when you '
                      "write a campaign." % name)
    return redirect(url_for("subscribers", **_keep(request)))


@app.route("/subscribers/list/<int:sid>/delete", methods=["POST"])
@login_required
def delete_list(sid):
    seg = db.segment(conn(), sid)
    db.delete_segment(conn(), sid)
    session["msg"] = ('Deleted the list "%s". Nobody was removed from your '
                      "subscribers." % (seg["name"] if seg else sid))
    return redirect(url_for("subscribers", **_keep(request)))


@app.route("/subscribers/import/crm", methods=["POST"])
@login_required
def import_crm():
    """Bring the CRM's customers in, then their spend in the same press.

    Only the CRM's own newsletter flag becomes consent. Everybody else is
    stored and unmailable, exactly as with the Shopify import.
    """
    if not crm.configured():
        session["msg"] = "No CRM connection configured."
        return redirect(url_for("subscribers", **_keep(request)))
    try:
        tally = crm.import_customers(conn())
        spend = crm.sync(conn())
    except Exception as exc:                       # noqa: BLE001
        session["msg"] = "Could not reach the CRM: %s" % str(exc)[:140]
        return redirect(url_for("subscribers", **_keep(request)))
    session["msg"] = (
        "%s customers read from the CRM. %s added, %s of them had ticked the "
        "newsletter box and can be mailed; the rest are stored but cannot. "
        "%s now have a spend history."
        % (tally["seen"], tally["created"], tally["with_consent"],
           spend["matched"]))
    return redirect(url_for("subscribers", **_keep(request)))


@app.route("/subscribers/sync/crm", methods=["POST"])
@login_required
def sync_crm():
    """Refresh what each subscriber has spent, from the CRM.

    Read only, through a Postgres role that has SELECT on two tables and can
    write nothing. Run on demand rather than on a timer, because a figure that
    is a few days old is fine and a background job nobody asked for is not.
    """
    if not crm.configured():
        session["msg"] = ("No CRM connection configured. Set CRM_DSN and "
                          "restart to switch this on.")
        return redirect(url_for("subscribers", **_keep(request)))
    try:
        result = crm.sync(conn())
    except Exception as exc:                       # noqa: BLE001
        # Never take the screen down because another system is unreachable.
        session["msg"] = "Could not reach the CRM: %s" % str(exc)[:140]
        return redirect(url_for("subscribers", **_keep(request)))
    session["msg"] = ("Spend updated. %s of your subscribers have bought from "
                      "%s before." % (result["matched"], config.SHOP_NAME))
    return redirect(url_for("subscribers", **_keep(request)))


@app.route("/subscribers/export.csv")
@login_required
def subscribers_export():
    """The whole list as a CSV, so it is never trapped in this tool."""
    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["email", "name", "consent", "source", "consent_at",
                "unsubscribed_at", "bounced", "last_sent"])
    for r in conn().execute(
            "SELECT email, name, consent, source, consent_at, unsubscribed_at,"
            " bounced, last_sent FROM subscriber ORDER BY created DESC"):
        w.writerow([r[k] for k in r.keys()])
    return buf.getvalue(), 200, {
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": "attachment; filename=subscribers.csv",
    }


@app.route("/subscribers/import/shopify", methods=["POST"])
@login_required
def import_shopify():
    try:
        t = sources.import_shopify(conn())
        session["msg"] = ("Shopify: %s seen, %s new, %s updated, "
                          "%s stayed unsubscribed." % (t["seen"], t["created"],
                                                       t["updated"], t["kept_unsubscribed"]))
    except sources.SourceError as e:
        session["msg"] = "Shopify: %s" % e
    return redirect(url_for("subscribers"))


@app.route("/subscribers/import/csv", methods=["POST"])
@login_required
def import_csv():
    text = request.form.get("csv") or ""
    upload = request.files.get("file")
    if upload and upload.filename:
        text = upload.read().decode("utf-8", "replace")
    assume = request.form.get("assume_consent") == "on"
    t = sources.import_csv(conn(), text, assume_consent=assume)
    session["msg"] = ("CSV: %s rows, %s new, %s updated, %s skipped%s."
                      % (t["seen"], t["created"], t["updated"], t["skipped"],
                         "" if assume else " (stored without consent)"))
    return redirect(url_for("subscribers"))


@app.route("/subscribers/<int:sid>/unsubscribe", methods=["POST"])
@login_required
def subscriber_unsubscribe(sid):
    """Unsubscribe somebody from this screen. Recorded as an admin action, which
    is what makes it undoable if it was a slip."""
    db.unsubscribe(conn(), sid, by=db.BY_ADMIN)
    conn().commit()
    session["msg"] = "Unsubscribed. You can undo this from the list if it was a mistake."
    return redirect(url_for("subscribers", **_keep(request)))


@app.route("/subscribers/<int:sid>/revoke", methods=["POST"])
@login_required
def subscriber_revoke(sid):
    """Take consent away without marking them as having opted out.

    Useful when a consent was recorded by mistake - a CSV imported with the
    "they opted in" box ticked, say. Always safe: removing consent can only ever
    mean fewer people get an email.
    """
    db.revoke_consent(conn(), sid)
    conn().commit()
    session["msg"] = "Consent removed. The address is kept but will not be mailed."
    return redirect(url_for("subscribers", **_keep(request)))


@app.route("/api/subscriber/<int:sid>/<action>", methods=["POST"])
@login_required
def api_subscriber_action(sid, action):
    """Every consent correction, without leaving the page.

    Returns the row re-rendered by the same partial the list uses, so the page
    cannot end up showing a state the database does not hold. The rules live
    here, not in the browser: a page can be edited, this cannot.
    """
    row = conn().execute("SELECT * FROM subscriber WHERE id = ?", (sid,)).fetchone()
    if row is None:
        return {"ok": False, "message": "That subscriber no longer exists."}, 404

    form = request.form
    msg = ""
    if action == "revoke":
        db.revoke_consent(conn(), sid)
        msg = "Consent removed. The address is kept but will not be mailed."
    elif action == "unsubscribe":
        db.unsubscribe(conn(), sid, by=db.BY_ADMIN)
        msg = "Unsubscribed. You can undo that if it was a slip."
    elif action == "restore":
        if row["unsubscribed_by"] == db.BY_SELF and form.get("they_asked") != "yes":
            return {"ok": False,
                    "message": "%s unsubscribed themselves. Only put them back if "
                               "they asked you to." % row["email"]}, 400
        db.restore_consent(conn(), sid,
                           source="asked_to_rejoin" if form.get("they_asked") == "yes" else None)
        msg = "%s is back on the list." % row["email"]
    elif action == "grant":
        if row["consent"] == db.NO:
            return {"ok": False,
                    "message": "%s is unsubscribed. Use the option on that row "
                               "instead." % row["email"]}, 400
        if form.get("they_opted_in") != "yes":
            return {"ok": False, "message": "Consent has to be confirmed."}, 400
        db.grant_consent(conn(), sid, source="manual")
        msg = "%s can now receive campaigns." % row["email"]
    elif action == "unbounce":
        db.clear_bounce(conn(), sid)
        msg = "Bounce cleared."
    else:
        return {"ok": False, "message": "Unknown action."}, 400

    conn().commit()
    fresh = conn().execute("SELECT * FROM subscriber WHERE id = ?", (sid,)).fetchone()
    return {
        "ok": True, "message": msg,
        "row": render_template("_subrow.html", s=fresh),
        "stats": db.stats(conn()),
    }


@app.route("/subscribers/<int:sid>/grant", methods=["POST"])
@login_required
def subscriber_grant(sid):
    """Mark somebody as having opted in, when it happened away from the tool.

    Gated on an explicit statement rather than a plain click. This is the one
    action in the whole tool that creates permission out of nothing, so the
    person pressing it should have to say what they are asserting. Same reasoning
    as the "they opted in" tick on a CSV import.
    """
    row = conn().execute("SELECT * FROM subscriber WHERE id = ?", (sid,)).fetchone()
    if row is None:
        abort(404)
    if row["consent"] == db.NO:
        # Never a route around an unsubscribe. That is what /restore is for, and
        # it has its own, stricter rule.
        session["msg"] = ("%s is unsubscribed. Use the option on that row instead."
                          % row["email"])
        return redirect(url_for("subscribers", **_keep(request)))
    if request.form.get("they_opted_in") != "yes":
        session["msg"] = "Nothing changed: consent has to be confirmed."
        return redirect(url_for("subscribers", **_keep(request)))
    db.grant_consent(conn(), sid, source="manual")
    conn().commit()
    session["msg"] = "%s can now receive campaigns." % row["email"]
    return redirect(url_for("subscribers", **_keep(request)))


@app.route("/subscribers/<int:sid>/unbounce", methods=["POST"])
@login_required
def subscriber_unbounce(sid):
    """Clear a bounce flag, for when the bounce was temporary."""
    db.clear_bounce(conn(), sid)
    conn().commit()
    session["msg"] = "Bounce cleared. They are back in the audience if they have consent."
    return redirect(url_for("subscribers", **_keep(request)))


@app.route("/subscribers/<int:sid>/restore", methods=["POST"])
@login_required
def subscriber_restore(sid):
    """Put somebody back on the list.

    Two different situations, and the difference matters:

    - An operator clicked Unsubscribe by accident. That is a slip, and undoing it
      is simply correcting the record.
    - The person clicked the link in their own email. That is their decision.
      Reversing it because we would rather they were on the list is exactly the
      behaviour unsubscribe rules exist to prevent, so it is refused unless
      whoever is at the keyboard explicitly states the person asked to come back.
    """
    row = conn().execute("SELECT * FROM subscriber WHERE id = ?", (sid,)).fetchone()
    if row is None:
        abort(404)
    asked = request.form.get("they_asked") == "yes"
    if row["unsubscribed_by"] == db.BY_SELF and not asked:
        session["msg"] = ("%s unsubscribed themselves. Only put them back if they "
                          "asked you to." % row["email"])
        return redirect(url_for("subscribers", **_keep(request)))
    # A slip being undone keeps its original provenance; somebody asking to come
    # back is a new consent event and is recorded as one.
    db.restore_consent(conn(), sid, source="asked_to_rejoin" if asked else None)
    conn().commit()
    session["msg"] = "%s is back on the list." % row["email"]
    return redirect(url_for("subscribers", **_keep(request)))


# --- public. no session. opened from inside somebody's inbox. -----------------

@app.route("/p/<token>.gif")
def pixel(token):
    try:
        sender.record_open(conn(), token)
    except Exception:
        pass
    import io as _io
    resp = send_file(_io.BytesIO(PIXEL), mimetype="image/gif")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route("/t/cart", methods=["POST", "OPTIONS"])
def track_cart():
    """The shop telling us somebody filled a basket.

    Public, because it is called from a customer's browser. It answers the same
    empty 204 to everything, whether it stored the event, refused it or could
    not tell who the person was: a public endpoint that reports back is a public
    endpoint that can be asked whether an address is on the list.
    """
    if request.method == "OPTIONS":
        return ("", 204, _cart_cors())
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        # sendBeacon posts text/plain, which keeps the browser from asking
        # permission first. Parsing both means the page can use either.
        try:
            payload = json.loads(request.get_data(as_text=True) or "{}")
        except ValueError:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        carts.record(conn(), payload)
    except Exception:                                  # noqa: BLE001
        pass
    return ("", 204, _cart_cors())


def _cart_cors():
    """Only our own shop may call it, and only to post.

    The list is built from the settings rather than written out here, so it is
    right for whatever shop this tool is pointed at. Both spellings of the host
    count, because the browser sends whichever one the customer typed, and the
    shop platform's own domain is included as well: that is the origin a theme
    posts from while it is being previewed, before the real name is in front of
    it.
    """
    origin = request.headers.get("Origin") or ""
    host = urlsplit(config.SHOP_URL or "").netloc.lower()
    bare = host[4:] if host.startswith("www.") else host
    allowed = {"https://" + bare, "https://www." + bare} if bare else set()
    platform = (config.SHOPIFY_STORE_DOMAIN or "").strip().lower()
    if platform:
        allowed.add("https://" + platform)
    head = {"Vary": "Origin"}
    if origin and origin in allowed:
        head["Access-Control-Allow-Origin"] = origin
        head["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        head["Access-Control-Allow-Headers"] = "Content-Type"
        head["Access-Control-Max-Age"] = "86400"
    return head


@app.route("/c/<token>")
def click(token):
    """Record the click, then send them where they were going.

    Two checks, and both are needed.

    The scheme, so this cannot be turned into a javascript: link. And the
    signature, because without it this is an open redirect: our own domain,
    which customers are being taught to trust, forwarding to wherever a
    stranger likes. Validating the token would not have helped, since everyone
    who gets an email holds a valid one.

    An unsigned or wrongly signed link goes to the shop's front page. Silently:
    telling a prober which half they got wrong is telling them how to fix it.
    """
    url = request.args.get("u") or ""
    given = request.args.get("s") or ""
    if not (url.startswith("http://") or url.startswith("https://")):
        return redirect(config.FALLBACK_URL)
    if not hmac.compare_digest(given, sender.click_signature(token, url)):
        return redirect(config.FALLBACK_URL)
    try:
        sender.record_click(conn(), token, url)
    except Exception:
        pass
    return redirect(url)


@app.route("/u/<token>", methods=["GET", "POST"])
def unsubscribe(token):
    """One click for a person, one button for a robot.

    Gmail's List-Unsubscribe-Post sends a bare POST and expects the person to be
    gone afterwards, so POST still acts immediately with no login and no second
    step. That is the rule and breaking it gets us reported as spam.

    A GET does NOT act. Microsoft Defender Safe Links, Proofpoint and Barracuda
    fetch every URL in an inbound message before the human sees it, and 55% of
    this list is behind Microsoft, so acting on GET means scanners silently
    unsubscribing real customers. RFC 8058 says the one-click URI must not act
    on GET for exactly this reason. A person who clicks gets a page with one
    button, which costs them a click and costs the shop nothing.
    """
    row = conn().execute(
        "SELECT s.subscriber_id AS sid, u.email, u.consent FROM send s"
        " JOIN subscriber u ON u.id = s.subscriber_id WHERE s.token = ?",
        (token,)).fetchone()
    if row is None:
        row = conn().execute(
            "SELECT id AS sid, email, consent FROM subscriber WHERE token = ?",
            (token,)).fetchone()
    if row is None:
        # A reminder from the checkout flow. Its token is in `flow_send`, and
        # the person may not be a subscriber at all: they reached checkout, they
        # never joined a mailing list. Unsubscribing still has to work, and it
        # has to be recorded against an address, or the flow would simply mail
        # them again tomorrow. This route 404'd for every flow email until now.
        fs = conn().execute(
            "SELECT to_email FROM flow_send WHERE token = ?", (token,)).fetchone()
        if fs is not None:
            db.upsert_subscriber(conn(), fs["to_email"], source="checkout")
            conn().commit()
            row = conn().execute(
                "SELECT id AS sid, email, consent FROM subscriber WHERE email = ?",
                (fs["to_email"],)).fetchone()
            # And stop every sequence they are currently in, not just this one.
            conn().execute(
                "UPDATE flow SET stopped = 'unsubscribed'"
                " WHERE email = ? AND stopped = ''", (fs["to_email"],))
            conn().commit()
    if row is None:
        abort(404)
    already = row["consent"] == db.NO
    if request.method == "GET" and not already:
        # Show, do not act. See the docstring: this is the robots.
        return render_template("unsubscribe_confirm.html",
                               email=row["email"], token=token)
    if not already:
        # by=self: this came from the link in their own email. That is a decision
        # to respect, and the admin screen will refuse to undo it silently.
        db.unsubscribe(conn(), row["sid"], by=db.BY_SELF)
        conn().commit()
    return render_template("unsubscribed.html", email=row["email"], already=already)


if __name__ == "__main__":
    db.init()
    port = int(os.environ.get("PORT", "5000"))
    print("%s email marketing on http://127.0.0.1:%s" % (config.SHOP_NAME, port))
    if not config.PASSWORD:
        print("WARNING: no MAILER_PASSWORD set, this tool is unprotected.")
    if not config.SENDING_ENABLED:
        print("Sending is OFF (SENDING_ENABLED). Test mail still works.")
    app.run(host="127.0.0.1", port=port, debug=False)
