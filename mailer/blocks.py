"""Starting points and snippets for the composer.

Nobody running a shop is going to hand-type <p> tags into a textarea, so the
editor offers whole layouts to start from and small pieces to drop in.

What a starter says about the shop, its name, its web address and where it is,
comes from the settings, so the words read as the shop's own from the first day
rather than as somebody else's left behind.

The names of the layouts are English because the owner picks them. Everything
INSIDE them is Dutch, because it ends up in a customer's inbox.

Everything here is deliberately inline-styled and table-free. Outlook ignores
<style> blocks and half of flexbox, so styles that are not on the element itself
simply do not exist for a large slice of any Dutch mailing list.
"""
import html as _html
from urllib.parse import quote

from . import config

#: Brand colours, kept here rather than in the CSS so the email and the tool can
#: drift apart without breaking each other. The email is the one that must not
#: change casually: a colour that renders wrong in Outlook is a reprint.
INK = "#23211e"
MUTED = "#7a736a"
ACCENT = "#9a6f3f"
LINE = "#e6e0d7"

_H = ('<h1 style="font:600 24px/1.3 Georgia,serif;color:%s;margin:0 0 14px">'
      '%%s</h1>') % INK
_P = ('<p style="font:15px/1.65 Arial,sans-serif;color:%s;margin:0 0 16px">'
      '%%s</p>') % INK
_BTN = ('<table role="presentation" cellpadding="0" cellspacing="0" border="0"'
        ' style="margin:22px 0"><tr><td bgcolor="%s" style="border-radius:6px">'
        '<a href="%%s" style="display:inline-block;padding:12px 26px;'
        'font:600 15px/1 Arial,sans-serif;color:#fff;text-decoration:none">%%s</a>'
        '</td></tr></table>') % ACCENT
_IMG = ('<img src="%s" alt="%s" width="560" '
        'style="width:100%%;max-width:560px;height:auto;display:block;'
        'border-radius:8px;margin:0 0 18px">')
_HR = '<hr style="border:none;border-top:1px solid %s;margin:26px 0">' % LINE
_SMALL = ('<p style="font:13px/1.6 Arial,sans-serif;color:%s;margin:0 0 12px">'
          '%%s</p>') % MUTED


def _wrap(inner):
    """One outer table so the message is centred and readable everywhere.

    A bare <div style="max-width"> is ignored by Outlook, which then renders the
    text edge to edge across a 1400px window.
    """
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"'
        ' border="0" style="background:#faf9f7;padding:24px 12px">'
        '<tr><td align="center">'
        '<table role="presentation" width="560" cellpadding="0" cellspacing="0"'
        ' border="0" style="width:560px;max-width:100%;background:#ffffff;'
        'border:1px solid ' + LINE + ';border-radius:10px;padding:30px 28px">'
        '<tr><td>' + inner + '</td></tr></table>'
        '</td></tr></table>'
    )


#: Public names for the pieces above. `layouts.py` assembles whole emails out of
#: them, and reaching into another module's underscored names to do that would
#: be the sort of thing that quietly breaks on a rename.
HEADING, PARA, BUTTON, IMAGE, RULE, SMALL = _H, _P, _BTN, _IMG, _HR, _SMALL
wrap = _wrap


#: The shop's own details, escaped once here rather than at nine call sites,
#: because everything below goes straight into HTML.
_NAME = _html.escape(config.SHOP_NAME)
_SITE = _html.escape(config.SHOP_URL, quote=True)

#: The postal address on one line. Empty is normal, and every starter that uses
#: it leaves that line out rather than printing a blank one. The address the law
#: requires is in the footer send.py writes; this is only the friendly repeat.
_ADDRESS = _html.escape(config.POSTAL_ADDRESS.strip()).replace("\n", " &middot; ")

#: A route link needs somewhere to route to, so no address means no button.
_ROUTE = ("https://maps.google.com/?q=%s" % quote(config.POSTAL_ADDRESS.strip())
          if _ADDRESS else "")


#: Small pieces the toolbar drops in at the cursor.
SNIPPETS = {
    "heading":   _H % "Kop",
    "paragraph": _P % "Schrijf hier uw tekst.",
    "link":      '<a href="%s" style="color:%s">'
                 'bekijk de collectie</a>' % (_SITE, ACCENT),
    "image":     _IMG % ("%s/pad-naar-afbeelding.jpg" % _SITE, "Afbeelding"),
    "button":    _BTN % (_SITE, "Bekijk de collectie"),
    "divider":   _HR,
    "small":     _SMALL % "Kleine aanvullende regel.",
}

#: Whole layouts, offered when the message is still empty.
STARTERS = [
    {
        "key": "arrivals",
        "name": "New arrivals",
        "blurb": "A photo, a short intro and one button. The everyday one.",
        "subject": "Nieuwe collectie binnen bij %s" % config.SHOP_NAME,
        "preheader": "Kom kijken in onze showroom",
        "body": _wrap(
            (_H % "Nieuw binnen") +
            (_P % "Hallo {{naam}},") +
            (_P % "We hebben nieuwe modellen in de showroom staan. "
                  "Kom vrijblijvend kijken en voel het verschil.") +
            (_IMG % ("%s/pad-naar-afbeelding.jpg" % _SITE, "Nieuwe collectie")) +
            (_BTN % (_SITE, "Bekijk de collectie")) +
            _HR +
            ((_SMALL % _ADDRESS) if _ADDRESS else "")
        ),
    },
    {
        "key": "sale",
        "name": "Offer",
        "blurb": "One offer, stated plainly, with a deadline.",
        "subject": "Tijdelijke actie bij %s" % config.SHOP_NAME,
        "preheader": "Alleen deze maand",
        "body": _wrap(
            (_H % "Tijdelijke actie") +
            (_P % "Hallo {{naam}},") +
            (_P % "Deze maand hebben we een scherpe aanbieding op een deel "
                  "van onze collectie. Op = op.") +
            (_BTN % (_SITE, "Bekijk de actie")) +
            (_SMALL % "Geldig tot het einde van de maand, zolang de voorraad strekt.")
        ),
    },
    {
        "key": "showroom",
        "name": "Showroom invite",
        "blurb": "Gets people through the door. Address and directions.",
        "subject": "Kom langs in onze showroom",
        "preheader": "Kom rustig kijken en vergelijken",
        "body": _wrap(
            (_H % "Kom langs in de showroom") +
            (_P % "Hallo {{naam}},") +
            (_P % "Sommige dingen koopt u niet van een foto. Kom kijken, "
                  "voelen en rustig vergelijken. Onze verkopers nemen er de "
                  "tijd voor.") +
            # Both of these claim something only an address can make true, so a
            # shop that has not set one gets the invitation without them rather
            # than a route to nowhere.
            ((_P % ("<b>%s</b>" % _ADDRESS)) if _ADDRESS else "") +
            ((_BTN % (_ROUTE, "Route bekijken")) if _ROUTE else "")
        ),
    },
    {
        "key": "plain",
        "name": "Plain message",
        "blurb": "Just words. Often the one people actually reply to.",
        "subject": "",
        "preheader": "",
        "body": _wrap((_P % "Hallo {{naam}},") +
                      (_P % "Schrijf hier uw bericht.") +
                      (_P % ("Met vriendelijke groet,<br>%s" % _NAME))),
    },
]


def starter(key):
    for s in STARTERS:
        if s["key"] == key:
            return s
    return None
