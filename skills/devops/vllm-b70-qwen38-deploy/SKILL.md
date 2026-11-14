---
name: vllm-b70-qwen38-deploy
description: "Reproduce the tuned Qwen3.8-27B vLLM container on a B70."
version: 0.2.0
author: ChrisL2000, Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [vllm, docker, intel-arc, xpu, qwen, exl3, gptq, mtp, deploy, reproducible]
    related_skills: [hermes-vllm-operations-tuning, intel-xe-gpu-setup, ai-container-ops]
    category: devops
---

# Deploy the tuned Qwen3.8-27B vLLM container (Intel Arc Pro B70)

Rebuilds, from a bare Debian host, the exact container this skill was extracted from:
vLLM 0.30.0 XPU + Danmoreng's B70 kernels. Everything is pinned (upstream commit,
model revision, image tag) so two hosts produce the same server.

**Two deployment generations — v3 (EXL3) is the accepted current deployment:**

| | v1/v2 (legacy, this skill's deploy.sh) | **v3 (current, ACCEPTED 2026-10-24)** |
|---|---|---|
| quant | GPTQ-INT4 + MTP | **EXL3 4.00bpw** (XPU, turboderp) + MTP |
| image | `local/b70-qwen38-vllm:base-dev` | `local/b70-qwen38-vllm:zeppo-exl3-v3` |
| model | `mikeinnyc/Qwen3.8-27B-GPTQ-Int4-sym-G128-MTP-BF16` @ `a47b0c6f…` | `turboderp/Qwen3.8-27B-exl3` @ `113cf7ab` |
| dir | `~/vllm-b70-qwen38-gptq` (v2: `~/vllm-b70-qwen38-v2`) | `~/vllm-b70-qwen38-v3` |
| state | Exited, `restart=no` (superseded) | **Up, `restart=always`, healthy** |

The GPTQ procedure below (deploy.sh/verify.sh, `.env`, `base-dev` image) still
documents the v1/v2 build path — keep it for rollback or fresh-host builds where
the EXL3 release image isn't available. v3's full compose is
`assets/docker-compose-v3-exl3.yml` (byte-for-byte the live one).

## v3 deployment (EXL3 4.00bpw) — current

Live since 2026-10-24. Container `vllm-b70-qwen38-v3`, image
`local/b70-qwen38-vllm:zeppo-exl3-v3`, served name `Qwen3.8-27B` on host `:8081`.

Build lineage (from the compose header + upstream branch `zeppo-v3`):
- Image = migration-target built by exl3xpu `scripts/zeppo_build_target_image.py`
  on top of `zeppo-v2-gptq-onednn` + release metadata
  (`scripts/zeppo-prepare-exl3-release-image.py`, upstream branch `zeppo-v3`).
  `_C.so` and `m04.so` hashes match the author's release exactly; `libdnnl` is our
  GPU-only build (same as v2). `m04.so` sha256 is pinned in the compose env
  (`EXL3_M04_LIBRARY_SHA256=eaa18427…`).
- Model: `turboderp/Qwen3.8-27B-exl3` @ `113cf7ab958054860e43fb7f3063b1af19171095`,
  snapshot at `~/.cache/exl3xpu/turboderp-Qwen3.8-27B-exl3-4.00bpw`, bind-mounted
  read-only at `/models/checkpoint`. `HF_HUB_OFFLINE=1`.
- Args/env = the image's frozen migration-target-optimized.yaml profile (same as
  the author's `run-server-exl3.py`).

Key v3 profile values (full list in `assets/docker-compose-v3-exl3.yml`):
`--max-model-len 262144 --kv-cache-dtype fp8 --gpu-memory-utilization 0.965
--max-num-seqs 16 --max-num-batched-tokens 4096 --mamba-cache-mode align
--speculative-config mtp/3 --enable-prefix-caching` + EXL3 env
(`EXL3_TARGET_RUNTIME=vllm030`, `EXL3_INT8_PREFILL=1`, `EXL3_GUARDED_ATTN=1`,
`EXL3_SDPA_CACHE_CAPACITY=64`, onednn routes OFF, `B70_GPTQ_*` OFF).

Operate (v1/v2/v3 share port 8081 and the GPU — only ONE may run):
```
# switch TO v3:
(cd ~/vllm-b70-qwen38-v2 && docker compose stop) ; (cd ~/vllm-b70-qwen38-v3 && docker compose up -d)
# switch back to v2:
(cd ~/vllm-b70-qwen38-v3 && docker compose down) ; (cd ~/vllm-b70-qwen38-v2 && docker compose up -d)
# health:
curl -fsS http://localhost:8081/health && docker ps --filter name=vllm-b70-qwen38-v3
```

Hermes model alias `qwen-b70` (admin scope, `/etc/hermes/config.yaml`) points at this
provider — the user may refer to the brain by that name.

## Second model on the same B70 (mutually-exclusive sibling stack)

A second ~16 GB EXL3 checkpoint does not fit alongside the live one in 32 GB
VRAM, so a second model is a SIBLING stack, not a concurrent one (see
`~/deploy-uncensored/` — an uncensored `junafinity/Qwen-3.8-27B-Uncensored`
checkpoint served from the same image; only the checkpoint mount, cache volumes
and directory differ). Pattern that keeps the swap safe and transparent:

- **Both projects bind the SAME host port** (`:8081`). Docker's port allocator is
  then a hard mutex: `up` on the second while the first holds the port fails
  "port is already allocated" BEFORE the vLLM process launches, so the running
  brain and its 0.965 VRAM reservation are never touched.
- **A `switch.sh` stops one stack before starting the other** (`docker compose
  stop`, then `up -d`), and waits on `/health` (first boot recompiles cold, up to
  ~25 min).
- **Keep `--served-model-name` identical across both** so a client pointed at the
  port needs no config change; tell the two apart by container name, not model
  name (the Hermes provider `default_model` stays `Qwen3.8-27B` either way).
- **Separate named cache volumes per stack**, and `restart: "no"` on the opt-in
  sibling so a host reboot brings back only the primary.

GPU utilization on the B70 (for monitoring/MOTD): read the xe driver per-tile
idle residency, not `intel_gpu_top` — the latter needs CAP_PERFMON (sudo) as
non-root and its default table shows the first card (usually the iGPU), not the
discrete Arc. Sample
`/sys/class/drm/card*/device/tile*/gt*/gtidle/idle_residency_ms` twice ~1 s
apart; busy% = 100 × (1 − (min tile's idle delta ÷ window)). `idle_status` in
the same dir gives the C-state (`gt-c6` = deep idle).

## When to Use

- New or rebuilt host needs the Qwen3.8-27B brain back exactly as tuned
- Upgrading the host OS / Docker and re-validating the brain afterwards
- Someone else wants this stack on their B70 (point them at the GitHub repo)
- Don't use for: day-to-day start/stop (`ai-container-ops`), driver problems
  (`intel-xe-gpu-setup`), performance analysis (`hermes-vllm-operations-tuning`)

## Prerequisites

- Debian 13 (kernel 7.1+) with the `xe` driver loaded and the B70 on a render node
  (`intel-xe-gpu-setup` covers this); `getent group render` GID goes in `.env`
- Docker 27+ with compose v2; the user in the `docker` group
- ~60 GB free disk: 14 GB image + 17 GB model + compile caches; 32 GB VRAM free (no other
  GPU container running — ComfyUI etc. must be stopped, see tuning skill)
- Outbound HTTPS to github.com, files.pythonhosted.org, huggingface.co during deploy only
- `python3`, `git`, `curl`, `sha256sum`

## How to Run (v1/v2 GPTQ path)

> The v3 deployment does NOT use deploy.sh — its compose is applied directly
> from `~/vllm-b70-qwen38-v3` (see "v3 deployment" above). This path is for
> the GPTQ image: fresh hosts, or rollback to v1/v2.

```
# 0. configure
terminal(command="cd ~/.hermes/skills/devops/vllm-b70-qwen38-deploy && cp -n .env.example .env && $EDITOR .env")

# 1. full deploy: preflight -> clone+patch upstream -> build image (~40 min) -> volumes/network -> model download (~17 GB) -> start -> wait healthy (<= 25 min first boot)
terminal(command="~/.hermes/skills/devops/vllm-b70-qwen38-deploy/scripts/deploy.sh", timeout=7200)

# re-runs are idempotent; skip stages you already have:
#   deploy.sh --skip-build --skip-download        (just (re)create the container)
#   deploy.sh --no-start                           (prepare everything, do not touch the running brain)

# 2. prove it works (health, model list, one real completion, token counts)
terminal(command="~/.hermes/skills/devops/vllm-b70-qwen38-deploy/scripts/verify.sh", timeout=300)

# 3. optional: throughput audit + bench via the sibling skill
terminal(command="python3 ~/.hermes/skills/devops/hermes-vllm-operations-tuning/scripts/vllm_bench.py --prompt-tokens 2048 --max-tokens 256", timeout=300)
```

## Quick Reference (v1/v2 GPTQ)

| Item | Value |
|---|---|
| upstream | github.com/Danmoreng/intel-b70-qwen38-vllm @ `ed47c5b` (pinned in `.env`) |
| image | `local/b70-qwen38-vllm:base-dev` built by upstream `scripts/build-image.sh` + `patches/dockerfile-b70-attention-sha.patch` |
| model | `mikeinnyc/Qwen3.8-27B-GPTQ-Int4-sym-G128-MTP-BF16` @ `a47b0c6f…` |
| served name / port | `Qwen3.8-27B` on host `:8081` (container 8000) |
| compose | `assets/docker-compose.yml` (rendered with `.env`) |
| volumes | HF cache (model), vllm compile cache, triton cache — all named, survive recreate |
| middleware | `runtime/request_defaults.py` (16K max tokens, 8K thinking budget, reasoning medium) bind-mounted ro |
| healthcheck | `/health` every 15 s, `start_period` 1200 s (first boot compiles ~100 s, cold model load longer) |

Compose args are the tuned profile: `--max-model-len 200704 --gpu-memory-utilization 0.965
--kv-cache-dtype fp8 --max-num-seqs 4 --max-num-batched-tokens 6656 --scheduler-reserve-full-isl
--watermark 0.0 --enable-prefix-caching --mamba-cache-mode align --speculative-config mtp/4`.

## Procedure

1. `cp .env.example .env`; set `RENDER_GID` (from `getent group render`), `HOST_PORT`, and
   `B70_REPO_DIR` (where upstream will be cloned). Completion: `.env` exists and
   `scripts/deploy.sh --no-start` passes preflight without errors.
2. Run `scripts/deploy.sh`. It stops at the first failing stage with the stage name;
   re-run after fixing — every stage is idempotent (clone is fetch+checkout, build is
   skipped when the image exists, download is skipped when the snapshot exists).
   Completion: output ends with `healthy` and the container name.
3. Run `scripts/verify.sh`. Completion: `/health` 200, `/v1/models` lists `Qwen3.8-27B`,
   a chat completion returns text with `usage.completion_tokens > 0`.
4. If this host runs Hermes on the brain: set the context window to the auto-detected
   `max_model_len` (`curl http://localhost:8081/v1/models`: v3 EXL3 = 262144; the v2 GPTQ
   server was 200704, pinned at 131072). On this host set the top-level `model.context_length`
   pin (user scope) — the provider entry is admin-managed in `/etc/hermes/config.yaml`, and the
   pin takes precedence over it while the route matches the configured default.
   Completion: `hermes config get model` shows the pin; verify the effective window with the
   tuning skill's resolver recipe.
5. Record the deploy in `hermes-vllm-operations-tuning`'s `references/b70-host-baseline.md` if numbers differ.

## Pitfalls

- Upstream's `docker/Dockerfile` pins a sha256 for `m04/b70_attention.py` that does not match
  the file in the same commit; the build fails at the `sha256sum -c` step. The patch in
  `patches/` fixes the expected hash (verified: the file's real sha is `d84d857e…`).
- `build-image.sh` validates the tag with `release_integrity.py`; production/rollback tag
  names are refused. Keep `VLLM_IMAGE=local/b70-qwen38-vllm:base-dev` or another non-release tag.
- The model download runs `hf download` inside the built image (no host Python deps);
  it needs the image first. Snapshot lands at `<volume>/hub/models--mikeinnyc--…/snapshots/<rev>`.
- `gpu-memory-utilization 0.965` is reserved at startup: any other process holding VRAM makes
  vLLM crash-loop with `Free memory on device … less than desired`. deploy.sh refuses to start
  while another running container has `/dev/dri` mapped; pass `--force` to override.
- Docker's `start_period` only suppresses health failures; `docker ps` shows `Up` during model
  load. deploy.sh waits for `health=healthy`, not `Up`.
- Recreating the container is a brain restart if Hermes is running on it. deploy.sh prints a
  warning and requires `--yes` when the container already exists and is healthy.
- Deploying a new checkpoint that is already in the mounted model dir (or hardlinked there) is
  just a restart: `docker start <name>` resumes the existing container with its current mounts —
  no copy, no compose edit, no recreation. Prefer `docker start` over `docker compose up -d` for
  this: the latter re-evaluates the compose (may recreate) and is flagged by the terminal tool as
  a long-lived server process.
- The render GID is baked into the container's supplementary groups via `group_add`; a wrong
  GID gives `Failed to infer device type` with no clearer error.
- To serve a DIFFERENT model, its EXL3 quant must be produced on a CUDA box, not this host:
  exllamav3's convert.py is CUDA-only (torch CUDA 12.4+, fused Viterbi trellis kernel, no CPU or
  XPU/SYCL mode), and the exl3xpu runtime image is serving-only (no converter in the fork). This
  host has no CUDA GPU (only Intel UHD 630 + Battlemage G21). The output must be a mul1-codebook
  EXL3 at 4 or 6 bpw — the default exl3xpu build rejects 2/3/5 bpw and mcg/3inst codebooks.
  Converter version + vision quantization are also load-critical: exllamav3 1.6.6 with `-vb 6`
  (quantized vision tower) fails VllmConfig validation with "Duplicate canonical EXL3 module: k_proj"
  on this exl3xpu runtime. The known-good reference (turboderp/Qwen3.8-27B-exl3) is version 1.4.2 with
  no `vision_bits`. Re-convert at 1.4.2, text-first (drop `-vb`), keeping `-b 4.00 -hb 6 -mb 4`.
- Converter version AND vision quantization must match the runtime image, which was built against
  exllamav3 1.4.2 with NO `vision_bits`. A quant made with 1.6.6 + `-vb 6` (vision_bits=6) crashes
  the server at load with `pydantic_core.ValidationError: Value error, Duplicate canonical EXL3
  module: k_proj` (the quantized vision k_proj collides with the language k_proj in the runtime's
  canonical map). Fix: re-convert on the CUDA box (host `peeweeherman`) with converter ~1.4.2 and
  drop `-vb 6`, keeping `-b 4.00 -hb 6 -mb 4`. The `junafinity/Qwen-3.8-27B-Uncensored` sibling
  is an abliterated (zerofuse) fork; it has been re-converted to the fixed 1.4.2/no-`vision_bits`
  format and now loads cleanly (409 EXL3 modules, 8 in the MTP head).

## Verification

- `scripts/verify.sh` exits 0 and prints the completion text and token usage.
- `docker logs <name> | grep 'XPU KV cache size'` shows ~241,000 tokens at 0.965.
- Sibling bench: ~80 tok/s decode at 2K prompt on an idle card at 275 W.
- v3 (EXL3): `curl -fsS http://localhost:8081/health` returns 200,
  `docker ps --filter name=vllm-b70-qwen38-v3` shows `Up ... (healthy)`, and
  `/v1/models` lists `Qwen3.8-27B`. v3 was ACCEPTED by the user 2026-10-24
  after the off-hours run; v1/v2 containers are kept (Exited, `restart=no`)
  purely as rollback targets.
