---
name: remote-host-ssh-setup
description: Use when setting up agent ssh/sudo access on a remote host.
version: 1.0.0
metadata:
  hermes:
    category: secops-os-linux
---

# Remote Host SSH Setup

Give this agent passwordless ssh + sudo on a new remote host. Two sides: one command line for the USER to run on the remote host, then local ssh config + verification by the agent.

## 1. Reuse the agent's keypair

- The persistent agent key is `~/.ssh/id_ed25519` (home dir survives container restarts). Read `id_ed25519.pub`; only generate a new key if the file is missing.
- Confirm which user account the setup runs as — both the authorized_keys entry and the sudoers entry are for that account.

## 2. Hand the user one paste-ready, idempotent command line

Embed the real public key — no placeholders the user must edit:

```bash
U=$USER && P="ssh-ed25519 <agent pub key>" && mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && (grep -qxF "$P" ~/.ssh/authorized_keys || echo "$P" >> ~/.ssh/authorized_keys) && echo "$U ALL=(ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/$U >/dev/null && sudo chmod 0440 /etc/sudoers.d/$U && sudo visudo -cf /etc/sudoers.d/$U && echo OK
```

- `grep -qxF` makes re-runs safe; `visudo -cf` validates the sudoers file before OK is reported.
- If the user runs as root, drop the sudoers segment — key install alone suffices.

## 3. Agent side: config + one-call verification

- Add a Host block to `~/.ssh/config` (HostName, User, IdentityFile ~/.ssh/id_ed25519).
- Verify in one call: `ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new <host> 'whoami; sudo -n true && echo SUDO_OK'`

## 4. Windows hosts (OpenSSH over Tailscale)

Windows has no sudoers and a different key-file rule. Shape: Tailscale for transport, the
Microsoft OpenSSH Server capability for control, one pinned key, no password auth.

- Publish the recipe as a standalone script the user runs elevated on the box (see the
  `standalone-script-authoring` skill), not as a paste-block: account type, ACLs, firewall
  scoping and service state are multi-step and must be idempotent.
- Verify with `ssh -o BatchMode=yes <host> 'whoami; $PSVersionTable.PSVersion'` — BatchMode
  fails instead of prompting, so a pass proves key auth rather than a password.
- Full recipe, key-file rules, firewall scoping and troubleshooting:
  references/windows-openssh.md.

## Windows hosts

Windows needs a different recipe: the OpenSSH Server capability, `sshd` restarting after config
edits, `administrators_authorized_keys` ACLs, and certificate auth with a bare username
principal, certificate auth, and the sshd-restarts-only-on-config-change trap. Full field
notes: `references/windows-openssh.md`.

## Pitfalls
- A closed port is a service problem, not a key problem. Probe before touching keys:
  `timeout 3 bash -c 'echo > /dev/tcp/<ip>/22'`. A user saying the setup "is done" does not
  mean sshd is listening — check, then ask for the service state rather than re-issuing keys.
- On Windows, an SSH session runs in session 0: no desktop, and none of the logged-in user's
  audio devices. Anything needing GUI or audio must be launched into the interactive session
  (a scheduled task run with `/it`), never started from the SSH command itself.
- Never assume the authorized_keys path on Windows: accounts in the local Administrators
  group are read from `C:\ProgramData\ssh\administrators_authorized_keys`, everyone else from
  `%USERPROFILE%\.ssh\authorized_keys`. Decide by group membership, not by whether the
  current shell is elevated.

- If the hostname doesn't resolve, don't blame the key — check public DNS first: `curl -s "https://cloudflare-dns.com/dns-query?name=<host>&type=A" -H 'accept: application/dns-json'` (Status 3 = NXDOMAIN). Container DNS can lag or miss private zones; get an IP or a confirmed DNS record from the user before iterating on ssh.
- When the user re-sends a hostname after a correction, use the new spelling exactly — do not keep probing the earlier string.
