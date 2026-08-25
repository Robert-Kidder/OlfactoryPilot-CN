from __future__ import annotations

import re
from pathlib import Path


def test_story_4_6_gate_matches_the_single_dynamic_development_status() -> None:
    content = Path("docs/sprint-artifacts/sprint-status.yaml").read_text(
        encoding="utf-8"
    )
    development = re.search(
        r"(?m)^  4-6-manual-experiment-v3-replacement:\s*(\S+)\s*$",
        content,
    )
    gate = re.search(
        r"(?ms)^  - scope: 4-6-manual-experiment-v3-replacement\s*\n"
        r"    status:\s*(\S+)\s*$",
        content,
    )

    assert development is not None
    assert gate is not None
    assert development.group(1) == gate.group(1) == "review"
