# Email marketing

A small standalone newsletter tool. Replaces what Klaviyo was being paid for.

It is **separate from the CRM on purpose**: its own folder, its own SQLite file,
its own login, its own sending switch. Nothing here can reach the CRM database,
and a mistake in one cannot take the other down.

## Running it

First time:

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env
```

Then edit `.env`, and from that point on:

```
run.bat
```

It comes up on <http://127.0.0.1:5000>.

## What it does

- Holds a subscriber list with **consent, and where that consent came from**
- Imports from the webshop (only people Shopify marks `SUBSCRIBED`) or a pasted CSV
- Writes a campaign in HTML, with `{{naam}}` replaced by the subscriber's name
- Sends a test to yourself first, then the real thing in batches
- Tracks opens and clicks, and handles unsubscribes

## The rule the whole thing is built around

**An email address is not consent.** Shopify knows 404 customers; 248 of them
ticked the box. The CRM holds 5,904 addresses; 184 ticked the box. The rest gave
their address so a sofa could be delivered. Only people who said yes are ever
mailable, and an import can add people but can never resurrect somebody who
unsubscribed.

## The six guards

There is no undo on a sent email, so the tool is built to refuse rather than to
be helpful:

1. `SENDING_ENABLED` is separate from whether SMTP works, and defaults to off.
   Getting the mailbox working must not by itself be what mails 300 people.
2. Nothing sends on a trigger or a timer. A person presses a button. There is no
   scheduler in here at all.
3. Recipients are frozen into rows **before** the first message leaves, so a
   crash or a pause resumes rather than restarting and mailing everyone twice.
4. A unique index refuses a duplicate even when the code asks for one.
5. `DAILY_CAP` bounds a runaway to one day of sending.
6. A campaign will not send while `PUBLIC_URL` is localhost, because an
   unsubscribe link pointing at your laptop is illegal as well as useless.

Consent is re-checked at the moment of sending, not only when the queue was
built. The queue is frozen; consent is not, and consent wins.

## Deliverability

Every message carries `List-Unsubscribe` and `List-Unsubscribe-Post: One-Click`
(Gmail and Yahoo require these from bulk senders), a plain-text part as well as
HTML, and the postal address in the footer. The unsubscribe page needs no login
and works on a bare POST, because making somebody log in to leave a list gets you
reported as spam instead.

The From address is set in `.env`, and it must be on a domain whose SPF, DKIM
and DMARC are published and passing. Nothing else in here matters if that is
not true.

**Warm up.** A new sending domain has no reputation at all. Do not send
300 in one go the first time. Start with the most recent subscribers, check the
open rate, then widen. The reputation you are protecting also carries the
webshop's order confirmations.

## Sending for real

Tracking links and the unsubscribe link have to be reachable from a recipient's
inbox, which a laptop is not. So for a real campaign this needs to run somewhere
public with `PUBLIC_URL` set to that address. Until then everything except the
final send works locally, which is enough to write, import and preview.

## Tests

```
.venv\Scripts\python -m unittest discover -s tests
```

24 tests. Nearly all of them check that the tool refuses something.

## Layout

```
app.py              Flask routes. The three at the bottom (/p /c /u) are public.
mailer/config.py    settings, all defaulting to the safe choice
mailer/db.py        SQLite schema and the only place consent is ever written
mailer/send.py      the guards, rendering, and the resumable send loop
mailer/sources.py   Shopify and CSV import
templates/          the pages
tests/              what it must refuse to do
```
