"""Email layouts: the house design, with named boxes for the words.

The composer used to be a textarea containing HTML. It said "you never have to
type HTML", and that was half true: the toolbar wrote the tags, but they sat in
the box and editing the words meant editing around them. One deleted closing tag
and the email renders wrong in somebody's inbox, where there is no undo.

So the layout owns the HTML and the owner owns the words.

**Every campaign gets the same chrome, in the same order**, because a customer
should not be able to tell which tool sent the message:

    logo  ->  steel-blue hero with the WhatsApp pill inside it  ->  cream hook
    strip  ->  optional orange offer box  ->  picture  ->  words  ->  one square
    uppercase button  ->  USP row  ->  cream footer

The layouts differ only in what the boxes start out saying. If the brand changes,
it changes here once, and every campaign follows.

The logo, the shop name, the address the logo links to and the WhatsApp number
all come from the settings. A shop that has set none of them still gets a whole
email, minus only the parts it has nothing to put in.

Everything typed is escaped. A product name with an ampersand in it, left
unescaped, silently eats the rest of the line in some clients.

Table-based and inline-styled throughout, because Outlook ignores a stylesheet
and most of flexbox. The one <style> block carries the mobile media queries
only, which is exactly the part Outlook is allowed to ignore.
"""
import html as _html
from urllib.parse import quote

from . import config, copy

# --- the house palette --------------------------------------------------------

CREAM = "#f5f2ed"        # page background, hook strip, footer
BLUE = "#44596B"         # hero panel and the one button
INK = "#1b1a18"          # body text
MUTED = "#736e66"        # footer and USP row
SUBLINE = "#d7dee5"      # the line under the hero heading, on blue
LINE = "#e6e0d6"         # hairline above the footer
GREEN = "#25D366"        # WhatsApp, their colour not ours
ORANGE = "#ec734d"       # the offer box

# The promotion email runs warmer than the rest: terracotta and walnut rather
# than the steel blue. Deliberately a different moment, not a redesign of the
# others. See _render_promo.
TERRA = "#b4532a"        # the discount badge and the one big button
TERRA_DARK = "#8f4020"   # the strip above the badge
BEIGE = "#efe6da"        # the warm panel behind the hero
SAND = "#fbf7f2"         # card and section backgrounds, a shade off white
WALNUT = "#4a3728"       # headings on the warm panel
CHAR = "#2b2724"         # body text on this layout

FONT = "'Montserrat',Arial,Helvetica,sans-serif"

#: Where send.py drops the two things it is not allowed to leave out. Markers
#: rather than string-appending, so the postal address and the unsubscribe link
#: land inside the design instead of loose underneath it. send.py falls back to
#: appending when a campaign has no markers, so the guarantee does not depend on
#: a layout remembering them.
FOOTER_MARK = "<!--FOOTER-->"

#: Where a per-person discount code goes. The layout writes this, and the send
#: loop swaps it for a code made for that one recipient. It is spelled like the
#: name placeholder because it behaves like one.
CODE_MARK = "{{code}}"
PREHEADER_MARK = "<!--PREHEADER-->"


# --- turning what somebody typed into something safe to put in an email -------

def _line(raw):
    """One line of text: escaped, with any newlines flattened away."""
    return _html.escape(" ".join((raw or "").split()))


def _url(raw):
    """Only http(s) survives.

    Anything else is dropped rather than passed through, so a typo cannot put a
    `javascript:` link or a bare `www.` into a customer's inbox. The block that
    needed it is then left out entirely.
    """
    u = (raw or "").strip()
    if u.startswith("http://") or u.startswith("https://"):
        return _html.escape(u, quote=True)
    return ""


def _filled(values, key):
    return bool((values.get(key) or "").strip())


def _site():
    """The shop's own address, for every place a campaign left a link empty."""
    return _url(config.SHOP_URL)


def _whatsapp_url():
    """The WhatsApp link, with the same prefilled question wherever it appears,
    so the conversation starts identically however it was opened.

    Empty when no number is configured, and every caller then leaves its button
    out: a chat that reaches nobody is worse than no chat button at all.
    """
    number = config.WHATSAPP_NUMBER.strip()
    if not number:
        return ""
    return ("https://wa.me/%s?text=%s"
            % (quote(number),
               quote("Hallo %s, ik heb een vraag" % config.SHOP_NAME)))


def _whatsapp_default(label):
    """A WhatsApp box starts out empty when there is no number to send anybody
    to, so the composer never offers a button that cannot be drawn."""
    return label if config.WHATSAPP_NUMBER.strip() else ""


# --- the pieces ---------------------------------------------------------------

def _row(inner, bg="#ffffff", pad="0 40px"):
    return ('<tr><td align="center" bgcolor="%s" style="padding:%s;">%s</td></tr>'
            % (bg, pad, inner))


def _logo():
    """The logo row, or nothing at all when no logo is configured.

    A shop that has not set one gets a clean top edge rather than the broken
    image every mail client draws in its place.
    """
    src = _url(config.LOGO_URL)
    if not src:
        return ""
    img = ('<img src="%s" alt="%s" width="170" '
           'style="display:block;border:0;max-width:170px;height:auto;">'
           % (src, _line(config.SHOP_NAME)))
    site = _site()
    if site:
        img = ('<a href="%s" target="_blank" style="display:inline-block;'
               'text-decoration:none;">%s</a>' % (site, img))
    return _row(img, pad="26px 40px 20px")


def _hero(v):
    """Heading, subline and the WhatsApp pill, all on the steel blue.

    The pill sits inside the panel rather than below it because asking a
    question is the action most of these emails are actually trying to cause.
    """
    out = ('<h1 class="hero-h1" style="margin:0 0 8px;color:#ffffff;font-family:%s;'
           'font-size:23px;font-weight:600;line-height:1.25;letter-spacing:-0.01em;">'
           '%s</h1>' % (FONT, _line(v.get("kop"))))
    if _filled(v, "subline"):
        out += ('<p style="margin:0 0 20px;color:%s;font-family:%s;font-size:14px;'
                'font-weight:400;line-height:1.5;">%s</p>'
                % (SUBLINE, FONT, _line(v.get("subline"))))
    wa = _whatsapp_url()
    if wa and _filled(v, "whatsapp"):
        out += ('<table border="0" cellpadding="0" cellspacing="0" align="center"><tr>'
                '<td align="center" bgcolor="%s" style="border-radius:999px;">'
                '<a href="%s" target="_blank" style="display:inline-block;'
                'padding:12px 26px;font-family:%s;font-size:13px;font-weight:600;'
                'color:#ffffff;text-decoration:none;border-radius:999px;">%s</a>'
                '</td></tr></table>'
                % (GREEN, wa, FONT, _line(v.get("whatsapp"))))
    return ('<tr><td align="center" bgcolor="%s" class="hero-pad" '
            'style="padding:32px 40px 28px;">%s</td></tr>' % (BLUE, out))


