# Context window & context engine (compaction)

Depth for when compaction doesn't match the model's real window, or when verifying a custom context engine. Quick answers: SKILL.md → Troubleshooting → "Context window / compaction not what you expect".

## 1. How Hermes resolves a model's context window

Resolution order in `agent/model_metadata.py`:

1. Explicit config wins: `model.context_length` or provider-scoped `providers.<key>.context_length`.
2. Provider-aware sources: local-endpoint probe (parses `max_model_len` / `context_length` / `max_position_embeddings`-style keys from /v1/models and model payloads), then models.dev / OpenRouter catalog caches (`~/.hermes/models_dev_cache.json`, `~/.hermes/cache/endpoint_model_metadata.json`).
3. Family catch-all table `DEFAULT_CONTEXT_LENGTHS` — longest-key-first substring match on the lowercased model id (e.g. `"qwen": 131072`, `"deepseek": 128000`, `"grok": 131072`, `"llama": 131072`).
4. Hard fallback `DEFAULT_FALLBACK_CONTEXT` (256000, top of `CONTEXT_PROBE_TIERS`), logged once as "Could not determine context length".

**Pitfall:** an id matching no specific entry silently gets the family catch-all, which can sit far below the served window. Compaction triggers at the configured compression ratio × the *resolved* window, so an underestimated window compresses early — often at half the true capacity. Never trust the model name to carry the right window; verify the resolved value.

## 2. Verify what was resolved

Ground truth for the model's window:
- Model files: `config.json` → `text_config.max_position_embeddings` (check `rope_scaling` for extended windows), `tokenizer_config.json` → `model_max_length`.
- Local server: `curl -s localhost:<port>/v1/models` → `max_model_len`; the `--max-model-len` flag in the compose file and the startup log line "Using max model len N".
- Remote provider: query that provider's OWN `/v1/models` catalog using its stored credential
  (not models.dev — the catalog carries the served `context_length` / `max_model_len` per id).
  For Nous, the credential is `~/.hermes/auth.json` → `credential_pool.nous[0].access_token`
  against `inference_base_url` (default `https://inference-api.nousresearch.com/v1`); read the
  target id's `context_length`. A config pin (`model.context_length`) can be a stale leftover
  from a previous primary (e.g. pinned `262144` while the catalog serves `1048576`), so verify
  against the catalog before trusting it — `per_request_limits: null` means no lower per-request cap.

What Hermes actually used:

```bash
grep "compression completed" ~/.hermes/logs/agent.log | tail -3
```

Telemetry fields: `main_context_limit` (resolved window), `effective_threshold` (actual trigger), `effective_aux_context` (window used by the compaction aux model — often the main model on local stacks), `method` (`llm_summary` vs a plugin engine).

If resolved < served, set it explicitly and restart the process:

```bash
hermes config set model.context_length <served-max-model-len>
# or, for a named local provider:
hermes config set providers.<key>.context_length <served-max-model-len>
```

## 3. Custom context engine (VCC plugin)

The stock engine compacts via LLM summarization — with a local main model that is a minutes-long aux call per compaction. The `hermes-vcc-engine` plugin replaces it with a deterministic local compactor (millisecond handoff; originals archived and recoverable via session_search).

- Install location: `~/.hermes/plugins/hermes-vcc-engine`; enabled via `context.engine: vcc` in config.yaml.
- Verify a process uses it: `grep "registered context engine" ~/.hermes/logs/agent.log` — the line must exist **after that process's start time**. Missing line = silent fallback to the stock engine.
- Per-process semantics: the plugin applies only to processes started after install/config change. CLI sessions pick it up on relaunch; a long-lived gateway keeps the stock engine until restarted (`hermes gateway restart` / `/restart`).
- Update hazard: the plugin hooks private `ContextCompressor` internals. After any `hermes update`, re-grep for the registration line; if it is gone, the engine fell back and LLM compaction (and its stall times) are back.

## 4. Log attribution (shared agent.log)

`~/.hermes/logs/agent.log` is shared by ALL Hermes processes of a profile (CLI, gateway, cron, spawned agents). Never conclude what a surface uses from a log line alone — attribute first:

```bash
ps -eo pid,lstart,args | grep -i hermes | grep -v grep
```

Compare line timestamps against process start times before drawing any conclusion about a running surface.
