"""Unit tests for vllm_tune_audit.py and vllm_bench.py.

stdlib `unittest` only (pytest also runs them). No network, no docker.
  python3 -m unittest discover -s scripts/tests -v
"""
import io
import json
import pathlib
import sys
import unittest
from contextlib import redirect_stdout

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import vllm_bench as bench  # noqa: E402
import vllm_tune_audit as audit  # noqa: E402

FIXTURES = HERE / "fixtures"


def keys(findings, sev=None):
    return {f["key"] for f in findings if sev is None or f["severity"] == sev}


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.metrics = audit.parse_prometheus((FIXTURES / "metrics_b70.txt").read_text())
        cls.info = audit.parse_inspect(json.loads((FIXTURES / "inspect_b70.json").read_text()))
        cls.stats = audit.derive_stats(cls.metrics)

    def approx(self, a, b, rel=1e-6):
        self.assertIsNotNone(a)
        self.assertAlmostEqual(a, b, delta=abs(b) * rel + 1e-12)


class TestPrometheusParsing(Base):
    def test_labels_and_values(self):
        txt = ('# HELP x\n'
               'vllm:num_preemptions_total{engine="0",model_name="M"} 257.0\n'
               'vllm:kv_cache_usage_perc{engine="0"} 0.65\n'
               'plain_metric 3\n'
               'bad line here\n')
        m = audit.parse_prometheus(txt)
        self.assertEqual(m["vllm:num_preemptions_total"], [({"engine": "0", "model_name": "M"}, 257.0)])
        self.assertEqual(m["vllm:kv_cache_usage_perc"][0][1], 0.65)
        self.assertEqual(m["plain_metric"], [({}, 3.0)])
        self.assertNotIn("bad", m)

    def test_metric_sum_filters_by_label(self):
        m = audit.parse_prometheus('vllm:request_success_total{finished_reason="stop"} 10\n'
                                   'vllm:request_success_total{finished_reason="length"} 2\n')
        self.assertEqual(audit.metric_sum(m, "vllm:request_success_total"), 12)
        self.assertEqual(audit.metric_sum(m, "vllm:request_success_total", finished_reason="length"), 2)
        self.assertIsNone(audit.metric_sum(m, "vllm:request_success_total", finished_reason="nope"))
        self.assertIsNone(audit.metric_sum(m, "missing"))


class TestArgParsing(Base):
    def test_parse_vllm_args_forms(self):
        argv = ["org/model", "--max-model-len=200704", "--gpu-memory-utilization", "0.93",
                "--enable-prefix-caching", "--no-enable-chunked-prefill",
                '--speculative-config={"method":"mtp","num_speculative_tokens":4}', "--port=8000"]
        a = audit.parse_vllm_args(argv)
        self.assertEqual(a["model"], "org/model")
        self.assertEqual(a["max_model_len"], 200704)
        self.assertEqual(a["gpu_memory_utilization"], 0.93)
        self.assertIs(a["enable_prefix_caching"], True)
        self.assertIs(a["enable_chunked_prefill"], False)
        self.assertEqual(a["speculative_config"], {"method": "mtp", "num_speculative_tokens": 4})
        self.assertEqual(a["port"], 8000)

    def test_parse_inspect_strips_entrypoint(self):
        i = self.info
        self.assertEqual(i["name"], "vllm-b70-qwen38-gptq")
        self.assertTrue(i["args"]["model"].startswith("mikeinnyc/"))
        self.assertEqual(i["args"]["max_num_seqs"], 4)
        self.assertEqual(i["env"]["VLLM_TARGET_DEVICE"], "xpu")
        self.assertEqual(i["shm_bytes"], 8589934592)


class TestDerivedStats(Base):
    def test_matches_hand_computed_from_fixture(self):
        # constants are read straight off fixtures/metrics_b70.txt (snapshot of the live B70)
        s = self.stats
        self.assertEqual(s["requests"], 1402)
        self.assertEqual(s["preemptions"], 257)
        self.approx(s["preemption_rate"], 257 / 1402)
        self.approx(s["prefix_hit_rate"], 82977024 / 92041843)
        self.assertEqual(s["spec_k"], 4)
        self.approx(s["spec_accepted_per_draft"], 949533 / 520440)
        self.approx(s["spec_accept_by_pos"][0], 365726 / 520440)
        self.approx(s["spec_accept_by_pos"][3], 139005 / 520440)
        self.approx(s["avg_prompt_tokens"], 86946044 / 1402)
        self.approx(s["decode_tok_s_per_request"], 1388889 / 32009.24581572318)
        self.assertEqual(s["kv_cache_tokens"], 212255)
        self.approx(s["kv_max_concurrency"], 1.0575539568345325)
        self.assertEqual(s["kv_dtype"], "fp8")

    def test_handles_empty(self):
        s = audit.derive_stats({})
        self.assertIsNone(s["preemption_rate"])
        self.assertIsNone(s["spec_k"])
        self.assertEqual(s["spec_accept_by_pos"], {})


