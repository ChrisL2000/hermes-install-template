#!/usr/bin/env python3
"""Refresh the nftables egress allowlist on a host whose outbound is locked.

Builds `table inet egress` (output policy drop) from two sources:
  1. DNS snapshots of the package mirrors / container registries the host needs
  2. AWS's published CLOUDFRONT prefixes - image blob CDNs answer with AAAA from a
     huge rotating pool that a DNS snapshot can never cover

Overlapping entries are removed because nftables interval sets reject conflicts
("conflicting intervals specified"). The generated ruleset is syntax-checked with
`nft -c -f` BEFORE the live table is deleted, so a bad build leaves the running
policy untouched. Use --dry-run to inspect without applying.
"""
import ipaddress
import json
import os
import socket
import subprocess
import sys
import tempfile
import urllib.request

DOMAINS = [
    "deb.debian.org", "security.debian.org", "ftp.debian.org",
    "download.docker.com", "pkg.cloudflare.com",
    "auth.docker.io", "registry-1.docker.io", "index.docker.io",
    "production.cloudflare.docker.com",
    "api.cloudflare.com",
    "region1.v2.argotunnel.com", "region2.v2.argotunnel.com",
    "ip-ranges.amazonaws.com",
]
CACHE = "/var/cache/nft-egress/aws-ip-ranges.json"
# Cloudflare tunnel edge (published ranges: TCP+UDP 7844, plus the HTTPS control channel)
CF_EDGE = ["198.41.128.0/17"]
CF_EDGE6 = "2606:4700:a0::/48"


def resolve(domains, family):
    out = set()
    for d in domains:
        try:
            for ai in socket.getaddrinfo(d, 443, family):
                out.add(ai[4][0].split("%")[0])
        except OSError:
            continue
    return out


def cf_prefixes():
    data = None
    try:
        with urllib.request.urlopen("https://ip-ranges.amazonaws.com/ip-ranges.json", timeout=25) as r:
            data = json.load(r)
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w") as f:
            json.dump(data, f)
    except Exception:
        try:
            data = json.load(open(CACHE))
        except Exception:
            data = {"prefixes": [], "ipv6_prefixes": []}
    v4 = [p["ip_prefix"] for p in data.get("prefixes", []) if p.get("service") == "CLOUDFRONT"]
    v6 = [p["ipv6_prefix"] for p in data.get("ipv6_prefixes", []) if p.get("service") == "CLOUDFRONT"]
    return v4, v6


