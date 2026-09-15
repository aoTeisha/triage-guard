"""reassessment_required — re-enters case intake after a reassessment timer
fires.

A pass-through node: the timer already fired back in `monitoring`. This
node's only job is to mark the case as re-filed and hand it back to
`parsing`, which re-validates whatever `raw_payload` is already stored on
the case.

# ponytail: reuses the existing raw_payload rather than collecting a fresh
# nurse re-filing; add a real resubmission endpoint when that gap gets
# prioritized.
"""

from __future__ import annotations

from typing import Any

from app.deterministic import audit
from app.graph.state import TriageState
from app.labels import Arrow
from app.states import State


def reassessment_required(state: TriageState) -> dict[str, Any]:
    return {
        "control_state": State.REASSESSMENT_REQUIRED.value,
        "audit_log": [
            audit(state.case_id, State.REASSESSMENT_REQUIRED, "emit_event_log",
                  "re-entering intake after reassessment timeout", Arrow.FRONT_DOOR_RERUN),
        ],
    }
