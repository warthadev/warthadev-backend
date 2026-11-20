import asyncio
import socket
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from modules import agent_ssrf


class _FakeLoop:
    def __init__(self, records):
        self.records = records
        self.calls = []

    async def getaddrinfo(self, host, port, **kwargs):
        self.calls.append((host, port, kwargs))
        return self.records


def _ipv4_record(address):
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))


def _ipv6_record(address):
    return (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443, 0, 0))


class RouterUrlValidationTests(unittest.IsolatedAsyncioTestCase):
    def test_normalizes_safe_url_and_rejects_non_https_or_ambiguous_urls(self):
        self.assertEqual(
            agent_ssrf.normalize_router_base_url(" HTTPS://Router.Example.com:443/v1/ "),
            ("https://router.example.com/v1", "router.example.com", 443),
        )
        invalid_urls = [
            "http://router.example.com/v1",
            "https://user:pass@router.example.com/v1",
            "https://router.example.com/v1?token=secret",
            "https://router.example.com/v1#fragment",
            "https://router.example.com:8443/v1",
            "https://127.0.0.1/v1",
            "https://[::1]/v1",
            "https://localhost/v1",
            "https://router.local/v1",
            "https://router.example.com/a/../v1",
            "https://router.example.com/%2e%2e/v1",
            "https://router.example.com/a%2f%2fb",
            "https://bad host.example.com/v1",
        ]
        for url in invalid_urls:
            with self.subTest(url=url), self.assertRaises(HTTPException):
                agent_ssrf.normalize_router_base_url(url)

    async def test_dns_resolution_rejects_private_and_link_local_addresses(self):
        for record in (_ipv4_record("127.0.0.1"), _ipv4_record("169.254.169.254"), _ipv6_record("::1"), _ipv6_record("fe80::1")):
            loop = _FakeLoop([record])
            with self.subTest(record=record), patch.object(agent_ssrf.asyncio, "get_running_loop", return_value=loop):
                with self.assertRaises(HTTPException) as raised:
                    await agent_ssrf.resolve_public_addresses("router.example.com", 443)
            self.assertEqual(raised.exception.status_code, 400)

    async def test_validated_dns_address_is_pinned_and_other_hosts_are_rejected(self):
        loop = _FakeLoop([_ipv4_record("8.8.8.8")])
        with patch.object(agent_ssrf.asyncio, "get_running_loop", return_value=loop):
            target = await agent_ssrf.validate_router_base_url("https://router.example.com/v1")

        self.assertEqual(target.addresses, ("8.8.8.8",))
        resolver = agent_ssrf.PinnedPublicResolver(target)
        result = await resolver.resolve("router.example.com", 443, socket.AF_UNSPEC)
        self.assertEqual([item["host"] for item in result], ["8.8.8.8"])
        self.assertEqual(result[0]["flags"], socket.AI_NUMERICHOST)
        with self.assertRaises(OSError):
            await resolver.resolve("attacker.example.com", 443, socket.AF_UNSPEC)
        with self.assertRaises(OSError):
            await resolver.resolve("router.example.com", 80, socket.AF_UNSPEC)
        await resolver.close()

    async def test_all_dns_results_must_be_public(self):
        loop = _FakeLoop([_ipv4_record("8.8.8.8"), _ipv4_record("10.0.0.4")])
        with patch.object(agent_ssrf.asyncio, "get_running_loop", return_value=loop):
            with self.assertRaises(HTTPException) as raised:
                await agent_ssrf.resolve_public_addresses("router.example.com", 443)
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("only to public", raised.exception.detail)

    async def test_dns_failure_is_rejected(self):
        class FailingLoop:
            async def getaddrinfo(self, *_args, **_kwargs):
                raise OSError("no DNS")

        with patch.object(agent_ssrf.asyncio, "get_running_loop", return_value=FailingLoop()):
            with self.assertRaises(HTTPException) as raised:
                await agent_ssrf.resolve_public_addresses("router.example.com", 443)
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("could not be resolved", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()
