#!/usr/bin/env python3
"""Small streaming benchmark for an OpenAI-compatible vLLM endpoint.

Measures TTFT and post-first-token decode tok/s per request, optionally at concurrency N.
Uses only urllib + threads. Thinking is disabled (reasoning_effort=none) unless --thinking.

  vllm_bench.py --url http://localhost:8081/v1 --model Qwen3.8-27B \
      --prompt-tokens 2048 --max-tokens 256 --runs 2 [--concurrency 1] [--json]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
import urllib.request

_FILLER = ("The quick brown fox jumps over the lazy dog while the committee reviews the quarterly "
           "infrastructure report and notes several open action items. ")


_CHARS_PER_TOKEN = 6  # measured on Qwen3 tokenizer for the filler below


def make_prompt(approx_tokens: int, salt: str = "") -> str:
    """Build a prompt of roughly `approx_tokens` tokens (English filler, ~6 chars/token on Qwen3).
    `salt` defeats prefix-cache reuse between runs so prefill is really measured."""
    target = approx_tokens * _CHARS_PER_TOKEN
    body = (_FILLER * (target // len(_FILLER) + 1))[: max(target - 200, 0)]
    return f"[run {salt}]\n{body}\n\nSummarize the text above in three sentences."


def parse_sse_chunk(line: str) -> dict | None:
    """Return the JSON payload of a `data: {...}` SSE line, None for keepalive/[DONE]."""
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


def chunk_text(obj: dict) -> str:
    try:
        d = obj["choices"][0]["delta"]
    except (KeyError, IndexError, TypeError):
        return ""
    return (d.get("content") or "") + (d.get("reasoning_content") or "")


def summarize(result: dict) -> dict:
    """Derive ttft_s, decode_tok_s, total_s from raw timings + token counts."""
    t0, t_first, t_end = result["t_start"], result["t_first"], result["t_end"]
    comp = result.get("completion_tokens") or result.get("chunks", 0)
    out = {
        "ttft_s": (t_first - t0) if t_first else None,
        "total_s": t_end - t0,
        "prompt_tokens": result.get("prompt_tokens"),
        "completion_tokens": comp,
    }
    if t_first and comp and comp > 1 and t_end > t_first:
        out["decode_tok_s"] = (comp - 1) / (t_end - t_first)
    else:
        out["decode_tok_s"] = None
    return out


def one_request(url: str, model: str, prompt: str, max_tokens: int, thinking: bool, timeout: float,
                ignore_eos: bool = True) -> dict:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if ignore_eos:
        body["ignore_eos"] = True  # vLLM extension: always generate exactly max_tokens
    if not thinking:
        body["reasoning_effort"] = "none"
        body["chat_template_kwargs"] = {"enable_thinking": False}
    req = urllib.request.Request(f"{url.rstrip('/')}/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    res = {"t_start": time.perf_counter(), "t_first": None, "chunks": 0, "text_len": 0}
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - local endpoint
        for raw in r:
            obj = parse_sse_chunk(raw.decode("utf-8", "replace"))
            if obj is None:
                continue
            usage = obj.get("usage")
            if usage:
                res["prompt_tokens"] = usage.get("prompt_tokens")
                res["completion_tokens"] = usage.get("completion_tokens")
            txt = chunk_text(obj)
            if txt:
                if res["t_first"] is None:
                    res["t_first"] = time.perf_counter()
                res["chunks"] += 1
                res["text_len"] += len(txt)
    res["t_end"] = time.perf_counter()
    return res


def run_wave(ns, salt: str) -> list[dict]:
    results: list[dict | None] = [None] * ns.concurrency

    def worker(i: int) -> None:
        prompt = make_prompt(ns.prompt_tokens, f"{salt}-{i}")
        try:
            results[i] = summarize(one_request(ns.url, ns.model, prompt, ns.max_tokens, ns.thinking, ns.timeout,
                                               ignore_eos=not ns.allow_eos))
        except Exception as e:  # noqa: BLE001
            results[i] = {"error": str(e)}

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(ns.concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return [r for r in results if r is not None]


def aggregate(waves: list[list[dict]]) -> dict:
    flat = [r for w in waves for r in w if "error" not in r]
    errs = [r for w in waves for r in w if "error" in r]
    def med(key):
        vals = [r[key] for r in flat if r.get(key) is not None]
        return statistics.median(vals) if vals else None
    agg = {
        "requests": len(flat), "errors": len(errs),
        "median_ttft_s": med("ttft_s"),
        "median_decode_tok_s": med("decode_tok_s"),
        "median_prompt_tokens": med("prompt_tokens"),
        "median_completion_tokens": med("completion_tokens"),
    }
    if flat and waves and len(waves[0]) > 1:
        # aggregate decode across the wave: sum of completion tokens / wall of the wave's decode phase
        agg["aggregate_decode_tok_s_per_wave"] = [
            (sum((r.get("completion_tokens") or 0) for r in w if "error" not in r)
             / max(max(r["total_s"] for r in w if "error" not in r) - min(r["ttft_s"] or 0 for r in w if "error" not in r), 1e-6))
            for w in waves if any("error" not in r for r in w)
        ]
    return agg


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://localhost:8081/v1")
    p.add_argument("--model", default="Qwen3.8-27B")
    p.add_argument("--prompt-tokens", type=int, default=2048)
    p.add_argument("--max-tokens", type=int, default=256)
    p.add_argument("--runs", type=int, default=2, help="waves (sequential)")
    p.add_argument("--concurrency", type=int, default=1, help="requests per wave (parallel)")
    p.add_argument("--thinking", action="store_true", help="leave reasoning on")
    p.add_argument("--allow-eos", action="store_true", help="let the model stop early (default: ignore_eos so every run emits max_tokens)")
    p.add_argument("--timeout", type=float, default=600.0)
    p.add_argument("--json", action="store_true")
    ns = p.parse_args(argv)

    salt = str(int(time.time()))
    waves = [run_wave(ns, f"{salt}-{w}") for w in range(ns.runs)]
    agg = aggregate(waves)
    if ns.json:
        print(json.dumps({"config": vars(ns), "waves": waves, "summary": agg}, indent=2))
    else:
        print(f"model={ns.model} prompt~{ns.prompt_tokens} max_tokens={ns.max_tokens} "
              f"concurrency={ns.concurrency} runs={ns.runs} thinking={ns.thinking}")
        for wi, w in enumerate(waves):
            for r in w:
                if "error" in r:
                    print(f"  wave {wi}: ERROR {r['error']}")
                else:
                    print(f"  wave {wi}: prompt={r['prompt_tokens']} out={r['completion_tokens']} "
                          f"ttft={r['ttft_s']:.2f}s decode={r['decode_tok_s'] or 0:.1f} tok/s total={r['total_s']:.1f}s")
        print(f"median: ttft={agg['median_ttft_s']:.2f}s decode={agg['median_decode_tok_s'] or 0:.1f} tok/s"
              + (f"  aggregate/wave={['%.1f' % x for x in agg['aggregate_decode_tok_s_per_wave']]}"
                 if "aggregate_decode_tok_s_per_wave" in agg else ""))
    return 0 if agg["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
