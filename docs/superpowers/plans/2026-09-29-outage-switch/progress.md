# Progress — outage switch

- [x] 1. `component_outages` table + `app/outages.py` (tests first)
- [x] 2. Hooks in classifier, OPA, Prolog, Datalog, sweeper
- [x] 3. Shift-lead stand-in for move, release, gate; nodes mark `degraded`
- [x] 4. Writeback timer retried, not cancelled, when OPA cannot answer
- [x] 5. Board API `/api/outages`
- [x] 6. Board UI: System health panel, outage strip, role pickers
- [x] 7. Remove per-case `component_down` scenario
- [x] 8. Docs: STATUS.md, SYSTEM_MODELING.md
- [x] 9. Full test runs
- [x] 10. Validator down: a shift lead may clear the case to the queue without the check (`safety_waived`); move accepts it; trace check allows it only after a check that could not run
