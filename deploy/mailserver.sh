#!/usr/bin/env bash
# Your own sending route: Postfix that delivers straight to the receiving mail
# server, signing every message with your own DKIM key. No Brevo, no SendGrid,
# nobody in the middle.
#
#   DOMAIN=example.com SERVER_IP=203.0.113.10 \
#   KEY=~/.ssh/id_ed25519 HOST=you@your-server bash deploy/mailserver.sh
#
# WHAT IT SETS UP
#   * Postfix, listening on 127.0.0.1 ONLY. Nothing on this machine is reachable
#     from the internet on port 25, so it can never be used as an open relay.
#   * OpenDKIM, signing anything from $DOMAIN with the key it generates here.
#   * A sending hostname of its own, kept apart from the mail host the domain
#     already uses, so the two cannot damage each other's reputation.
#
# WHAT IT DOES NOT DO
# It prints the DNS records at the end. They have to be added wherever the
# nameservers for your domain live, by hand, because this holds no API key for
# your registrar. Until they are published, mail from here fails DKIM and is not
# authorised by SPF, so DO NOT send to real customers before the check at the
# bottom of this script passes.
set -euo pipefail

# Nothing here falls back to a working value. A default that is somebody's real
# server or somebody's real domain is a script that quietly configures the wrong
# machine, and this one runs as root and rewrites a mail system.
KEY=${KEY:?set KEY to the ssh private key that reaches the server}
HOST=${HOST:?set HOST to user@your-server}
DOMAIN=${DOMAIN:?set DOMAIN to the domain you send from, for example example.com}
# The public IPv4 address of that server. It goes in the A record, in SPF and in
# the reverse DNS, so guessing it wrong means mail that authenticates as nobody.
SERVER_IP=${SERVER_IP:?set SERVER_IP to the public IPv4 address of the server}
MODE=${1:-install}
SELECTOR=${SELECTOR:-mail1}
SENDING_NAME=${SENDING_NAME:-mail2.$DOMAIN}
# Marketing sends as this; the webshop and anything else keep the bare domain.
# Bounces come back here too, so Postfix has to accept mail for it.
MARKETING=${MARKETING:-news.$DOMAIN}
# The service user and data directory from deploy/provision.sh. The bounce
# mailbox belongs to the mailer, so these have to be the same values.
SERVICE_USER=${SERVICE_USER:-mailer}
DATA_DIR=${DATA_DIR:-/var/lib/mailer}
# The authoritative nameserver for your domain, for the check below. Worth
# setting: the machine's own resolver is a cache, and a cache served a record an
# hour after it changed and made a finished job look unfinished twice. Left
# empty the check asks that resolver and may be reading yesterday's answer.
AUTH_NS=${AUTH_NS:-}

if [ "$MODE" = "check" ]; then
  # Does the outside world agree with what was set up here? Every one of these
  # has to pass before a single customer is emailed from this machine.
  # Asked from the server, which is both where dig lives and where the mail
  # actually leaves from.
  NS=${AUTH_NS:+@$AUTH_NS}
  ssh -i "$KEY" "$HOST" "
    echo '--- A record for the sending name'
    dig +short ${NS} ${SENDING_NAME}
    echo '--- SPF (must list ip4:${SERVER_IP})'
    dig +short ${NS} TXT ${DOMAIN} | grep -i spf1 || echo 'NO SPF RECORD'
    echo '--- DKIM published'
    dig +short ${NS} TXT ${SELECTOR}._domainkey.${DOMAIN} | head -c 90; echo
    echo '--- published key matches the private half here'
    sudo opendkim-testkey -d ${DOMAIN} -s ${SELECTOR} -vvv 2>&1 | tail -2
    echo '--- reverse DNS of the sending IP (want ${SENDING_NAME})'
    dig +short ${NS} -x ${SERVER_IP}
    echo '--- marketing subdomain SPF'
    dig +short ${NS} TXT ${MARKETING} | grep -i spf1 || echo 'NOT SET'
    echo '--- marketing subdomain MX (where its bounces come home)'
    dig +short ${NS} MX ${MARKETING} || echo 'NOT SET'
    echo '--- bounce name MX'
    dig +short ${NS} MX ${SENDING_NAME} || echo 'NOT SET'
    echo '--- how old the signing key is'
    # Nothing forces a rotation, so the only way this gets noticed is if it is
    # printed next to everything else that gets checked.
    sudo stat -c '%y' /etc/opendkim/keys/${DOMAIN}/${SELECTOR}.private | cut -d' ' -f1
    echo '--- can the mailer actually READ a delivered bounce'
    sudo -u ${SERVICE_USER} bash -c 'printf probe > ${DATA_DIR}/Maildir/new/.probe && chmod 600 ${DATA_DIR}/Maildir/new/.probe'
    sudo -u ${SERVICE_USER} cat ${DATA_DIR}/Maildir/new/.probe >/dev/null 2>&1 && echo '  yes' || echo '  NO, bounce handling is blind'
    sudo rm -f ${DATA_DIR}/Maildir/new/.probe
    echo '--- anything stuck in the queue'
    mailq | tail -3
  "
  exit 0
