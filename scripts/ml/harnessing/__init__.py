"""Recursive agent harnessing (Harness²) for addr roof, scene-object and boundary tracing agents.

Task-level recursion refines the tracing agent's harness on one case with a contrastive
Propose–Probe–Compose step; harnessing-level recursion keeps the adopted harness and an
evidence-supported lesson per domain for later cases. Model weights and saved scenes are never
changed. Adapted from Harness² (google-research/harness2, Apache-2.0); see NOTICE.md.
"""