def _hook(v):
    """One line on cream. The promise that earns the next scroll."""
    if not _filled(v, "hook"):
        return ""
    return _row('<p style="margin:0;font-family:%s;font-size:13px;font-weight:600;'
                'color:%s;">%s</p>' % (FONT, INK, _line(v.get("hook"))),
                bg=CREAM, pad="13px 30px")


def _offer(v):
    """The orange box. Built for a discount code, useful for any one loud fact."""
    if not _filled(v, "actie_code"):
        return ""
    inner = ('<p style="margin:0 0 4px;font-family:%s;font-size:11px;font-weight:600;'
             'letter-spacing:0.18em;text-transform:uppercase;color:#ffffff;">%s</p>'
             % (FONT, _line(v.get("actie_label")) or "Uw persoonlijke code"))
    # AUTO is not a code, it is an instruction: make one per person. Left as a
    # marker here so the same rendered body can carry a different code to every
    # recipient without being re-rendered.
    code = _line(v.get("actie_code"))
    if code.strip().upper() == "AUTO":
        code = CODE_MARK
    inner += ('<p style="margin:0 0 4px;font-family:%s;font-size:26px;font-weight:700;'
              'letter-spacing:0.06em;color:#ffffff;">%s</p>'
              % (FONT, code))
    if _filled(v, "actie_tekst"):
        inner += ('<p style="margin:0;font-family:%s;font-size:12px;color:#ffffff;">'
                  '%s</p>' % (FONT, _line(v.get("actie_tekst"))))
    return ('<tr><td align="center" bgcolor="#ffffff" style="background-color:#ffffff;padding:22px 40px 4px;">'
            '<table border="0" cellpadding="0" cellspacing="0" width="100%%" '
            'style="background-color:%s;border-radius:16px;"><tr>'
            '<td align="center" style="padding:20px 24px;">%s</td></tr></table>'
            '</td></tr>' % (ORANGE, inner))


def _picture(v):
    if not _url(v.get("afbeelding")):
        return ""
    img = ('<img src="%s" alt="%s" width="340" style="display:block;max-width:340px;'
           'width:100%%;height:auto;border-radius:16px;background-color:%s;">'
           % (_url(v.get("afbeelding")),
              _line(v.get("kop")) or _line(config.SHOP_NAME), CREAM))
    link = _url(v.get("knop_link"))
    if link:
        img = ('<a href="%s" target="_blank" style="text-decoration:none;">%s</a>'
               % (link, img))
    return ('<tr><td align="center" class="pad-sides" bgcolor="#ffffff" style="background-color:#ffffff;padding:26px 40px 12px;">'
            '%s</td></tr>' % img)


def _words(v, key="tekst"):
    """The message itself.

    A blank line starts a new paragraph; a single newline is a line break inside
    one. Done by splitting rather than by regex, so nothing anybody types can be
    mistaken for a pattern.
    """
    text = (v.get(key) or "").replace("\r\n", "\n").replace("\r", "\n")
    groups, current = [], []
    for line in text.split("\n"):
        if line.strip():
            current.append(line.strip())
        elif current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    if not groups:
        return ""
    paras = "".join(
        '<p style="margin:0 0 14px;font-family:%s;font-size:15px;font-weight:400;'
        'line-height:1.65;color:%s;">%s</p>'
        % (FONT, INK, "<br>".join(_html.escape(l) for l in group))
        for group in groups)
    return ('<tr><td align="left" class="pad-sides" bgcolor="#ffffff" style="background-color:#ffffff;padding:22px 40px 4px;">'
            '%s</td></tr>' % paras)


def _product_rows(products):
    """The product list, as the cart emails drew it.

    72px rounded thumbnail, name in bold, price right-aligned. The photo, name
    and price come from the webshop, so nobody types a price into a mailing
    going to hundreds of people. A link that could not be read is skipped here
    and reported in the composer instead, because a broken row in a customer's
    inbox is worse than a missing one.
    """
    rows = ""
    for p in products or []:
        if not p:
            continue
        thumb = ""
        if p.get("image"):
            thumb = ('<img src="%s" alt="" width="72" style="display:block;'
                     'width:72px;height:72px;object-fit:cover;border-radius:12px;'
                     'background-color:%s;">'
                     % (_html.escape(p["image"], quote=True), CREAM))
        title = _html.escape(p.get("title") or "")
        link = _url(p.get("url"))
        if link:
            if thumb:
                thumb = '<a href="%s" target="_blank">%s</a>' % (link, thumb)
            title = ('<a href="%s" target="_blank" style="color:%s;'
                     'text-decoration:none;">%s</a>' % (link, INK, title))
        was = ('<p style="margin:2px 0 0;font-family:%s;font-size:12px;color:%s;">'
               'van %s</p>' % (FONT, MUTED, _html.escape(p.get("was") or ""))
               ) if p.get("was") else ""
        # "Aantal: 2" under the name, for a basket. A product pasted into a
        # campaign has no quantity, so this simply does not appear there.
        if p.get("qty"):
            was += ('<p style="margin:2px 0 0;font-family:%s;font-size:12px;'
                    'color:%s;">Aantal: %s</p>'
                    % (FONT, MUTED, _html.escape(str(p["qty"]))))
        rows += (
            '<tr>'
            '<td width="76" valign="top" bgcolor="#ffffff" style="padding:0 0 16px;">%s</td>'
            '<td valign="middle" bgcolor="#ffffff" style="padding:0 12px 16px;">'
            '<p style="margin:0;font-family:%s;font-size:14px;font-weight:600;'
            'color:%s;line-height:1.4;">%s</p>%s</td>'
            '<td valign="middle" align="right" bgcolor="#ffffff" style="padding:0 0 16px;'
            'white-space:nowrap;">'
            '<p style="margin:0;font-family:%s;font-size:14px;font-weight:600;'
            'color:%s;">%s</p></td>'
            '</tr>'
            % (thumb, FONT, INK, title, was, FONT, INK,
               _html.escape(p.get("price") or "")))
    if not rows:
        return ""
    return ('<tr><td class="pad-sides" bgcolor="#ffffff" style="background-color:#ffffff;padding:24px 40px 10px;">'
            '<table border="0" cellpadding="0" cellspacing="0" width="100%%">'
            '%s</table></td></tr>' % rows)


