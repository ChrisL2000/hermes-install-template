# Skill taxonomy and publishing rules

This repo is both an installer **and** the distribution tree for bundled Hermes skills.
Anything an agent publishes here, or imports back into a profile, follows the rules below
so that two different agents working months apart produce the same shape.

These are **conventions, not enforcement**. The single hard gate is the scrub check in
`scripts/publish-check.sh` — publishing around a scrub failure defeats the point of it.

## The tree

```
skills/<category>/<name>/SKILL.md      a skill
skills/<category>/<name>/references/   long-form material, one topic per file
skills/<category>/<name>/scripts/      runnable helpers the skill executes
templates/skills.txt                   the manifest every install reads
```

- **`<category>`** groups skills that answer the same class of question. Current categories:
  `autonomous-ai-agents`, `creative`, `devops`, `secops-os-linux`.
- **`<name>`** is the skill's identity and must equal the frontmatter `name:`.
- A category may also contain an **umbrella** skill of the same name as the category
  (`skills/secops-os-linux/secops-os-linux/`) that routes to the pieces under it and owns
  the vocabulary for that tree. Umbrellas carry routing + rules; pieces carry depth.

### Choosing a category

| The skill is about… | Category |
|---|---|
| Hermes itself — configuring, extending, operating the agent | `autonomous-ai-agents` |
| Making artefacts — scenes, mockups, media, diagrams | `creative` |
| Service stacks, pipelines, deployment, observability | `devops` |
| Hardening/access/exposure of a Linux host (ssh, egress, ports, tunnels) | `secops-os-linux` |

When nothing fits, add a category rather than widening an existing one. A new category is a
directory plus a row in the table above plus a line in the README layout.

## Authoring rules

1. **One concern per skill.** If the description needs "and", it is two skills.
2. **Description** is `Use when <trigger>. <behaviour>.` and stays under 60 characters —
   it is what every other agent sees in the skill index.
3. **Frontmatter** carries `name`, `description`, `version`, `metadata.hermes.category`
   (matching the directory) and, where useful, `metadata.hermes.tags` / `related_skills`.
4. **Size:** SKILL.md under ~120 lines. Depth goes to `references/<topic>.md`; anything
   executable goes to `scripts/` with a shebang and no secrets.
5. **Write lessons, not logs.** State the rule and why it exists. No PR numbers, no dates,
   no incident narration, no "we discovered on Tuesday that…".
6. **Placeholders over facts.** Host-specific values are written as `<host>`, `<zone>.tld`,
   `<v4-addr>` — never real addresses (see the scrub gate).
7. **Versioning:** bump `version` when content changes; note breaking renames in the commit
   body. A retired skill is deleted from the tree and from `templates/skills.txt`.

## The scrub gate (hard rule)

`scripts/publish-check.sh` scans every tracked text file for:

- credential shapes: `cfat_`, `ghp_`, `github_pat_`, `sk-`, `AKIA`, `xox[bap]-`, private keys
- any IPv4/IPv6 address outside an allowlist of loopback, RFC1918, link-local, CGNAT and
  the published Cloudflare tunnel edge ranges
- a denylist of hostnames belonging to the operator's own infrastructure
- absolute paths outside `$HERMES_HOME` (`~/.hermes`)

Run it before every commit. A match is a **failure**: fix the file, do not exempt it.

## Publishing a skill here

1. Author or update the skill locally at `$HERMES_HOME/skills/<category>/<name>/`.
2. Copy the directory into `skills/<category>/<name>/` (whole directory — references and
   scripts travel with the skill).
3. Add the skill name to `templates/skills.txt` — active if it belongs in a default install,
   commented under "bundled but opt-in" otherwise.
4. If it is a new piece, add a row to the umbrella's dispatch table and inventory.
5. `bash scripts/publish-check.sh` → must print `PASS`.
6. Commit with `type: subject` (`feat:`, `fix:`, `docs:`, `chore:`), push, then verify the
   tree actually arrived:
   `gh api repos/<owner>/<repo>/git/trees/HEAD?recursive=1 --jq '.tree[].path' | grep <name>`

## Importing a skill from this tree

- Whole tree: run `install.sh` (honours `--skills`, `HERMES_HOME`, `--force`).
- One skill: copy `skills/<category>/<name>/` to `$HERMES_HOME/skills/<category>/<name>/`,
  then `/reload-skills` in a live session (or start a new session).
- Never hand-edit a skill in `$HERMES_HOME` and expect the change to survive: this repo is
  the source of truth for anything published.

## Review checklist before merging a published skill

- [ ] frontmatter `name` equals the directory name; `category` equals its parent directory
- [ ] description starts with "Use when" and fits in 60 characters
- [ ] SKILL.md has a "When to Use" section and at least one verification step
- [ ] references/ and scripts/ referenced by the body actually exist
- [ ] `scripts/publish-check.sh` → PASS
- [ ] `templates/skills.txt` updated
- [ ] umbrella dispatch table/inventory updated (if a new piece)
