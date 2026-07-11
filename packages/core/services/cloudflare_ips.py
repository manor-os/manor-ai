"""Cloudflare-published egress IP ranges.

Real users on this deployment connect through their own ISP / mobile
carrier — the public IP recorded on a waitlist submit is theirs. A
submit whose origin IP falls inside Cloudflare's published ranges is
therefore one of:

* A Cloudflare Worker / Pages function scraping our public endpoint.
* A third-party monitor or bot routed through Cloudflare egress.
* A legit user behind Cloudflare WARP (consumer VPN) or Zero Trust.

The first two are spam; the third is a small but real false-positive
risk, so we don't block — callers use ``is_cloudflare_ip`` to tag
the source for admin review rather than reject the request.

The published lists at https://www.cloudflare.com/ips-v4 and
https://www.cloudflare.com/ips-v6 change roughly once a year. Re-sync
this file from those URLs when CF announces a new range.
"""
from __future__ import annotations

import ipaddress
from functools import lru_cache
from typing import Iterable, Optional

# Snapshot of Cloudflare's public ranges.
# IPv4 source: https://www.cloudflare.com/ips-v4
# IPv6 source: https://www.cloudflare.com/ips-v6
_CLOUDFLARE_IPV4 = (
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
)
_CLOUDFLARE_IPV6 = (
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)


def _parse_networks(cidrs: Iterable[str]):
    return tuple(ipaddress.ip_network(c) for c in cidrs)


@lru_cache(maxsize=1)
def _networks():
    return _parse_networks(_CLOUDFLARE_IPV4) + _parse_networks(_CLOUDFLARE_IPV6)


def is_cloudflare_ip(ip: Optional[str]) -> bool:
    """True when ``ip`` falls in any published Cloudflare range.

    Tolerates missing / unparsable input — returns False rather than
    raising. Caller can pass the raw X-Forwarded-For value or the
    socket ``client.host`` and never have to validate first.
    """
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return False
    for net in _networks():
        if addr in net:
            return True
    return False
