# Sandbox forward proxy security (SEC-01)

The switchboard uses a TLS-only Squid forward proxy and a separate nftables
destination fence. This replaces the plaintext Tinyproxy listener and its shared
password. Hosted runtime `7bc9296` passed live proxy acceptance on September 17, 2026;
see the [deployment record](oauth-credential-security.md#hosted-acceptance).

## Destination boundary

`infra/proxy_policy.py` renders both policies. Squid resolves destination names and
rejects loopback, private, link-local, metadata, shared-address, documentation,
multicast, reserved and IPv6 translation/tunnel ranges. Ordinary HTTP is limited to
ports 80/443; CONNECT is limited to 443. IPv6 is restricted to global unicast space,
with special-purpose ranges excluded. The switchboard's external IP is also denied.

The kernel independently restricts sockets owned by the proxy UID to public TCP
80/443. It rejects connections routed through loopback, including the host's own
public interfaces. This check applies to the actual destination of every outgoing
packet, so DNS rebinding, an ACL error or a redirect cannot grant internal access.
Only replies to inbound connections and TCP/UDP DNS to the explicitly configured
resolvers are exceptions. GCE can use its metadata address as a DNS resolver; this
allows DNS on port 53 only, never its HTTP service. Squid's unused pinger is disabled
so it needs no local-network exception or additional capabilities.

The firewall owns only the `inet tin_lite_proxy` table. It does not flush or replace
other firewall state. The proxy service runs entirely as the unprivileged `proxy`
user, with no capabilities, and requires the firewall service before starting.
Stopping the firewall service does not remove its rules. No cloud network or
existing fleet firewall rule is changed by this patch.

Public web access remains available because Codex tools fetch public sources and
Git repositories. A provider-only domain allowlist would break that contract.
The accepted browser profile retains its independent open-egress browsing path;
requests through the switchboard proxy receive the same destination restrictions.
Service/API relay traffic continues to use the canonical HTTPS origin via `NO_PROXY`.

## Authentication and lifetime

`TIN_LITE_PROXY_URL` is now a credential-free HTTPS origin:

```text
TIN_LITE_PROXY_URL=https://tin-lite-proxy.<dashed-static-ip>.sslip.io:8888
TIN_LITE_PROXY_GRANT_DIR=/var/lib/tin-lite-proxy-grants
```

The installer creates the grant directory with owner `tinlite`, group `proxy`, and
mode `2750`. Each design, task turn or procedure invocation gets a random 256-bit
bearer credential. Its SHA-256 filename indexes a local record containing the
execution key, sandbox ID and expiry. Grant files are `0640`; the cleartext token
is never persisted there. Model-service credentials remain on the switchboard.

The E2B runtime inserts the scoped URL into the existing proxy environment variables
and rollout redaction set. Command errors are also redacted before reaching activity
receipts or Temporal failures, with raw exception chaining suppressed. Grants expire
after the invocation's timeout plus 60
seconds (maximum 7260 seconds; 3660 before October 2, 2026, and the proxy host's own copy of
`proxy_grants.py` must be reinstalled before it accepts the longer grants) and are deleted on
return, exception or cancellation,
including failed sandbox cleanup. Failure to create a grant kills the already-created
sandbox without starting a command. Worker startup removes orphaned grants. A
crashed worker's grants remain bounded by expiry until that cleanup runs.

Squid checks the grant helper with a maximum one-second authentication cache. Zero
seconds does not work with the supported Squid 5 Basic-auth implementation. New
requests fail after expiry/removal and that cache interval. An already established
CONNECT tunnel is not reauthenticated midstream; it remains subject to the network
fence and the one-hour connection lifetime. Normal sandbox deletion closes its
connections. This is not a claim of instantaneous termination of a copied, already
open tunnel.

The grant directory is intentionally local to the existing single switchboard,
with its local grant store. A future multi-host deployment needs a shared authorization
and revocation design before issuing grants across hosts.

TLS terminates at Squid without decrypting the tunneled provider traffic. Certbot
obtains and renews a certificate for the dedicated proxy hostname through Caddy's
HTTP ACME webroot. Other paths on that HTTP host return 404. The renewal hook copies
only this certificate/key into the proxy's protected directory and reloads Squid.
The proxy never offers a plaintext listener or an insecure-certificate fallback.
Settings reject the old HTTP URL, embedded static credentials and a missing grant
directory. Dashboard, MCP, callbacks and canonical service URLs are unchanged.

## Verification

```sh
uv run pytest -q tests/test_proxy_grants.py tests/test_deployment_runtime.py tests/test_runtime.py
python3 scripts/verify_forward_proxy.py
```

The second command requires Docker. It builds only `tests/proxy`, creates disposable
containers with no Internet routes, and removes its containers and network on exit.
All addresses, certificates, grants and the metadata endpoint are controlled test
fixtures. The image remains cached for subsequent runs. The `proxy-security` CI job
runs this same proof.

The proof uses the generated Squid/nftables policies, shortening only DNS cache
timers. It checks public HTTP/HTTPS and IPv6 success; private, localhost, metadata,
alternate IPv4 encodings, IPv4-mapped IPv6, translation prefixes and local public
addresses; redirects; DNS rebinding; blocked ports; TLS certificate verification;
anonymous, legacy, expired and revoked credentials; and authentication reuse on a
client connection. It also attempts direct sockets as the proxy UID independently
of Squid, verifies that another firewall table survives reinstallation, and checks
that controlled origins observed no forbidden request.

## Hosted rollout and rollback

1. Drain active sandbox executions and pause dispatch during the infrastructure and
   application cutover. Existing sandboxes carry the old proxy URL and cannot be
   migrated in place. Do not enable a compatibility plaintext listener.
2. Deploy the reviewed application and installer together. The installer stops and
   masks Tinyproxy first, and masks the distribution's default Squid service before
   package installation. Certificate or firewall validation failure leaves the old
   insecure listener disabled. The new service is `tin-lite-proxy`.
3. Verify certificate issuance/renewal, the proxy service's unprivileged UID and
   required firewall dependency, and that only TLS is accepted on 8888. Run the
   destination checks against controlled disposable targets, never production
   metadata or customer services. Preserve the existing GCP fleet boundaries.
4. Verify protected API execution, fenced direct egress denial, sandbox cleanup,
   procedure Git/public-web access, and protected browser execution. Pooled OAuth
   and its refresh probe are removed; see [API-only execution](oauth-credential-security.md).
5. Resume dispatch only after those checks pass and record the deployed revision.

If rollout fails, keep sandbox dispatch paused and keep Tinyproxy disabled. Roll back
application changes only with proxy-dependent execution paused; the old application
cannot authenticate to the new listener. Do not remove the destination fence or
restore the old shared password to regain availability. Fix forward, then repeat the
acceptance checks. Other audit findings, including OAuth credential isolation, remain
separate work.

Configuration references: [Squid TLS listener](https://www.squid-cache.org/Doc/config/https_port/),
[Squid ACLs](https://www.squid-cache.org/Doc/config/acl/),
[Squid authentication](https://www.squid-cache.org/Doc/config/auth_param/),
[Certbot webroot and renewal](https://eff-certbot.readthedocs.io/en/stable/using.html).