# --- the promotion layout -----------------------------------------------------

def _p_row(inner, bg="#ffffff", pad="0 40px"):
    return ('<tr><td class="pad-sides" bgcolor="%s" '
            'style="background-color:%s;padding:%s;">%s</td></tr>'
            % (bg, bg, pad, inner))


def _p_trust_strip(v):
    """The one-line reassurance under the logo. Small, grey, never shouted."""
    text = (v.get("strip") or "").strip()
    if not text:
        return ""
    return _p_row(
        '<p style="margin:0;font-family:%s;font-size:11px;font-weight:600;'
        'letter-spacing:0.08em;text-transform:uppercase;color:%s;'
        'text-align:center;">%s</p>' % (FONT, MUTED, _html.escape(text)),
        bg=SAND, pad="11px 30px")


def _p_hero(v):
    """Headline, deadline, badge, and the lifestyle photograph.

    The badge sits on the warm panel rather than on the photo: text over a
    photograph is unreadable on half the phones in the country, and a badge
    that has to survive somebody else's living room lighting is a badge that
    does not survive.
    """
    kop = _html.escape(v.get("kop") or "")
    sub = _html.escape(v.get("subline") or "")
    korting = (v.get("korting") or "").strip()
    tot = (v.get("geldig_tot") or "").strip()

    badge = ""
    if korting:
        badge = (
            '<table border="0" cellpadding="0" cellspacing="0" align="center" '
            'style="margin:0 auto 18px;"><tr>'
            '<td align="center" bgcolor="%s" style="background-color:%s;'
            'border-radius:999px;padding:9px 26px;">'
            '<span style="font-family:%s;font-size:26px;font-weight:700;'
            'color:#ffffff;line-height:1;letter-spacing:-0.01em;">%s</span>'
            '</td></tr></table>' % (TERRA, TERRA, FONT, _html.escape(korting)))

    deadline = ""
    if tot:
        deadline = (
            '<p style="margin:14px 0 0;font-family:%s;font-size:12px;'
            'font-weight:600;letter-spacing:0.06em;text-transform:uppercase;'
            'color:%s;">Geldig t/m %s</p>' % (FONT, TERRA_DARK, _html.escape(tot)))

    hero = ('<tr><td align="center" bgcolor="%s" class="hero-pad" '
            'style="background-color:%s;padding:34px 40px 30px;">'
            '%s'
            '<h1 class="hero-h1" style="margin:0 0 10px;color:%s;font-family:%s;'
            'font-size:27px;font-weight:700;line-height:1.2;'
            'letter-spacing:-0.015em;">%s</h1>'
            '<p style="margin:0;color:%s;font-family:%s;font-size:15px;'
            'line-height:1.55;max-width:420px;">%s</p>%s'
            '</td></tr>'
            % (BEIGE, BEIGE, badge, WALNUT, FONT, kop, CHAR, FONT, sub, deadline))

    photo = ""
    link = _url(v.get("knop_link")) or _site()
    if _url(v.get("afbeelding")):
        photo = ('<tr><td bgcolor="%s" style="background-color:%s;padding:0;">'
                 '<a href="%s" target="_blank">'
                 '<img src="%s" alt="" width="620" style="display:block;border:0;'
                 'width:100%%;max-width:620px;height:auto;"></a></td></tr>'
                 % (BEIGE, BEIGE, link, _url(v["afbeelding"])))
    return hero + photo


def _p_cta(v):
    text = (v.get("knop_tekst") or "").strip()
    if not text:
        return ""
    link = _url(v.get("knop_link")) or _site()
    return _p_row(
        '<table border="0" cellpadding="0" cellspacing="0" align="center" '
        'style="margin:0 auto;"><tr>'
        '<td align="center" bgcolor="%s" style="background-color:%s;'
        'border-radius:10px;">'
        '<a href="%s" target="_blank" style="display:inline-block;'
        'padding:17px 44px;font-family:%s;font-size:14px;font-weight:700;'
        'letter-spacing:0.04em;color:#ffffff;text-decoration:none;">%s</a>'
        '</td></tr></table>' % (TERRA, TERRA, link, FONT, _html.escape(text)),
        pad="28px 40px 8px")


def _bare(price):
    """A price with no currency on the front.

    `shop.lookup` returns "EUR 1.495" because that is what a campaign wants to
    print, and this layout printed "EUR %s" on top of it, so every product card
    in the promotion read "EUR EUR 1.495". Stripping here rather than changing
    shop.py, because nine other layouts depend on the prefixed shape.
    """
    raw = str(price or "").strip()
    for prefix in ("EUR ", "EUR", "\u20ac "):
        if raw.upper().startswith(prefix.upper().strip()) and prefix.strip():
            return raw[len(prefix.strip()):].strip()
    return raw