fi


ssh -i "$KEY" "$HOST" "sudo bash -s" <<REMOTE
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "--- packages"
# postfix asks a question during install unless it is told the answers first.
debconf-set-selections <<< "postfix postfix/main_mailer_type select Internet Site"
debconf-set-selections <<< "postfix postfix/mailname string ${SENDING_NAME}"
apt-get update -qq
apt-get install -y -qq postfix opendkim opendkim-tools >/dev/null

echo "--- dkim key"
install -d -o opendkim -g opendkim -m 750 /etc/opendkim/keys/${DOMAIN}
if [ ! -f /etc/opendkim/keys/${DOMAIN}/${SELECTOR}.private ]; then
  opendkim-genkey -b 2048 -d ${DOMAIN} -s ${SELECTOR} \
                  -D /etc/opendkim/keys/${DOMAIN}
  chown opendkim:opendkim /etc/opendkim/keys/${DOMAIN}/${SELECTOR}.private
  chmod 600 /etc/opendkim/keys/${DOMAIN}/${SELECTOR}.private
fi

echo "--- opendkim"
cat > /etc/opendkim.conf <<'EOF'
# Sign our own outgoing mail. Verification is off: nothing arrives here.
Syslog                  yes
UMask                   007
Mode                    s
Canonicalization        relaxed/simple
SubDomains              no
OversignHeaders         From
Socket                  inet:8891@127.0.0.1
PidFile                 /run/opendkim/opendkim.pid
UserID                  opendkim
KeyTable                /etc/opendkim/key.table
SigningTable            refile:/etc/opendkim/signing.table
InternalHosts           /etc/opendkim/trusted.hosts
EOF
echo "${SELECTOR}._domainkey.${DOMAIN} ${DOMAIN}:${SELECTOR}:/etc/opendkim/keys/${DOMAIN}/${SELECTOR}.private" \
  > /etc/opendkim/key.table
# Both domains, ONE key. The signature says d=${DOMAIN} while the From says
# @${MARKETING}, and DMARC's relaxed alignment counts that as aligned because
# the two share an organisational domain. Miss this line and marketing mail
# goes out unsigned, which is worse than not having a subdomain at all.
{
  echo "*@${DOMAIN} ${SELECTOR}._domainkey.${DOMAIN}"
  echo "*@${MARKETING} ${SELECTOR}._domainkey.${DOMAIN}"
} > /etc/opendkim/signing.table
printf '127.0.0.1\nlocalhost\n%s\n' "${DOMAIN}" > /etc/opendkim/trusted.hosts
chown -R opendkim:opendkim /etc/opendkim
systemctl enable --now opendkim >/dev/null
systemctl restart opendkim

