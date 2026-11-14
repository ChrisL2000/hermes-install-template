# B70 host baseline (zeppo, Debian 13) — audit snapshot 2026-10-11

Host: i7-8700K (12 threads, governor `powersave`/intel_pstate, turbo on), 62 GB RAM,
Intel Arc Pro B70 32 GB at PCI 0000:03:00.0 (`xe`, renderD129; renderD128 = i915 iGPU),
kernel 7.1.13, Docker 29.8. GPU power cap `power1_cap` = 275 W (card max).

## Containers that touch the GPU

| Container | State | Role |
|---|---|---|
| `vllm-b70-qwen38-gptq` | up, healthy, `restart=always` | **brain** — serves `Qwen3.8-27B` on :8081 |
| `comfyui-intel-arc` | up, `restart=no` | image gen; lives in the ~1 GB VRAM vLLM leaves + host RAM (fp32 offload) |
| `vllm-tools-qwen3.8-27B-INT4` | exited 0 | `intel/vllm` 0.21 AutoRound INT4, 64K ctx, no prefix cache, no spec decode, XPU graph off |
| `vLLM-Tools` | exited 0 | `intel/vllm` 0.21 Qwen2.5-7B, yarn 131K |

The two stopped `intel/vllm` containers are older generation (vLLM 0.21, no MTP, no fp8 KV,
`VLLM_XPU_ENABLE_XPU_GRAPH=0`). Do not start them while the brain is up (VRAM race).

## Brain launch profile (compose: ~/vllm-b70-qwen38-gptq/docker-compose.yml)

Image `local/b70-qwen38-vllm:base-dev` = vLLM 0.30.0 + Torch 2.13 XPU, built from
`~/src/intel-b70-qwen38-vllm` (Danmoreng). It is the author's **GPTQ rollback** profile minus
the oneDNN-prefill / W4A8 flags (that code is not in the base image). Upstream has since moved
production to **EXL3 v2** (`turboderp/Qwen3.8-27B-exl3`, 0.965 GPU fraction, 16 seqs,
4096 batched tokens, MTP k=3 + 64K draft vocab, 262K ctx, 180 W cap).

Key flags: GPTQ INT4 fp16, `max-model-len 200704`, `gpu-memory-utilization 0.93`,
`kv-cache-dtype fp8`, `max-num-seqs 4`, `max-num-batched-tokens 6656`,
`--scheduler-reserve-full-isl --watermark 0.0`, prefix caching, MTP k=4,
`VLLM_XPU_ENABLE_XPU_GRAPH=1`, `PYTORCH_ALLOC_CONF=expandable_segments:True`, shm 8 GB.

Startup accounting (from the log): weights 19.26 GiB, activation 0.94, graphs 0.99,
KV **7.98 GiB = 212,255 fp8 tokens** → `kv_cache_max_concurrency 1.06` at 200K.
torch.compile 97 s (cached on the `b70-vllm-cache` volume); engine init 161 s.

## Live metrics after 8 days / 1,402 requests

| Stat | Value | Reading |
|---|---|---|
| avg prompt / generation tokens | 62,016 / 991 | agent sessions average 62K context, peak seen ~140K (KV 66 % with 1 request) |
| prefix-cache hit rate | 90 % | excellent — only ~10 % of prompt tokens are computed |
| preemptions | 257 (18 % of requests) | **bad** — a second long session cannot fit beside a 100K+ one |
| mean queue time | 0.95 s | admission waits, same cause |
| mean TTFT | 8.1 s | ≈ 6.5K uncached tokens / request at ~935 tok/s |
| decode tok/s per request | 43.4 | cumulative average (includes concurrent periods) |
| MTP k=4 | 1.82 accepted/draft; by position 70/50/36/27 % | ≈ 2.8 tokens per step; k=4 is justified, k=3 is the alternative to A/B |

Benchmark (`vllm_bench.py`, thinking off, `ignore_eos`, while one other request was running):

| prompt | TTFT | decode |
|---|---|---|
| 2K | 1.57 s | 39 tok/s |
| 16K | 10.8 s (~1.5K tok/s prefill) | 32 tok/s |

Upstream EXL3 v2 reference on the same card at 180 W (C1, nothing else running):
58 tok/s @2K, 56 @16K, 53 @64K, 40 @128K, 38 @200K; prefill 2.2K→1.0K tok/s.

## 2026-10-12 after #1 (context_length 131072) + #2 (gpu-memory-utilization 0.965) applied

Container recreated by the user (`docker compose up -d`); healthy in 70 s (compile cache hit:
torch.compile 0.09 s). KV **9.04 GiB = 241,133 tokens**, max concurrency 1.20 at 200K.
Card VRAM 99.7 % committed by vLLM alone, smem spill 0.26 GiB (vs 1.0 GiB with ComfyUI) —
that is the normal signature of 0.965 and is harmless.

