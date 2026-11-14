#!/usr/bin/env python3
"""Audit a vLLM Docker container for token-throughput tuning.

Inputs: `docker inspect` JSON (live or file) + Prometheus /metrics text (live or file).
Output: derived stats and ranked findings (text or --json). Pure stdlib, no side effects.

  vllm_tune_audit.py --container NAME --metrics-url http://localhost:8081/metrics [--json]
  vllm_tune_audit.py --inspect-file inspect.json --metrics-file metrics.txt [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from typing import Any

# ----------------------------------------------------------------------------- parsing

_METRIC_RE = re.compile(r"^([A-Za-z_:][A-Za-z0-9_:]*)(\{([^}]*)\})?\s+([-+0-9.eE]+|NaN|[-+]Inf)\s*$")
_LABEL_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"')


def parse_prometheus(text: str) -> dict[str, list[tuple[dict[str, str], float]]]:
    """Return {metric_name: [(labels, value), ...]} for every sample line."""
    out: dict[str, list[tuple[dict[str, str], float]]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _METRIC_RE.match(line)
        if not m:
            continue
        name, _, label_blob, raw = m.groups()
        labels = {k: v for k, v in _LABEL_RE.findall(label_blob or "")}
        try:
            val = float(raw)
        except ValueError:
            continue
        out.setdefault(name, []).append((labels, val))
    return out


def metric_sum(metrics: dict, name: str, **where: str) -> float | None:
    """Sum samples of `name` whose labels match all `where` pairs. None if absent."""
    samples = metrics.get(name)
    if not samples:
        return None
    total = 0.0
    hit = False
    for labels, val in samples:
        if all(labels.get(k) == v for k, v in where.items()):
            total += val
            hit = True
    return total if hit else None


def parse_vllm_args(cmd: list[str]) -> dict[str, Any]:
    """Turn a `vllm serve` argv tail into {flag: value}. Flags use their --name with
    dashes replaced by underscores. Bare flags get True. JSON-looking values are decoded."""
    args: dict[str, Any] = {}
    positional: list[str] = []
    i = 0
    while i < len(cmd):
        tok = cmd[i]
        if tok.startswith("--"):
            key, eq, val = tok[2:].partition("=")
            if not eq:
                nxt = cmd[i + 1] if i + 1 < len(cmd) else None
                if nxt is not None and not nxt.startswith("--"):
                    val = nxt
                    i += 1
                else:
                    val = ""
            key = key.replace("-", "_")
            if key.startswith("no_") and val == "":
                args[key[3:]] = False
            elif val == "":
                args[key] = True
            else:
                args[key] = _coerce(val)
        else:
            positional.append(tok)
        i += 1
    if positional:
        args["model"] = positional[0]
    return args


def _coerce(val: str) -> Any:
    s = val.strip()
    if s and s[0] in "{[":
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return s
    for cast in (int, float):
        try:
            return cast(s)
        except ValueError:
            pass
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    return s


def parse_inspect(inspect_json: Any) -> dict[str, Any]:
    """Extract what the audit needs from `docker inspect` output (list or single dict)."""
    obj = inspect_json[0] if isinstance(inspect_json, list) else inspect_json
    cfg = obj.get("Config", {})
    host = obj.get("HostConfig", {})
    env = {}
    for kv in cfg.get("Env") or []:
        k, _, v = kv.partition("=")
        env[k] = v
    entry = cfg.get("Entrypoint") or []
    cmd = cfg.get("Cmd") or []
    argv = list(entry) + list(cmd)
    # strip leading `vllm serve` if present
    tail = argv
    for i in range(len(argv) - 1):
        if argv[i].endswith("vllm") and argv[i + 1] == "serve":
            tail = argv[i + 2 :]
            break
    return {
        "name": obj.get("Name", "").lstrip("/"),
        "image": cfg.get("Image"),
        "args": parse_vllm_args(tail),
        "env": env,
        "shm_bytes": host.get("ShmSize", 0),
        "restart_policy": (host.get("RestartPolicy") or {}).get("Name"),
        "health": ((obj.get("State") or {}).get("Health") or {}).get("Status"),
    }

# ----------------------------------------------------------------------------- derived stats


def _ratio(num: float | None, den: float | None) -> float | None:
    if num is None or not den:
        return None
    return num / den


def derive_stats(metrics: dict) -> dict[str, Any]:
    """Compute the handful of numbers that drive tuning decisions."""
    s: dict[str, Any] = {}
    succ = metric_sum(metrics, "vllm:request_success_total")
    s["requests"] = succ
    s["preemptions"] = metric_sum(metrics, "vllm:num_preemptions_total")
    s["preemption_rate"] = _ratio(s["preemptions"], succ)

    q = metric_sum(metrics, "vllm:prefix_cache_queries_total")
    h = metric_sum(metrics, "vllm:prefix_cache_hits_total")
    s["prefix_hit_rate"] = _ratio(h, q)
    s["prefix_queries"] = q

    drafts = metric_sum(metrics, "vllm:spec_decode_num_drafts_total")
    draft_tok = metric_sum(metrics, "vllm:spec_decode_num_draft_tokens_total")
    acc = metric_sum(metrics, "vllm:spec_decode_num_accepted_tokens_total")
    s["spec_drafts"] = drafts
    s["spec_k"] = round(draft_tok / drafts) if drafts and draft_tok else None
    s["spec_accept_rate"] = _ratio(acc, draft_tok)
    s["spec_accepted_per_draft"] = _ratio(acc, drafts)
    per_pos = {}
    for labels, val in metrics.get("vllm:spec_decode_num_accepted_tokens_per_pos_total", []):
        pos = labels.get("position")
        if pos is not None:
            per_pos[int(pos)] = per_pos.get(int(pos), 0.0) + val
    s["spec_accept_by_pos"] = (
        {p: (v / drafts if drafts else None) for p, v in sorted(per_pos.items())} if per_pos else {}
    )

    pc = metric_sum(metrics, "vllm:request_prompt_tokens_count")
    ps = metric_sum(metrics, "vllm:request_prompt_tokens_sum")
    gc = metric_sum(metrics, "vllm:request_generation_tokens_count")
    gs = metric_sum(metrics, "vllm:request_generation_tokens_sum")
    s["avg_prompt_tokens"] = _ratio(ps, pc)
    s["avg_generation_tokens"] = _ratio(gs, gc)

    ttft_s = metric_sum(metrics, "vllm:time_to_first_token_seconds_sum")
    ttft_c = metric_sum(metrics, "vllm:time_to_first_token_seconds_count")
    s["mean_ttft_s"] = _ratio(ttft_s, ttft_c)

    dec_s = metric_sum(metrics, "vllm:request_decode_time_seconds_sum")
    s["decode_tok_s_per_request"] = _ratio(gs, dec_s)
    pre_s = metric_sum(metrics, "vllm:request_prefill_time_seconds_sum")
    miss = (q - h) if (q is not None and h is not None) else None
    s["prefill_tok_s_on_misses"] = _ratio(miss, pre_s)

    qt_s = metric_sum(metrics, "vllm:request_queue_time_seconds_sum")
    qt_c = metric_sum(metrics, "vllm:request_queue_time_seconds_count")
    s["mean_queue_s"] = _ratio(qt_s, qt_c)

    s["kv_usage_now"] = metric_sum(metrics, "vllm:kv_cache_usage_perc")
    s["running_now"] = metric_sum(metrics, "vllm:num_requests_running")
    s["waiting_now"] = metric_sum(metrics, "vllm:num_requests_waiting")

    cc = metrics.get("vllm:cache_config_info") or []
    if cc:
        labels = cc[0][0]
        s["kv_cache_tokens"] = _coerce(labels.get("kv_cache_size_tokens", ""))
        s["kv_max_concurrency"] = _coerce(labels.get("kv_cache_max_concurrency", ""))
        s["kv_dtype"] = labels.get("cache_dtype")
    return s

# ----------------------------------------------------------------------------- rules

SEV_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


def finding(sev: str, key: str, title: str, detail: str, action: str) -> dict[str, str]:
    return {"severity": sev, "key": key, "title": title, "detail": detail, "action": action}


def recommend(args: dict[str, Any], env: dict[str, str], stats: dict[str, Any],
              host: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Apply every tuning rule; return findings sorted by severity. `host` may carry
    power_cap_w, cpu_governor, other_gpu_tenants (list), hermes_context_length,
    hermes_compression_threshold."""
    host = host or {}
    out: list[dict[str, str]] = []

    pr = stats.get("preemption_rate")
    if pr is not None and pr > 0.05:
        out.append(finding(
            "high", "preemptions",
            f"Preemption rate {pr:.0%} (healthy < 2%)",
            "Each preemption evicts a running request's KV and re-prefills it later. With "
            f"kv_max_concurrency={stats.get('kv_max_concurrency')} at max_model_len="
            f"{args.get('max_model_len')}, two long contexts cannot coexist.",
            "Bound session size (Hermes provider context_length / compression.threshold) so "
            "avg_prompt_tokens stays well under kv_cache_tokens/2, or raise KV memory.",
        ))
    elif pr is not None and pr > 0.02:
        out.append(finding("medium", "preemptions", f"Preemption rate {pr:.1%}",
                           "Occasional re-prefill under concurrent long contexts.",
                           "Watch; reduce working context or raise KV if it grows."))

    ap = stats.get("avg_prompt_tokens")
    kv = stats.get("kv_cache_tokens")
    if ap and kv and ap > kv * 0.25:
        out.append(finding(
            "high", "context_size",
            f"Average prompt {ap:,.0f} tokens is {ap / kv:.0%} of the KV cache ({kv:,} tokens)",
            "Decode speed on the B70 falls ~30% from 64K to 128K context and prefill cost is "
            "linear in prompt size; large working contexts are the dominant per-token cost.",
            "Lower Hermes providers.<name>.context_length (e.g. 131072) and/or "
            "compression.threshold so sessions compact earlier.",
        ))

    hr = stats.get("prefix_hit_rate")
    if args.get("enable_prefix_caching") is False:
        out.append(finding("high", "prefix_caching", "Prefix caching disabled",
                           "Agent traffic resends the same system prompt + history every turn.",
                           "Add --enable-prefix-caching."))
    elif hr is not None and hr < 0.5 and (stats.get("prefix_queries") or 0) >= 1_000_000:
        out.append(finding("medium", "prefix_caching", f"Prefix-cache hit rate {hr:.0%}",
                           "Low reuse: unstable system prompt, or KV too small to retain history.",
                           "Keep the system prompt byte-stable across turns; raise KV memory."))
    elif hr is not None:
        out.append(finding("info", "prefix_caching", f"Prefix-cache hit rate {hr:.0%}",
                           "Healthy reuse.", "None."))

    k = stats.get("spec_k")
    by_pos = stats.get("spec_accept_by_pos") or {}
    if k and by_pos:
        last = by_pos.get(max(by_pos))
        apd = stats.get("spec_accepted_per_draft")
        if last is not None and last < 0.25:
            out.append(finding(
                "medium", "spec_tokens",
                f"MTP position {max(by_pos)} accepted only {last:.0%}",
                f"k={k}, accepted/draft={apd:.2f}. The last draft position adds little but costs an "
                "extra MTP forward per step (single MTP layer re-run; vLLM warns about this).",
                f"Benchmark num_speculative_tokens={k - 1} vs {k}; keep whichever gives higher decode tok/s.",
            ))
        else:
            out.append(finding("info", "spec_tokens",
                               f"MTP k={k}, accepted/draft={apd:.2f}, last position {last:.0%}",
                               "Speculative decoding is paying for itself.", "None."))
    elif not args.get("speculative_config"):
        out.append(finding("medium", "spec_tokens", "No speculative decoding configured",
                           "Decode on a single memory-bound GPU is ~1 token/step without it.",
                           "If the checkpoint ships an MTP head, add --speculative-config "
                           '{"method":"mtp","num_speculative_tokens":3}.'))

    mnbt = args.get("max_num_batched_tokens")
    if k and mnbt and args.get("max_num_seqs"):
        need = int(args["max_num_seqs"]) * (k + 1)
        if mnbt < need:
            out.append(finding("medium", "batched_tokens",
                               f"max_num_batched_tokens={mnbt} < max_num_seqs*(k+1)={need}",
                               "Scheduler cannot give every sequence its full draft slot in one step.",
                               f"Raise --max-num-batched-tokens to >= {need} (forces recompile)."))

    kvd = (args.get("kv_cache_dtype") or stats.get("kv_dtype") or "auto")
    if str(kvd).lower() not in ("fp8", "fp8_e4m3", "fp8_e5m2"):
        out.append(finding("medium", "kv_dtype", f"KV cache dtype {kvd}",
                           "fp8 KV halves cache bytes -> ~2x tokens of KV for the same VRAM.",
                           "Add --kv-cache-dtype=fp8 (verify quality on a short panel)."))

    gmu = args.get("gpu_memory_utilization")
    tenants = host.get("other_gpu_tenants") or []
    if gmu is not None and float(gmu) < 0.95 and not tenants:
        out.append(finding("low", "gpu_mem", f"gpu_memory_utilization={gmu} with no other GPU tenant",
                           "Unused VRAM is KV that could serve longer/more contexts.",
                           "Raise to 0.95-0.965 (upstream production uses 0.965)."))
    elif gmu is not None and tenants:
        out.append(finding("info", "gpu_mem", f"gpu_memory_utilization={gmu}; sharing GPU with {', '.join(tenants)}",
                           "Headroom is intentionally left for other tenants.",
                           "Stop tenants before raising it."))

    mc = stats.get("kv_max_concurrency")
    if isinstance(mc, (int, float)) and mc < 1.5:
        out.append(finding("medium", "kv_concurrency",
                           f"kv_cache_max_concurrency={mc:.2f} at max_model_len={args.get('max_model_len')}",
                           "Only one max-length request fits; a second must queue or preempt.",
                           "Lower --max-model-len to the context Hermes actually uses, or add KV."))

    if env.get("VLLM_TARGET_DEVICE") == "xpu" and env.get("VLLM_XPU_ENABLE_XPU_GRAPH") != "1":
        out.append(finding("medium", "xpu_graph", "XPU graph capture disabled",
                           "Graph replay removes per-step kernel launch overhead on decode.",
                           "Set VLLM_XPU_ENABLE_XPU_GRAPH=1."))

    if args.get("enforce_eager"):
        out.append(finding("high", "eager", "--enforce-eager set",
                           "Disables torch.compile and graph capture; decode will be slow.",
                           "Remove --enforce-eager."))

    qt = stats.get("mean_queue_s")
    if qt is not None and qt > 0.5:
        out.append(finding("low", "queue", f"Mean queue time {qt:.2f}s",
                           "Requests wait for admission (KV or max_num_seqs).",
                           "Same levers as preemptions; raise --max-num-seqs only if KV allows."))

    vf = host.get("vram_used_frac")
    spill = host.get("smem_spill_bytes")
    if vf is not None and vf > 0.97 and ((spill or 0) > 512 * 1024 * 1024 or tenants):
        out.append(finding("high", "vram_oversub",
                           f"Card VRAM {vf:.0%} committed, {(spill or 0) / 2**30:.2f} GiB spilled to system memory",
                           "At ~100% the xe driver pages buffers to system memory over PCIe; measured on the "
                           "B70: idle ComfyUI resident beside vLLM cut decode from 60 to 39 tok/s.",
                           "Stop the other GPU tenant(s) (`docker stop`, restart=no) or lower "
                           "gpu_memory_utilization so total stays <= ~95%."))
    elif vf is not None and vf > 0.97:
        out.append(finding("info", "vram_oversub",
                           f"Card VRAM {vf:.0%} committed by vLLM alone, spill {(spill or 0) / 2**30:.2f} GiB",
                           "Expected at gpu_memory_utilization >= 0.95 with no other tenant; harmless while spill stays < 0.5 GiB.",
                           "None."))

    pcap = host.get("power_cap_w")
    if pcap is not None:
        if pcap < 180:
            out.append(finding("medium", "power", f"GPU power cap {pcap} W",
                               "Upstream B70 table: 150 W -> 42 tok/s, 180 W -> 51, 275 W -> 62 (mixed workload).",
                               "Raise to 180 W (best tok/Wh) or 275 W (fastest)."))
        else:
            out.append(finding("info", "power", f"GPU power cap {pcap} W",
                               "180 W is the efficiency optimum, 275 W the throughput optimum.", "None."))

    gov = host.get("cpu_governor")
    if gov and gov != "performance":
        out.append(finding("info", "cpu_governor", f"CPU governor '{gov}'",
                           "Measured on the B70 host (i7-8700K, intel_pstate active): performance vs powersave "
                           "changed decode by < 3% at 2K/16K/64K — not a lever here.",
                           "Leave as is unless a benchmark on your host shows otherwise."))

    hcl = host.get("hermes_context_length")
    mml = args.get("max_model_len")
    if hcl and mml and int(hcl) > int(mml):
        out.append(finding("high", "hermes_ctx", f"Hermes context_length {hcl} > max_model_len {mml}",
                           "Hermes will build prompts the server rejects.", f"Set context_length <= {mml}."))

    out.sort(key=lambda f: SEV_ORDER.get(f["severity"], 9))
    return out

