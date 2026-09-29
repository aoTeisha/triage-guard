# Progress — component-down demo

- [x] 1. OPA privacy check can't answer → model skipped, nurse acuity, `degraded=["opa"]` (tests first)
- [x] 2. Real identifier leak still halts (regression test)
- [x] 3. Prolog / Datalog can't answer → `ValidatorUnavailable` → validator-down gate, `degraded` names the engine
- [x] 4. Gate `revalidate` (no change needed) → safety re-run → queue; rounds → shift lead
- [x] 5. Trace check accepts the revalidate loop
- [x] 6. Board: `component_down` submission type + `component` field + patches
- [x] 7. Board UI: component dropdown, chips, "Running degraded" panel box, validator-down gate, monitor banner
- [x] 8. Board tests (lifecycle per component, 422, demo-cases)
- [x] 9. Docs: STATUS.md, SYSTEM_MODELING.md Safe Fallbacks
- [ ] 10. Full test runs (done) + manual check in a browser (not done: board not running, browser tool unavailable)
