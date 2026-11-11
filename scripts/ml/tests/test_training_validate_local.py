"""End-to-end local validation of the training kit (deploy/validate_local.sh) when the training venv exists.

Exports 3 tiles, smoke-trains 2 steps on CPU, resumes to step 3, exports ONNX with a parity check, infers through the
Sam3TextSegmenter-style interface and evaluates on the held-out references (about 2-3 minutes). Skips cleanly when
scripts/ml/training/.venv or the three source scenes are absent. The main venv never imports torch here: the work
runs in a subprocess under the training venv's interpreter.
"""
import os
import shutil
import subprocess

import pytest
from scripts.ml.training import common

VENV_PYTHON = common.PACKAGE / '.venv' / 'bin' / 'python'
SCRIPT = common.PACKAGE / 'deploy' / 'validate_local.sh'
SCENES = ('mooroolbark-50011498-17-118437-80408', 'mooroolbark-50011498-17-118438-80408', 'map-d34a1fe62200fa75')


@pytest.mark.skipif(not VENV_PYTHON.exists(), reason='training venv absent (python3.12 -m venv scripts/ml/training/.venv)')
def test_validate_local_end_to_end(tmp_path):
    missing = [s for s in SCENES if not (common.REPO / 'data/scenes' / s / 'latest.json').exists()]
    if missing:
        pytest.skip(f'source scenes absent: {missing}')
    probe = subprocess.run([str(VENV_PYTHON), '-c', 'import torch, torchvision, onnx, onnxruntime'], capture_output=True)
    if probe.returncode:
        pytest.skip('training venv lacks torch/torchvision/onnx (pip install -r scripts/ml/training/requirements-local.txt)')
    out = tmp_path / 'validate'
    try:
        result = subprocess.run(['bash', str(SCRIPT)], cwd=common.REPO, env={**os.environ, 'OUT': str(out)},
                                capture_output=True, text=True, timeout=1500)
        assert result.returncode == 0, result.stdout[-4000:] + '\n' + result.stderr[-4000:]
        assert 'VALIDATE_LOCAL_OK' in result.stdout
        assert (out / 'run' / 'roof_outline.onnx').exists() and (out / 'onnx_eval' / 'test_summary.json').exists()
    finally:
        shutil.rmtree(out, ignore_errors=True)   # ~1 GB of checkpoints and the ONNX file
