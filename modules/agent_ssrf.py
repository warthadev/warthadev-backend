from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit, urlunsplit

import aiohttp
from fastapi import HTTPException


@dataclass(frozen=True)
class RouterTarget:
    base_url: str
    host: str
    port: int
    addresses: tuple[str, ...]


def normalize_router_base_url(value: str) -> tuple[str, str, int]:
    raw = value.strip()
    if not raw or len(raw) > 2048 or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise HTTPException(status_code=400, detail="Enter a valid public HTTPS router base URL")
    try:
        parsed = urlsplit(raw)
        host_value = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="The router URL is malformed") from exc

    if parsed.scheme.lower() != "https" or not host_value:
        raise HTTPException(status_code=400, detail="The router URL must use HTTPS and include a hostname")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise HTTPException(status_code=400, detail="The router URL cannot contain credentials, query parameters, or a fragment")
    if port not in (None, 443):
        raise HTTPException(status_code=400, detail="The router URL must use the standard HTTPS port")

    try:
        ipaddress.ip_address(host_value.rstrip("."))
    except ValueError:
        pass
    else:
        raise HTTPException(status_code=400, detail="Use a public DNS hostname, not an IP address")

    try:
        host = host_value.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise HTTPException(status_code=400, detail="The router hostname is invalid") from exc
    if len(host) > 253 or "." not in host or host in {"localhost", "localhost.localdomain"}:
        raise HTTPException(status_code=400, detail="The router hostname must be a public DNS name")
    blocked_suffixes = (".localhost", ".local", ".internal", ".lan", ".home", ".test", ".example", ".invalid", ".onion")
    if host.endswith(blocked_suffixes):
        raise HTTPException(status_code=400, detail="Local or special-use hostnames are not allowed")
    labels = host.split(".")
    if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
        raise HTTPException(status_code=400, detail="The router hostname is invalid")

    try:
        path = unquote(parsed.path or "", errors="strict")
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="The router URL path is invalid") from exc
    if "\\" in path or "//" in path or any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise HTTPException(status_code=400, detail="The router URL path is invalid")
    if any(part in {".", ".."} for part in path.split("/")):
        raise HTTPException(status_code=400, detail="Dot segments are not allowed in the router URL path")
    normalized_path = "/" + path.strip("/") if path.strip("/") else ""
    base_url = urlunsplit(("https", host, normalized_path, "", "")).rstrip("/")
    return base_url, host, 443


async def resolve_public_addresses(host: str, port: int) -> tuple[str, ...]:
    try:
        loop = asyncio.get_running_loop()
        records = await asyncio.wait_for(
            loop.getaddrinfo(
                host,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            ),
            timeout=5,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise HTTPException(status_code=400, detail="The router hostname could not be resolved") from exc

    addresses: list[str] = []
    for family, _socktype, _proto, _canonname, sockaddr in records:
        raw_ip = str(sockaddr[0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="The router hostname resolved to an invalid address") from exc
        if not ip.is_global:
            raise HTTPException(status_code=400, detail="The router hostname must resolve only to public IP addresses")
        normalized = str(ip)
        if normalized not in addresses:
            addresses.append(normalized)

    if not addresses:
        raise HTTPException(status_code=400, detail="The router hostname has no public IP addresses")
    return tuple(addresses)


async def validate_router_base_url(value: str) -> RouterTarget:
    base_url, host, port = normalize_router_base_url(value)
    addresses = await resolve_public_addresses(host, port)
    return RouterTarget(base_url=base_url, host=host, port=port, addresses=addresses)


class PinnedPublicResolver(aiohttp.abc.AbstractResolver):
    """Resolve only the previously validated host to its pinned public addresses."""

    def __init__(self, target: RouterTarget):
        self._host = target.host
        self._port = target.port
        self._addresses = target.addresses

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_UNSPEC) -> list[dict]:
        if host.lower().rstrip(".") != self._host or port != self._port:
            raise OSError("Unvalidated network destination")
        results: list[dict] = []
        for address in self._addresses:
            ip = ipaddress.ip_address(address)
            address_family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
            if family not in (socket.AF_UNSPEC, address_family):
                continue
            results.append({
                "hostname": host,
                "host": address,
                "port": port,
                "family": address_family,
                "proto": socket.IPPROTO_TCP,
                "flags": socket.AI_NUMERICHOST,
            })
        if not results:
            raise OSError("No pinned address for this address family")
        return results

    async def close(self) -> None:
        return None


def make_pinned_connector(target: RouterTarget) -> aiohttp.TCPConnector:
    return aiohttp.TCPConnector(
        resolver=PinnedPublicResolver(target),
        use_dns_cache=False,
        family=socket.AF_UNSPEC,
        ssl=True,
        limit=4,
        limit_per_host=2,
    )
