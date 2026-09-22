"""The words. Every default sentence in every campaign layout lives here.

Separate from `layouts.py` on purpose: the design changes once a year and the
copy changes every month, and whoever rewrites a subject line should not have to
read table markup to do it.

WHAT IS IN HERE NOW
-------------------
Placeholder text, in English, for a shop that has not written its copy yet.
Every line below is meant to be replaced. Nothing here is a promise: there is no
delivery time, no warranty length and no discount you have not decided on,
because a default that quietly claims something is a default that goes out in
somebody's name.

The structure is the real thing though. Each layout ships with words in every
box it has, so a fresh install previews and sends a complete email rather than a
page of empty rows, and so you can see what a box is for by reading what is in
it.

HOUSE RULES FOR THE COPY, once you write yours
----------------------------------------------
* One language, and one register, matching whatever your order confirmations
  already sound like, so the two do not read as different companies.
* Around fifty words of body. These are read on a phone, between other things.
* One idea and one action per email. A second call to action halves the first.
* Subject under about forty-five characters or a phone truncates it. No capitals
  shouting, no exclamation marks, no "free!!" - the spam filters read those and
  so does the reader.
* The preheader extends the subject, it never repeats it. Repeating wastes the
  only other line the inbox gives you.
* No long dashes anywhere. Commas and full stops instead.
* Specifics beat adjectives. "At your home within two weeks" outsells "fast
  delivery", because only one of them is a promise.

Everything shop-specific comes from `config.py`, so the same words work for the
next shop without an edit here.
"""
from urllib.parse import quote_plus

from . import config

# --- the standing lines, shared by every layout -------------------------------

WHATSAPP_LABEL = "Ask us on WhatsApp"

#: The bottom line. Three reasons to trust, in the order they matter to somebody
#: about to spend real money on something they cannot touch.
USP = "Your first promise  •  Your second promise  •  Your third promise"

#: The cream strip under the header. One promise, on the layouts that want one.
#: It was five different lines once, one per layout, and that is worth doing
#: again: the promise that sells a visit is not the promise that sells stock.
HOOK = "Your one promise here"

SHOP = config.SHOP_URL

#: The layouts that are about one part of the range point here instead, so you
#: can send people to a category page without editing every layout. It starts
#: out as the shop itself, which is never wrong, only vague.
CATEGORY = config.SHOP_URL

#: Directions, built from the address the footer needs anyway, so there is one
#: place to change it. With no address there is nothing to point at, and a map
#: link to nowhere is worse than none, so the button falls back to the website.
ROUTE = ("https://maps.google.com/?q=" + quote_plus(config.POSTAL_ADDRESS)
         if config.POSTAL_ADDRESS.strip() else SHOP)


# --- the layouts --------------------------------------------------------------
#
# `products` gives the layout its three product boxes; `offer` gives it the
# orange code box. Both are off unless a layout genuinely needs them.
#
# The keys are fixed: `layouts.py`, `db.py` and `app.py` all look layouts up by
# them, and saved campaigns remember which one they were written in. Rename the
# name and the blurb freely, they are only what the picker shows.

