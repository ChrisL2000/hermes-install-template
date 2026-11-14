#!/usr/bin/env bash
# hermes-install-template — automated Hermes Agent install + default-LLM + skills template.
#
# One-liner:
#   curl -fsSL https://raw.githubusercontent.com/ChrisL2000/hermes-install-template/main/install.sh | bash
#
# Or:
#   bash install.sh [--provider NAME] [--model MODEL] [--base-url URL] [--context-length N]
#                   [--skills a,b,c] [--tap owner/repo] [--no-install] [--force] [--doctor]
#
# What it does:
#   1. Installs Hermes Agent if not already present (curl installer from hermes-agent.nousresearch.com).
#   2. Points the default LLM at a named custom provider (default: vllm-b70-qwen38,
#      Qwen3.8-27B @ http://localhost:8081/v1) — from templates/config.yaml on a fresh install,
#      or via idempotent `hermes config set` on an existing install.
#   3. Seeds ~/.hermes/.env from templates/.env.example (only if missing).
#   4. Installs the skills listed in templates/skills.txt:
#        - bundled skills/ in this repo  -> copied into $HERMES_HOME/skills/
#        - https://.../SKILL.md URLs     -> `hermes skills install <url>`
#        - anything else                 -> treated as a skills-hub id
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------- defaults (override with flags or env) ----------
PROVIDER_NAME="${PROVIDER_NAME:-vllm-b70-qwen38}"
MODEL_NAME="${MODEL_NAME:-Qwen3.8-27B}"
BASE_URL="${BASE_URL:-http://localhost:8081/v1}"
CONTEXT_LENGTH="${CONTEXT_LENGTH:-262144}"
SKILLS_OVERRIDE=""
TAP_REPO=""
DO_INSTALL=1
FORCE=0
RUN_DOCTOR=0

usage() {
  cat <<'USAGE'
Usage: install.sh [options]
  --provider NAME       provider name (default: vllm-b70-qwen38)
  --model MODEL         model name (default: Qwen3.8-27B)
  --base-url URL        LLM endpoint (default: http://localhost:8081/v1)
  --context-length N    context window (default: 262144)
  --skills a,b,c        override templates/skills.txt list
  --tap owner/repo      add a GitHub skills tap first
  --no-install          hermes is already installed
  --force               overwrite existing config/skills (config is backed up)
  --doctor              run hermes doctor at the end
  -h, --help            this help
Env: HERMES_HOME, PROVIDER_NAME, MODEL_NAME, BASE_URL, CONTEXT_LENGTH
USAGE
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --provider)       PROVIDER_NAME="$2"; shift 2 ;;
    --model)          MODEL_NAME="$2"; shift 2 ;;
    --base-url)       BASE_URL="$2"; shift 2 ;;
    --context-length) CONTEXT_LENGTH="$2"; shift 2 ;;
    --skills)         SKILLS_OVERRIDE="$2"; shift 2 ;;
    --tap)            TAP_REPO="$2"; shift 2 ;;
    --no-install)     DO_INSTALL=0; shift ;;
    --force)          FORCE=1; shift ;;
    --doctor)         RUN_DOCTOR=1; shift ;;
    -h|--help)        usage ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 1 ;;
  esac
done

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERMES_BIN="$(command -v hermes || true)"

log()  { printf '\n==> %s\n' "$*"; }
warn() { printf '    (warn) %s\n' "$*" >&2; }

# ---------- 1. install Hermes if missing ----------
if [ "$DO_INSTALL" = "1" ]; then
  if [ -z "$HERMES_BIN" ]; then
    log "Installing Hermes Agent (curl installer)"
    curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
    # make sure the fresh launcher is on PATH for the rest of this run
    for d in "$HOME/.local/bin" "$HERMES_HOME/bin"; do
      [ -x "$d/hermes" ] && export PATH="$d:$PATH" && break
    done
  else
    log "Hermes already installed: $HERMES_BIN"
  fi
  HERMES_BIN="$(command -v hermes || true)"
else
  log "Skipping Hermes install (--no-install)"
fi
if [ -z "$HERMES_BIN" ]; then
  echo "ERROR: hermes not found on PATH. Install it first or drop --no-install." >&2
  exit 1
fi
export HERMES_HOME

# ---------- 2. config: template (fresh) or config set (existing) ----------
CFG="$HERMES_HOME/config.yaml"
mkdir -p "$HERMES_HOME"

render_template() {
  sed -e "s/__PROVIDER__/$PROVIDER_NAME/g" \
      -e "s/__MODEL__/$MODEL_NAME/g" \
      -e "s|__BASE_URL__|$BASE_URL|g" \
      -e "s/__CONTEXT_LENGTH__/$CONTEXT_LENGTH/g" \
      "$SCRIPT_DIR/templates/config.yaml"
}

