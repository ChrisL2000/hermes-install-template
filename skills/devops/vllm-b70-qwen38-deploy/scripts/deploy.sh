#!/usr/bin/env bash
# Reproduce the tuned Qwen3.8-27B vLLM container on an Intel Arc Pro B70.
# Stages (each idempotent): preflight -> clone -> patch -> build -> docker objects -> model -> start -> wait.
#   deploy.sh [--skip-build] [--skip-download] [--no-start] [--force] [--yes]
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="$here/.env"
[[ -f "$env_file" ]] || { echo "deploy: no $env_file — copy assets/env.example to .env and edit it" >&2; exit 2; }
set -a; # shellcheck disable=SC1090
source "$env_file"; set +a

skip_build=0 skip_download=0 no_start=0 force=0 yes=0
for a in "$@"; do case "$a" in
  --skip-build) skip_build=1;; --skip-download) skip_download=1;; --no-start) no_start=1;;
  --force) force=1;; --yes) yes=1;; -h|--help) sed -n '2,5p' "$0"; exit 0;;
  *) echo "unknown flag $a" >&2; exit 2;; esac; done

stage() { printf '\n==> [%s] %s\n' "$1" "$2"; }
die()   { echo "deploy: $*" >&2; exit 1; }

: "${B70_REPO_URL:?}" "${B70_REPO_COMMIT:?}" "${B70_REPO_DIR:?}" "${VLLM_IMAGE:?}" "${MODEL_ID:?}" \
  "${MODEL_REVISION:?}" "${CONTAINER_NAME:?}" "${HF_CACHE_VOLUME:?}" "${VLLM_NETWORK:?}" "${RENDER_GID:?}"

# ---------------------------------------------------------------- 1 preflight
stage preflight "host checks"
for c in docker git curl sha256sum python3; do command -v "$c" >/dev/null || die "missing command: $c"; done
docker compose version >/dev/null 2>&1 || die "docker compose v2 not available"
grep -q '^xe ' /proc/modules || die "xe kernel module not loaded (see intel-xe-gpu-setup skill)"
xe_node=""; for n in /sys/class/drm/renderD*; do
  [[ "$(readlink -f "$n/device/driver")" == */xe ]] && xe_node="/dev/dri/$(basename "$n")"; done
[[ -n "$xe_node" ]] || die "no render node bound to the xe driver"
actual_gid="$(getent group render | cut -d: -f3 || true)"
[[ "$actual_gid" == "$RENDER_GID" ]] || die "RENDER_GID=$RENDER_GID in .env but host render GID is '$actual_gid'"
[[ -d "$B70_REPO_DIR" || -w "$(dirname "$B70_REPO_DIR")" ]] || die "cannot create $B70_REPO_DIR"
echo "xe render node: $xe_node   render gid: $RENDER_GID   docker: $(docker version --format '{{.Server.Version}}')"

# ---------------------------------------------------------------- 2 clone
stage clone "$B70_REPO_URL @ ${B70_REPO_COMMIT:0:9} -> $B70_REPO_DIR"
if [[ ! -d "$B70_REPO_DIR/.git" ]]; then git clone --no-checkout "$B70_REPO_URL" "$B70_REPO_DIR"; fi
git -C "$B70_REPO_DIR" fetch --quiet origin
git -C "$B70_REPO_DIR" checkout --quiet "$B70_REPO_COMMIT"

# ---------------------------------------------------------------- 3 patch
stage patch "upstream Dockerfile sha fix"
patch_file="$here/assets/patches/dockerfile-b70-attention-sha.patch"
if git -C "$B70_REPO_DIR" apply --check --reverse "$patch_file" >/dev/null 2>&1; then
  echo "already applied"
else
  git -C "$B70_REPO_DIR" apply "$patch_file" && echo "applied"
fi
want_sha="$(grep -o '^+ && echo "[0-9a-f]\{64\}' "$patch_file" | grep -o '[0-9a-f]\{64\}')"
have_sha="$(sha256sum "$B70_REPO_DIR/docker/m04/b70_attention.py" | cut -d' ' -f1)"
[[ "$want_sha" == "$have_sha" ]] || die "b70_attention.py sha $have_sha != patched expectation $want_sha — upstream changed; update the patch"

