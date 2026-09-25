"""Observed HTTPS redirects, not guessed same-site ownership or arbitrary subdomains."""

import asyncio
import ipaddress
import socket
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit

import httpx

from tin_lite.organic_audit import AUDIT_POLICY, audit_hosts, public_site


async def resolve_site_identity(url, *, client=None, resolver=None):
    """Inspect headers only. Every hop is DNS-pinned; no cookies, credentials or HTML retained.

    A blocked site still gets its crawler audit. Failure to observe a redirect never
    authorizes an alias. An observed HTTPS www redirect does, even if a later hop fails.
    """
    url, host = public_site(url)
    own_client = client is None
    client = client or httpx.AsyncClient(trust_env=False, timeout=8, follow_redirects=False)
    resolver = resolver or asyncio.get_running_loop().getaddrinfo
    result = {
        "observed_at": datetime.now(UTC).isoformat(),
        "redirects": [],
        "status": "unavailable",
    }
    seen = set()
    try:
        async with asyncio.timeout(20):
            for _ in range(5):
                if url in seen:
                    result["status"] = "redirect_loop"
                    break
                seen.add(url)
                current_host = urlsplit(url).hostname
                addresses = await resolver(current_host, 443, type=socket.SOCK_STREAM)
                # Keep the resolver's route preference; lexical sorting can select unreachable IPv6.
                ips = list(dict.fromkeys(row[4][0] for row in addresses))
                if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
                    result["status"] = "non_public_address"
                    break
                async with client.stream(
                    "GET",
                    httpx.URL(url).copy_with(host=ips[0]),
                    headers={"Host": current_host, "User-Agent": "Tin-Organic-Audit/1.0"},
                    extensions={"sni_hostname": current_host},
                ) as response:
                    if response.status_code not in {301, 302, 303, 307, 308}:
                        result.update(status="observed", response_status=response.status_code)
                        break
                    target = urljoin(url, response.headers.get("location", ""))
                    hop = {"from": url, "to": target, "status_code": response.status_code}
                    try:
                        audit_hosts(
                            {
                                "url": f"https://{host}/",
                                "host": host,
                                "policy_version": AUDIT_POLICY["version"],
                                "site_identity": {"redirects": [*result["redirects"], hop]},
                            }
                        )
                    except ValueError:
                        result["status"] = "outside_site_redirect"
                        break
                    target_addresses = await resolver(
                        urlsplit(target).hostname, 443, type=socket.SOCK_STREAM
                    )
                    if not target_addresses or any(
                        not ipaddress.ip_address(row[4][0]).is_global for row in target_addresses
                    ):
                        result["status"] = "non_public_address"
                        break
                    result["redirects"].append(hop)
                    url = target
            else:
                result["status"] = "redirect_limit"
    except (httpx.HTTPError, OSError, TimeoutError, ValueError):
        # Never leak network exceptions/headers into the public report.
        pass
    finally:
        if own_client:
            await client.aclose()
    return result
