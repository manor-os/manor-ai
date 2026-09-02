FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    dante-server \
    gettext-base \
    stunnel4 \
    tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 manor \
    && useradd --uid 10001 --gid 10001 --create-home manor

COPY deploy/smtp-egress/entrypoint.sh /usr/local/bin/manor-smtp-egress
RUN chmod 0755 /usr/local/bin/manor-smtp-egress

USER 10001:10001

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/manor-smtp-egress"]
CMD ["stunnel-client"]
