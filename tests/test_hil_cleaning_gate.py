from __future__ import annotations

import argparse

import pytest

from scripts.hil_cleaning_gate import (
    validate_cleaning_config,
    validate_source_candidate,
)


def _config(*, flow: float = 1500, approved: float = 1500) -> dict:
    return {
        "cleaning": {
            "gas_label": "Air",
            "default_channels": [2, 3],
            "default_flow_sccm": flow,
            "max_approved_flow_sccm": approved,
            "fixed_flow_setpoints_sccm": {"B": 0, "C": 0},
            "default_open_duration_s": 10,
            "default_cycles": 3,
        }
    }


def test_live_cleaning_gate_rejects_flow_above_approved_boundary() -> None:
    with pytest.raises(ValueError, match="超出批准范围"):
        validate_cleaning_config(_config(flow=2001, approved=1500))


def test_live_cleaning_gate_records_frozen_recipe() -> None:
    recipe = validate_cleaning_config(_config())

    assert recipe["selected_channels"] == [2, 3]
    assert recipe["flow_sccm"] == 1500
    assert recipe["max_approved_flow_sccm"] == 1500
    assert recipe["fixed_flow_setpoints_sccm"] == {"B": 0, "C": 0}


def test_formal_live_gate_requires_clean_matching_candidate() -> None:
    parser = argparse.ArgumentParser()
    with pytest.raises(SystemExit):
        validate_source_candidate(
            parser,
            live=True,
            exploratory=False,
            candidate_commit="a" * 40,
            state={
                "head": "b" * 40,
                "worktree_clean": False,
            },
        )


def test_exploratory_live_gate_preserves_dirty_source_disclosure() -> None:
    parser = argparse.ArgumentParser()
    validate_source_candidate(
        parser,
        live=True,
        exploratory=True,
        candidate_commit="",
        state={
            "head": "b" * 40,
            "worktree_clean": False,
        },
    )
