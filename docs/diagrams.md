# Triage Guard Diagrams

_This is a companion to `triage-guard-spec.md`. It shows two views of the same system: the architecture diagram and the central control-plane state machine, as Mermaid diagrams._

---

## Architecture diagram

_The diagram groups, agents, and numbered arrows below match [`../assets/triage-guard-system.png`](../assets/triage-guard-system.png). **Solid** arrows show Orchestrator to agent actions. **Dashed** arrows show agent to Orchestrator proposals. The **thick** arrow is the only time the Orchestrator writes to State. The numbers match the arrow numbers in the Transitions table of the spec._

![Triage Guard architecture](../assets/triage-guard-system.png)

## State-machine diagram

The central control-plane state machine, drawn from the states in the specification's Transitions table and the edges the running graph wires. Guards, actions and world effects stay in that table. Each arrow is labelled with the transition name the audit trail records for it.

How to read it:

- A self-loop is a step that ran again without moving the case: a retry after malformed output, a refused action (`blk`), a hand-off to a shift lead (`senior_escalation`), or a board move inside the queue (`move_confirmed`, `formal_validation`).
- "nurse acuity" marks the degrade path: the model is skipped or unusable and the case continues on the nurse's own acuity, with the acuity gate off.
- `release` closes the card. A charge nurse can release a patient from any state where the case is paused for a person, which is every state with a `release` arrow.

```mermaid
stateDiagram-v2
    direction TB
    [*] --> intake_received : entry
    intake_received --> parsing : normalized

    parsing --> data_parsed : submission_valid
    parsing --> missing_fields_requested : missing_fields
    parsing --> submission_failed : submission_unusable
    parsing --> input_rejected : invalid_input

    missing_fields_requested --> parsing : fields_resubmitted
    submission_failed --> parsing : fields_resubmitted
    input_rejected --> [*] : run ends

    data_parsed --> resolving_identity : lookup
    resolving_identity --> redacting_routing : crm_found / crm_new / af_db
    resolving_identity --> resolving_identity : retry
    resolving_identity --> input_rejected : duplicate_case

    redacting_routing --> classifying : payload_clean
    redacting_routing --> safety_validating : privacy_refused / privacy_gate_down (nurse acuity)
    redacting_routing --> agent_failed : af_pii (payload builder crashed)

    classifying --> acuity_proposed : acuity_proposed
    classifying --> classifying : v_retry_classifier
    classifying --> safety_validating : v_exhausted_classifier (nurse acuity)

    acuity_proposed --> safety_validating : acuity_agree / acuity_gap_minor
    acuity_proposed --> awaiting_human_approval : acuity_gap_major

    safety_validating --> verdict_proposed : safety_passed
    safety_validating --> awaiting_human_approval : safety_failed
    safety_validating --> awaiting_human_approval : v_exhausted_safety (validator down)

    verdict_proposed --> monitoring : cleared_to_queue
    verdict_proposed --> awaiting_human_approval : escalation_needed

    awaiting_human_approval --> safety_validating : gate_acuity_resolved / gate_safety_corrected / gate_revalidate
    awaiting_human_approval --> monitoring : gate_safety_waived
    awaiting_human_approval --> awaiting_human_approval : senior_escalation / blk

    monitoring --> monitoring : move_confirmed / formal_validation / blk
    monitoring --> reassessment_required : reassessment_due
    reassessment_required --> parsing : front_door_rerun

    agent_failed --> redacting_routing : af_recover

    missing_fields_requested --> case_closed : release
    submission_failed --> case_closed : release
    awaiting_human_approval --> case_closed : release
    monitoring --> case_closed : release
    reassessment_required --> case_closed : release
    agent_failed --> case_closed : release
    case_closed --> [*]
```
