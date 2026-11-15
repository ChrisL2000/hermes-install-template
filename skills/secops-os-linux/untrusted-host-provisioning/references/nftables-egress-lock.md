# Egress lock recipe (nftables, separate table)

Goal: the host may initiate only DHCP, DNS to its configured resolvers, NTP, the Cloudflare tunnel edge, and allowlisted mirrors/registries over 80/443. Everything else is dropped and counted.

## Files

```
/etc/nftables.conf                                  + include "/etc/nftables.d/egress.nft"
/etc/nftables.d/egress.nft                          boot-time definition (drop policy, static tunnel + DNS)
/usr/local/sbin/nft-egress-refresh.sh               rebuilds the table with freshly resolved v4+v6 sets
/etc/systemd/system/nft-egress-refresh.service      oneshot -> the refresher
/etc/systemd/system/nft-egress-refresh.timer        OnBootSec=30s, OnUnitActiveSec=6h
```

The boot file and the refresher define the SAME table; the refresher wins at runtime because it rewrites the whole table atomically. Keep the boot file's `elements` minimal (static tunnel range + one known-good resolver) so the box can still resolve and keep its tunnel up before the first timer run.

## Ruleset skeleton

```
table inet egress {
    set http_allow  { type ipv4_addr; flags interval,timeout; elements = { … timeout 12h } }
    set http_allow6 { type ipv6_addr; flags interval,timeout; elements = { … timeout 12h } }
    set cf_edge     { type ipv4_addr; flags interval; elements = { 198.41.128.0/17 } }
    set dns_allow   { type ipv4_addr; flags interval; elements = { … } }
    set dns_allow6  { type ipv6_addr; flags interval; elements = { … } }

    chain output {
        type filter hook output priority filter; policy drop;
        oif "lo" accept
        ct state established,related accept
        ct state invalid drop

        udp sport 68 udp dport 67 accept                 # DHCP — omitting this loses the IP
        ip  daddr @dns_allow  udp dport 53 accept
        ip  daddr @dns_allow  tcp dport 53 accept
        ip6 daddr @dns_allow6 udp dport 53 accept
        ip6 daddr @dns_allow6 tcp dport 53 accept
        udp dport 123 accept                             # NTP

        ip  daddr @cf_edge  udp dport 7844 accept
        ip  daddr @cf_edge  tcp dport 7844 accept        # cloudflared
        ip6 daddr 2606:4700:a0::/48 udp dport 7844 accept
        ip6 daddr 2606:4700:a0::/48 tcp dport 7844 accept

        ip  daddr @http_allow  tcp dport { 80, 443 } accept
        ip6 daddr @http_allow6 tcp dport { 80, 443 } accept

        ip protocol icmp icmp type echo-request accept
        ip6 nexthdr icmpv6 accept
        counter drop
    }
}
```

Remove the `elements` clause entirely for any set whose resolved list is empty — `elements = { }` does not parse.

## Allowlisted names

Package mirrors, container registries and the tunnel control channel; every one is resolved to A and AAAA at refresh time:

```
deb.debian.org security.debian.org ftp.debian.org
download.docker.com pkg.cloudflare.com
auth.docker.io registry-1.docker.io index.docker.io production.cloudflare.docker.com
api.cloudflare.com region1.v2.argotunnel.com region2.v2.argotunnel.com
```

`production.cloudflare.docker.com` (blob CDN) and its AAAA records are the ones that bite: without v6 the image pull downloads the manifest and then times out on the layer.

## Apply sequence

1. Install files; back up `/etc/nftables.conf`; append the `include` line; `systemctl daemon-reload`; `systemctl enable --now nft-egress-refresh.timer`.
2. Arm the safety net BEFORE applying: `systemd-run --on-active=20min --unit=egress-rollback /usr/sbin/nft delete table inet egress`. This auto-unlocks the box if the new policy strands you.
3. Run the refresher. It resolves, builds, runs `nft -c -f` for a syntax check, and only then deletes + loads.
4. Verify (matrix below).
5. Cancel the net: `systemctl stop egress-rollback.timer; systemctl reset-failed egress-rollback`.

## Verification matrix

| check | command | expect |
|---|---|---|
| DNS | `resolvectl query download.docker.com` | resolves |
| apt | `apt-get update` | `Hit:` lines, no `Err:` |
| registry | `docker pull alpine:latest` | completes |
| tunnel | `curl -s 127.0.0.1:20241/metrics \| grep ha_connections` | non-zero |
| tunnel hostname | `curl -sS -o /dev/null -w '%{http_code}' https://<tunnel-host>` | 404 (catch-all) not 000 |
| blocked | `/dev/tcp/<unallowlisted-ip>/443`, `/dev/tcp/<unallowlisted-ip>/53` | connection fails |
| counter | `nft list chain inet egress output \| grep 'packets .* drop'` | increments |

Test blocked destinations with numeric IPv4; hostname-based tests can pick AAAA and mislead.

## Refresh semantics

Elements carry `timeout 12h` and the timer rewrites them every 6h. A dead timer therefore degrades to "new mirror IPs stop being accepted" rather than locking the box out of its own mirrors; the DHCP/DNS/NTP/tunnel rules are static and never expire.
