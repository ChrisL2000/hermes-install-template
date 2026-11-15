# hermes-install-template

One script to set up [Hermes Agent](https://hermes-agent.nousresearch.com/) with a
sensible local default LLM and a starter set of skills — a template you can tweak
before or after running.

Defaults (all overridable):

| What | Default |
|---|---|
| LLM provider name | `vllm-b70-qwen38` |
| Model | `Qwen3.8-27B` |
| Endpoint | `http://localhost:8081/v1` (OpenAI-compatible: vLLM, llama.cpp, LM Studio, ...) |
| Context length | `262144` |
| Skills | `hermes-agent`, `vllm-b70-qwen38-deploy`, `povray` |

## Quick start

```bash
curl -fsSL https://raw.githubusercontent.com/ChrisL2000/hermes-install-template/main/install.sh | bash
```

Or clone and run with options:

```bash
git clone https://github.com/ChrisL2000/hermes-install-template
cd hermes-install-template
bash install.sh --provider vllm-b70-qwen38 --model Qwen3.8-27B \
    --base-url http://localhost:8081/v1 --context-length 262144
```

Options (also settable via env: `PROVIDER_NAME`, `MODEL_NAME`, `BASE_URL`, `CONTEXT_LENGTH`):

```
--provider NAME       provider name (default: vllm-b70-qwen38)
--model MODEL         model name (default: Qwen3.8-27B)
--base-url URL        LLM endpoint (default: http://localhost:8081/v1)
--context-length N    context window (default: 262144)
--skills a,b,c        override templates/skills.txt list
--tap owner/repo      add a GitHub skills tap first
--no-install          hermes is already installed
--force               overwrite existing config/skills (config is backed up)
--doctor              run hermes doctor at the end
```

`HERMES_HOME` is honored (default `~/.hermes`), so the script can target a named
profile's home or a test directory.

## What the script does

1. **Install** — installs Hermes Agent via the official curl installer if `hermes`
   is not on PATH (`--no-install` to skip).
2. **Point at the default LLM** —
   - fresh install: renders `templates/config.yaml` (placeholders `__PROVIDER__`,
     `__MODEL__`, `__BASE_URL__`, `__CONTEXT_LENGTH__`) into `$HERMES_HOME/config.yaml`;
   - existing install: applies the same settings with idempotent
     `hermes config set model.*` / `providers.<name>.*` calls — no other keys touched.
   - `--force` backs up the existing config to `config.yaml.bak-<timestamp>` and
     re-renders the template.
3. **Seed `.env`** — copies `templates/.env.example` to `$HERMES_HOME/.env` (chmod 600)
   only if missing.
4. **Install skills** — reads `templates/skills.txt` (or `--skills`):
   - names bundled under `skills/` in this repo are copied to
     `$HERMES_HOME/skills/<category>/<name>`;
   - `https://.../SKILL.md` URLs go through `hermes skills install <url>`;
   - anything else is treated as a skills-hub id;
   - `tap:owner/repo` lines run `hermes skills tap add` once.
   - existing skill dirs are kept unless `--force`.
5. **Summary** — prints what was installed; `--doctor` runs `hermes doctor`.

## Customizing the template

- **Different LLM:** pass flags, or edit `templates/config.yaml` placeholders.
  Any OpenAI-compatible endpoint works (vLLM, llama.cpp, LM Studio, Ollama with a
  /v1 shim). Keyed endpoints: add `api_key` or `key_env` under the provider entry.
- **Different skills:** edit `templates/skills.txt`, or drop a skill directory into
  `skills/<category>/<name>/` (a `SKILL.md` with `name:` frontmatter matching the
  dir name). For skills that live in a GitHub repo, use a `tap:` line.
- **More config defaults:** add blocks to `templates/config.yaml`
  (terminal backend, toolsets, memory, gateway platforms — see
  [Hermes configuration docs](https://hermes-agent.nousresearch.com/docs/user-guide/configuration)).

## Verify after install

```bash
hermes config        # model + provider section
hermes skills list   # installed skills
hermes doctor        # dependencies + config health
hermes               # chat
```

## Repo layout

```
install.sh               the installer
templates/config.yaml    default-LLM config template (placeholders)
templates/skills.txt     skills manifest (bundled names / URLs / taps)
templates/.env.example   .env seed (API-key placeholders)
skills/autonomous-ai-agents/hermes-agent/      bundled: Hermes operating guide (MIT)
skills/devops/vllm-b70-qwen38-deploy/          bundled: tuned Qwen3.8-27B vLLM deploy (MIT)
skills/creative/povray/                         bundled: POV-Ray scene-writing reference + scenes (MIT)
```

## License

MIT. Bundled skills keep their own licenses (MIT).
