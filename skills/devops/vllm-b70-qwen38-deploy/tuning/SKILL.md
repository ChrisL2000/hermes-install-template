---
name: hermes-vllm-operations-tuning
description: "Audit and tune vLLM Docker token throughput from metrics."
version: 0.1.0
author: ChrisL2000, Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [vllm, docker, intel-arc, xpu, throughput, speculative-decoding, metrics, tuning]
    related_skills: [ai-container-ops, intel-xe-gpu-setup, serving-llms-vllm, local-llm-providers]
    category: devops
---

# vLLM operations tuning (Docker, Intel Arc B70)

Turns a running vLLM container's launch flags + Prometheus `/metrics` into a ranked list of
throughput findings, and measures TTFT / decode tok/s with a small streaming benchmark.
Analysis is offline-safe (works from saved `docker inspect` + metrics text) and unit-tested.
It never restarts anything: applying a change to the brain container is gated by
`ai-container-ops` rule 1.

## When to Use

- "Is vLLM tuned well / why is decode slow / why are prompts slow"
- Before/after changing any `vllm serve` flag, `gpu-memory-utilization`, spec-decode settings,
  Hermes `context_length` or compression threshold
- Periodic health: preemptions, prefix-cache hit rate, MTP acceptance, queue time
- Don't use for: GPU driver/passthrough problems (`intel-xe-gpu-setup`), container
  start/stop procedure (`ai-container-ops`), generic vLLM install (`serving-llms-vllm`)

## Prerequisites

- Python 3 stdlib only (scripts use `urllib`, `json`, `subprocess`, `unittest`).
- `docker inspect` access and the vLLM metrics port reachable (host port, e.g. 8081).
- Chat endpoint accepts `reasoning_effort: "none"` (Qwen3 reasoning parser) so the benchmark
  measures plain decode, not thinking tokens.

## How to Run

```
# 1. Audit the live container (flags + env + metrics + card-wide VRAM -> findings)
terminal(command="python3 ~/.hermes/skills/devops/hermes-vllm-operations-tuning/scripts/vllm_tune_audit.py --container vllm-b70-qwen38-gptq --metrics-url http://localhost:8081/metrics --drm-metrics-url http://100.93.126.10:8082/metrics")

# 2. Same, machine-readable (for diffing before/after)
... --json > /path/audit-before.json

# 3. Offline from saved artifacts (no docker/network)
... --inspect-file inspect.json --metrics-file metrics.txt

# 4. Benchmark decode / TTFT (1 request, thinking off, ~30 s). Raise --concurrency for C2/C4.
terminal(command="python3 ~/.hermes/skills/devops/hermes-vllm-operations-tuning/scripts/vllm_bench.py --url http://localhost:8081/v1 --model Qwen3.8-27B --prompt-tokens 2048 --max-tokens 256 --runs 2", timeout=300)

# 5. Unit tests (stdlib unittest; no network, no docker, no pytest needed)
terminal(command="python3 -m unittest discover -s ~/.hermes/skills/devops/hermes-vllm-operations-tuning/scripts/tests -v")
```

## Quick Reference

| Signal (from `/metrics`) | Healthy | Lever |
|---|---|---|
| `num_preemptions_total / request_success_total` | < 2 % | smaller sessions (Hermes `context_length` / compression), more KV (`gpu-memory-utilization`, fp8 KV), lower `max-model-len` |
| `prefix_cache_hits / queries` | > 70 % for agent traffic | keep `--enable-prefix-caching`; stable system prompt |
| MTP accepted per draft (`accepted_tokens / num_drafts`) | > 1.5 with k=4 | if last position < 25 % accepted, try `num_speculative_tokens` k-1 |
| `request_queue_time` mean | < 0.5 s | `max-num-seqs`, KV headroom (`kv_cache_max_concurrency` >= 2) |
| per-request decode tok/s (`gen_tokens / decode_time`) | 40-60 on B70 @ <=64K ctx | context size dominates; power cap; `VLLM_XPU_ENABLE_XPU_GRAPH=1` |
| card VRAM used/total + `smem` spill (drm-exporter `drm_memory_used_bytes{pool="vram"|"smem"}`) | spill < 0.5 GiB; vLLM alone at 0.965 legitimately reads ~99.7 % VRAM | any co-resident GPU tenant, even idle, pushes spill to ~1 GiB and the xe driver pages over PCIe: measured -35 % decode. Stop tenants. |
| GPU `power1_cap` (hwmon) | 180 W = best tok/Wh, 275 W = fastest (upstream table) | `set-power-limit.py` in the b70 repo |

