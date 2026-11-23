---
name: vllm-tuning
description: "Tune live vLLM safely with production network gating."
version: 0.1.0
author: ChrisL2000, Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [vllm, tuning, docker, network-gating, rollback, qwen, b70]
    related_skills: [vllm-b70-qwen38-deploy, hermes-vllm-operations-tuning, ai-container-ops]
    category: devops
---

# vLLM tuning with production network gating

Keeps the production vLLM container running while cutting off client access on the
production port, then exposes a separate test port for tuning traffic. Includes
verified rollback back to production access.

## When to Use

- You need to tune or benchmark the live brain container without stopping it
- You want to block normal clients from `:8081` during tuning
- You want a one-command rollback after tuning
- Don’t use for: full deploy/rebuild (`vllm-b70-qwen38-deploy`), deep metric analysis
  (`hermes-vllm-operations-tuning`), GPU/driver issues (`intel-xe-gpu-setup`)

## Prerequisites

- Linux host with Docker and `sudo` for iptables
- Running container: `vllm-b70-qwen38-v3`
- `curl` available on host
- Free test port (default below uses `18082`)

## How to Run

```
# 1) network-prod-off: keep vLLM up, block prod ingress on :8081, expose test proxy :18082
terminal(command="docker rm -f qwen38-v3-test-proxy || true && docker run -d --name qwen38-v3-test-proxy --restart unless-stopped --network host alpine/socat TCP-LISTEN:18082,reuseaddr,fork TCP:$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' vllm-b70-qwen38-v3):8000 && sudo iptables -C INPUT -p tcp --dport 8081 -j REJECT --reject-with tcp-reset 2>/dev/null || sudo iptables -I INPUT 1 -p tcp --dport 8081 -j REJECT --reject-with tcp-reset")

# 2) verify: test path works, prod path blocked
terminal(command="curl -fsS http://127.0.0.1:18082/health && ! curl -fsS --max-time 3 http://127.0.0.1:8081/health")

# 3) run tuning workload against test port (example)
terminal(command="python3 ~/.hermes/skills/devops/hermes-vllm-operations-tuning/scripts/vllm_bench.py --url http://127.0.0.1:18082/v1 --model Qwen3.8-27B --prompt-tokens 2048 --max-tokens 256 --runs 2", timeout=300)

# 4) rollback (network-prod-on): restore prod access, remove test proxy
terminal(command="sudo iptables -D INPUT -p tcp --dport 8081 -j REJECT --reject-with tcp-reset || true; docker rm -f qwen38-v3-test-proxy || true")

# 5) verify rollback
terminal(command="curl -fsS http://127.0.0.1:8081/health")
```

## Procedure

1. Confirm the target vLLM container is healthy before changing ingress.
   Completion: `docker ps` shows `vllm-b70-qwen38-v3` as `healthy`.
2. Apply `network-prod-off` and verify test/prod behavior split.
   Completion: `:18082/health` succeeds and `:8081/health` is blocked.
3. Run tuning against the test endpoint only.
   Completion: benchmark/audit commands complete without touching container runtime.
4. Apply rollback (`network-prod-on`) and verify prod endpoint.
   Completion: `:8081/health` succeeds and proxy container is removed.

## Pitfalls

- If test port `18082` is occupied, pick a free port and change both proxy and verify commands.
- This method changes network ingress only; it does not restart `vllm-b70-qwen38-v3`.
- If the INPUT chain is managed externally, the iptables rule may be reordered later.
- Multiple stacked reject rules on `8081` are harmless but noisy; remove duplicates if needed.

## Verification

- Before gating: `curl -fsS http://127.0.0.1:8081/health` returns 200.
- During gating: `curl -fsS http://127.0.0.1:18082/health` returns 200 and
  `curl http://127.0.0.1:8081/health` fails.
- After rollback: `curl -fsS http://127.0.0.1:8081/health` returns 200 and
  `docker ps --format '{{.Names}}'` does not include `qwen38-v3-test-proxy`.
