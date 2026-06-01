#!/usr/bin/env bash
set -euo pipefail

python -m pip install -U pip setuptools wheel

pip install -e ".[all]"

if [[ -t 0 ]] && [[ -z "${HF_TOKEN:-}" ]] && [[ -z "${HUGGING_FACE_HUB_TOKEN:-}" ]]; then
  hf auth login
else
  echo "Skipping hf auth login (non-TTY stdin and/or HF_TOKEN or HUGGING_FACE_HUB_TOKEN already set)."
fi

if ! python -c "import torch" 2>/dev/null; then
  echo "PyTorch not found; installing torch (RunPod bare pods may need this)."
  pip install torch
fi

echo "Optional: export ELECTRICITYMAPS_API_KEY for live grid carbon (not required for default static grid)."

if ! watermark --help >/dev/null; then
  echo "error: watermark --help failed" >&2
  exit 1
fi
