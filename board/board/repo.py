"""Listing cases — the one thing the control plane could not do.

`app.runner` gives `snapshot(case_id)` and `history(case_id)`; both need an id you
already have. A board is exactly an enumeration, so this module supplies it.

`CheckpointRepo` enumerates the checkpointer itself: `SqliteSaver` keeps one
row-set per `thread_id`, and `thread_id == case_id` by construction in
`app.runner`. No new writes, no schema to keep in sync with the graph.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from typing import Any

from app import runner
from app.states import State
from app.views import CaseCard, card_from_state


class CheckpointRepo:
    """Read the checkpointer directly.

    # ponytail: one state load per case per refresh, and no server-side filter.
    # Fine to ~100 cases on one box. Past that, write a `board_cards` projection
    # row at the end of start_case/resume_case and read the board off that table
    # instead — same three methods, so only this class changes.
    """

    def case_ids(self) -> list[str]:
        # runner.DB_PATH is read per call, not captured at import: the tests
        # point it at a temp file, and a captured path would ignore them.
        # Read-only: the board must not be able to write the checkpoint store
        # even by accident. Before the first case there is no file, or a file
        # whose tables the checkpointer has not created yet — both mean an empty
        # board, which is the correct answer and not an error page.
        try:
            with closing(sqlite3.connect(f"file:{runner.DB_PATH}?mode=ro", uri=True)) as conn:
                rows = conn.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()
        except sqlite3.OperationalError:
            return []
        return [r[0] for r in rows]

    def load(self, case_id: str) -> tuple[dict[str, Any], bool]:
        """One case as (state, at_gate), from a single checkpoint read.

        `values` alone is not enough: a run suspended at the human gate has not
        committed the gate node's write, so the fact that a charge nurse is being
        waited on lives in the checkpoint's *next* task. Reading both at once is
        what keeps those cases on the board without loading each case twice.
        """
        snap = runner.graph().get_state(runner.config_for(case_id))
        at_gate = State.AWAITING_HUMAN_APPROVAL.value in [
            getattr(n, "value", n) for n in snap.next
        ]
        return snap.values, at_gate

    def scan(self) -> list[tuple[dict[str, Any], bool]]:
        """Every persisted case as (state, at_gate). One pass, so a refresh that
        needs both cards and the notification feed loads each case once.
        """
        return [self.load(cid) for cid in self.case_ids()]

    def cards(self, now: datetime | None = None) -> list[CaseCard]:
        """Every case that is on the board. Cases still in intake, refused, or
        closed project to None and are dropped here.
        """
        cards = (card_from_state(s, now, gated) for s, gated in self.scan())
        return [c for c in cards if c is not None]

    def card(self, case_id: str, now: datetime | None = None) -> CaseCard | None:
        state, at_gate = self.load(case_id)
        return card_from_state(state, now, at_gate)