class TestRules(Base):
    def test_real_snapshot(self):
        f = audit.recommend(self.info["args"], self.info["env"], self.stats,
                            {"other_gpu_tenants": ["comfyui-intel-arc"], "power_cap_w": 275.0,
                             "cpu_governor": "powersave"})
        self.assertTrue({"preemptions", "context_size"} <= keys(f, "high"))
        self.assertIn("kv_concurrency", keys(f, "medium"))
        self.assertIn("cpu_governor", keys(f, "info"))
        # already-correct settings must not be flagged
        self.assertNotIn("kv_dtype", keys(f))
        self.assertNotIn("xpu_graph", keys(f))
        self.assertNotIn("eager", keys(f))
        self.assertNotIn("gpu_mem", keys(f, "low"))          # tenant present -> info only
        self.assertNotIn("spec_tokens", keys(f, "medium"))   # pos3 = 26.7% >= 25%
        order = [audit.SEV_ORDER[x["severity"]] for x in f]
        self.assertEqual(order, sorted(order))

    def test_prefix_caching_disabled(self):
        self.assertIn("prefix_caching", keys(audit.recommend({"enable_prefix_caching": False}, {}, {}), "high"))

    def test_low_prefix_hit_rate(self):
        f = audit.recommend({"enable_prefix_caching": True}, {}, {"prefix_hit_rate": 0.3, "prefix_queries": 5_000_000})
        self.assertIn("prefix_caching", keys(f, "medium"))

    def test_spec_last_position_weak(self):
        st = {"spec_k": 4, "spec_accepted_per_draft": 1.5,
              "spec_accept_by_pos": {0: 0.6, 1: 0.4, 2: 0.3, 3: 0.2}}
        m = [x for x in audit.recommend({}, {}, st) if x["key"] == "spec_tokens"][0]
        self.assertEqual(m["severity"], "medium")
        self.assertIn("num_speculative_tokens=3", m["action"])

    def test_no_spec_decode(self):
        self.assertIn("spec_tokens", keys(audit.recommend({}, {}, {}), "medium"))

    def test_batched_tokens_too_small(self):
        st = {"spec_k": 4, "spec_accepted_per_draft": 2.0, "spec_accept_by_pos": {0: 0.7}}
        self.assertIn("batched_tokens", keys(audit.recommend({"max_num_seqs": 16, "max_num_batched_tokens": 64}, {}, st), "medium"))
        self.assertNotIn("batched_tokens", keys(audit.recommend({"max_num_seqs": 4, "max_num_batched_tokens": 6656}, {}, st)))

    def test_kv_dtype(self):
        self.assertIn("kv_dtype", keys(audit.recommend({"kv_cache_dtype": "auto"}, {}, {}), "medium"))
        self.assertNotIn("kv_dtype", keys(audit.recommend({"kv_cache_dtype": "fp8"}, {}, {})))
        self.assertNotIn("kv_dtype", keys(audit.recommend({}, {}, {"kv_dtype": "fp8"})))

    def test_gpu_mem_headroom(self):
        self.assertIn("gpu_mem", keys(audit.recommend({"gpu_memory_utilization": 0.9}, {}, {}, {"other_gpu_tenants": []}), "low"))
        self.assertIn("gpu_mem", keys(audit.recommend({"gpu_memory_utilization": 0.9}, {}, {}, {"other_gpu_tenants": ["comfy"]}), "info"))

    def test_xpu_graph_and_eager(self):
        f = audit.recommend({"enforce_eager": True}, {"VLLM_TARGET_DEVICE": "xpu", "VLLM_XPU_ENABLE_XPU_GRAPH": "0"}, {})
        self.assertIn("xpu_graph", keys(f, "medium"))
        self.assertIn("eager", keys(f, "high"))

    def test_power_cap(self):
        self.assertIn("power", keys(audit.recommend({}, {}, {}, {"power_cap_w": 150}), "medium"))
        self.assertIn("power", keys(audit.recommend({}, {}, {}, {"power_cap_w": 180}), "info"))

    def test_hermes_context_exceeds_server(self):
        f = audit.recommend({"max_model_len": 131072}, {}, {}, {"hermes_context_length": 200704})
        self.assertIn("hermes_ctx", keys(f, "high"))

    def test_queue_time(self):
        self.assertIn("queue", keys(audit.recommend({}, {}, {"mean_queue_s": 0.95}), "low"))
        self.assertNotIn("queue", keys(audit.recommend({}, {}, {"mean_queue_s": 0.1})))

    def test_vram_oversubscription(self):
        big = 1024 ** 3
        self.assertIn("vram_oversub", keys(audit.recommend({}, {}, {}, {"vram_used_frac": 0.9998, "smem_spill_bytes": big}), "high"))
        self.assertIn("vram_oversub", keys(audit.recommend({}, {}, {}, {"vram_used_frac": 0.9998, "other_gpu_tenants": ["comfy"]}), "high"))
        # vLLM alone at 0.965 legitimately reads ~99.7% with a small spill -> info only
        self.assertIn("vram_oversub", keys(audit.recommend({}, {}, {}, {"vram_used_frac": 0.997, "smem_spill_bytes": 284008448}), "info"))
        self.assertNotIn("vram_oversub", keys(audit.recommend({}, {}, {}, {"vram_used_frac": 0.87})))

    def test_low_prefix_hit_rate_needs_volume(self):
        # fresh counters after a restart (few queries, salted bench prompts) must not alarm
        f = audit.recommend({"enable_prefix_caching": True}, {}, {"prefix_hit_rate": 0.0, "prefix_queries": 83422})
        self.assertNotIn("prefix_caching", keys(f, "medium"))

    def test_read_drm_vram(self):
        txt = ('drm_memory_total_bytes{device="0000:03:00.0",pool="vram"} 34242297856\n'
               'drm_memory_used_bytes{device="0000:03:00.0",pool="vram"} 34234908672\n'
               'drm_memory_used_bytes{device="0000:03:00.0",pool="smem"} 1000140800\n'
               'drm_memory_total_bytes{device="0000:00:02.0",pool="smem"} 1\n')
        frac, spill = audit.read_drm_vram(txt, "0000:03:00.0")
        self.approx(frac, 34234908672 / 34242297856)
        self.assertEqual(spill, 1000140800)
        self.assertIsNone(audit.read_drm_vram(txt, "0000:00:02.0")[0])


