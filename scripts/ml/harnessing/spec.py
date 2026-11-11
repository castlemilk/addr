"""The adapter seam: everything a case family supplies to the method, and nothing more.

Method (recursion, prompts, selection, library), executor (the agent CLI a rollout runs under)
and case family (an adapter) stay separate. A `RunView` never carries a measurement: the judge
reads runs only after selection committed. Adapted from Harness² core/spec.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

MEASUREMENT_WORDS = ('benchmark', 'evaluation', 'grader', 'rubric', 'score', 'ground truth',
                     'reference polygon', 'reference outline')


@dataclass(frozen=True)
class WorkPrompts:
    """The work-shaped text the shared prompts interpolate. It names the work, never its measure."""

    work: str
    domain_word: str = 'case family'
    deliverable_word: str = 'outline'
    inv_levers: str = ''
    dom_levers: str = ''
    yardstick: str = ''
    domain_guidance: str = ''
    selection_checks: str = ''

    def __post_init__(self):
        for name in ('work', 'inv_levers', 'dom_levers', 'yardstick', 'domain_guidance',
                     'selection_checks'):
            low = getattr(self, name).lower()
            bad = next((word for word in MEASUREMENT_WORDS if word in low), None)
            if bad:
                raise ValueError(f'WorkPrompts.{name} must describe the WORK, not its measurement '
                                 f'(found {bad!r}). Say what the practitioner needs.')


@dataclass
class RunView:
    """What the method knows about one finished rollout."""

    label: str
    run_id: str
    run_dir: Path
    ok: bool
    delivered: bool
    cost_usd: float = 0.0
    wall_s: float = 0.0
    error: str = ''


@runtime_checkable
class Adapter(Protocol):
    """Implement these and the whole method works; nothing here is method logic."""

    name: str
    executor: str
    prompts: WorkPrompts
    leak_globs: tuple[str, ...]
    allow_subagents: bool
    allow_hooks: bool
    obs_trunc: int

    def tasks(self, split: str = 'all') -> list[str]:
        """Case ids in a stable order: library state must be reproducible."""

    def domain_of(self, task_id: str) -> str:
        """The library grouping: roof outlines, one scene-object class, or segmentation."""

    def task_prompt(self, task_id: str) -> str:
        """The task as the agent received it, with any held-out outline stripped."""

    def task_files(self, task_id: str) -> Path | None:
        """The case source material the work was done against, or None."""

    def execute(self, task_id: str, harness_dir: Path | None, run_id: str, *,
                timeout: int | None = None) -> Path:
        """Run one rollout idempotently by run_id; `harness_dir=None` is the base harness."""

    def delivered(self, run_dir: Path) -> bool:
        """Did this rollout submit the outline the task asked for?"""

    def trajectory(self, run_dir: Path, *, obs_trunc: int) -> str:
        """The rollout in the shared [ACTION]/[OBSERVATION]/[ERROR] grammar."""

    def deliverable_text(self, run_dir: Path) -> str:
        """The submitted outline and its notes as text, from agent-authored output only."""

    def deliverable_images(self, run_dir: Path) -> list[Path]:
        """Rendered overlays of the submitted outline on the case imagery."""

    def base_harness(self) -> Path:
        """A directory holding the unmodified harness tree this family starts from."""

    def validate_tree(self, tree: dict[str, str]) -> None:
        """Family invariants; raise ValueError in actionable prose (fed back as retry feedback)."""

    def score(self, run_dir: Path, task_id: str) -> dict | None:
        """Judge one run against held-out data; None is a failed judge, never a zero."""


def resolve_prompts(adapter, task_id):
    """The case-specific prompt profile when the adapter supplies one."""
    fn = getattr(adapter, 'prompts_for', None)
    prompts = fn(task_id) if callable(fn) else adapter.prompts
    if not isinstance(prompts, WorkPrompts):
        raise TypeError(f'{type(adapter).__name__}.prompts_for({task_id!r}) returned '
                        f'{type(prompts).__name__}, expected WorkPrompts')
    return prompts