echo "--- postfix"
postconf -e "myhostname = ${SENDING_NAME}"
postconf -e "myorigin = ${DOMAIN}"
# It has to be reachable now, because a bounce is an email and it has to be
# able to arrive. What keeps this off every open-relay list is the pair below:
# mynetworks is ONLY this machine, and mydestination is ONLY our own bounce
# name. Postfix refuses anything addressed elsewhere.
#
# mynetworks is spelled out rather than left at the default: the default is
# "the local subnet", which on a hosting provider means every other customer
# on the same rack could relay through us.
postconf -e "inet_interfaces = all"
postconf -e "mynetworks = 127.0.0.0/8 [::1]/128"
postconf -e "mydestination = localhost, ${SENDING_NAME}, ${MARKETING}"
postconf -e "smtpd_relay_restrictions = permit_mynetworks reject_unauth_destination"
postconf -e "smtpd_recipient_restrictions = permit_mynetworks reject_unauth_destination"
# Bounces are addressed to bounce+<token>@, so the plus part has to survive.
postconf -e "recipient_delimiter = +"
# Everything that arrives for our bounce name goes to one mailbox, whatever the
# local part says. A DSN is generated by the FAR end and can be addressed
# oddly, and a bounce we refuse is a dead address we never learn about.
postconf -e "local_recipient_maps ="
# Delivered to the account that READS them. It used to be a bounce user of its
# own, and Postfix writes each message 0600, so the mailer could list the
# mailbox and not open a single file in it. Group membership does not help
# against mode 0600. One user, no permission question.
postconf -e "luser_relay = ${SERVICE_USER}"
postconf -e "home_mailbox = Maildir/"
# Nothing here should ever try to be a real mail host for people.
postconf -e "smtpd_banner = ${SENDING_NAME} ESMTP"
postconf -e "smtp_tls_security_level = may"
postconf -e "smtp_tls_loglevel = 1"
postconf -e "smtpd_tls_security_level = none"
# Talk to the receiving server as our sending name, not as the machine's.
postconf -e "smtp_helo_name = ${SENDING_NAME}"
# IPv4 only. The server has an IPv6 address, Postfix would prefer it, and Gmail
# hard-rejects IPv6 mail from an address whose reverse DNS does not match. One
# reverse DNS entry to get right instead of two.
postconf -e "inet_protocols = ipv4"
# Every message goes through the signer before it leaves.
postconf -e "milter_default_action = accept"
postconf -e "milter_protocol = 6"
postconf -e "smtpd_milters = inet:127.0.0.1:8891"
postconf -e "non_smtpd_milters = inet:127.0.0.1:8891"
# One hop at a time. A shop this size does not need 20 parallel connections to
# one provider, and going slowly is what a new address has to do anyway.
postconf -e "default_destination_concurrency_limit = 2"
postconf -e "default_destination_rate_delay = 2s"
echo "--- the mailbox bounces land in"
# Owned by the mailer itself. review/ is where anything that is not a bounce
# goes, so one out-of-office reply cannot sit at the front of new/ for ever
# and stop the scan window from advancing.
install -d -o ${SERVICE_USER} -g ${SERVICE_USER} -m 700 ${DATA_DIR}/Maildir \
    ${DATA_DIR}/Maildir/new ${DATA_DIR}/Maildir/cur \
    ${DATA_DIR}/Maildir/tmp ${DATA_DIR}/Maildir/review
# Postfix needs to traverse into it to deliver.
chmod 751 ${DATA_DIR}

systemctl enable --now postfix >/dev/null
systemctl restart postfix

echo "--- port 25 open inbound"
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 25/tcp >/dev/null && echo "ufw: allowed"
fi

echo "--- state"
systemctl is-active opendkim postfix
ss -lntp | grep ':25 ' || true
REMOTE

echo
echo "============ DNS, to add where your domain's nameservers live ==========="
echo
echo "1. A record, so the sending name resolves:"
echo "   ${SENDING_NAME}.   A   ${SERVER_IP}"
echo
echo "2. SPF, REPLACE the existing v=spf1 record with this one (it adds only"
echo "   the new server, everything else is unchanged):"
echo "   ${DOMAIN}.   TXT   \"v=spf1 mx a:${DOMAIN} ip4:${SERVER_IP} -all\""
echo
echo "3. DKIM, the public half of the key just made. ONE line: paste the"
echo "   whole value, starting at v=DKIM1, and nothing else:"
ssh -i "$KEY" "$HOST" "sudo cat /etc/opendkim/keys/${DOMAIN}/${SELECTOR}.txt" \
  | tr -d '\n\t' | sed 's/.*(\(.*\)).*/\1/' | tr -d '"' | sed 's/ //g' \
  | sed "s|^|   ${SELECTOR}._domainkey.${DOMAIN}   TXT   |"
echo
echo "4. An MX for the bounce name, so a rejected email can find its way home:"
echo "   ${SENDING_NAME}.   MX   10   ${SENDING_NAME}."
echo
echo "5. The marketing subdomain. Its own SPF, and an MX so ITS bounces come"
echo "   home too. No DKIM record: the key above already covers it."
echo "   ${MARKETING}.   TXT   \"v=spf1 ip4:${SERVER_IP} -all\""
echo "   ${MARKETING}.   MX    10   ${SENDING_NAME}."
echo
echo "6. In your hosting provider's panel, set the reverse DNS (PTR) of"
echo "   ${SERVER_IP} to ${SENDING_NAME}. Microsoft in particular checks this."
echo
echo "Then run: bash deploy/mailserver.sh check"
echo
echo "When every line of that check passes, and NOT before, move the mailer onto"
echo "this route. Until then it keeps using whatever relay SMTP_HOST already"
echo "points at, which at least passes SPF today:"
echo "   SMTP_HOST=localhost   SMTP_PORT=25   SMTP_USER=   SMTP_PASS="
echo "   SMTP_STARTTLS=false      (in /etc/mailer.env, then restart)"