class TestCli(Base):
    def test_offline_json(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = audit.main(["--inspect-file", str(FIXTURES / "inspect_b70.json"),
                             "--metrics-file", str(FIXTURES / "metrics_b70.txt"), "--json",
                             "--tenant", "comfyui-intel-arc"])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["stats"]["preemptions"], 257)
        self.assertTrue(any(f["key"] == "preemptions" for f in out["findings"]))
        self.assertEqual(out["host"]["other_gpu_tenants"], ["comfyui-intel-arc"])


class TestBench(Base):
    def test_sse_parsing(self):
        self.assertIsNone(bench.parse_sse_chunk("data: [DONE]"))
        self.assertIsNone(bench.parse_sse_chunk(": keepalive"))
        obj = bench.parse_sse_chunk('data: {"choices":[{"delta":{"content":"hi"}}]}')
        self.assertEqual(bench.chunk_text(obj), "hi")
        self.assertEqual(bench.chunk_text({"choices": []}), "")
        self.assertEqual(bench.chunk_text({"choices": [{"delta": {"reasoning_content": "r"}}]}), "r")

    def test_summarize_math(self):
        s = bench.summarize({"t_start": 0.0, "t_first": 1.0, "t_end": 6.0, "completion_tokens": 251, "prompt_tokens": 100})
        self.assertEqual(s["ttft_s"], 1.0)
        self.approx(s["decode_tok_s"], 50.0)
        self.assertEqual(s["total_s"], 6.0)
        self.assertIsNone(bench.summarize({"t_start": 0, "t_first": None, "t_end": 1, "chunks": 0})["decode_tok_s"])

    def test_prompt_size_and_salt(self):
        p1, p2 = bench.make_prompt(2048, "a"), bench.make_prompt(2048, "b")
        self.assertTrue(10000 < len(p1) < 12400)
        self.assertNotEqual(p1, p2)
        self.assertEqual(p1[20:], p2[20:])  # only the salt differs -> prefix cache cannot reuse it

    def test_aggregate(self):
        waves = [[{"ttft_s": 1, "decode_tok_s": 40, "prompt_tokens": 10, "completion_tokens": 5, "total_s": 2},
                  {"ttft_s": 2, "decode_tok_s": 50, "prompt_tokens": 10, "completion_tokens": 5, "total_s": 3}],
                 [{"error": "boom"}]]
        a = bench.aggregate(waves)
        self.assertEqual((a["requests"], a["errors"]), (2, 1))
        self.assertEqual(a["median_decode_tok_s"], 45)
        self.assertEqual(len(a["aggregate_decode_tok_s_per_wave"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
