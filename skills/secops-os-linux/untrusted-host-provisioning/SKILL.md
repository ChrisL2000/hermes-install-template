---
name: untrusted-host-provisioning
description: "Use when standing up services on an untrusted host."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [docker, nftables, egress, vps, hardening, provisioning, cloudflare-tunnel]
    related_skills: [cloudflare-tunnel-ssh-tls, remote-host-ssh-setup, hermes-ai-host-operations]
    category: secops-os-linux
---

# Provisioning services on an untrusted host

## When to Use

- Installing Docker or a service stack on a public VPS, rented box, or any host whose policy is "reachable, but not trusted".
- Deciding how services are exposed (loopback + tunnel vs published ports).
- Locking egress down without stranding DNS, DHCP, NTP, or a running Cloudflare tunnel.

## Step 0 — read the host's standing rules first

Before proposing anything, search durable memory and the skill library for this host:

```
mnemosyne_recall "<host> policy untrusted approval egress"
skills_list  # then skill_view the class skills it points at
```

Hosts carry explicit gates — commonly "no outbound-initiated work" plus "security questions answered and approved by the user before any deployment/change". A Docker install moves three surfaces at once (egress, port exposure, storage), so present those as choices with a recommended option first, get answers, then act. Starting the install without that pass reads as ignoring the user's own written policy — and the user will say so.

## Step 1 — recon, read-only, single batch

```
. /etc/os-release; uname -r; dpkg --print-architecture
free -h; df -h / /var
dpkg -l | grep -E '^ii\s+(docker|containerd)'; command -v docker
sudo -n ufw status; sudo -n iptables -S | head; sudo -n nft list ruleset | head -30
sudo -n -l | tail -3;  resolvectl status | grep -E 'DNS Servers|Current DNS'
systemctl is-active systemd-networkd NetworkManager chrony systemd-timesyncd
ss -ltnup | head -20
```

Record three things that change the plan: the existing firewall's `output` policy, whether a DHCP client runs (needs 67/68 or the box loses its lease), and which upstream resolvers are configured.

## Step 2 — install Docker from the user's published assets

Prefer the Docker assets in the user's GitHub repo ChrisL2000/hermes-ai-host-operations — the apt keyring, the apt source line (`deb [arch=amd64 signed-by=…] https://download.docker.com/linux/debian trixie stable`) and daemon.json, all under that repo's assets tree — over `curl … | sh`: the key and pinning come from a reviewed source. Check the key fingerprint before trusting it — Docker CE is `9DC8 5822 9FC7 DD38 854A E2D8 8D81 803C 0EBF CD88`.

Adapt, don't copy:

- Drop the `data-root` override unless that volume actually exists on the target (`df` first); stock `/var/lib/docker` is right when the root FS has room.
- Keep `storage-driver: overlay2`. Docker 29 reports it as `overlayfs` in `docker info` — that is the new name, not a misconfiguration.
- `sudo usermod -aG docker <admin>`. Group membership applies to NEW logins: verify `docker version` in a fresh ssh session, not the one that ran usermod.

Accept four independent proofs, not one: `docker version` (client+server), `systemctl is-active docker containerd`, `docker compose version`, and a real pull + run.

## Step 3 — exposure: loopback-only publishing

The tunnel (or an ssh forward) is the ingress; the host's public IP should serve nothing. Publish as `-p 127.0.0.1:<port>:<cport>` and prove it from both sides:

```
local:  curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:<port>/     # expect 200
remote: timeout 6 bash -c 'echo > /dev/tcp/<public-ip>/<port>'               # expect failure
ss -ltnp | grep <port>                                                      # expect 127.0.0.1, never 0.0.0.0
```

State the mechanism when recommending this: Docker writes its own DOCKER/FORWARD chains, so a later `ufw` on its own does not filter published ports — source restriction needs DOCKER-USER rules.

## Step 4 — egress lock

Full recipe (ruleset skeleton, systemd timer, verification matrix): `references/nftables-egress-lock.md`. Re-runnable allowlist refresher: `scripts/nft-egress-refresh.py` (Python; supersedes the earlier bash version, which could not cover image blob CDNs and could emit overlapping interval-set entries).

Non-negotiables: syntax-check before touching the live table, allow DHCP + DNS + NTP + the tunnel's edge ports, allowlist dual-stack, and arm an auto-rollback timer before applying.

## Step 5 — leave zero residue

Remove every test container and image, then report `docker system df` (0 containers / 0 images / 0 volumes / 0 cache proves it). A test container left running is an untracked service on a public host.

## Pitfalls

- An empty `elements = { }` in an nftables set is a PARSE ERROR, not an empty set. Emit the `elements` clause only when the list is non-empty or the whole ruleset fails to load — and the failure looks like a syntax error several lines below the real cause.
- `nft -f` cannot re-declare an existing table. Put the policy in a separate table (`inet egress`) and persist it via `include "/etc/nftables.d/egress.nft"` in `/etc/nftables.conf`; editing the existing `inet filter` block in place will not reload.
- Never `nft delete table` before the replacement passes `nft -c -f <file>`. A load failure after the delete leaves egress WIDE OPEN — the opposite of the intent — and reads as success unless you re-read the chain policy afterwards.
- An IPv4-only allowlist breaks `docker pull`: registry blob CDNs answer with AAAA and the pull dies with `dial tcp [2600:…]: i/o timeout`. Allowlist v6, or pin the client to IPv4.
- Allow DHCP (`udp sport 68 dport 67`) or the lease eventually expires; allow the tunnel edge (`198.41.128.0/17` TCP+UDP 7844, v6 `2606:4700:a0::/48`) or every tunnel hostname goes dark.
- `bash /dev/tcp/<hostname>/<port>` may try IPv6 first and report "blocked" while IPv4 is permitted. Resolve the name and test the numeric IPv4 before concluding a rule is wrong.
- The egress chain governs the host (OUTPUT); container traffic is FORWARD. A `forward` policy of `drop` makes containers fully offline — decide that deliberately rather than discovering it through a failing container.
- `docker pull` is host-side (dockerd), so "containers have no egress" and "pulls work" are both true at once. Do not use a successful pull as evidence that container networking is open.
- Image blob CDNs answer with AAAA from a huge rotating pool, so a DNS snapshot allowlist cannot cover them and the pull dies on an IPv6 dial. Ingest AWS's published CLOUDFRONT prefixes from `ip-ranges.amazonaws.com/ip-ranges.json` (service `CLOUDFRONT`, roughly 216 v4 + 35 v6) and cache the file so a failed fetch degrades to the last good copy instead of shrinking the allowlist.
- nftables interval sets reject an element that sits inside another (`conflicting intervals specified`). Merging resolved addresses with prefixes fails until addresses already covered by a prefix are dropped, or prefixes and hosts live in separate sets.
- A failing refresher is SILENT: the timer keeps firing, `nft -c` refuses to load, and the live policy simply stays as it was — so `timer active` does not mean `lock active`. Check `nft list tables | grep egress` and `systemctl status nft-egress-refresh` on every visit, and treat a missing table as "egress open".
- End the egress chain with `reject`, not `drop`. A silently dropped attempt makes Go/containerd and apt wait out the full timeout instead of retrying an allowlisted IPv4 address; reject turns a 30-second hang into a millisecond error.