def resolvectl_dns():
    """Upstream resolvers as currently configured, so a lease/DNS change cannot strand resolution."""
    v4, v6 = set(), set()
    try:
        txt = subprocess.run(["resolvectl", "status"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        txt = ""
    for tok in txt.replace(",", " ").split():
        t = tok.strip("[]")
        try:
            a = ipaddress.ip_address(t)
        except ValueError:
            continue
        if a.version == 6:
            if t.startswith(("fe80", "::")):
                continue
            v6.add(str(a))
        else:
            v4.add(str(a))
    return v4, v6


def dedupe(hosts, prefixes):
    """nftables interval sets reject an element that falls inside another."""
    nets = [ipaddress.ip_network(p) for p in prefixes]
    keep = []
    for h in hosts:
        try:
            a = ipaddress.ip_address(h)
        except ValueError:
            continue
        if any(a.version == n.version and a in n for n in nets):
            continue
        keep.append(str(a))
    return sorted(set(keep))


def emit_set(name, kind, elements, timeout=None):
    lines = [f"    set {name} {{ type {kind}; flags interval{',timeout' if timeout else ''};"]
    if elements:  # an empty `elements = { }` is a PARSE ERROR, not an empty set
        lines.append("        elements = {")
        for e in elements:
            lines.append(f"            {e}{' timeout ' + timeout if timeout else ''},")
        lines.append("        }")
    lines.append("    }")
    return "\n".join(lines)


def build():
    cf4, cf6 = cf_prefixes()
    h4 = resolve(DOMAINS, socket.AF_INET)
    h6 = resolve(DOMAINS, socket.AF_INET6)
    d4, d6 = resolvectl_dns()
    host4, host6 = dedupe(h4, cf4), dedupe(h6, cf6)
    dns4, dns6 = dedupe(d4, cf4), dedupe(d6, cf6)
    if not host4:
        raise SystemExit("no IPv4 resolved for mirrors; refusing to rewrite the allowlist")
    body = [
        "table inet egress {",
        emit_set("http_pfx4", "ipv4_addr", sorted(set(cf4))),
        emit_set("http_pfx6", "ipv6_addr", sorted(set(cf6))),
        emit_set("http_host4", "ipv4_addr", host4, "12h"),
        emit_set("http_host6", "ipv6_addr", host6, "12h"),
        emit_set("dns_allow4", "ipv4_addr", sorted(dns4)),
        emit_set("dns_allow6", "ipv6_addr", sorted(dns6)),
        emit_set("cf_edge", "ipv4_addr", CF_EDGE),
        f'''    chain output {{
        type filter hook output priority filter; policy drop;

        oif "lo" accept
        ct state established,related accept
        ct state invalid drop

        # DHCP lease renewal - losing this loses the IP
        udp sport 68 udp dport 67 accept

        # DNS to configured upstream resolvers
        ip daddr @dns_allow4 udp dport 53 accept
        ip daddr @dns_allow4 tcp dport 53 accept
        ip6 daddr @dns_allow6 udp dport 53 accept
        ip6 daddr @dns_allow6 tcp dport 53 accept

        # NTP
        udp dport 123 accept

        # tunnel edge + control channel
        ip daddr @cf_edge udp dport 7844 accept
        ip daddr @cf_edge tcp dport 7844 accept
        ip6 daddr {CF_EDGE6} udp dport 7844 accept
        ip6 daddr {CF_EDGE6} tcp dport 7844 accept

        # allowlisted mirrors + registries: CloudFront prefixes, then resolved hosts
        ip daddr @http_pfx4 tcp dport {{ 80, 443 }} accept
        ip6 daddr @http_pfx6 tcp dport {{ 80, 443 }} accept
        ip daddr @http_host4 tcp dport {{ 80, 443 }} accept
        ip6 daddr @http_host6 tcp dport {{ 80, 443 }} accept

        ip protocol icmp icmp type echo-request accept
        ip6 nexthdr icmpv6 accept

        # reject, not drop: a silently dropped attempt makes Go/apt wait out the
        # timeout instead of falling back to an allowlisted IPv4 path
        counter reject
    }}
}}''',
    ]
    return "\n".join(body), len(host4), len(host6), len(cf4), len(cf6)


def main():
    dry = "--dry-run" in sys.argv
    text, n4, n6, p4, p6 = build()
    fd, path = tempfile.mkstemp(suffix=".nft", prefix="egress.")
    with os.fdopen(fd, "w") as f:
        f.write(text + "\n")
    try:
        chk = subprocess.run(["/usr/sbin/nft", "-c", "-f", path], capture_output=True, text=True)
        if chk.returncode != 0:
            print("nft-egress-refresh: SYNTAX ERROR, live rules untouched:", file=sys.stderr)
            print(chk.stderr.strip()[:400], file=sys.stderr)
            return 1
        if dry:
            print(text)
            print(f"# dry-run OK: {n4} v4 hosts, {n6} v6 hosts, {p4} v4 prefixes, {p6} v6 prefixes")
            return 0
        subprocess.run(["/usr/sbin/nft", "delete", "table", "inet", "egress"], capture_output=True)
        load = subprocess.run(["/usr/sbin/nft", "-f", path], capture_output=True, text=True)
        if load.returncode != 0:
            print("nft-egress-refresh: LOAD FAILED after delete - egress is OPEN", file=sys.stderr)
            print(load.stderr.strip()[:400], file=sys.stderr)
            return 1
        print(f"nft-egress-refresh: OK - {n4} v4 + {n6} v6 hosts, {p4} v4 + {p6} v6 CloudFront prefixes")
        return 0
    finally:
        os.unlink(path)


if __name__ == "__main__":
    sys.exit(main())
