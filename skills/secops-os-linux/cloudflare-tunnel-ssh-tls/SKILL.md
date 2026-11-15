---
name: cloudflare-tunnel-ssh-tls
description: "Use when a Cloudflare Tunnel hostname fails TLS or SSH."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [cloudflare, tunnel, tls, ssh, fail2ban, troubleshooting, dns]
    category: secops-os-linux
---

# Cloudflare Tunnel hostname fails TLS or SSH

## When to Use

- A tunnel hostname returns `SSL alert number 40 (handshake_failure)` or `http=000` from every source IP.
- SSH over a Cloudflare Tunnel times out or hangs, or the tunnel hostname serves error 1033.
- A tunnel's DNS record may point at a deleted/recreated tunnel UUID, or its ingress must be inspected without dashboard access.
- A "the host banned my IP" incident where the real fault may be TLS, DNS or ingress rather than the firewall (see the fail2ban section).

## Symptom

`openssl s_client -connect <host>:443 -servername <host>` returns `SSL alert number 40 (handshake_failure)`, or SSH through the tunnel hangs/dies. Tell-tale: it fails from EVERY source IP, including the tunnel's own host. That rules out any IP ban.

## Diagnose in this order (cheapest first)

1. **Is the edge issuing a cert at all?** Compare a working hostname in the same zone with the broken one:

   `openssl s_client -connect <working-host>:443 -servername <working-host> </dev/null 2>/dev/null | openssl x509 -noout -subject -ext subjectAltName`

   Free Universal SSL covers ONLY `<zone>` and `*.<zone>` — one label, not two. A hostname like `ssh.sub.zone.tld` is NOT covered, so the edge sends alert 40 and never presents a cert. This is the most common cause and no tunnel, DNS-target or Access change fixes it. Fix: rename the public hostname to a single label (`sub.zone.tld`), or buy Total TLS / upload a custom cert.

2. **Does the edge reach the connector?**

   `curl -sS -D- https://<host>`

   - Empty-bodied `404` with `cf-ray` + `server: cloudflare` = the connector's `http_status:404` catch-all. Routing works; only the ingress hostname is missing or mismatched.
   - HTML page saying `error code: 1033` = no connector for that tunnel UUID.

3. **Does DNS point at the LIVE tunnel?** The running tunnel UUID is inside the connector token:

   `sudo cat /etc/cloudflared/token | base64 -d | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["t"])'`

   Compare with the proxied `CNAME <host> -> <uuid>.cfargotunnel.com`. Deleting and recreating a tunnel issues a NEW uuid; DNS keeps pointing at the dead one. Fix the CNAME, not the tunnel.

4. **Is the ingress correct?** cloudflared logs the remotely-managed config on every start:

   `journalctl -u cloudflared | grep 'Updated to new configuration'`

   Expect `{"hostname":"<host>","service":"ssh://localhost:22"}`. A token-run tunnel has no local config.yml — the ingress lives in the dashboard/API, so `config.yml` absence is normal.

5. **Access/mTLS in play?** `GET /accounts/{acct}/access/apps` returning `9999 access.api.error.not_enabled` means Zero Trust is off and nothing is gating the hostname. If Access IS on, an Access-gated SSH hostname needs `cloudflared access ssh` (browser login) or a service token for headless use.

## Client-side mode matters

`cloudflared access ssh` requires Cloudflare Access. For a raw tunnel with no Access use TCP mode:

`cloudflared access tcp --hostname <host> --url 127.0.0.1:2222` then `ssh -p 2222 user@127.0.0.1`

Start it as a tracked background process (`terminal(background=true)`), never `nohup`/`setsid` — the tool rejects shell-level background wrappers.

## Verify traffic really traverses the tunnel

A `404` alone does not prove traversal — Cloudflare can answer before the connector. Prove it with the connector's own counter:

1. Snapshot on the origin: `curl -s http://127.0.0.1:20241/metrics | grep '^cloudflared_tunnel_total_requests'` (metrics listener is 127.0.0.1:20241 in the shipped systemd unit).
2. Fire exactly one request per hostname from OUTSIDE the box (`curl`, not urllib).
3. Re-read the counter. Delta must equal the number of requests that got past the edge. A mismatch means some hostnames never reached the connector.

