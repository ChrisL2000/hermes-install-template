# Windows: key-only SSH control channel

Adds a remote-control channel to a Windows 10/11 box with the smallest useful surface: no
inbound port on the router, no password auth, no forwarding, one pinned key.

Transport is Tailscale (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`). Control is the Microsoft
OpenSSH Server capability. There is no sudoers equivalent — elevation is a separate problem
(see "Execution: session 0" below).

## The six changes, in order

1. **Capability** `OpenSSH.Server~~~~0.0.1.0` via `Add-WindowsCapability -Online` (skipped if
   `State -eq 'Installed'`). Needs Windows Update access; if it fails, install "OpenSSH Server"
   from Settings → Optional features.
2. **Service** `Set-Service sshd -StartupType Automatic; Start-Service sshd`.
3. **sshd_config** (`%ProgramData%\ssh\sshd_config`), top-level keys only, never inside a
   `Match` block:
   `Port 22`, `PubkeyAuthentication yes`, `PermitEmptyPasswords no`,
   `PasswordAuthentication no`. Back up to `sshd_config.bak-<timestamp>` first, write ASCII.
4. **Default shell** `HKLM:\SOFTWARE\OpenSSH\DefaultShell` → pwsh 7 if present, else
   `WindowsPowerShell\v1.0\powershell.exe`. Without it every session is `cmd`, which breaks
   quoting for everything the agent sends.
5. **Firewall** inbound TCP/22 allowed only from the tailnet ranges, and only on the Tailscale
   interface when `Get-NetAdapter -InterfaceAlias Tailscale` finds it. An existing rule with
   the same DisplayName is updated rather than duplicated.
6. **Key** one line, with options:
   `from="<agent-ip>",no-agent-forwarding,no-port-forwarding,no-user-rc ssh-ed25519 AAAA…`

Idempotency matters more than speed here: the user will re-run this after a half-finished
attempt, and every step must converge rather than duplicate.

## Key-file selection (the failure that looks like a bad key)

| account | file sshd reads | required ACL |
|---|---|---|
| member of local Administrators | `C:\ProgramData\ssh\administrators_authorized_keys` | owner `BUILTIN\Administrators`, rules for `SYSTEM` + `BUILTIN\Administrators` only, inheritance disabled |
| standard user | `%USERPROFILE%\.ssh\authorized_keys` | user + `SYSTEM`, inheritance disabled |

Decide by **group membership** (`Get-LocalGroupMember -Group Administrators`), not by whether
the current shell is elevated — the setup script is always elevated, so elevation says
nothing about the login account.

OpenSSH silently ignores a key file that any other principal can write to; "key is correct
but Permission denied" is almost always this.

## Execution: session 0 vs the interactive session

`sshd` sessions are non-interactive. They have no visible desktop, no UI automation target,
and no access to the logged-in user's audio session (WASAPI render/loopback endpoints belong
to that session). Consequences:

- headless work (pytest, pip, builds, file edits, packaging) → run it straight over SSH;
- anything touching audio, WASAPI loopback, a GUI app, or OBS's UI → register it once as a
  scheduled task and start it with `schtasks /run`:

```
schtasks /create /tn <name> /tr "C:\path\tool.exe run --config C:\path\tool.toml" ^
         /sc once /st 00:00 /rl LIMITED /it
```

`/rl LIMITED` keeps it unprivileged; `/it` places it in the interactive session, which is the
whole point. The agent then reads logs over SSH while the process lives where the devices are.

## Verify

```bash
# from the agent host — the honest test
ssh -o BatchMode=yes -o ConnectTimeout=8 <host> 'whoami; $PSVersionTable.PSVersion'
```

```powershell
# on the box
Get-Service sshd
Get-NetTCPConnection -LocalPort 22 -State Listen
Get-NetFirewallRule -DisplayName '<rule>' | Get-NetFirewallPortFilter
Get-WinEvent -LogName 'OpenSSH/Operational' -MaxEvents 40
```

`BatchMode=yes` never prompts, so a pass proves key authentication rather than a cached
password or an agent prompt.

## Troubleshooting

| symptom | cause | fix |
|---|---|---|
| port 22 times out | sshd stopped, or the firewall rule never applied | `Get-Service sshd`; re-run the firewall step |
| `Permission denied (publickey)` | key in the wrong file for the account type | move it per the table above |
| same, key looks right | key file ACL too permissive | re-apply SYSTEM+Administrators only |
| fails only from one host | `from="…"` pin doesn't match | `tailscale ip -4` on the agent host, update `-AllowFrom` |
| password prompt appears | `PasswordAuthentication` still yes, or sshd not restarted | fix config, restart service |

## Revoke

Remove the key line, then `Stop-Service sshd; Set-Service sshd -StartupType Disabled`, then
remove the firewall rule. The capability can stay installed — re-enabling is one command and
an installed-but-stopped server listens on nothing.

## Also worth closing on a tailnet Windows box

SMB (TCP/445) is commonly reachable across the tailnet by default. Restrict it to the
Tailscale interface and require SMB signing — an open 445 is a far bigger surface than the
one SSH port this recipe opens.

## Certificate auth instead of a pinned key

A pinned key is fine for one host. For a fleet, or when the credential should expire, trust an
**SSH certificate authority** instead: the agent signs itself a short-lived user cert and the
Windows box stores only the CA's public key.

```powershell
# write the CA public key to %ProgramData%\ssh\trusted_ssh_ca_keys
# then, in sshd_config (absolute path, or a __PROGRAMDATA__ prefix):
#   TrustedUserCAKeys C:\ProgramData\ssh\trusted_ssh_ca_keys
# a bare filename here resolves relative to the sshd binary directory
```

```bash
ssh-keygen -s ~/.ssh/hvt-ca -I "agent@host-$(date +%Y%m%d)" -n <windows-user> -V -5m:+12h \
  -O clear -O no-port-forwarding -O no-agent-forwarding -O no-user-rc -O no-x11-forwarding \
  -O source-address=100.64.0.0/10,fd7a:115c:a1e0::/48 ~/.ssh/id_ed25519.pub
```

- **Principal = the Windows login name.** Local account: bare username (`14843`). Domain
  account: `DOMAIN\user`, usually with `AuthorizedPrincipalsFile` (absolute path —
  `__PROGRAMDATA__` does not expand there).
- `-O clear` first, so no default critical options are inherited; `source-address` is what
  replaces a `from=` pin — any tailnet host may connect, only from tailnet addresses.
- `AuthorizedKeysCommand` is **not supported** on Windows. CA trust is the supported way to
  avoid per-host key files.
- Revocation = delete `trusted_ssh_ca_keys` (or rotate the CA): every cert it signed dies at
  once, no per-host cleanup.

## sshd reads sshd_config only at start

An edit while the service is running changes nothing, and the failure is silent. The fastest
signal: `ssh -v` still lists `password` among the server's methods even though
`PasswordAuthentication no` is in the file.

```powershell
Restart-Service sshd
Get-NetTCPConnection -LocalPort 22 -State Listen
```

Any setup script must restart the service **after** the last config write, or every credential
it installs is inert. This is the single most expensive bug in this recipe.

## Prove the guardrails bite

- `ssh -f -N -L 18080:127.0.0.1:445 host` then `curl` through it: `Connection reset by peer`
  (curl exit 56) is the refusal. A working tunnel is a leak. Note `-N -L` itself exits 0 as
  soon as it is listening — the refusal only appears when traffic is attempted.
- `ssh -A host 'echo $SSH_AUTH_SOCK'` must print nothing.
- `ssh <other-account>@host` must fail with a valid cert for a different principal.

## Tailscale SSH server

Documented for Linux and macOS only. On Windows the supported path is Microsoft's OpenSSH
Server reached over the tailnet, with the firewall rule bound to the tailnet ranges.
