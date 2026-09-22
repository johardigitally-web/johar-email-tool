"""Everything the tool should do on its own, in one run.

Called by a systemd timer every fifteen minutes. Guard 2 of this tool used to be
"nothing sends on a trigger or a timer; a person presses a button", and this is
the thing that breaks it, so it is written to be as boring as possible:

* It does exactly what the buttons on the screens do, in the same order, through
  the same functions. There is no separate code path that only runs unattended.
* Every brake still applies. `SENDING_ENABLED`, `FLOWS_ENABLED`, `FLOWS_FROM`,
  the allowlist, the daily cap and the one-email-a-day rule are all checked
  inside those functions, per message. With sending switched off this run does
  the reading and sends nothing at all.
* It prints what it did to the journal, so `journalctl -u mailer-cron` is a
  history of every automatic decision.

    python cron.py            everything
    python cron.py bounces    just one part, for testing
"""
import sys
import traceback

from mailer import attrib, bounces, config, crm, db, flows

#: How often the CRM is re-read. It reads every customer and every sale out of
#: Postgres and rewrites several thousand rows here, and the answer does not
#: change four times an hour.
CRM_MINUTES = 60


def step(name, fn, conn):
    try:
        out = fn(conn)
    except Exception as exc:                                   # noqa: BLE001
        # One part failing must not stop the rest. A Shopify outage should not
        # mean bounces go unread for a day.
        print("%-9s FAILED %s: %s" % (name, type(exc).__name__, str(exc)[:200]))
        traceback.print_exc()
        return
    print("%-9s %s" % (name, out))


def _crm(conn):
    if not crm.configured():
        return "not configured"
    if not db.due(conn, "crm_sync", CRM_MINUTES):
        return "not due"
    return crm.sync(conn)


def main(only=""):
    conn = db.connect()
    try:
        parts = [
            # Read what came back first: an address that died overnight should
            # not be emailed again this morning.
            ("bounces", lambda c: bounces.scan(c)),
            # Then who bought in the SHOWROOM, which is most of the money and
            # none of it visible to Shopify. Before the flows are polled and
            # before anything is sent, so somebody who walked in yesterday and
            # bought the sofa is not emailed a discount code for it today.
            ("crm", _crm),
            # Who has just triggered something.
            ("poll", lambda c: flows.poll(c)),
            # And what is due for them. Blocked entirely while sending is off.
            ("send", lambda c: flows.run(c)),
            # Who bought afterwards, so the numbers on Results stay current and
            # nobody in a flow gets chased about a thing they already own.
            ("orders", lambda c: attrib.match(c)),
        ]
        print("sending=%s flows=%s from=%s allowlist=%s"
              % (config.SENDING_ENABLED, config.FLOWS_ENABLED,
                 config.FLOWS_FROM or "(unset)",
                 ",".join(config.ALLOWED_RECIPIENTS) or "(open)"))
        for name, fn in parts:
            if only and only != name:
                continue
            step(name, fn, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