| prompt | ComfyUI resident, 0.93 | vLLM alone, 0.93 (another session active) | vLLM alone, 0.965, idle card |
|---|---|---|---|
| 2K   | 39 tok/s | 60 | **82 tok/s**, TTFT 2.7 s |
| 16K  | 32 | 55 | **71 tok/s**, TTFT 9.2 s |
| 64K  | — | 47 | **66 tok/s**, TTFT 53 s |

The jump 60→82 is mostly the card being idle (the 140K-context session that was running
during the earlier benches died with the restart), not the extra KV; it is the true
single-user ceiling of this image. MTP acceptance on the salted bench prompts: 2.29/draft.

CPU governor test (2026-10-12, performance vs powersave, idle card, 2 waves each):
2K 80 vs 82 tok/s, 16K 70 vs 71, 64K 66 vs 66, TTFT unchanged — **no effect** (within
noise). The per-step CPU work is not on the critical path on this host. Leave `powersave`
(intel_pstate active mode still boosts on demand) and do not persist a performance unit.

Remaining levers: spec tokens A/B (restart), EXL3/oneDNN rebuild.

## 2026-10-11 re-audit after ComfyUI disabled (user decision: no co-residence)

`docker stop comfyui-intel-arc` (restart=no, compose header marks it DISABLED). Card VRAM went
34.23 -> 29.73 GB of 34.24 GB; the ~4.5 GB ComfyUI held was pushing the card to 100 % and the
xe driver was paging to system memory (drm `smem` pool 1 GB on the B70).

| prompt | before (ComfyUI idle, resident) | after (vLLM alone) | upstream EXL3 v2 @180 W |
|---|---|---|---|
| 2K   | 39 tok/s, TTFT 1.6 s | **60 tok/s**, TTFT 1.2 s | 58 tok/s |
| 16K  | 32 tok/s, TTFT 10.8 s | **55 tok/s**, TTFT 9.7 s | 56 tok/s |
| 64K  | — | **47 tok/s**, TTFT 55 s (1.2K tok/s prefill) | 53 tok/s, 39 s |
| C2 @2K | — | 49 + 50 = 98 tok/s aggregate | 108 |

Decode is now at parity with upstream's qualified image for short/medium context; the gap
that remains is prefill at 64K+ (1.2K vs 1.7K tok/s — that is the oneDNN/W4A8 prefill path
the base-dev image lacks) and the long-context tail.

Revised ranking (ComfyUI gone):

1. Hermes `context_length` 200704 -> 131072 on both configs (no restart). Still #1: the
   18 % preemption rate and 62K average prompt are workload, not engine, problems.
2. `gpu-memory-utilization 0.93 -> 0.965` (brain restart). Now safe; +~1 GiB KV ≈ +28K
   tokens (212K -> ~240K), kv_max_concurrency 1.06 -> ~1.2. Pair with #1 so two 65K
   sessions plus headroom fit comfortably.
3. CPU governor performance (no restart) — measure, expect a few %.
4. A/B `num_speculative_tokens` 3 vs 4 (brain restart) — position-3 acceptance 27 %.
5. EXL3 v2 / oneDNN image rebuild — only lever left for 64K+ prefill.

Hard rule going forward: nothing else holds VRAM on the B70 while the brain runs. Audit with
`--drm-metrics-url` and treat `vram_used_frac > 0.97` as a HIGH finding.

## Ranked recommendations (original, ComfyUI co-resident)

1. **Bound the working context (no restart).** Hermes `providers.vllm-b70-qwen38.context_length`
   is 200704 and `compression.threshold` 0.5 → sessions grow to ~100–140K before compaction.
   Setting `context_length: 131072` (compaction at ~65K) keeps decode in the 50 tok/s band
   instead of 35–40, cuts TTFT roughly in half, and makes two sessions fit in KV
   (2 × 65K < 212K) which removes the 18 % preemption rate. Apply to both chrismok's and
   zeppo's config. This is the biggest lever and costs nothing.
2. **CPU governor → performance (no restart).** `powersave` on intel_pstate; the scheduler +
   MTP rejection sampling run on CPU 15+ times/s. Expect a few % on decode; measure with the
   bench before/after.
3. **A/B `num_speculative_tokens` 3 vs 4 (brain restart).** Position-3 acceptance is 27 %; the
   extra MTP forward may or may not pay. Needs a handoff window; ~3 min restart (compile cached).
4. **`gpu-memory-utilization 0.93 → 0.95` only if ComfyUI is retired from co-residence**
   (+0.6 GiB ≈ +16K KV tokens). Not worth it while ComfyUI shares the card.
5. **Rebuild path (largest engine gain, biggest effort):** upstream's EXL3 v2 image
   (oneDNN prefill, M04 verification, draft vocab, 16 seqs) measures ~1.5× this profile's
   decode and ~1.5–2× prefill. Requires the EXL3 checkpoint download, the production image
   build (`scripts/build-image.sh`), and a full compile; ComfyUI cannot co-reside at 0.965.
6. Leave alone: fp8 KV, prefix caching, XPU graph, `max-num-batched-tokens 6656` (tied to the
   compiled range), power cap 275 W (already max; 180 W only if tok/Wh matters), shm 8 GB.
