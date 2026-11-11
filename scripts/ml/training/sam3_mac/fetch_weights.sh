#!/bin/sh
# Fetch the gated SAM3 checkpoint from Hugging Face into scripts/ml/models/sam3-hf (gitignored).
# Needs one of: `hf auth login` done once on this machine, or HF_TOKEN in the environment.
set -eu
cd "$(dirname "$0")/../../../.."
DEST=scripts/ml/models/sam3-hf
HF=scripts/ml/training/.venv-sam3/bin/hf
if [ ! -x "$HF" ]; then
  echo "hf CLI missing in the sam3 venv; see scripts/ml/training/sam3_mac/VENDOR.json" >&2
  exit 2
fi
if [ -z "${HF_TOKEN:-}" ] && ! "$HF" auth whoami >/dev/null 2>&1; then
  echo "Not logged in to Hugging Face. Run:  hf auth login   (or export HF_TOKEN=...) then re-run this script." >&2
  exit 3
fi
mkdir -p "$DEST"
# the CLI did not pick up the saved read token on this machine (401 "requires approval") while the library did; pass it explicitly
TOKEN=${HF_TOKEN:-$(scripts/ml/training/.venv-sam3/bin/python -c 'from huggingface_hub import get_token; print(get_token() or "")')}
"$HF" download facebook/sam3 sam3.pt config.json --local-dir "$DEST" --token "$TOKEN"
ls -la "$DEST"
shasum -a 256 "$DEST/sam3.pt" | tee "$DEST/sam3.pt.sha256"
