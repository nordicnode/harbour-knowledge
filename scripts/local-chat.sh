#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OLLAMA_VERSION="${OLLAMA_VERSION:-0.40.17}"
OLLAMA_BIN="${OLLAMA_BIN:-$ROOT/storage/ollama-runtime/bin/ollama}"
OLLAMA_MODELS="${OLLAMA_MODELS:-$ROOT/storage/ollama-models}"
LOCAL_CHAT_MODEL="${LOCAL_CHAT_MODEL:-qwen3:4b-instruct-2507-q4_K_M}"
OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11434}"

export OLLAMA_HOST OLLAMA_MODELS
export OLLAMA_CONTEXT_LENGTH="${OLLAMA_CONTEXT_LENGTH:-8192}"
export OLLAMA_KV_CACHE_TYPE="${OLLAMA_KV_CACHE_TYPE:-q8_0}"
export OLLAMA_FLASH_ATTENTION="${OLLAMA_FLASH_ATTENTION:-1}"
export OLLAMA_NUM_PARALLEL="${OLLAMA_NUM_PARALLEL:-1}"
export OLLAMA_MAX_LOADED_MODELS="${OLLAMA_MAX_LOADED_MODELS:-1}"
export OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-30m}"

usage() {
  cat <<'EOF'
Usage: scripts/local-chat.sh <command>

  install   Download the project-local Ollama runtime into storage/
  serve     Start Ollama on 127.0.0.1:11434
  pull      Pull the recommended chat model
  warm      Load the model with a tiny request
EOF
}

install_runtime() {
  if [[ -x "$OLLAMA_BIN" ]]; then
    echo "Ollama runtime already installed at $OLLAMA_BIN"
    return
  fi
  command -v curl >/dev/null || { echo "curl is required" >&2; exit 1; }
  command -v tar >/dev/null || { echo "tar is required" >&2; exit 1; }
  command -v zstd >/dev/null || { echo "zstd is required to unpack Ollama" >&2; exit 1; }

  local archive="$ROOT/storage/ollama-linux-amd64.tar.zst"
  local checksums="$ROOT/storage/ollama-sha256sum.txt"
  mkdir -p "$ROOT/storage/ollama-runtime" "$OLLAMA_MODELS"
  echo "Downloading Ollama $OLLAMA_VERSION..."
  curl -fL --retry 3 --retry-delay 5 \
    "https://github.com/ollama/ollama/releases/download/v${OLLAMA_VERSION}/ollama-linux-amd64.tar.zst" \
    -o "$archive"
  curl -fsSL \
    "https://github.com/ollama/ollama/releases/download/v${OLLAMA_VERSION}/sha256sum.txt" \
    -o "$checksums"
  local expected actual
  expected="$(awk '$2 ~ /ollama-linux-amd64.tar.zst$/ {print $1}' "$checksums")"
  actual="$(sha256sum "$archive" | awk '{print $1}')"
  if [[ -z "$expected" || "$actual" != "$expected" ]]; then
    echo "Ollama download checksum mismatch" >&2
    exit 1
  fi
  tar --zstd -xf "$archive" -C "$ROOT/storage/ollama-runtime"
  echo "Installed $("$OLLAMA_BIN" --version 2>&1 | tail -1)"
  echo "Next: make local-chat  (in one terminal), then make local-chat-pull"
}

require_runtime() {
  if [[ ! -x "$OLLAMA_BIN" ]]; then
    echo "Ollama runtime not found at $OLLAMA_BIN" >&2
    echo "Run: make local-chat-install" >&2
    exit 1
  fi
}

case "${1:-serve}" in
  install) install_runtime ;;
  serve)
    require_runtime
    exec "$OLLAMA_BIN" serve
    ;;
  pull)
    require_runtime
    "$OLLAMA_BIN" pull "$LOCAL_CHAT_MODEL"
    ;;
  warm)
    require_runtime
    curl -fsS --max-time 300 "http://${OLLAMA_HOST}/v1/chat/completions" \
      -H 'content-type: application/json' \
      -d "{\"model\":\"${LOCAL_CHAT_MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"Say ready in one word.\"}],\"temperature\":0,\"max_tokens\":8}" \
      >/dev/null
    echo "Warmed $LOCAL_CHAT_MODEL"
    ;;
  -h|--help|help) usage ;;
  *)
    usage >&2
    exit 2
    ;;
esac