apply_config_set() {
  # idempotent: dots split key paths; existing unrelated config is untouched.
  # Keys managed by a system layer (e.g. /etc/hermes/config.yaml) are skipped
  # with a warning instead of aborting the install.
  local k v
  for kv in "model.default=$MODEL_NAME" \
            "model.provider=custom:$PROVIDER_NAME" \
            "model.context_length=$CONTEXT_LENGTH" \
            "providers.$PROVIDER_NAME.api=$BASE_URL" \
            "providers.$PROVIDER_NAME.default_model=$MODEL_NAME" \
            "providers.$PROVIDER_NAME.context_length=$CONTEXT_LENGTH"; do
    k="${kv%%=*}"; v="${kv#*=}"
    if ! "$HERMES_BIN" config set "$k" "$v"; then
      warn "could not set $k (already managed elsewhere?) — leaving it as-is"
    fi
  done
}

if [ -f "$CFG" ] && [ "$FORCE" != "1" ]; then
  log "Existing config found — applying LLM settings via \`hermes config set\` (other keys untouched)"
  apply_config_set
else
  [ -f "$CFG" ] && cp "$CFG" "$CFG.bak-$(date +%Y%m%d-%H%M%S)"
  log "Writing $CFG from templates/config.yaml"
  render_template > "$CFG"
fi

# ---------- 3. .env seed ----------
if [ ! -f "$HERMES_HOME/.env" ]; then
  log "Seeding $HERMES_HOME/.env from templates/.env.example"
  cp "$SCRIPT_DIR/templates/.env.example" "$HERMES_HOME/.env"
  chmod 600 "$HERMES_HOME/.env"
else
  log ".env already exists — leaving it alone"
fi

# ---------- 4. skills ----------
SKILLS_FILE="$SCRIPT_DIR/templates/skills.txt"
if [ -n "$SKILLS_OVERRIDE" ]; then
  SKILLS_LIST="$(echo "$SKILLS_OVERRIDE" | tr ',' '\n')"
else
  SKILLS_LIST="$(grep -v '^\s*#' "$SKILLS_FILE" | grep -v '^\s*$')"
fi

installed=0; skipped=0; failed=0
TAP_DONE=0

for entry in $SKILLS_LIST; do
  # tap:owner/repo — add the tap once, then install whatever names follow
  if [[ "$entry" == tap:* ]]; then
    if [ "$TAP_DONE" = "0" ]; then
      log "Adding skills tap: ${entry#tap:}"
      "$HERMES_BIN" skills tap add "${entry#tap:}" || warn "tap add failed"
      TAP_DONE=1
    fi
    continue
  fi

  # bundled copy in this repo?
  bundled="$(find "$SCRIPT_DIR/skills" -maxdepth 2 -type d -name "$entry" -print -quit 2>/dev/null || true)"
  if [ -n "$bundled" ]; then
    category="$(basename "$(dirname "$bundled")")"
    dest="$HERMES_HOME/skills/$category/$entry"
    if [ -d "$dest" ] && [ "$FORCE" != "1" ]; then
      echo "    skill $entry: already installed — keeping (use --force to overwrite)"
      skipped=$((skipped+1))
    else
      mkdir -p "$(dirname "$dest")"
      cp -a "$bundled" "$dest"
      echo "    skill $entry: installed from template (bundled)"
      installed=$((installed+1))
    fi
    continue
  fi

  # URL -> install from URL; otherwise treat as hub id
  if [[ "$entry" == http://* || "$entry" == https://* ]]; then
    if "$HERMES_BIN" skills install "$entry" >/dev/null 2>&1; then
      echo "    skill $entry: installed from URL"
      installed=$((installed+1))
    else
      echo "    skill $entry: URL install FAILED"
      failed=$((failed+1))
    fi
  else
    if "$HERMES_BIN" skills install "$entry" >/dev/null 2>&1; then
      echo "    skill $entry: installed from skills hub"
      installed=$((installed+1))
    else
      echo "    skill $entry: hub install FAILED (not found on hub? add it to skills/ or use a SKILL.md URL)"
      failed=$((failed+1))
    fi
  fi
done

# ---------- 5. summary ----------
log "Done. Summary:"
echo "    hermes:      $HERMES_BIN"
echo "    HERMES_HOME: $HERMES_HOME"
echo "    config:      $CFG"
echo "    model:       custom:$PROVIDER_NAME / $MODEL_NAME @ $BASE_URL (ctx $CONTEXT_LENGTH)"
echo "    skills:      $installed installed, $skipped already present, $failed failed"

if [ "$RUN_DOCTOR" = "1" ]; then
  log "Running hermes doctor"
  "$HERMES_BIN" doctor || warn "doctor reported issues (see above)"
fi

cat <<EOF

Next steps:
  hermes                        # start chatting
  hermes config                 # verify LLM settings
  hermes skills list            # verify skills
  hermes model                  # interactive model/provider picker
  hermes doctor                 # full health check
EOF