LAYOUTS = [
    {
        "key": "arrivals",
        "name": "New arrivals",
        "blurb": "Something new, with pictures and one button. The one you "
                 "will send most often.",
        "products": True,
        "subject": "Your subject line here",
        "preheader": "One line that extends it, never a repeat",
        "kop": "Your headline here",
        "subline": "Dear {{naam}}, one line under the headline.",
        "hook": HOOK,
        "tekst": "Say what is new and why it is worth a look.\n"
                 "\n"
                 "A blank line starts a new paragraph.",
        "knop_tekst": "Your button text",
        "knop_link": SHOP,
    },
    {
        "key": "sale",
        "name": "Offer",
        "blurb": "An offer with an end date, and the orange code box.",
        "products": True,
        "offer": True,
        # No cream strip: on this layout the orange box is the hook, and two
        # competing promises above the fold is one too many.
        "hook": "",
        "subject": "Your offer in one line",
        "preheader": "What makes it worth opening",
        "kop": "Your offer headline here",
        "subline": "Dear {{naam}}, one line under the headline.",
        "actie_label": "Your personal code",
        # AUTO, never a literal code. AUTO makes one single-use code per person;
        # a word here sends the same code to everybody, and a code that does not
        # exist in the shop is a customer typing it at the checkout and being
        # told it is not valid.
        "actie_code": "AUTO",
        "actie_tekst": "What the code gives, and how long it lasts",
        "tekst": "Say what the offer is and when it ends.",
        "knop_tekst": "Your button text",
        "knop_link": CATEGORY,
    },
    {
        "key": "showroom",
        "name": "Showroom invite",
        "blurb": "Gets people through the door. Address, hours, directions.",
        "subject": "Your invitation in one line",
        "preheader": "Opening hours, parking, whatever makes it easy",
        "kop": "Your headline here",
        "subline": "Dear {{naam}}, one line under the headline.",
        "hook": HOOK,
        "tekst": "Say where you are, when you are open and what a visitor can "
                 "expect.\n"
                 "\n"
                 "Your address and your opening hours belong here, in full.",
        # The button follows the address in config, so this one link is right
        # as soon as the footer is.
        "knop_tekst": "Get directions",
        "knop_link": ROUTE,
    },
    {
        "key": "welcome",
        "name": "Welcome",
        "blurb": "The first email after somebody signs up. Sets expectations.",
        # The one email with a guaranteed open. It should say what they have
        # signed up for and then get out of the way, not sell.
        "subject": "Welcome to %s" % config.SHOP_NAME,
        "preheader": "What to expect from us",
        "kop": "Your headline here",
        "subline": "Dear {{naam}}, one line under the headline.",
        "hook": HOOK,
        "tekst": "Say what this list is for and how often you will write.\n"
                 "\n"
                 "Invite a reply. The first email is the one people answer.",
        "knop_tekst": "Your button text",
        "knop_link": SHOP,
    },
    {
        "key": "sleep",
        "name": "Advice",
        "blurb": "For whatever people need help choosing. Earns a visit or a "
                 "question, not a click.",
        "products": True,
        # The key is historical and stays, because saved campaigns are stored
        # against it. Sells the advice, not the product: somebody who asks a
        # question buys, somebody who reads a spec sheet compares prices.
        "subject": "Your advice subject here",
        "preheader": "The question this email answers",
        "kop": "Your headline here",
        "subline": "Dear {{naam}}, one line under the headline.",
        "hook": HOOK,
        "tekst": "Say what you can help people choose between, and invite the "
                 "question.\n"
                 "\n"
                 "Name the thing a photograph cannot tell them.",
        "knop_tekst": "Your button text",
        "knop_link": CATEGORY,
    },
    {
        "key": "klarna",
        "name": "Paying in instalments",
        "blurb": "Answers the price objection on something expensive.",
        # The key names the provider this started with, the words are yours.
        # Put the real limits and the real cost in: vague copy here turns into
        # an argument at the counter.
        "subject": "Your payment subject here",
        "preheader": "The condition that matters most",
        "kop": "Your headline here",
        "subline": "Dear {{naam}}, one line under the headline.",
        "hook": HOOK,
        "tekst": "Say how paying in instalments works, what it costs, and "
                 "which orders it applies to.\n"
                 "\n"
                 "Invite the question, because there is always one.",
        "knop_tekst": "Your button text",
        "knop_link": SHOP,
    },
    {
        "key": "stock",
        "name": "In stock now",
        "blurb": "Delivery time as the argument. Usually the strongest fact a "
                 "shop has.",
        "products": True,
        "subject": "Your delivery promise here",
        "preheader": "What that means for the customer",
        "kop": "Your headline here",
        "subline": "Dear {{naam}}, one line under the headline.",
        "hook": HOOK,
        "tekst": "Say how long delivery takes, and what is ready to go now.\n"
                 "\n"
                 "Give the number. A number is a promise, fast is not.",
        "knop_tekst": "Your button text",
        "knop_link": SHOP,
    },
    {
        "key": "cart",
        "name": "Abandoned checkout",
        "blurb": "The reminder flow. Its words come from the three steps below.",
        # Used by the automated flow rather than picked by hand, so unlike the
        # rest of this file it ships with words that work as they are: a timer
        # can fire this before anybody has been through the composer, and a
        # reminder reading "your headline here" would go out in your name.
        "products": False,
        "subject": "Your order is not finished yet",
        "preheader": "Everything is still in your basket",
        "kop": "Your order is not finished yet",
        "subline": "Dear {{naam}}, you were nearly there.",
        "hook": HOOK,
        "tekst": "You have not finished your order. Everything is still in "
                 "your basket.",
        "knop_tekst": "Finish your order",
        "knop_link": SHOP,
    },
    {
        "key": "promo",
        "name": "Discount promotion",
        "blurb": "A discount with a deadline. Warm terracotta, product cards "
                 "with the old price struck through, and its own WhatsApp "
                 "moment.",
        "products": True,
        "offer": True,
        "promo": True,
        # The hook strip is replaced on this layout by the trust row further
        # down, and by the badge above the headline. Three promises stacked
        # above the fold is two too many.
        "hook": "",
        "subject": "Your discount in one line",
        "preheader": "Add the deadline, it is the reason to act",
        "strip": "Your delivery line  -  Your warranty  -  How people can pay",
        "kop": "Your discount headline here",
        "subline": "Dear {{naam}}, one line under the headline.",
        # Drawn in the badge, and it also works out the struck-through price on
        # the product cards, so it is a number rather than a phrase.
        "korting": "-20%",
        # Empty, so the deadline line is simply absent until you set one. A date
        # left over from the last shop is a deadline that has already passed.
        "geldig_tot": "",
        "actie_label": "Your personal code",
        "actie_code": "AUTO",
        "actie_tekst": "What the code gives, and how long it lasts",
        "tekst": "Say what the discount is, what it covers and when it ends.",
        "usp1_kop": "Your first promise",
        "usp1_tekst": "one short line under it",
        # If you promise a warranty here, say it sits alongside the statutory
        # rights the customer already has. A commercial guarantee never replaces
        # them, and a line that gives the length without saying so is the
        # wording consumer authorities write to shops about.
        "usp2_kop": "Your second promise",
        "usp2_tekst": "one short line under it",
        "usp3_kop": "Your third promise",
        "usp3_tekst": "one short line under it",
        "usp4_kop": "Your fourth promise",
        "usp4_tekst": "empty removes this block, the others spread out",
        "wa_kop": "Would you rather ask us?",
        "wa_tekst": "Say how quickly you answer, if you answer quickly.",
        "wa_knop": "Chat on WhatsApp",
        "sterren": "★★★★★",
        # Deliberately empty, and it must stay empty until you have a review to
        # put here. It has to be one somebody actually left, word for word, from
        # wherever you collect them. An invented testimonial is a misleading
        # commercial practice. An empty quote removes the whole block rather
        # than leaving a gap, so shipping it blank is safe.
        "quote": "",
        "quote_naam": "",
        "knop_tekst": "Your button text",
        "knop_link": SHOP,
    },
    {
        "key": "plain",
        "name": "Plain message",
        "blurb": "Just words, in the house style. Often the one people reply to.",
        "subject": "",
        "preheader": "",
        "hook": "",
        "kop": "A message from %s" % config.SHOP_NAME,
        "subline": "Dear {{naam}},",
        "tekst": "Write your message here.\n"
                 "\n"
                 "Kind regards,\n"
                 "%s" % config.SHOP_NAME,
        "knop_tekst": "",
        "knop_link": "",
    },
]