def _discounted(price, percent):
    """A price with the promotion taken off, as a plain string.

    Returns "" when it cannot be worked out, and the card then shows the shop's
    own price with nothing struck through. A wrong price in a discount email is
    the one mistake that costs money at the till.
    """
    try:
        clean = str(price).replace("EUR", "").replace("\u20ac", "").strip()
        clean = clean.replace(".", "").replace(",", ".")
        value = float(clean)
    except (TypeError, ValueError):
        return ""
    try:
        pct = float(str(percent).replace("-", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return ""
    if not 0 < pct < 100:
        return ""
    return "{:,.0f}".format(round(value * (100 - pct) / 100)).replace(",", ".")


def _p_products(products, v):
    """One product per row: photo, name, the old price struck through, the new.

    ONE PER ROW ON PURPOSE. Three across looked better and broke on a real
    phone three times, through three different techniques, while the message
    itself was provably correct every time. Everything that puts them side by
    side depends on the client laying the email out at the width it was given,
    and this one does not depend on width at all. It cannot go wrong, because
    there is no arrangement for it to go wrong into.

    A product that is ALREADY marked down keeps its own van/nu and is never
    shown with the promotion percentage on top of it. That is not a nicety: the
    discount code is scoped to full-price products, so promising the percentage
    on a sale item would be an offer the checkout refuses.
    """
    rows = ""
    for p in (products or [])[:3]:
        if not p:
            continue
        photo = "&nbsp;"
        if p.get("image"):
            photo = ('<a href="%s" target="_blank"><img src="%s" alt="" '
                     'width="200" style="display:block;border:0;width:100%%;'
                     'max-width:200px;max-height:180px;height:auto;'
                     'border-radius:10px;margin:0 auto;"></a>'
                     % (_url(p.get("url")) or _site(),
                        _html.escape(p["image"], quote=True)))
        now = _html.escape(_bare(p.get("price")))
        was = ""
        if p.get("was"):
            # The shop's own markdown. Left exactly as the webshop has it.
            was = _html.escape(_bare(p["was"]))
        else:
            cut = _discounted(_bare(p.get("price")), v.get("korting"))
            if cut:
                was, now = now, cut
        price_html = (
            '<span style="font-family:%s;font-size:13px;color:%s;'
            'text-decoration:line-through;">EUR %s</span>&nbsp;&nbsp;'
            % (FONT, MUTED, was) if was else "")
        price_html += ('<span style="font-family:%s;font-size:17px;'
                       'font-weight:700;color:%s;">EUR %s</span>'
                       % (FONT, TERRA, now))
        rows += (
            '<tr><td align="center" bgcolor="#ffffff" '
            'style="background-color:#ffffff;padding:0 0 12px;">'
            '<table border="0" cellpadding="0" cellspacing="0" width="100%%" '
            'bgcolor="%s" style="background-color:%s;border:1px solid %s;'
            'border-radius:12px;">'
            '<tr><td align="center" style="padding:18px 16px 0;">%s</td></tr>'
            '<tr><td align="center" style="padding:12px 16px 0;">'
            '<p style="margin:0;font-family:%s;font-size:14px;font-weight:600;'
            'color:%s;line-height:1.4;">%s</p></td></tr>'
            '<tr><td align="center" style="padding:8px 16px 0;">%s</td></tr>'
            '<tr><td align="center" style="padding:14px 16px 18px;">'
            '<a href="%s" target="_blank" style="display:inline-block;'
            'padding:10px 26px;font-family:%s;font-size:13px;font-weight:600;'
            'color:%s;text-decoration:none;border:1px solid %s;'
            'border-radius:8px;">Bekijk</a></td></tr>'
            '</table></td></tr>'
            % (SAND, SAND, LINE, photo, FONT, CHAR,
               _html.escape(p.get("title") or ""), price_html,
               _url(p.get("url")) or _site(), FONT, TERRA, TERRA))
    if not rows:
        return ""
    return _p_row(
        '<table border="0" cellpadding="0" cellspacing="0" width="100%">'
        + rows + '</table>', pad="26px 34px 6px")


def _p_trust_row(v):
    """Short promises, TWO PER ROW. Icons are drawn with text on purpose: an
    image per block is four more things to load, and half of them are blocked
    anyway.

    Two per row and not four, because four columns on a phone is 80px each and
    "vanaf EUR 500, tot aan de deur" does not fit in 80px. This is real table
    rows rather than a media query, so it holds at every width and in every
    client, the same way the product list does.
    """
    blocks = [(v.get("usp%s_kop" % i) or "", v.get("usp%s_tekst" % i) or "")
              for i in (1, 2, 3, 4)]
    blocks = [(a.strip(), b.strip()) for a, b in blocks if a.strip()]
    if not blocks:
        return ""
    rows = ""
    for i in range(0, len(blocks), 2):
        pair = blocks[i:i + 2]
        cells = ""
        for kop, tekst in pair:
            cells += (
                '<td width="50%%" valign="top" align="center" bgcolor="%s" '
                'style="background-color:%s;padding:8px 10px;">'
                '<p style="margin:0 0 3px;font-family:%s;font-size:13px;'
                'font-weight:700;color:%s;line-height:1.3;">%s</p>'
                '<p style="margin:0;font-family:%s;font-size:12px;color:%s;'
                'line-height:1.45;">%s</p></td>'
                % (SAND, SAND, FONT, WALNUT, _html.escape(kop),
                   FONT, MUTED, _html.escape(tekst)))
        # An odd number of promises leaves a gap rather than a stretched cell.
        if len(pair) == 1:
            cells += ('<td width="50%%" bgcolor="%s" '
                      'style="background-color:%s;">&nbsp;</td>' % (SAND, SAND))
        rows += '<tr>' + cells + '</tr>'
    return _p_row(
        '<table border="0" cellpadding="0" cellspacing="0" width="100%">'
        + rows + '</table>', bg=SAND, pad="20px 24px 20px")


def _p_whatsapp(v):
    """Its own moment, in WhatsApp's green, well below the terracotta button so
    the two never compete for the same glance."""
    text = (v.get("wa_knop") or "").strip()
    wa = _whatsapp_url()
    if not text or not wa:
        return ""
    kop = _html.escape(v.get("wa_kop") or "")
    onder = _html.escape(v.get("wa_tekst") or "")
    return _p_row(
        '<table border="0" cellpadding="0" cellspacing="0" width="100%%" '
        'bgcolor="#ffffff" style="background-color:#ffffff;border:1px solid %s;'
        'border-radius:12px;"><tr><td align="center" style="padding:22px 20px;">'
        '<p style="margin:0 0 4px;font-family:%s;font-size:15px;font-weight:700;'
        'color:%s;">%s</p>'
        '<p style="margin:0 0 16px;font-family:%s;font-size:13px;color:%s;'
        'line-height:1.5;">%s</p>'
        '<table border="0" cellpadding="0" cellspacing="0" align="center">'
        '<tr><td align="center" bgcolor="%s" style="background-color:%s;'
        'border-radius:999px;">'
        '<a href="%s" target="_blank" style="display:inline-block;'
        'padding:13px 30px;font-family:%s;font-size:13.5px;font-weight:700;'
        'color:#ffffff;text-decoration:none;">%s</a>'
        '</td></tr></table></td></tr></table>'
        % (LINE, FONT, WALNUT, kop, FONT, MUTED, onder, GREEN, GREEN,
           wa, FONT, _html.escape(text)),
        pad="24px 40px 6px")


def _p_quote(v):
    """One customer, one sentence. Five stars drawn as characters, because an
    image of stars is an image that does not load."""
    quote = (v.get("quote") or "").strip()
    if not quote:
        return ""
    who = _html.escape(v.get("quote_naam") or "")
    stars = _html.escape((v.get("sterren") or "").strip())
    # Arial first, NOT Montserrat: the webfont has no star glyph, and a missing
    # glyph is an empty box on somebody's phone. Arial and Helvetica have
    # carried U+2605 since before email had pictures.
    return _p_row(
        '<p style="margin:0 0 8px;font-family:Arial,Helvetica,sans-serif;'
        'font-size:19px;color:%s;letter-spacing:0.18em;text-align:center;">%s</p>'
        '<p style="margin:0 0 8px;font-family:%s;font-size:14.5px;'
        'font-style:italic;color:%s;line-height:1.6;text-align:center;">'
        '&ldquo;%s&rdquo;</p>'
        '<p style="margin:0;font-family:%s;font-size:12px;color:%s;'
        'text-align:center;">%s</p>'
        % (TERRA, stars, FONT, CHAR, _html.escape(quote), FONT, MUTED, who),
        pad="26px 40px 26px")


def _render_promo(v, products=None):
    return _document(
        _logo() + _p_trust_strip(v) + _p_hero(v) + _offer(v) + _p_cta(v) +
        _p_products(products, v) + _words(v) + _p_trust_row(v) +
        _p_whatsapp(v) + _p_quote(v) + _footer())


def _cta(v):
    """One square uppercase button. Both halves or nothing: a button with no
    destination is a dead end, and two buttons is a choice nobody asked for."""
    text, link = _line(v.get("knop_tekst")), _url(v.get("knop_link"))
    if not text or not link:
        return ""
    return ('<tr><td align="center" bgcolor="#ffffff" style="background-color:#ffffff;padding:14px 40px 34px;">'
            '<table border="0" cellpadding="0" cellspacing="0" align="center"><tr>'
            '<td align="center" bgcolor="%s" style="border-radius:0;">'
            '<a href="%s" target="_blank" style="display:inline-block;'
            'padding:15px 38px;font-family:%s;font-size:12px;font-weight:600;'
            'letter-spacing:0.16em;text-transform:uppercase;color:#ffffff;'
            'text-decoration:none;">%s</a></td></tr></table></td></tr>'
            % (BLUE, link, FONT, text))


def _usp(v):
    if not _filled(v, "usp"):
        return ""
    return ('<tr><td align="center" bgcolor="#ffffff" style="background-color:#ffffff;padding:0 30px 26px;">'
            '<p style="margin:0;font-family:%s;font-size:10px;font-weight:600;'
            'letter-spacing:0.12em;text-transform:uppercase;color:%s;'
            'line-height:1.8;">%s</p></td></tr>'
            % (FONT, MUTED, _line(v.get("usp"))))


def _footer():
    """The cell send.py fills. Left empty here on purpose: what goes in it is
    legally required, so it is not a layout's decision whether to include it."""
    return ('<tr><td align="center" bgcolor="%s" style="padding:20px 30px;'
            'border-top:1px solid %s;">%s</td></tr>' % (CREAM, LINE, FOOTER_MARK))


def _document(rows):
    """The whole message.

    A complete document rather than a fragment, because the mobile media queries
    need a <style> block and Outlook needs the doctype to size tables correctly.
    """
    return (
        '<!DOCTYPE html>'
        '<html lang="nl"><head>'
        '<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        # Without these a phone in dark mode inverts the whole email, and the
        # cream and terracotta this was designed in become mud. Gmail, Outlook
        # and Apple Mail all do it, and Chrome on Android does it to any page
        # that has not said otherwise. Saying it is a light design is how you
        # keep the colours you chose.
        '<meta name="color-scheme" content="light">'
        '<meta name="supported-color-schemes" content="light">'
        '<style type="text/css">'

        'body,table,td,a{-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;}'
        'table,td{mso-table-lspace:0pt;mso-table-rspace:0pt;}'
        'img{border:0;height:auto;line-height:100%;outline:none;text-decoration:none;}'
        'body{margin:0 !important;padding:0 !important;background-color:' + CREAM + ';}'
        '@media only screen and (max-width:640px){'
        '.email-wrap{width:100% !important;}'
        '.pad-sides{padding-left:18px !important;padding-right:18px !important;}'
        '.hero-pad{padding:26px 18px 24px !important;}'
        # Everything that used to reflow the product cards and the trust row
        # lived here and did not work on a real phone. Both are now built to
        # stack at any width, so this block is back to what it was for: the
        # wrapper, the side padding and the headline size.
        'h1.hero-h1{font-size:20px !important;}}'
        '</style></head>'
        '<body style="margin:0;padding:0;color-scheme:light;'
        'supported-color-schemes:light;background-color:' + CREAM + ';">'
        + PREHEADER_MARK +
        '<table border="0" cellpadding="0" cellspacing="0" width="100%" '
        'bgcolor="' + CREAM + '" style="background-color:' + CREAM + ';">'
        '<tr><td align="center" valign="top" bgcolor="' + CREAM + '" '
        'style="padding:28px 14px;">'
        '<table border="0" cellpadding="0" cellspacing="0" width="620" '
        'class="email-wrap" bgcolor="#ffffff" style="max-width:620px;width:100%;'
        'background-color:#ffffff;border-radius:16px;overflow:hidden;">'
        + rows +
        '</table></td></tr></table></body></html>')


def _render(v, products=None):
    """Every campaign is this, in this order. Empty boxes drop out."""
    return _document(
        _logo() + _hero(v) + _hook(v) + _offer(v) + _picture(v) +
        _words(v) + _product_rows(products) + _words(v, "tekst_na") +
        _cta(v) + _usp(v) + _footer())


# --- the layouts --------------------------------------------------------------
#
# Each field is (key, label, type, hint, default). Types:
#   line  - one line, rendered as a single input
#   text  - several paragraphs, rendered as a textarea
#   url   - a web address, validated before it is used
#
# The layouts share every field except the offer box. What differs is the words
# they start with, so choosing one is choosing a starting point, never a
# different-looking email.

_NAAM_HINT = "{{naam}} becomes their first name. Works in any box."


#: Paste a product page link and the photo, name and price come off the webshop.
#: Three, because that is about as many as anyone scrolls in a newsletter and
#: twelve boxes is already a long form. Empty slots disappear.
PRODUCT_KEYS = ("product1", "product2", "product3")

_PRODUCT_FIELDS = [
    (key, "Product %d" % (i + 1), "url",
     ("Paste a product link from your webshop. The photo, name and price are "
      "taken from the webshop when you save." if i == 0 else ""),
     "")
    for i, key in enumerate(PRODUCT_KEYS)
]

_OFFER_FIELDS_FOR = lambda c: [
    ("actie_label", "Offer box label", "line",
     "Small uppercase line in the orange box.", c.get("actie_label", "")),
    ("actie_code", "Offer box code", "line",
     "The big line. Empty means no orange box at all.", c.get("actie_code", "")),
    ("actie_tekst", "Offer box note", "line", "", c.get("actie_tekst", "")),
]


def _promo_fields(c):
    """The promotion has parts no other layout has, so it has its own boxes.

    Ordered the way the email reads down the page, because a form that jumps
    around is a form where somebody edits the wrong line.
    """
    return [
        ("strip", "Trust line under the logo", "line",
         "Small grey line. Empty removes it.", c.get("strip", "")),
        ("kop", "Headline", "line", "The big line on the warm panel.", c["kop"]),
        ("subline", "Line under the headline", "line", _NAAM_HINT, c["subline"]),
        ("korting", "Discount badge", "line",
         "Shown in the terracotta circle, e.g. -20%. It ALSO works out the new "
         "price on the product cards below.", c.get("korting", "")),
        ("geldig_tot", "Valid until", "line",
         "The deadline, e.g. 6 september. Empty removes the line.",
         c.get("geldig_tot", "")),
        ("afbeelding", "Hero photo", "url",
         "A living room or bedroom scene. Empty means no photo.", ""),
        ("actie_label", "Code box label", "line",
         "Small line above the code.", c.get("actie_label", "")),
        ("actie_code", "Discount code", "line",
         "AUTO makes a single-use code per person. A word here uses that one "
         "code for everybody.", c.get("actie_code", "")),
        ("actie_tekst", "Code box note", "line", "", c.get("actie_tekst", "")),
        ("knop_tekst", "Main button", "line",
         "The big terracotta button.", c["knop_tekst"]),
        ("knop_link", "Main button link", "url",
         "Also where the hero photo links to.", c["knop_link"]),
    ] + _PRODUCT_FIELDS + [
        ("tekst", "Text", "text", "A blank line starts a new paragraph.",
         c["tekst"]),
        ("usp1_kop", "Trust block 1", "line", "Bold line.", c.get("usp1_kop", "")),
        ("usp1_tekst", "Trust block 1, under", "line", "", c.get("usp1_tekst", "")),
        ("usp2_kop", "Trust block 2", "line", "", c.get("usp2_kop", "")),
        ("usp2_tekst", "Trust block 2, under", "line", "", c.get("usp2_tekst", "")),
        ("usp3_kop", "Trust block 3", "line", "", c.get("usp3_kop", "")),
        ("usp3_tekst", "Trust block 3, under", "line", "", c.get("usp3_tekst", "")),
        ("usp4_kop", "Trust block 4", "line",
         "Empty removes the block, and the others spread out.",
         c.get("usp4_kop", "")),
        ("usp4_tekst", "Trust block 4, under", "line", "", c.get("usp4_tekst", "")),
        ("wa_kop", "WhatsApp heading", "line", "", c.get("wa_kop", "")),
        ("wa_tekst", "WhatsApp line", "line", "", c.get("wa_tekst", "")),
        ("wa_knop", "WhatsApp button", "line",
         "Empty removes the whole WhatsApp block.", c.get("wa_knop", "")),
        ("sterren", "Stars", "line", "Drawn as text so it always shows.",
         c.get("sterren", "")),
        ("quote", "Customer quote", "line",
         "A real one, word for word, from your own reviews. Empty removes the "
         "block. Never write one yourself: an invented review is illegal.",
         c.get("quote", "")),
        ("quote_naam", "Who said it", "line", "", c.get("quote_naam", "")),
    ]


def _fields(c):
    """The boxes for one layout, in the order the parts appear in the email.

    Built by composing rather than by splicing into index positions: the offer
    box and the product list both go in the middle, and two slice operations
    fighting over the same list is how defaults quietly go missing.
    """
    if c.get("promo"):
        return _promo_fields(c)
    out = [
        ("kop", "Heading", "line", "The big white line on the blue panel.",
         c["kop"]),
        ("subline", "Line under the heading", "line", _NAAM_HINT, c["subline"]),
        ("whatsapp", "WhatsApp button", "line",
         "The green pill on the blue. Empty means no button.",
         _whatsapp_default(copy.WHATSAPP_LABEL)),
        ("hook", "Cream strip", "line",
         "One promise, right under the blue. Empty means no strip.",
         c.get("hook", "")),
    ]
    if c.get("offer"):
        out += _OFFER_FIELDS_FOR(c)
    out += [
        ("afbeelding", "Picture", "url",
         "A link to an image. Empty means no picture.", ""),
        ("tekst", "Text", "text", "A blank line starts a new paragraph.",
         c["tekst"]),
    ]
    if c.get("products"):
        out += _PRODUCT_FIELDS
    out += [
        ("knop_tekst", "Button text", "line",
         "The square button. Empty means no button.", c["knop_tekst"]),
        ("knop_link", "Button link", "url",
         "Also where the picture links to.", c["knop_link"]),
        ("usp", "Bottom line", "line",
         "The small grey line above the footer.", copy.USP),
    ]
    return out


#: One entry per campaign type the shop actually sends. The words are all in
#: `copy.py`; this only decides which boxes each one gets.
LAYOUTS = [
    {"key": c["key"], "name": c["name"], "blurb": c["blurb"],
     "subject": c["subject"], "preheader": c["preheader"],
     "fields": _fields(c)}
    for c in copy.LAYOUTS
]



# --- reading the Dutch, for whoever is choosing it ----------------------------
#
# The interface is English and the emails are Dutch, because who reads the words
# decides. That leaves a gap: the person picking the words cannot necessarily
# read them. This closes it.
#
# It covers ONLY the text shipped with the layouts, never text somebody typed.
# A translation that has quietly gone stale is worse than none at all, so an
# edited box simply stops showing one.

ENGLISH = dict(copy.ENGLISH)

#: The abandoned checkout sequence, surfaced here so `flows.py` has one place to
#: look for both the design and the words.
CART_SUBJECTS = copy.CART_SUBJECTS
CART_PREHEADERS = copy.CART_PREHEADERS
CART_HEADINGS = copy.CART_HEADINGS
CART_SUBLINES = copy.CART_SUBLINES
CART_BODIES = copy.CART_BODIES
CART_BUTTONS = copy.CART_BUTTONS

#: Fixed Dutch that send.py adds, not a layout. Replaced in the rendered HTML
#: for the English preview so the footer reads too.
FIXED_ENGLISH = {
    "U ontvangt deze e-mail omdat u zich heeft aangemeld voor onze nieuwsbrief.":
        "You are receiving this email because you signed up for our newsletter.",
    "U ontvangt deze e-mail omdat u een bestelling bij ons bent begonnen.":
        "You are receiving this email because you started an order with us.",
    # The existing-customer basis. It is the line most of this list will
    # actually see, and it was the one line of the footer with no reading.
    "U ontvangt deze e-mail omdat u eerder bij ons heeft gekocht. "
    "Afmelden kan altijd, met één klik.":
        "You are receiving this email because you have bought from us before. "
        "You can unsubscribe at any time, in one click.",
    ">Afmelden<": ">Unsubscribe<",
    ">Privacybeleid<": ">Privacy policy<",
    # Drawn by the promotion header, not typed into a box, so it is fixed text.
    "Geldig t/m ": "Valid until ",
    # The small button on each product card. Anchored with the closing tag so
    # it cannot match the word inside somebody's own sentence.
    ">Bekijk</a>": ">View</a>",
}


def english(value):
    """What a shipped Dutch default says, or None if we did not write it."""
    if not (value or "").strip():
        return None
    raw = (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return ENGLISH.get(raw)


def to_english(values):
    """The same words with every known default swapped for its translation.

    Anything the owner has edited is left in Dutch, which is the honest answer:
    it is visibly still Dutch rather than quietly mistranslated.
    """
    out = {}
    for key, value in (values or {}).items():
        out[key] = english(value) or value
    return out


def english_html(html):
    """Translate the fixed Dutch that is not a layout field."""
    for nl, en in FIXED_ENGLISH.items():
        html = html.replace(nl, en)
    return html


# --- blocks ------------------------------------------------------------------
#
# Every section of every email, named, with the boxes it needs and the function
# that draws it. A template the owner builds is an ordered list of these keys
# plus the words she typed. The logo and the footer are not blocks: they are
# always first and always last, and the footer carries the legal lines, so
# they are not offered as things to remove.

def _f(key, label, kind, hint, default=""):
    return (key, label, kind, hint, default)


BLOCKS = {
    "hero": {
        "label": "Blue header",
        "hint": "Heading, a line under it, and the WhatsApp button, on the blue.",
        "fields": [_f("kop", "Heading", "line", "The big white line.", ""),
                   _f("subline", "Line under the heading", "line", _NAAM_HINT, ""),
                   _f("whatsapp", "WhatsApp button", "line",
                      "The green pill. Empty means no button.",
                      _whatsapp_default(copy.WHATSAPP_LABEL))],
        "render": lambda v, p: _hero(v),
    },
    "hero_warm": {
        "label": "Warm header with badge",
        "hint": "The terracotta version: badge, heading, deadline, photo.",
        "fields": [_f("korting", "Badge", "line", "e.g. -20%. Also prices the product cards.", ""),
                   _f("kop", "Headline", "line", "", ""),
                   _f("subline", "Line under the headline", "line", _NAAM_HINT, ""),
                   _f("geldig_tot", "Valid until", "line", "Empty removes the line.", ""),
                   _f("afbeelding", "Hero photo", "url", "Empty means no photo.", "")],
        "render": lambda v, p: _p_hero(v),
    },
    "strip": {
        "label": "Cream strip",
        "hint": "One promise under the header. Empty removes it.",
        "fields": [_f("hook", "Cream strip", "line", "", "")],
        "render": lambda v, p: _hook(v),
    },
    "trust_strip": {
        "label": "Small grey trust line",
        "hint": "Under the logo, uppercase, e.g. free delivery, warranty, iDEAL.",
        "fields": [_f("strip", "Trust line", "line", "", "")],
        "render": lambda v, p: _p_trust_strip(v),
    },
    "offer": {
        "label": "Orange code box",
        "hint": "A discount code. AUTO makes one per person.",
        "fields": [_f("actie_label", "Small line above", "line", "", "Uw persoonlijke code"),
                   _f("actie_code", "Code", "line",
                      "AUTO for one code per person, or a fixed code.", ""),
                   _f("actie_tekst", "Line underneath", "line", "", "")],
        "render": lambda v, p: _offer(v),
    },
    "picture": {
        "label": "Picture",
        "hint": "A full-width photo, linking to the button link.",
        "fields": [_f("afbeelding", "Picture", "url", "A link to an image.", "")],
        "render": lambda v, p: _picture(v),
    },
    "text": {
        "label": "Text",
        "hint": "Paragraphs. A blank line starts a new one.",
        "fields": [_f("tekst", "Text", "text", "", "")],
        "render": lambda v, p: _words(v),
    },
    "products": {
        "label": "Product list",
        "hint": "Up to three products as rows, from pasted webshop links.",
        "fields": list(_PRODUCT_FIELDS),
        "render": lambda v, p: _product_rows(p),
    },
    "product_cards": {
        "label": "Product cards with prices",
        "hint": "Three cards, old price struck through when the badge is set.",
        "fields": list(_PRODUCT_FIELDS),
        "render": lambda v, p: _p_products(p, v),
    },
    "text_after": {
        "label": "Text under the products",
        "hint": "",
        "fields": [_f("tekst_na", "Text under the products", "text", "", "")],
        "render": lambda v, p: _words(v, "tekst_na"),
    },
    "button": {
        "label": "Button",
        "hint": "The square blue button.",
        "fields": [_f("knop_tekst", "Button text", "line", "Empty means no button.", ""),
                   _f("knop_link", "Button link", "url", "", copy.SHOP)],
        "render": lambda v, p: _cta(v),
    },
    "button_warm": {
        "label": "Terracotta button",
        "hint": "The big rounded button from the promotion.",
        "fields": [_f("knop_tekst", "Button text", "line", "", ""),
                   _f("knop_link", "Button link", "url", "", copy.SHOP)],
        "render": lambda v, p: _p_cta(v),
    },
    "trust_row": {
        "label": "Trust blocks",
        "hint": "Three or four short promises side by side.",
        "fields": [_f("usp%s_kop" % i, "Trust block %s" % i, "line", "", "")
                   for i in (1, 2, 3, 4)] +
                  [_f("usp%s_tekst" % i, "Trust block %s, under" % i, "line", "", "")
                   for i in (1, 2, 3, 4)],
        "render": lambda v, p: _p_trust_row(v),
    },
    "whatsapp_block": {
        "label": "WhatsApp block",
        "hint": "Its own moment, in WhatsApp green.",
        "fields": [_f("wa_kop", "Heading", "line", "", "Liever persoonlijk advies?"),
                   _f("wa_tekst", "Line", "line", "", ""),
                   _f("wa_knop", "Button", "line", "Empty removes the block.",
                      _whatsapp_default("Chat via WhatsApp"))],
        "render": lambda v, p: _p_whatsapp(v),
    },
    "quote": {
        "label": "Customer quote",
        "hint": "Stars, one sentence, a name.",
        "fields": [_f("sterren", "Stars", "line", "", "\u2605\u2605\u2605\u2605\u2605"),
                   _f("quote", "Quote", "line",
                      "A real one from your own reviews, word for word. Empty "
                      "removes the block.", ""),
                   _f("quote_naam", "Who said it", "line", "", "")],
        "render": lambda v, p: _p_quote(v),
    },
    "usp_line": {
        "label": "Small grey line above the footer",
        "hint": "",
        "fields": [_f("usp", "Bottom line", "line", "", copy.USP)],
        "render": lambda v, p: _usp(v),
    },
}

#: The order the palette offers them in.
BLOCK_ORDER = ["hero", "hero_warm", "strip", "trust_strip", "offer", "picture",
               "text", "products", "product_cards", "text_after", "button",
               "button_warm", "trust_row", "whatsapp_block", "quote", "usp_line"]

#: How each built-in layout is made of blocks, so any of them can be duplicated
#: as a starting point. This is the same order _render and _render_promo use.
BUILT_IN_BLOCKS = {
    "promo": ["trust_strip", "hero_warm", "offer", "button_warm", "product_cards",
              "text", "trust_row", "whatsapp_block", "quote"],
}


def blocks_of(key):
    """The block list a built-in layout amounts to."""
    if key in BUILT_IN_BLOCKS:
        return list(BUILT_IN_BLOCKS[key])
    spec = next((c for c in copy.LAYOUTS if c["key"] == key), None)
    if spec is None:
        return []
    out = ["hero", "strip"]
    if spec.get("offer"):
        out.append("offer")
    out += ["picture", "text"]
    if spec.get("products"):
        out.append("products")
    out += ["text_after", "button", "usp_line"]
    return out


def fields_for_blocks(keys):
    """The boxes a list of blocks needs, in block order, each box once.

    Two blocks can share a box (both headers use the heading), and a template
    with both would be odd but must not crash or show the box twice.
    """
    seen, out = set(), []
    for key in keys:
        for f in BLOCKS.get(key, {}).get("fields", []):
            if f[0] in seen:
                continue
            seen.add(f[0])
            out.append(f)
    return out


def render_blocks(keys, values, products=None):
    """A template of the owner's own making, drawn by the same functions."""
    v = values or {}
    body = "".join(BLOCKS[k]["render"](v, products) for k in keys if k in BLOCKS)
    return _document(_logo() + body + _footer())


#: Where custom templates come from. app.py points this at the database at
#: start-up; this module stays free of it so rendering remains a pure function.
CUSTOM = None
CUSTOM_PREFIX = "t:"


def get(key):
    for layout in LAYOUTS:
        if layout["key"] == key:
            return layout
    if key and key.startswith(CUSTOM_PREFIX) and CUSTOM is not None:
        return CUSTOM(key)
    return None


def defaults(key):
    layout = get(key)
    if layout is None:
        return {}
    return {f[0]: f[4] for f in layout["fields"]}


def collect(key, form):
    """Pull this layout's own fields out of a submitted form, and nothing else.

    Driven by the layout definition rather than by whatever arrived, so an extra
    field in the request cannot end up stored and a renamed one cannot linger.
    """
    layout = get(key)
    if layout is None:
        return {}
    return {f[0]: (form.get("f_" + f[0]) or "") for f in layout["fields"]}


def product_urls(values):
    """The product links that were pasted, in order, skipping empty slots."""
    return [values[k] for k in PRODUCT_KEYS
            if (values or {}).get(k, "").strip()]


def render(key, values, products=None):
    """The finished HTML. Empty string for a layout we do not know.

    `products` is what the webshop said about the pasted links. Passed in rather
    than fetched here, so rendering stays a pure function of its arguments and
    a preview cannot turn into three network calls per keystroke.
    """
    layout = get(key)
    if layout is None:
        return ""
    if layout.get("blocks") is not None:
        return render_blocks(layout["blocks"], values, products)
    if key == "promo":
        return _render_promo(values or {}, products)
    return _render(values or {}, products)
