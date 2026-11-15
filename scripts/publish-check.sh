#!/usr/bin/env bash
# publish-check.sh - scrub gate for skills published in this repo.
#
# Fails the publish when a tracked text file contains a credential shape, a real
# IPv4/IPv6 address outside the allowlist, a hostname on the operator denylist, or an
# absolute path outside $HERMES_HOME. Run before every commit; a match is a failure.
#
# Usage: bash scripts/publish-check.sh [root]        (default root: repo root)
# Usage: bash scripts/publish-check.sh [root] [category]
#   category - limit the scan to skills/<category>/ plus the repo docs/scripts
#              (default: every tracked file)
set -uo pipefail

ROOT=${1:-$(cd "$(dirname "$0")/.." && pwd)}
CATEGORY=${2:-}
HERMES_HOME=${HERMES_HOME:-$HOME/.hermes}
fail=0

# Text files only: skip .git, binaries, images and lock-like blobs
mapfile -t FILES < <(
  cd "$ROOT" && git ls-files -co --exclude-standard 2>/dev/null \
    | grep -vE '\.(png|jpg|jpeg|gif|pdf|zip|gz|tgz|whl|so|bin|ico|woff2?|ttf)$' \
    | grep -vE '^(\.git/|node_modules/)'
)
if [ ${#FILES[@]} -eq 0 ]; then
  mapfile -t FILES < <(cd "$ROOT" && find . -type f -not -path './.git/*' | sed 's|^\./||')
fi
if [ -n "$CATEGORY" ]; then
  mapfile -t FILES < <(printf '%s\n' "${FILES[@]}" \
    | grep -E "^(skills/${CATEGORY}/|scripts/|templates/|README\.md$|TAXONOMY\.md$|install\.sh$)" \
    | grep -vE '^scripts/publish-check\.sh$')
fi

scan() { # scan <label> <regex>
  local label=$1 re=$2 hits
  hits=$(cd "$ROOT" && grep -nIE -- "$re" "${FILES[@]}" 2>/dev/null | grep -v 'publish-check: allow')
  if [ -n "$hits" ]; then
    printf '\nFAIL  %s\n%s\n' "$label" "$hits"
    fail=1
  fi
}

echo "publish-check: scanning ${#FILES[@]} files under $ROOT"

# 1. credential shapes
scan "credential shape" \
  '(cfat_[A-Za-z0-9]{10,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[bap]-[A-Za-z0-9-]{10,}|BEGIN [A-Z ]*PRIVATE KEY|ssh-ed25519 AAAA[A-Za-z0-9+/]{20,})'

# 2. addresses outside the allowlist
IPS=$(cd "$ROOT" && grep -hoIE '([0-9]{1,3}\.){3}[0-9]{1,3}' "${FILES[@]}" 2>/dev/null | sort -u)
BAD_IPS=$(printf '%s\n' "$IPS" | grep -vE '^(127\.|10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|169\.254\.|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.|198\.4[1]\.(12[89]|1[3-9][0-9]|2[0-9][0-9])\.|0\.0\.0\.0|0\.0\.1\.0|255\.255\.255\.255|198\.1[89]\.)' | grep -v '^$')
if [ -n "$BAD_IPS" ]; then
  printf '\nFAIL  IPv4 addresses outside the allowlist:\n%s\n' "$BAD_IPS"
  fail=1
fi
IPS6=$(cd "$ROOT" && grep -hoIE '([0-9a-fA-F]{1,4}:){2,}[0-9a-fA-F:]{0,}' "${FILES[@]}" 2>/dev/null \
        | grep -vE '^::' | sort -u)
BAD_IPS6=$(printf '%s\n' "$IPS6" | grep -vE '^(fe80|2606:4700:a0:|fd7a:115c:a1e0:)' | grep -v '^$')
if [ -n "$BAD_IPS6" ]; then
  printf '\nFAIL  IPv6 addresses outside the allowlist:\n%s\n' "$BAD_IPS6"
  fail=1
fi

# 3. operator hostnames (keep this list free of the values it is hunting)
DENY='yourmom|fsck\.app|vps-de[0-9a-z]+|zeppo|robertdobbs'
scan "operator hostname" "$DENY"

# 4. operator's own absolute paths outside \$HERMES_HOME
#    (generic documentation examples like /home/me/projects are fine; the operator's real
#     home and known data mounts are not)
OPUSER=${USER:-$(id -un)}
PATHS=$(cd "$ROOT" && grep -hoIE '/home/[A-Za-z0-9_.-]+[^ "'"'"'`,)]*' "${FILES[@]}" 2>/dev/null \
        | sort -u \
        | grep -E "^/home/${OPUSER}/" \
        | grep -vF "$HERMES_HOME")
PATHS="$PATHS$(cd "$ROOT" && grep -hoIE '/(home|var/lib)/docker-data[^ "'"'"'`,)]*' "${FILES[@]}" 2>/dev/null | sort -u)"
PATHS=$(printf '%s\n' "$PATHS" | sed '/^$/d' | sort -u)
if [ -n "$PATHS" ]; then
  printf '\nFAIL  operator absolute paths outside %s:\n%s\n' "$HERMES_HOME" "$PATHS"
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  echo
  echo "publish-check: FAIL - fix the findings above; do not exempt them"
  exit 1
fi
echo "publish-check: PASS"
