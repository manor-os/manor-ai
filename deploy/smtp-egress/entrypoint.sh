#!/bin/sh
set -eu

role="${1:-stunnel-client}"

case "$role" in
  stunnel-client)
    : "${SMTP_EGRESS_REMOTE_HOST:?SMTP_EGRESS_REMOTE_HOST is required}"
    : "${SMTP_EGRESS_REMOTE_PORT:=8443}"
    : "${SMTP_EGRESS_CLIENT_CONFIG:=/etc/manor/stunnel/client.conf.template}"
    : "${SMTP_EGRESS_RENDERED_CONFIG:=/tmp/stunnel-client.conf}"
    export SMTP_EGRESS_REMOTE_HOST SMTP_EGRESS_REMOTE_PORT
    envsubst <"$SMTP_EGRESS_CLIENT_CONFIG" >"$SMTP_EGRESS_RENDERED_CONFIG"
    exec stunnel "$SMTP_EGRESS_RENDERED_CONFIG"
    ;;
  stunnel-server)
    exec stunnel /etc/manor/stunnel/server.conf
    ;;
  danted)
    exec danted -f /etc/manor/danted/danted.conf -p /tmp/danted.pid
    ;;
  *)
    echo "unknown SMTP egress role: $role" >&2
    exit 2
    ;;
esac