# ----------------------------------------------------------------------------- io


def docker_inspect(name: str) -> Any:
    raw = subprocess.check_output(["docker", "inspect", name], text=True)
    return json.loads(raw)


def fetch(url: str, timeout: float = 10.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 - local metrics
        return r.read().decode("utf-8", "replace")


def read_host_facts(pci: str | None) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    try:
        import glob
        caps = glob.glob(f"/sys/bus/pci/devices/{pci}/hwmon/hwmon*/power1_cap") if pci else []
        if caps:
            with open(caps[0]) as fh:
                facts["power_cap_w"] = int(fh.read().strip()) / 1e6
    except (OSError, ValueError):
        pass
    try:
        with open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor") as fh:
            facts["cpu_governor"] = fh.read().strip()
    except OSError:
        pass
    return facts


def read_drm_vram(metrics_text: str, pci: str) -> tuple[float | None, float | None]:
    """(fraction of card VRAM in use, bytes spilled to the smem pool) from drm-exporter for `pci`."""
    m = parse_prometheus(metrics_text)
    used = metric_sum(m, "drm_memory_used_bytes", device=pci, pool="vram")
    total = metric_sum(m, "drm_memory_total_bytes", device=pci, pool="vram")
    spill = metric_sum(m, "drm_memory_used_bytes", device=pci, pool="smem")
    return _ratio(used, total), spill


def format_text(info: dict, stats: dict, findings: list[dict]) -> str:
    lines = [f"== {info.get('name')}  image={info.get('image')}  health={info.get('health')}  restart={info.get('restart_policy')}"]
    a = info["args"]
    keys = ["model", "quantization", "dtype", "max_model_len", "gpu_memory_utilization", "kv_cache_dtype",
            "max_num_seqs", "max_num_batched_tokens", "enable_prefix_caching", "speculative_config"]
    lines.append("-- args: " + "  ".join(f"{k}={a.get(k)}" for k in keys if k in a))
    lines.append("-- stats:")
    for k, v in stats.items():
        if isinstance(v, float):
            v = f"{v:.4g}"
        lines.append(f"   {k}: {v}")
    lines.append("-- findings:")
    for f in findings:
        lines.append(f"   [{f['severity'].upper():6}] {f['title']}")
        lines.append(f"            {f['detail']}")
        lines.append(f"            -> {f['action']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--container")
    p.add_argument("--metrics-url")
    p.add_argument("--inspect-file")
    p.add_argument("--metrics-file")
    p.add_argument("--gpu-pci", default="0000:03:00.0", help="PCI id for hwmon power cap lookup")
    p.add_argument("--tenant", action="append", default=[], help="other container(s) sharing the GPU")
    p.add_argument("--hermes-context-length", type=int)
    p.add_argument("--drm-metrics-url", help="drm-exporter /metrics URL for card-wide VRAM use")
    p.add_argument("--drm-metrics-file")
    p.add_argument("--json", action="store_true")
    ns = p.parse_args(argv)

    if ns.inspect_file:
        with open(ns.inspect_file) as fh:
            inspect_json = json.load(fh)
    elif ns.container:
        inspect_json = docker_inspect(ns.container)
    else:
        p.error("need --container or --inspect-file")
    if ns.metrics_file:
        with open(ns.metrics_file) as fh:
            metrics_text = fh.read()
    elif ns.metrics_url:
        metrics_text = fetch(ns.metrics_url)
    else:
        p.error("need --metrics-url or --metrics-file")

    info = parse_inspect(inspect_json)
    stats = derive_stats(parse_prometheus(metrics_text))
    host = read_host_facts(ns.gpu_pci) if not ns.inspect_file else {}
    host["other_gpu_tenants"] = ns.tenant
    if ns.hermes_context_length:
        host["hermes_context_length"] = ns.hermes_context_length
    drm_text = None
    if ns.drm_metrics_file:
        with open(ns.drm_metrics_file) as fh:
            drm_text = fh.read()
    elif ns.drm_metrics_url:
        try:
            drm_text = fetch(ns.drm_metrics_url)
        except OSError:
            drm_text = None
    if drm_text:
        host["vram_used_frac"], host["smem_spill_bytes"] = read_drm_vram(drm_text, ns.gpu_pci)
    findings = recommend(info["args"], info["env"], stats, host)

    if ns.json:
        print(json.dumps({"container": info, "host": host, "stats": stats, "findings": findings}, indent=2, default=str))
    else:
        print(format_text(info, stats, findings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
