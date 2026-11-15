---
name: secops-os-linux
description: "Umbrella for Linux host security operations skills."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [secops, linux, hardening, egress, ssh, cloudflare-tunnel, taxonomy, publishing, umbrella]
    related_skills: [untrusted-host-provisioning, cloudflare-tunnel-ssh-tls, remote-host-ssh-setup]
    category: secops-os-linux
    family: secops-os-linux (3 pieces + this umbrella)
    taxonomy: "ChrisL2000/hermes-install-template -> TAXONOMY.md"
---

# secops-os-linux — umbrella

The family for hardening and operating a Linux host that is reachable from the internet:
access, exposure, and egress. This file routes; all depth lives in the pieces.

## When to Use

- Standing up, hardening, or auditing a Linux box that the agent reached over ssh
- Anything about what a host may initiate (egress) or what may reach it (publishing)
- A public hostname that should work but does not (TLS, SSH, tunnel routing)

Out of scope — load the owning family instead: application stacks (`devops`), vendor
network appliances (`fortinet`), inference/GPU (`mlops`), Hermes internals (`autonomous-ai-agents`).

## Dispatch table

| Piece | Load it when |
|---|---|
| `untrusted-host-provisioning` | Installing Docker or a service stack on a public/rented/untrusted host; deciding loopback-vs-published ports; locking egress with nftables |
| `cloudflare-tunnel-ssh-tls` | A tunnel hostname fails TLS (alert 40) or SSH; error 1033; suspicious DNS target; ingress/API permission traps |
| `remote-host-ssh-setup` | Giving the agent passwordless ssh + sudo on a new host (key reuse, authorized_keys, sudoers, ssh config, verification) |

Order of operations for a fresh host: `remote-host-ssh-setup` -> `untrusted-host-provisioning`
-> `cloudflare-tunnel-ssh-tls` if the public name misbehaves.

## Taxonomy + publishing rules

Full text: `TAXONOMY.md` at the root of ChrisL2000/hermes-install-template (this tree's home).
Summary that applies to anything published under `skills/secops-os-linux/`:

- **One concern per skill.** If a piece needs "and", it is two pieces.
- **Layout** `skills/<category>/<name>/SKILL.md`, with `references/` for long material and
  `scripts/` for runnable helpers. Keep SKILL.md under ~120 lines; push depth into references.
- **Frontmatter** `name`, `description` (`Use when …`, <=60 chars), `version`,
  `metadata.hermes.category` matching the directory the skill lives in.
- **Guidance, not enforcement** — the conventions are documented so agents converge on them;
  nothing blocks a publish for style alone. The one hard gate is the **scrub check** below.
- **Scrub gate:** published files must contain no tokens/keys, no real host IPs or internal
  hostnames, and no absolute paths outside `~/.hermes`. Run `scripts/publish-check.sh`; if it
  matches, fix the file — do not publish around it.
- **Publish:** copy the piece into `skills/<category>/<name>/` in hermes-install-template,
  add its path to `templates/skills.txt`, run the scrub gate, commit, push, then verify with
  `gh api repos/<owner>/<repo>/git/trees/HEAD?recursive=1 --jq '.tree[].path'`.
- **Import back into a profile:** copy `skills/<category>/<name>/` to
  `$HERMES_HOME/skills/<category>/<name>/`, then `/reload-skills` (or run `install.sh`).
- **New piece:** add a row to the dispatch table + the inventory below, and to TAXONOMY.md.

## Inventory

| Piece | Version | Covers |
|---|---|---|
| untrusted-host-provisioning | 1.0.0 | Docker install, loopback publishing, nftables egress lock |
| cloudflare-tunnel-ssh-tls | 1.0.0 | TLS/cert coverage, tunnel UUID mismatch, ingress, API permissions, fail2ban lockouts |
| remote-host-ssh-setup | 1.0.0 | Agent ssh/sudo onboarding on a remote host |

## Durable lessons that span the pieces

- Reachability problems are almost never the firewall first: check certificate coverage, then
  whether the request reached the origin connector, then the firewall. A TLS failure from every
  source IP is a name/config problem, not a ban.
- Locks fail open or fail silent. Anything that "should be blocking" gets verified against the
  live ruleset and counters, never against the intent or the unit's active state.
- Every change to an untrusted host is a security question: present the exposure, egress and
  storage choices, get them answered, then act.