Fingerprints:
- zero-byte body + bare `404` = cloudflared's `http_status:404` catch-all → routing works, ingress rule for that hostname is missing.
- HTML page with `error code: 1033` = no connector for that tunnel UUID (DNS points at a dead/recreated tunnel).
- `403` with body `error code: 1010\n` (17 bytes) = Cloudflare browser-integrity check rejecting the client. Python `urllib`'s default User-Agent trips it; `curl` does not. Use curl for reachability tests, or the result is indistinguishable from a real block.
- Resolve names before testing: if any hostname resolves to the origin's own public IP instead of 104.x/172.6x anycast, it bypasses Cloudflare entirely.

## Hostname provisioning and the naming scheme

- Cert coverage decides the namespace, so choose names before building anything. Only `<zone>` and `*.<zone>` are covered on the free plan; mass-provision single-label names (`www`, `ssh`, `sip`, `echo`, …) instead of buying Total TLS.
- Confirm coverage from the API rather than assuming: `GET /zones/{z}/ssl/certificate_packs?status=all` and read `hosts` on the universal pack, then cross-check with the SAN dump from a working sibling hostname.
- Bulk-create idempotently: `GET /zones/{z}/dns_records?per_page=200` first and key the results by `name`, then `POST /zones/{z}/dns_records` with `{"type":"CNAME","name":"<label>.<zone>","content":"<uuid>.cfargotunnel.com","proxied":true,"ttl":1}`. Re-runs then report EXISTS instead of failing on duplicates.
- Retarget a stale UUID with `PATCH /zones/{z}/dns_records/{id}` (repeat `type`, `proxied`, `ttl` in the body) after saving the old record JSON to disk for rollback.
- One hostname carries ONE service type — ingress is first-match-wins, so a name cannot be both `ssh://` and `http://`. HTTP and HTTPS DO share a hostname because the edge terminates TLS, and `path` matching splits several HTTP backends under one name.
- The tunnel carries TCP/HTTP and WebSockets only. SIP signalling over TCP is impractical (phones do not speak cloudflared) and RTP/UDP never traverses; browser SIP-over-WSS plus TURN over TCP 443 are the workable substitutes.

## API permission traps

- Tunnel endpoints need Account → **Cloudflare Tunnel** Read/Edit. Without it the LIST returns `200 success=true, result_info.total_count: 0` (silent empty, not 403) while per-ID GETs return `401 1001 Not authorized`. Never read that empty list as "the account has no tunnels" — check the connector token's `t` instead.
- `/user/tokens/verify` returns `401 1000 Invalid API Token` for account/zone-scoped tokens that work fine. Verify by making a real scoped read.
- Probe write scope harmlessly before mutating: `PATCH /zones/{z}/dns_records/<bogus-id>` → `81044 Record does not exist` means write is authorized; `403`/`9109` means it is not.
- Dashboard labels ≠ API reference names: dashboard "DNS: Edit" = API `DNS Write`; dashboard "Zone: Read" = API `Zone Read`. Quote both when instructing a user, and check `developers.cloudflare.com/fundamentals/api/reference/permissions/` before stating a scope.
- Snapshot the record before changing it (`GET` then save the JSON to disk) so the change is reversible.

## fail2ban lockout forensics

- Clear bans: `fail2ban-client unban --all`, or per jail `fail2ban-client set <jail> unbanip <ip>`; list jails with `fail2ban-client status`.
- A ban of the client's own IP looks like a filtered/timed-out `:22` (nftables `blocktype=drop`), NOT a TLS error. Do not confuse the two symptoms.
- Unbanning alone rarely sticks. Find the offender: `journalctl -u ssh | grep <ip> | grep -iE 'fail|invalid|closed'` — the USERNAME in `Failed publickey for <user>` names the misconfigured client. Fix the client's user/key, or whitelist with `ignoreip` in `/etc/fail2ban/jail.d/<host>.conf` then `fail2ban-client reload`.
- Bans expire on their own at `bantime`; check the exact timestamp before assuming manual intervention is needed (`zgrep 'Ban|Unban <ip>' /var/log/fail2ban.log*`).