Reference decode-vs-context numbers (upstream B70 EXL3 v2, 180 W, C1): 57 tok/s @4K, 56 @16K,
53 @64K, 40 @128K, 38 @200K. Prefill 2.2K tok/s @4K falling to 1.0K @200K. Shrinking the
working context is the biggest single-user throughput lever on this card.

## Procedure

1. Identify the brain container (`hermes config get model`, `docker ps`). Completion: you can
   name which container serves the model Hermes is on, so you know what must not restart.
2. Run the audit (How to Run step 1) and save `--json` output. Completion: findings list with
   severities, plus derived stats (preemption rate, prefix hit rate, MTP acceptance/position,
   avg prompt & generation tokens, mean TTFT, per-request decode tok/s).
3. Run the benchmark at the context size users actually hit (take `avg_prompt_tokens` from
   step 2; try 2K and that size). Completion: TTFT and decode tok/s recorded for each.
4. Pick the cheapest lever first, in this order:
   a. **No-restart levers** — Hermes `providers.<name>.context_length` and
      `compression.threshold` (bounds session size), GPU power cap, CPU governor.
   b. **Restart-of-brain levers** (needs user handoff) — `gpu-memory-utilization`,
      `num_speculative_tokens`, `max-num-seqs`, `max-num-batched-tokens`, `max-model-len`.
   c. **Rebuild levers** — image with oneDNN prefill / W4A8 / EXL3 (see `references/b70-host-baseline.md`).
   Completion: one change chosen, written to compose or Hermes config, NOT applied to the brain
   without a handoff window.
5. After apply, re-run steps 2-3 and diff the JSON. Completion: decode tok/s or preemption
   rate moved in the expected direction; otherwise revert.

## Pitfalls

- `/metrics` counters are cumulative since container start; rates derived from them average
  over the whole uptime. For a clean A/B, record two snapshots and subtract, or restart counters.
- vLLM's `Avg generation throughput` log line is aggregate across running requests; divide by
  `Running: N` to compare with per-request benchmark numbers.
- `max_num_batched_tokens` is tied to the compiled graph range on this image (6656). Changing
  it forces a ~100 s recompile and invalidates the torch_compile cache volume.
- `--gpu-memory-utilization` is reserved **at startup**; other GPU tenants (ComfyUI) must be
  stopped or vLLM crash-loops (see `intel-xe-gpu-setup`).
- `kv_cache_max_concurrency` ~1.0 means two near-max-context requests cannot coexist: the
  second queues or the first is preempted and re-prefilled. Lowering `max-model-len` does not
  add KV memory, it only stops the scheduler from admitting unservable contexts.
- Reasoning on by default (`ChatDefaults` middleware, 8K thinking budget) inflates generation
  tokens; benchmark with `reasoning_effort: none` to measure the engine, not the policy.
- Do not run the benchmark at high concurrency while the user is working; it shares the brain.

## Verification

- `python3 -m unittest discover -s scripts/tests` passes (parser, derived stats, every recommendation rule).
- Audit on the live container prints the same preemption/prefix numbers you can eyeball in
  `curl /metrics | grep -E 'preemptions|prefix_cache'`.
- Benchmark decode tok/s is within ~15 % of the upstream reference row for the same context.
