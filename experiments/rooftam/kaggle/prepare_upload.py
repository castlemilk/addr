"""Prepare PRIVATE Kaggle metadata; does not authenticate, upload or start a run."""
import argparse
import json
from pathlib import Path
import re

import nbformat
from .make_notebook import build

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--owner',required=True,help='Your actual Kaggle username, not assumed from GitHub')
    p.add_argument('--addr-ref',required=True,help='Full immutable addr commit SHA')
    p.add_argument('--output',required=True)
    p.add_argument('--dataset',action='append',default=[])
    a=p.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]+',a.owner) or not re.fullmatch(r'[0-9a-f]{40}',a.addr_ref):
        p.error('Supply a valid Kaggle username and full addr commit SHA')
    out=Path(a.output);out.mkdir(parents=True,exist_ok=True)
    if any(out.iterdir()): p.error('Output must be empty')
    nbformat.write(build(a.addr_ref),out/'rooftam_tpu.ipynb')
    metadata=dict(id=f'{a.owner}/addr-rooftam-tpu',title='addr RoofTAM TPU experiment',
        code_file='rooftam_tpu.ipynb',language='python',kernel_type='notebook',is_private=True,
        enable_gpu=False,enable_tpu=True,enable_internet=True,machine_shape='TpuV5E8',
        dataset_sources=a.dataset,competition_sources=[],kernel_sources=[])
    (out/'kernel-metadata.json').write_text(json.dumps(metadata,indent=2)+'\n')
    print(f'Prepared {out}. Review it, then: kaggle kernels push -p {out} --accelerator TpuV5E8')
    print('Verify TPU selection in the Kaggle editor. Metadata is not proof of attached hardware.')
