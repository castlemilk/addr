"""Generate the importable Kaggle notebook; optional immutable addr commit ref."""
from pathlib import Path
import argparse
import nbformat as nb


def build(addr_ref: str):
    book=nb.v4.new_notebook()
    book.metadata.kernelspec={'display_name':'Python 3','language':'python','name':'python3'}
    cells=[nb.v4.new_markdown_cell('''# addr / RoofTAM — Kaggle TPU experiment
Select a **TPU accelerator** in Kaggle settings and enable Internet. Keep this notebook private.
This is a **single-worker compatibility and decoder-adaptation experiment**, not an all-core TPU benchmark.
The default run uses generated shapes: it proves plumbing, not roof accuracy.
A real TPU arithmetic gate, tiny control and actual pretrained EdgeTAM forward/backward are separate stages.
CPU fallback is an error. XLA fallback counters need review before performance claims.
No real property imagery, human labels or credentials are included or uploaded by this notebook.
'''), nb.v4.new_code_cell(f'''from pathlib import Path
import datetime, json, os, subprocess, sys
ADDR_REF = {addr_ref!r}  # Replace with a full commit SHA to reproduce a previous run.
REAL_MANIFEST = None  # Example: '/kaggle/input/your-reviewed-roofs/manifest.jsonl'
SAM3_PREDICTIONS = None  # Optional pre-exported predictions.json for the SAME real manifest.
REAL_STEPS = 100
BATCH_SIZE = 1
SEED = 17
DEVICE = 'tpu'  # Do not change to CPU and call the result a TPU run.
WORK = Path('/kaggle/working')
SOURCE = WORK/'addr-rooftam-source'
RUNTIME = WORK/'rooftam-runtime'
RUN = WORK/('rooftam-run-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
RUN.mkdir(parents=True)
def command(args, **kw):
    print('Running:', ' '.join(map(str,args)))
    return subprocess.run(list(map(str,args)), check=True, **kw)
'''),nb.v4.new_code_cell('''# Isolated checkout; never reset or overwrite an edited worktree.
if not (SOURCE/'.git').exists():
    command(['git','init',SOURCE])
    command(['git','-C',SOURCE,'remote','add','origin','https://github.com/castlemilk/addr.git'])
assert not subprocess.check_output(['git','-C',str(SOURCE),'status','--porcelain'],text=True).strip(), 'Dirty checkout; choose another SOURCE'
command(['git','-C',SOURCE,'fetch','--depth','1','origin',ADDR_REF])
command(['git','-C',SOURCE,'checkout','--detach','FETCH_HEAD'])
resolved = subprocess.check_output(['git','-C',str(SOURCE),'rev-parse','HEAD'],text=True).strip()
(RUN/'addr-revision.txt').write_text(resolved+'\\n')
print('Resolved addr commit:',resolved)
command(['bash','experiments/rooftam/kaggle/bootstrap.sh','tpu'],cwd=SOURCE,
        env={**os.environ,'ROOFTAM_WORK':str(RUNTIME)})
PYTHON = RUNTIME/'venv/bin/python'
CHECKPOINT = (RUNTIME/'checkpoint-path.txt').read_text().strip()
# Record resolved transitive dependency versions with the run.
(RUN/'resolved-requirements.txt').write_text((RUNTIME/'resolved-requirements.txt').read_text())
def bench(action, output, *args):
    command([PYTHON,'-m','experiments.rooftam.bench',action,'--output',RUN/output,*args],cwd=SOURCE)
'''),nb.v4.new_code_cell('''# Gate 1: local unit tests, then actual TPU arithmetic + gradient execution.
command([PYTHON,'-m','pytest','-q','experiments/rooftam/tests'],cwd=SOURCE)
bench('preflight','01-preflight','--device',DEVICE)
print((RUN/'01-preflight/preflight.json').read_text())
'''),nb.v4.new_code_cell('''# Gate 2: generated fixture and a tiny TPU training control (NOT EdgeTAM accuracy).
bench('fixture','fixture')
FIXTURE = str(RUN/'fixture/manifest.jsonl')
shared=['--manifest',FIXTURE,'--allow-synthetic','--device',DEVICE,'--seed',str(SEED)]
bench('validate','02-validation',*shared)
bench('train','03-tiny-control',*shared,'--model','tiny','--side','64','--steps','3')
'''),nb.v4.new_code_cell('''# Gate 3: actual pinned EdgeTAM, with decoder gradients and saved weights.
edge=shared+['--model','edgetam','--side','1024','--checkpoint',CHECKPOINT]
bench('evaluate','04-edgetam-before',*edge)
bench('train','05-edgetam-smoke',*edge,'--steps','3','--batch-size',str(BATCH_SIZE))
bench('evaluate','06-edgetam-after',*edge,'--adapter',str(RUN/'05-edgetam-smoke/adapter.pt'))
bench('compare','07-smoke-comparison','--reports',str(RUN/'04-edgetam-before/evaluation.json'),
      str(RUN/'06-edgetam-after/evaluation.json'))
print((RUN/'05-edgetam-smoke/run.json').read_text())
print('Synthetic results are plumbing evidence only. Review aten_counters before interpreting TPU performance.')
'''),nb.v4.new_code_cell('''# Optional: real, human-reviewed, licensed data. No automatic test-set tuning.
if REAL_MANIFEST is None:
    print('REAL ROOF TRAINING NOT RUN: attach a reviewed dataset and set REAL_MANIFEST.')
else:
    real=['--manifest',REAL_MANIFEST,'--device',DEVICE,'--model','edgetam',
          '--side','1024','--checkpoint',CHECKPOINT,'--seed',str(SEED),'--split','val']
    bench('validate','10-real-validation',*real)
    bench('evaluate','11-real-before',*real)
    bench('train','12-real-train',*real,'--steps',str(REAL_STEPS),'--batch-size',str(BATCH_SIZE))
    bench('evaluate','13-real-after',*real,'--adapter',str(RUN/'12-real-train/adapter.pt'))
    reports=[str(RUN/'11-real-before/evaluation.json'),str(RUN/'13-real-after/evaluation.json')]
    if SAM3_PREDICTIONS:
        bench('score-export','14-sam3','--manifest',REAL_MANIFEST,'--split','val','--predictions',SAM3_PREDICTIONS)
        reports.append(str(RUN/'14-sam3/evaluation.json'))
    bench('compare','15-real-comparison','--reports',*reports)
'''),nb.v4.new_code_cell('''# Save a Kaggle notebook version to retain outputs; /kaggle/working is session storage.
import shutil
archive=shutil.make_archive(str(RUN),'zip',RUN)
print('Run artifacts:',RUN)
print('Archive:',archive)
print('Archive can contain property imagery/masks or learned weights: keep it private and respect dataset rights.')
'''),nb.v4.new_markdown_cell('''## What a successful run establishes
`01-preflight/preflight.json` proves device arithmetic; `03-tiny-control/run.json` proves training plumbing;
`05-edgetam-smoke/run.json` proves this EdgeTAM adapter updates weights on the selected runtime.
Only optional real-data evaluations measure roof masks. None of these stages establishes ridge/valley,
truss, height, survey, detection-recall or all-core TPU performance. CPU ONNX SAM3 timings are not TPU speedup evidence.
If a gate fails, keep `failure.json` and the notebook traceback; do not mark it passed or substitute a CPU run.
''')]
    book.cells=cells
    return book


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--addr-ref',default='experiments/rooftam-kaggle-tpu-20261114')
    p.add_argument('--output',default=str(Path(__file__).with_name('rooftam_tpu.ipynb')))
    args=p.parse_args()
    nb.write(build(args.addr_ref),args.output)
