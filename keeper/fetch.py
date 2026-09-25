"""Fetching web pages safely.

Keeper fetches URLs that come from outside: links you share, and URLs a model read off a
screenshot (which a malicious image could plant). To stop those requests from reaching
machines on your own network (router admin pages, cloud metadata endpoints, other
services on the server), every hop, redirects included, must resolve to a public IP
address. Set KEEPER_ALLOW_PRIVATE_URLS=true to allow LAN addresses on purpose.
"""

import asyncio
import ipaddress
import logging
import os
import socket
from urllib.parse import urljoin, urlsplit

import httpx

log = logging.getLogger(__name__)

MAX_REDIRECTS = 5
MAX_BYTES = 3_000_000


class BlockedURL(Exception):
    pass


def _allow_private() -> bool:
    return os.environ.get("KEEPER_ALLOW_PRIVATE_URLS", "").lower() in ("1", "true", "yes", "on")


async def resolve_host(host: str) -> list[str]:
    infos = await asyncio.to_thread(socket.getaddrinfo, host, None, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


async def check_url(url: str) -> None:
    """Raise BlockedURL unless the URL is http(s) and its host resolves only to public addresses."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BlockedURL(f"Only http(s) URLs can be fetched: {url[:100]}")
    if _allow_private():
        return
    host = parts.hostname
    try:
        addresses = [host] if _is_ip(host) else await resolve_host(host)
    except OSError as e:
        raise BlockedURL(f"Can't resolve {host}: {e}") from e
    for addr in addresses:
        ip = ipaddress.ip_address(addr.split("%")[0])
        if not ip.is_global:
            raise BlockedURL(f"{host} resolves to a non-public address ({ip}); set KEEPER_ALLOW_PRIVATE_URLS=true to allow")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


async def safe_get(http: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    """GET with the address check applied to the URL and every redirect."""
    headers = kwargs.pop("headers", None)
    for _ in range(MAX_REDIRECTS + 1):
        await check_url(url)
        r = await http.get(url, headers=headers, follow_redirects=False, **kwargs)
        if r.is_redirect and r.headers.get("location"):
            url = urljoin(str(r.url), r.headers["location"])
            continue
        return r
    raise httpx.TooManyRedirects(f"More than {MAX_REDIRECTS} redirects", request=r.request)