# --- what the words say in another language -----------------------------------
#
# There is an English toggle beside the preview, for an owner who sends in a
# language she does not read herself. It looks up the exact string a layout
# ships and shows the reading in its place. Text somebody typed is left alone:
# a translation that has quietly gone stale is worse than none, because it is
# the one that gets trusted.
#
# The copy above is plain English already, so there is nothing to translate and
# this is empty. It is still wired up, so fill it in the day you rewrite this
# file in another language: one entry per shipped string, the string itself as
# the key, exactly as it appears above, line breaks and all.
#
#     ENGLISH = {
#         "<the shipped string, exactly as it appears above>":
#             "<what it says in English>",
#     }
#
# A missing entry is not an error. The toggle just shows the original.

ENGLISH = {}


# --- the abandoned checkout sequence -----------------------------------------
#
# Three emails, at the hours set in `config.FLOW_STEPS`. Like the cart layout
# above, these go out on a timer rather than by hand, so they ship as working
# sentences. Read them once in your own voice before you switch flows on.
#
# The escalation is deliberate and it is NOT three discounts. Step 1 is a
# reminder and assumes they were interrupted. Step 2 assumes something stopped
# them and offers a person to ask, which on an expensive order is usually a real
# question about size, material or delivery. Step 3 is the last one and says so,
# because a reminder that never ends is what gets a sender reported.

CART_SUBJECTS = [
    "Your order is not finished yet",
    "Any questions about your order?",
    "Your basket is waiting for you",
]

CART_PREHEADERS = [
    "Everything is still in your basket",
    "We are happy to think it through with you",
    "This is our last reminder",
]

CART_HEADINGS = [
    "You were nearly there",
    "Can we help with anything?",
    "Still being kept for you",
]

CART_SUBLINES = [
    "Dear %(naam)s, your basket is still waiting for you.",
    "Dear %(naam)s, is there anything you are unsure about?",
    "Dear %(naam)s, this is the last time we will write to you about this.",
]

CART_BODIES = [
    "You have not finished your order. Everything is still here, so you can "
    "carry on where you left off.",

    "Unsure about the size, the material or the delivery time? Send us a "
    "message and we will think it through with you.\n"
    "\n"
    "Would you rather see it first? You are welcome to come and visit us.",

    "Your basket is still waiting for you, but we cannot hold it forever. This "
    "is our last reminder.\n"
    "\n"
    "If you have a question, do send us a message.",
]

CART_BUTTONS = [
    "Finish your order",
    "Finish your order",
    "Finish your order",
]
