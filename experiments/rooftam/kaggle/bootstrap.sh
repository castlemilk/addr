#!/usr/bin/env bash
# Run from the addr root. Isolated environment: never replace Kaggle's own torch.
set -euo pipefail
mode="${1:-tpu}"
[[ "$mode" == tpu || "$mode" == cpu ]] || { echo 'Usage: bootstrap.sh tpu|cpu'; exit 2; }
root="$(pwd)"
work="${ROOFTAM_WORK:-/kaggle/working/rooftam-runtime}"
rev=7711e012a30a2402c4eaab637bdb00a521302c91
mkdir -p "$work"
python -m venv "$work/venv"
py="$work/venv/bin/python"
"$py" -m pip install 'pip==25.2' 'setuptools==80.9.0' 'wheel==0.45.1'
"$py" -m pip install 'torch==2.8.0' 'torchvision==0.23.0' --index-url https://download.pytorch.org/whl/cpu
if [[ "$mode" == tpu ]]; then
  "$py" -m pip install 'torch_xla[tpu]==2.8.0'
fi
"$py" -m pip install -r "$root/experiments/rooftam/requirements.txt"
upstream="$work/EdgeTAM"
if [[ ! -d "$upstream/.git" ]]; then
  git init "$upstream"
  git -C "$upstream" remote add origin https://github.com/facebookresearch/EdgeTAM.git
fi
# Fail rather than silently overwrite an altered upstream checkout.
[[ -z "$(git -C "$upstream" status --porcelain)" ]] || { echo 'Upstream checkout is dirty; use a new ROOFTAM_WORK'; exit 1; }
git -C "$upstream" fetch --depth 1 origin "$rev"
GIT_LFS_SKIP_SMUDGE=1 git -C "$upstream" checkout --detach "$rev"
[[ "$(git -C "$upstream" rev-parse HEAD)" == "$rev" ]]
# Resolve Git LFS only when the pinned tree contains a pointer, with hash verification.
"$py" - "$upstream" "$rev" <<'PY'
import hashlib, pathlib, sys, urllib.request
repo, rev = pathlib.Path(sys.argv[1]), sys.argv[2]
p = repo/'checkpoints/edgetam.pt'
raw = p.read_bytes()
if raw.startswith(b'version https://git-lfs.github.com/spec/v1'):
    expected = [x.split(':',1)[1] for x in raw.decode().splitlines() if x.startswith('oid sha256:')][0]
    url = f'https://media.githubusercontent.com/media/facebookresearch/EdgeTAM/{rev}/checkpoints/edgetam.pt'
    data = urllib.request.urlopen(url, timeout=120).read()
    if hashlib.sha256(data).hexdigest() != expected:
        raise RuntimeError('Checkpoint hash mismatch')
    # Keep git checkout clean; put hydrated weights outside the upstream tree.
    p = repo.parent/'edgetam.pt'
    p.write_bytes(data)
else:
    p = repo/'checkpoints/edgetam.pt'
(repo.parent/'checkpoint-path.txt').write_text(str(p.resolve()))
print('Checkpoint SHA256:', hashlib.sha256(p.read_bytes()).hexdigest())
PY
SAM2_BUILD_CUDA=0 "$py" -m pip install --no-build-isolation --no-deps -e "$upstream"
"$py" -m pip check
"$py" -m pip freeze > "$work/resolved-requirements.txt"
printf '\nPython: %s\nCheckpoint: %s\n' "$py" "$(cat "$work/checkpoint-path.txt")"
