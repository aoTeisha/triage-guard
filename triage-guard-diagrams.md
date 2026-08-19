# Triage Guard Diagrams

_This is a companion to `triage-guard-spec.md`. It shows two views of the same system: the architecture, using a Mermaid version of the Whimsical board, and the central control-plane state machine._

---

## Architecture diagram

_The diagram uses the same groups, agents, and numbered arrows as the Whimsical board. **Solid** arrows show Orchestrator to agent actions. **Dashed** arrows show agent to Orchestrator proposals. The **thick** arrow is the only time the Orchestrator writes to State. The numbers match the arrow numbers in the Transitions table of the spec._

[![Triage Guard architecture — Whimsical board](assets/triage-guard-whimsical-board.png)](https://whimsical.com/board/ExceXhcWbUXuQe3Ahn63CB)

```mermaid
flowchart LR
  subgraph IN["Input channels"]
    PDF["PDF document<br/>(triage questionnaire)"]
    WEB["Website form"]
  end
  API["API Gateway"]

  subgraph INTAKE["Intake / ingestion"]
    ROUTER["Channel Router"]
    NORM["Input Normalizer<br/>unified message<br/>(mock data · no live OCR)"]
  end

  subgraph USR["Understanding, safety & routing"]
    PII["PII / sensitive-data filter<br/>(remove id · name · surname)"]
    POL["Policy Gate"]
    SENT["Sentiment / Urgency<br/>(distress · pain)"]
  end

  subgraph CORE["Agent Core"]
    ORCH["Orchestrator Agent<br/>state machine · planner<br/>sole writer to State"]
    IP["Intake Parser Agent<br/>(LLM)"]
    ACL["Acuity Classifier Agent<br/>(LLM)"]
    SV["Safety Validation Agent<br/>Prolog · Datalog · Z3 · OPA"]
    HE["Human Escalation Agent"]
    WM["Waiting Room Monitor Agent"]
    AUD["Audit Agent"]
  end

  STATE[("State<br/>Control · Data · World")]

  subgraph KM["Knowledge & Memory"]
    KB["Knowledge Base"]
    VDB["Vector DB<br/>(policies)"]
    CRM["CRM<br/>(patient profile + history)"]
    SESS["Session Memory"]
  end

  subgraph MON["Monitoring & Evaluation"]
    LOGS["Logs + Traces"]
    EVAL["Evaluation Pipeline<br/>(tests + regression)"]
    DASH["Metrics Dashboard"]
    FB["User / Agent feedback"]
  end

  NOTIFY["Notify user<br/>15 reassessment · 16 missing fields<br/>17 rescan/manual · 18 wrong doc<br/>19 accepted · 20 approval"]

  PDF -- "1a" --> API
  WEB -- "1b" --> API
  API --> ROUTER --> NORM
  NORM -- "2 message normalized" --> ORCH

  ORCH -- "5 redact + route" --> PII
  PII --> POL --> SENT
  SENT -- "6 redact/route done" --> ORCH

  ORCH -- "3 invoke: parse data" --> IP
  IP -. "4 propose: Data_Parsed" .-> ORCH
  ORCH -- "7 invoke: classify" --> ACL
  ACL -. "8 propose: acuity + confidence" .-> ORCH
  ORCH -- "9 invoke: validate" --> SV
  SV -. "10 propose: verdict" .-> ORCH
  ORCH -- "11 invoke: request approval" --> HE
  HE -. "12 propose: escalation needed?" .-> ORCH
  ORCH -- "13 invoke: start timer" --> WM
  WM -. "14 trigger: reassessment / deterioration" .-> ORCH

  ORCH == "sole writer" ==> STATE
  ORCH -- "emit: event log" --> AUD
  ORCH -- "fetch / patch patient data" --> CRM
  SV -. "read policies" .-> VDB
  ORCH -. "read" .-> KB
  ORCH -. "read / write" .-> SESS
  ORCH -. "15-20 notify" .-> NOTIFY

  AUD --> LOGS
  LOGS --> EVAL
  LOGS --> DASH
  FB --> EVAL
```

## State-machine diagram

The central control-plane state machine is built from the specification's Transitions table. All the details, such as guards, actions, world effects, and every agent-failure edge, remain in that table. This diagram shows the full control-plane flow from start to finish.

```mermaid
stateDiagram-v2
    direction TB
    [*] --> intake_received : 1a CASE_SUBMITTED

    intake_received --> parsing : 2 MESSAGE_NORMALIZED

    parsing --> data_parsed : 4 DATA_PARSED (fields ok)
    parsing --> missing_fields_requested : 16 MISSING_FIELDS (case 2)
    parsing --> scan_failed : 17 PARSE_FAILED (case 3)
    parsing --> erroneous_file_rejected : 18 WRONG_DOC (case 4)

    missing_fields_requested --> parsing : 1b.x FIELDS_SUBMITTED
    scan_failed --> intake_received : rescan or manual entry
    erroneous_file_rejected --> intake_received : resubmit correct doc

    data_parsed --> redacting_routing : 5 redact + route
    redacting_routing --> classifying : 6 redact done
    classifying --> acuity_proposed : 8 ACUITY_PROPOSED

    acuity_proposed --> safety_validating : 9a/9b agree or minor gap
    acuity_proposed --> awaiting_human_approval : 9c gap >= 2

    safety_validating --> verdict_proposed : 10 safety_pass
    safety_validating --> awaiting_human_approval : 10-fail (no safety_pass)

    verdict_proposed --> awaiting_human_approval : 11 escalation_needed
    verdict_proposed --> monitoring : 11 pass

    awaiting_human_approval --> safety_validating : 1b.z acuity resolved
    awaiting_human_approval --> monitoring : safety branch (open question)

    monitoring --> reassessment_required : 14 timeout / deterioration
    reassessment_required --> parsing : 15 nurse re-files
    monitoring --> case_closed : REL release (nurse-signed)

    case_closed --> [*]

    redacting_routing --> agent_failed : AF PII filter down (halt)
    agent_failed --> [*]
```
