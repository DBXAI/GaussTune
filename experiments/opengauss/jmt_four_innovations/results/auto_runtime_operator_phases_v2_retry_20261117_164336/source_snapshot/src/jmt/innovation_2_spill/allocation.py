"""Deprecated import shim; all allocation belongs to innovation THREE.

Kept solely for replay of the superseded 2026-11-12 fixed-SB experiment.
Innovation two's current interface is curves.py and never chooses SB/WM.
"""
from jmt.innovation_3_search.spill_allocator import (  # noqa: F401
    SpillOption, allocate_known_cohort, allocate_stages, identify_boundaries,
    mode_signature, option_from_forecast, pareto_options, solve_spill_dp, validate,
)
