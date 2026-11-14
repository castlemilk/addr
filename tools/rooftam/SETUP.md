# Kaggle client setup and repair

Run `task rooftam:setup` from the repository root. Setup explicitly installs
`kaggle==2.2.4` and `six==1.17.0` in the existing managed virtual environment,
then runs `pip check`, `kaggle --version`, and `kaggle auth login --help`.
Only after all checks pass does it report success. The help check does not
start sign-in, access an account, or submit compute.

## Repair `ModuleNotFoundError: No module named 'six'`

Kaggle 2.2.4 imports `six` without declaring it as a dependency. Older
`python-dateutil` versions installed it transitively; newer ones need not.
A successful pip install alone therefore does not establish that Kaggle starts.
This traceback occurs before OAuth, and is not evidence that account verification
failed or that Python 3.15 is inherently incompatible.

After pulling the fix, repair the existing environment and continue:

```sh
git pull --ff-only
task rooftam:setup
task rooftam:auth
task rooftam:doctor
```

On macOS, the immediate repair without updating the checkout is:

```sh
"$HOME/Library/Application Support/addr-rooftam/kaggle-2.2.4/bin/python" \
  -m pip install 'six==1.17.0'
"$HOME/Library/Application Support/addr-rooftam/kaggle-2.2.4/bin/kaggle" --version
task rooftam:auth
task rooftam:doctor
```

Use that environment's Python, not a system-wide pip; quote the Application
Support path. Do not use sudo or delete credentials/run directories. Setup repairs
in place and retains the managed environment path. For an explicitly overridden
`--kaggle` executable, repair that client's own environment instead.

Setup tests include fail-fast behavior at each stage and repeated repair. The
separate Kaggle setup smoke workflow installs the real client on Linux/macOS
with Python 3.12/3.15, removes `six` to reproduce the failure, and reruns setup to
check recovery. These startup checks do not authenticate or allocate Kaggle compute.
Workflow definitions alone are not execution evidence; inspect their actual runs.

Upstream acknowledgement:
https://github.com/Kaggle/kaggle-cli/blob/main/CHANGELOG.md