# ---------------------------------------------------------------- 4 build
stage build "$VLLM_IMAGE"
if (( skip_build )) || docker image inspect "$VLLM_IMAGE" >/dev/null 2>&1; then
  echo "image present (or --skip-build); skipping (docker rmi $VLLM_IMAGE to force)"
else
  B70_BASE_BUILD_IMAGE="$VLLM_IMAGE" bash "$B70_REPO_DIR/scripts/build-image.sh"
fi
docker image inspect "$VLLM_IMAGE" >/dev/null || die "image $VLLM_IMAGE missing after build"

# ---------------------------------------------------------------- 5 docker objects
stage objects "volume $HF_CACHE_VOLUME, network $VLLM_NETWORK"
docker volume inspect "$HF_CACHE_VOLUME" >/dev/null 2>&1 || docker volume create "$HF_CACHE_VOLUME" >/dev/null
docker network inspect "$VLLM_NETWORK" >/dev/null 2>&1 || docker network create "$VLLM_NETWORK" >/dev/null
echo ok

# ---------------------------------------------------------------- 6 model
stage model "$MODEL_ID @ ${MODEL_REVISION:0:9}"
snap_rel="hub/models--${MODEL_ID//\//--}/snapshots/$MODEL_REVISION"
have_model="$(docker run --rm -v "$HF_CACHE_VOLUME:/c:ro" --entrypoint sh "$VLLM_IMAGE" -c "[ -f /c/$snap_rel/config.json ] && echo yes || echo no")"
if (( skip_download )) || [[ "$have_model" == yes ]]; then
  echo "snapshot present (or --skip-download)"
else
  docker run --rm -v "$HF_CACHE_VOLUME:/root/.cache/huggingface" --entrypoint hf "$VLLM_IMAGE" \
    download "$MODEL_ID" --revision "$MODEL_REVISION"
fi

# ---------------------------------------------------------------- 7 start
stage start "$CONTAINER_NAME"
if (( no_start )); then echo "--no-start: everything prepared, container untouched"; exit 0; fi
# other GPU tenants?
others="$(docker ps -q | while read -r id; do
  n="$(docker inspect "$id" --format '{{.Name}}' | sed 's#^/##')"; [[ "$n" == "$CONTAINER_NAME" ]] && continue
  docker inspect "$id" --format '{{range .HostConfig.Devices}}{{.PathOnHost}} {{end}}' | grep -q '/dev/dri' && echo "$n"; done || true)"
if [[ -n "$others" && $force == 0 ]]; then
  die "other running containers map /dev/dri: $(echo "$others" | xargs). vLLM needs the whole card. Stop them or pass --force"
fi
if docker inspect "$CONTAINER_NAME" --format '{{.State.Health.Status}}' 2>/dev/null | grep -q healthy && (( yes == 0 )); then
  die "$CONTAINER_NAME is already running and healthy. Recreating it is a brain restart if Hermes runs on it; re-run with --yes"
fi
mkdir -p "$here/.run"
cp "$here/assets/docker-compose.yml" "$here/.run/docker-compose.yml"
cp "$env_file" "$here/.run/.env"
( cd "$here/.run" && docker compose up -d )

# ---------------------------------------------------------------- 8 wait
stage wait "health (start_period 20 min; first cold boot can take longer)"
for i in $(seq 1 180); do
  h="$(docker inspect "$CONTAINER_NAME" --format '{{.State.Health.Status}}' 2>/dev/null || echo missing)"
  st="$(docker inspect "$CONTAINER_NAME" --format '{{.State.Status}}' 2>/dev/null || echo missing)"
  if [[ "$h" == healthy ]]; then break; fi
  if [[ "$st" != running ]]; then docker logs --tail 30 "$CONTAINER_NAME" 2>&1 | tail -30; die "container state=$st (crash-loop? check VRAM tenants / render gid)"; fi
  (( i % 6 == 0 )) && echo "  $((i*10))s health=$h  $(docker logs --tail 1 "$CONTAINER_NAME" 2>&1 | cut -c1-110)"
  sleep 10
done
[[ "$h" == healthy ]] || die "not healthy after 30 min; docker logs $CONTAINER_NAME"
docker logs "$CONTAINER_NAME" 2>&1 | grep -E 'KV cache size|Free memory on device' | tail -2
echo "healthy $CONTAINER_NAME -> http://localhost:${HOST_PORT:-8081}/v1"
