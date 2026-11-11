#!/bin/sh
# Apply the Mac patches to the vendored sam3-apple-silicon clone. Idempotent: a patch that already applies in reverse is
# reported as applied; a patch that neither applies nor reverses is a conflict and the script exits non-zero.
set -eu
cd "$(dirname "$0")/../../vendor/sam3-apple-silicon"
status=0
for p in ../../sam3_mac/patches/*.patch; do
  if git apply --check "$p" 2>/dev/null; then
    git apply "$p" && echo "applied $(basename "$p")"
  elif git apply --reverse --check "$p" 2>/dev/null; then
    echo "already applied $(basename "$p")"
  else
    echo "CONFLICT $(basename "$p"): neither applies nor reverses cleanly" >&2
    status=1
  fi
done
exit $status
