# vllm-b70-qwen38-deploy

Reproducible deployment of **Qwen3.8-27B** served by **vLLM 0.30 XPU** on a single
**Intel Arc Pro B70 (32 GB)**, packaged as a
[Hermes Agent](https://hermes-agent.nousresearch.com) skill. Includes the throughput-tuning
skill that produced the profile.

**Current deployment (accepted 2026-10-24): v3 — EXL3 4.00bpw.**
Image `local/b70-qwen38-vllm:zeppo-exl3-v3`, model
`turboderp/Qwen3.8-27B-exl3` @ `113cf7ab`, container `vllm-b70-qwen38-v3`,
`--max-model-len 262144`, MTP 3, fp8 KV, 0.965 GPU. The live compose is committed
at `assets/docker-compose-v3-exl3.yml` (see SKILL.md → "v3 deployment" for the
build lineage and switch procedures).

This repo's `deploy.sh` documents the **v1/v2 GPTQ-INT4 path** (the original
tuned GPTQ profile below) — keep it for fresh-host builds or rollback:

Measured on an i7-8700K / Debian 13 host with the card to itself (275 W cap):

| prompt | TTFT | decode |
|---|---|---|
| 2K | 1.0 s | **82 tok/s** |
| 16K | 9.2 s | **71 tok/s** |
| 64K | 53 s | **66 tok/s** |

KV cache 241K tokens (fp8), 200K max context, prefix caching, 4 concurrent sequences.

Built on [Danmoreng/intel-b70-qwen38-vllm](https://github.com/Danmoreng/intel-b70-qwen38-vllm)
(the B70 kernels, image build and GPTQ profile are theirs; this repo pins a commit, fixes a
Dockerfile checksum, and wraps it in an idempotent deploy with a tuned compose).

## Layout

```
SKILL.md                      Hermes skill: v3 (EXL3) current deploy + v1/v2 (GPTQ) procedure
assets/docker-compose.yml     v1/v2 tuned compose, rendered from .env
assets/docker-compose-v3-exl3.yml  v3 EXL3 compose (byte-for-byte the live one)
assets/env.example            every pin and host setting in one place
assets/patches/*.patch        upstream Dockerfile sha fix (build fails without it)
scripts/deploy.sh             preflight -> clone -> patch -> build -> volumes -> model -> start -> wait healthy
scripts/verify.sh             /health, /v1/models, one real completion
tuning/                       hermes-vllm-operations-tuning skill (audit + bench + 27 unit tests)
```

## Quick start

```bash
git clone https://github.com/ChrisL2000/vllm-b70-qwen38-deploy.git
cd vllm-b70-qwen38-deploy
cp assets/env.example .env
$EDITOR .env                 # RENDER_GID (getent group render), B70_REPO_DIR, HOST_PORT
scripts/deploy.sh            # ~40 min image build + 17 GB model on first run
scripts/verify.sh
```

Prerequisites: Debian 13 (kernel 7.1+) with the `xe` driver bound to the B70, Docker 27+
with compose v2, ~60 GB disk, and **nothing else holding VRAM** (see tuning notes — an idle
ComfyUI beside vLLM cost 35 % decode throughput).

`deploy.sh` is idempotent: re-run after any failure; `--skip-build --skip-download` to only
(re)create the container; `--no-start` to prepare without touching a running server;
`--yes` to confirm replacing a healthy container; `--force` to ignore other GPU containers.

## Using it as Hermes skills

```bash
cp -r . ~/.hermes/skills/devops/vllm-b70-qwen38-deploy
cp -r tuning ~/.hermes/skills/devops/hermes-vllm-operations-tuning
```

Then in Hermes: "deploy the B70 vLLM container" / "audit vLLM throughput". If Hermes runs on
this server, set the provider's `context_length` to **131072** (not 200704) so sessions
compact around 65K tokens — the single biggest throughput lever found during tuning.

## Tuning summary (details in `tuning/references/b70-host-baseline.md`)

1. No GPU co-tenants. VRAM at 100 % makes the xe driver page over PCIe: 39 → 60 tok/s.
2. `gpu-memory-utilization 0.965` once alone on the card: KV 212K → 241K tokens.
3. Hermes `context_length` 131072: avg prompt was 62K with 18 % request preemption at 200K.
4. CPU governor performance vs powersave: no measurable effect.
5. Resolved 2026-10-24: upstream's EXL3 image adopted as v3 (see above).
   `num_speculative_tokens` A/B remains open.

Run the audit yourself:

```bash
python3 tuning/scripts/vllm_tune_audit.py --container vllm-b70-qwen38-gptq \
  --metrics-url http://localhost:8081/metrics
python3 tuning/scripts/vllm_bench.py --prompt-tokens 2048 --max-tokens 256
python3 -m unittest discover -s tuning/scripts/tests -v
```

## License

MIT. Upstream components keep their own licenses (see the Danmoreng repository).
