# Triage Guard: Architecture and State-Machine Specification

**Project:** Triage Guard, an AI-Governed ER Triage System
**Authors:** Idan Beck, Barak Amir

**What this is.** This document is the complete specification for **Triage Guard**, an AI-governed emergency-room (ER) triage system. It is designed to be self-contained, with each section starting with a brief introduction explaining its content and purpose. Please read the **Conventions & scope** section first, since all later sections rely on its definitions.

**Companion diagram.** This document matches the Triage Guard Whimsical board. The **arrow numbers** in the Transitions table and scenario paths are the same as those shown on the diagram, so you can easily find any row in both places. Agent names, world statuses, and the "notify user" list also correspond directly to the board. Mermaid versions of the architecture and the control-plane state machine are included in the companion file **[`triage-guard-diagrams.md`](triage-guard-diagrams.md)**.

**Open items.** One design decision remains unresolved: the safety-fail branch of the human gate, which is listed at the end under **Open design question**. Document intake uses **mock questionnaire data (no live OCR)**, and the method along with its demo cases is explained in **Document intake & demo data**. In other sections, items that still need to be finalized are marked as _(to confirm)_ or _(to define)_.

**Contents**

- [How the pieces fit (diagram groups)](#how-the-pieces-fit-diagram-groups)
- [Conventions & scope](#conventions--scope)
- [Document intake & demo data](#document-intake--demo-data)
- [Actors / Agents](#actors--agents)
- [States](#states)
- [Events](#events)
- [Guards / Conditions](#guards--conditions)
- [Actions](#actions)
- [Transitions (complete table)](#transitions-complete-table)
- [Context / State variables (Data plane)](#context--state-variables-data-plane)
- [Queue ordering rule](#queue-ordering-rule)
- [Safety invariants](#safety-invariants)
- [Temporal logic rules](#temporal-logic-rules)
- [Symbolic governance layer — OPA, Z3, Prolog, Datalog](#symbolic-governance-layer--opa-z3-prolog-datalog)
- [Open decisions](#open-decisions)
- [Open design question — safety-fail branch of the human gate](#open-design-question--safety-fail-branch-of-the-human-gate)

---

## How the pieces fit together

_This is a one-screen map of the system, giving the rest of the document a clear starting point. The system uses a single control loop, called the Orchestrator, which is supported by specialist agents and services._

- **Input Channels** are where a case starts, either through a **PDF document** (the triage questionnaire) or the **website** form.
- **Input processing** uses an **API Gateway** to pass along the raw input.
- **Intake Parser (ingestion)** is a **Channel Router** that reads the questionnaire and turns it into a single, unified message. In this version, the questionnaire uses **mock data** (no live OCR). For a real deployment, we could add an OCR or extraction adapter using the same interface.
- **Understanding, safety & routing** includes a **PII / sensitive-data filter** (removes ID, name, surname), a **policy gate**, and a **sentiment/urgency** scorer (distress or pain).
- **Agent Core** includes the **Orchestrator Agent** (the only one that writes to State) and six specialist agents: Intake Parser, Acuity Classifier, Safety Validation, Human Escalation, Waiting Room Monitor, and Audit.
- **Knowledge & Memory** covers the Knowledge Base, policy vector store, CRM (patient profile and history), and session memory.
- **Monitoring & Evaluation** includes user and agent feedback, an evaluation pipeline (test cases and regression checks), logs and traces, and a metrics dashboard.

---

## Conventions & scope

_This section sets the vocabulary and mental model for the entire document. It explains naming conventions, the three planes a case exists in, who can write State, and the overall approach to handling failures. Please read this first, as later sections rely on these definitions._

- **Events** are `UPPERCASE_SNAKE`; **states** are `lowercase_snake`; **guards** are boolean predicates; **actions** are verbs.
- The machine has **three state planes**:
  - **Control**: This shows where the Orchestrator is in processing a case (its position in the pipeline).
  - **Data**: This is the parsed or derived payload, including fields, acuity, confidence, and verdict.
  - **World**: This refers to the patient's clinical status on the board (the kanban column).
  - _Control and World evolve semi-independently: a case can hold control-state `monitoring` while its World status cycles `waiting → treatment_started → released`. They are kept as separate planes so the machine is not the cross-product of the two._
- **Writer rule:** agents propose, they never write. The **Orchestrator is the sole writer to State.** Every agent arrow terminates at the Orchestrator.
- **Design stance: fail-operational.** Unless a critical component fails, the system keeps working. Non-critical agents switch to a human or manual fallback. Critical agents either switch to human fallback or stop completely (see the **Per-agent failure model**).
- **Ingestion stance: no live OCR.** The triage questionnaire is ingested as **mock / structured data**; there is no OCR pipeline in this build, but a real deployment could add one using the same interface. Acuity is always confirmed by the nurse and never inferred from the raw document. See **Document intake & demo data**.

- The **arrow numbers** in the Transitions table match the arrows on the diagram.
- **Notation.** Acuity follows the ESI style, where a lower number means more acute (1 is most urgent, 5 is least). Logic operators: `∧` means and, `∨` means or, `¬` means not, and `→` means implies. Temporal operators (used in **Temporal logic rules** and **Safety invariants**) are: `G` for always or globally, `F` for eventually, `X` for next step, `U` for until, and `F≤t` for eventually within a set time t.

---

## Document intake & demo data

This section explains how a case enters the system and describes the four mock inputs used in the demo. There is no live OCR. Instead, the questionnaire's parsed content is provided as mock data, and each mock case tests one of the four intake outcomes. These four cases also serve as the intake regression and demo set: success, degraded, failed, and rejected.

The Intake Parser reads the mocked questionnaire and produces one of four possible outcomes. Each outcome matches an existing transition out of `parsing`, so no new components are needed. The mock data simply determines which branch is used.

| Demo case                           | Mock input                                                                      | Intake outcome (event)                                                 | Arrow | Lands in                   | System response                                                                                                                 |
| ----------------------------------- | ------------------------------------------------------------------------------- | ---------------------------------------------------------------------- | ----- | -------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| 1 - Clean parse                     | complete questionnaire, all required fields present                             | `DATA_PARSED` (`required_fields_complete` and `doc_is_valid_template`) | 4     | `data_parsed` → continues  | proceed to redaction, then classification                                                                                       |
| 2 - Partial or missing fields       | some required fields are absent or not extracted cleanly                        | `MISSING_FIELDS_DETECTED`                                              | 16    | `missing_fields_requested` | generate a form listing **exactly the missing fields**; nurse completes it, then `FIELDS_SUBMITTED` (arrow 1b.x), then re-parse |
| 3 - Unreadable or total failure     | nothing usable could be extracted                                               | `PARSE_FAILED_RESCAN`                                                  | 17    | `scan_failed`              | offer to **resubmit the document** or switch to **full manual entry** (nurse fills the whole form)                              |
| 4 - Wrong, irrelevant, or injection | unrecognized document, wrong form, or prompt-injection text inside the document | `WRONG_DOC_DETECTED` (not `doc_is_valid_template`)                     | 18    | `erroneous_file_rejected`  | reject; notify "wrong document"; log a security event; **never proceeds to classify**                                           |

> **Acuity is never taken from the document alone.** The nurse must always supply or confirm `nurse_proposed_acuity`. If this field is missing, the case is handled as demo case 2 (missing fields) and not by making an automatic guess.
>
> **Case 4 is also the injection defence.** A document with prompt-injection text is treated as unrecognized. It is rejected at intake and never reaches the classifier, which meets the **Injection rejected** invariant.

---

## Actors / Agents

This section lists all participants in a case: the Orchestrator, each proposing agent, the humans, and the ops technician. It explains what each one can read, what it can propose, and most importantly, whether it can write State. The key column is "Writes State?" Only the Orchestrator can do this.

| Actor                                             | Type                 | Reads                   | Proposes / Emits                                                    | Writes State?          | Tech                             |
| ------------------------------------------------- | -------------------- | ----------------------- | ------------------------------------------------------------------- | ---------------------- | -------------------------------- |
| **Orchestrator Agent**                            | Controller / planner | all planes              | — (it decides)                                                      | **Yes — sole writer**  | State machine / workflow graph   |
| Intake Parser Agent                               | LLM                  | raw intake doc (mock)   | `Data_Parsed` proposal                                              | No                     | LLM                              |
| Acuity Classifier Agent                           | LLM                  | parsed + CRM data       | acuity + confidence                                                 | No                     | LLM                              |
| Safety Validation Agent                           | Deterministic        | proposed classification | verdict (pass/fail)                                                 | No                     | Prolog / Datalog / Z3 / OPA      |
| Human Escalation Agent                            | Bridge to human      | case + verdict          | escalation + human response                                         | No                     | UI/queue _(to confirm)_          |
| Waiting Room Monitor Agent                        | Timer / watcher      | status + timers         | timeout / deterioration triggers                                    | No                     | _(to confirm)_                   |
| Audit Agent                                       | Sink                 | event log               | persists trace                                                      | No (write-only to log) | append-only store _(to confirm)_ |
| Channel Router                                    | Ingress              | raw input               | routes PDF vs website                                               | No                     | —                                |
| Input Normalizer + PII filter + Sentiment/Urgency | Pre-processor        | routed input            | redacted message + distress/pain score                              | No                     | BERT _(to confirm)_              |
| Triage Nurse / Charge Nurse / Clinician           | Human                | board + detail panel    | status changes, approvals, acuity, missing fields, release sign-off | via Orchestrator only  | —                                |
| Technician                                        | Human (ops)          | agent-failure alerts    | fixes / acknowledges                                                | No                     | —                                |

---

## States

This section lists all the possible states a case can have. The states are grouped into three planes and a separate recovery or error group. The control plane tracks the case’s place in the workflow. The recovery or error group explains what happens if something goes wrong. The world plane shows what nurses see on the board.

### Control plane (workflow position)

_The happy-path pipeline: where the Orchestrator is in processing a case, from the moment it arrives to the moment its card leaves the board._

| State                     | Type                  | Entry action                                 | Exit action     | Invariant                           | Arrow     |
| ------------------------- | --------------------- | -------------------------------------------- | --------------- | ----------------------------------- | --------- |
| `intake_received`         | initial               | log intake; **assign `order_key`**           | —               | channel + raw payload present       | 1a        |
| `parsing`                 | normal                | invoke Intake Parser                         | —               | doc routed                          | 3         |
| `data_parsed`             | normal                | —                                            | —               | fields extracted OR flagged missing | 4         |
| `redacting_routing`       | normal                | redact PII, score urgency                    | —               | no raw PII leaves this stage        | 5→6       |
| `classifying`             | normal                | invoke Acuity Classifier                     | —               | redacted payload ready              | 7         |
| `acuity_proposed`         | normal                | compute `acuity_gap`                         | —               | acuity + confidence present         | 8         |
| `safety_validating`       | normal                | invoke Safety Validation                     | —               | settled acuity present              | 9         |
| `verdict_proposed`        | normal                | —                                            | —               | verdict present                     | 10        |
| `awaiting_human_approval` | **wait / human gate** | invoke Human Escalation; start notify-ladder | record response | approval pending                    | 11–12, 20 |
| `monitoring`              | normal                | start reassessment timer                     | stop timer      | patient has active status           | 13        |
| `case_closed`             | terminal              | emit final log; **card leaves the board**    | —               | released, human-signed              | REL       |

`order_key` is assigned when the case enters the system (in `intake_received`). Each case gets a queue position right away, even if its first step is the gate.

### Recovery / error states (control plane)

This section explains error handling, following the lecture checklist. Each row shows where the system can end up if something fails, what kind of recovery happens, and what happens next. The last row covers the general agent-failure catch, which is explained in the Per-agent failure model.

| State                      | Recovery kind                               | Trigger                                              | Resolves to                                                                                  |
| -------------------------- | ------------------------------------------- | ---------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `missing_fields_requested` | Request → Retry                             | required field absent (demo case 2)                  | back to `parsing` on `FIELDS_SUBMITTED` (16 → 1b.x)                                          |
| `scan_failed`              | Retry / Manual                              | document unreadable, nothing extracted (demo case 3) | resubmit → back to `intake_received` (17 → 1a·rescan), **or** full manual entry by the nurse |
| `erroneous_file_rejected`  | Reject / Abort                              | wrong form or injection (demo case 4)                | back to `intake_received` + "wrong doc" (18 → 1a·wrong-doc)                                  |
| `reassessment_required`    | Replan                                      | timer timeout / deterioration                        | **re-enter at `parsing`** — nurse re-files (14 → 15)                                         |
| `agent_failed`             | **per-agent (see Per-agent failure model)** | any agent errors/times out past its retry budget     | degrade-and-continue **or** halt + technician, per agent                                     |

### World plane (clinical status = board column)

This section shows what the nurse actually sees: the kanban column for each patient. The system sets these columns, except where marked with a hand symbol for human input. The release rule and the manual-edit rule below are the two main constraints.

| Status                         | Who sets it             | Meaning                                                | Reachable from                                            |
| ------------------------------ | ----------------------- | ------------------------------------------------------ | --------------------------------------------------------- |
| `waiting`                      | system                  | queued for bed/provider, priority-sorted (1 = highest) | after triage decision                                     |
| `human_review`                 | system                  | flagged uncertainty; clinician must resolve            | acuity gap ≥2 / safety fail / low confidence              |
| `reassessment_required`        | system                  | vitals/condition changed → re-triage                   | any status, on deterioration/timeout                      |
| `treatment_started`            | human ✋                | active care begun                                      | `waiting` (manual)                                        |
| `formal_validation`            | system                  | final sign-off before close                            | `treatment_started`                                       |
| `patient_released`             | **human ✋ (required)** | discharged — leaves the board                          | **any active state** (discharge / AMA / transfer / admit) |
| `in_transition` _(to confirm)_ | system                  | transient during a requested move                      | during `MOVE_REQUESTED` handling                          |

> **Release rule:** `patient_released` is reachable from **any** live state, not only the treatment path. Every release **requires an authorized nurse sign-off** (`actor_authorized`) plus a valid `release_reason`. "Left the ward vs. left the hospital" is out of scope — both close the card.
>
> **AMA consequence:** the patient may physically leave before the card closes; the card stays open until a nurse signs. A monitor must **not** treat an unsigned-but-departed card as "still waiting."
>
> **Manual-edit rule:** nurses manually set status only for `treatment_started` (from `waiting`) and for release (any state, with reason). All other statuses are system-set.

---

## Events

Inputs that move the state machine come from external sources such as humans or channels, as well as from internal agent proposals, timers, or errors. The details in each payload matter. For example, `APPROVAL_RESPONSE_RECEIVED` now includes a resolution choice, and `RELEASE_REQUESTED` includes a reason.

| Event                        | Source                     | Payload                                                    | Type                      |
| ---------------------------- | -------------------------- | ---------------------------------------------------------- | ------------------------- |
| `CASE_SUBMITTED`             | Channel (PDF/website)      | raw doc + channel                                          | external                  |
| `FIELDS_SUBMITTED`           | Nurse                      | missing field values                                       | external (human)          |
| `MOVE_REQUESTED`             | Nurse                      | target status                                              | external (human)          |
| `APPROVAL_RESPONSE_RECEIVED` | Charge Nurse               | `{resolution: accept_system \| keep_nurse, resolver_role}` | external (human)          |
| `MESSAGE_NORMALIZED`         | Input Normalizer           | unified message                                            | internal                  |
| `DATA_PARSED`                | Intake Parser              | parsed fields (incl. `nurse_proposed_acuity`)              | internal (proposal)       |
| `MISSING_FIELDS_DETECTED`    | Intake Parser              | list of gaps                                               | internal                  |
| `PARSE_FAILED_RESCAN`        | Intake Parser              | error reason                                               | internal (error)          |
| `WRONG_DOC_DETECTED`         | Intake Parser / PII filter | reason (wrong form / injection)                            | internal (error/security) |
| `REDACT_ROUTE_DONE`          | Understanding/routing      | redacted payload + urgency                                 | internal                  |
| `ACUITY_PROPOSED`            | Acuity Classifier          | acuity + confidence                                        | internal (proposal)       |
| `VERDICT_PROPOSED`           | Safety Validation          | pass/fail + reasons                                        | internal (proposal)       |
| `ESCALATION_PROPOSED`        | Human Escalation           | needed? (bool)                                             | internal (proposal)       |
| `REASSESSMENT_TIMEOUT`       | Waiting Room Monitor       | patient id                                                 | timer                     |
| `DETERIORATION_DETECTED`     | Waiting Room Monitor       | patient id + signal                                        | internal                  |
| `TRANSITION_ACCEPTED`        | Orchestrator → user        | new status                                                 | notification              |
| `RELEASE_REQUESTED`          | Nurse                      | `{reason: discharge \| ama \| transfer \| admit, actor}`   | external (human)          |
| `AGENT_FAILED`               | Orchestrator               | `{agent, error, attempts}`                                 | internal (error)          |
| `GATE_TIMER_1`               | Waiting Room Monitor       | case id                                                    | timer                     |
| `GATE_TIMER_2`               | Waiting Room Monitor       | case id                                                    | timer                     |
| `EVENT_LOGGED`               | Orchestrator               | trace record                                               | emitted                   |

---

## Guards / Conditions

These are the boolean checks that decide which transition happens. This is where policy is set, such as acuity-gap bands, authorization, and retry budget. The Orchestrator evaluates each one unless otherwise noted.

| Guard                      | Expression                                                                                                                                                                                                                                | Evaluated by          |
| -------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------- |
| `required_fields_complete` | all mandatory fields present (incl. `nurse_proposed_acuity`)                                                                                                                                                                              | Orchestrator          |
| `doc_is_valid_template`    | matches expected form ∧ ¬injection                                                                                                                                                                                                        | Parser / PII filter   |
| `confidence_ok`            | `confidence >= threshold` _(threshold to confirm)_. This is optional: you can wire in (low confidence goes to gate) or drop. This does not apply during a classifier outage, since confidence only exists when the classifier is running. | Orchestrator          |
| `safety_pass`              | verdict == pass (Prolog/Z3/OPA)                                                                                                                                                                                                           | Safety Validation     |
| `escalation_needed`        | verdict fail ∨ low confidence ∨ policy hit                                                                                                                                                                                                | Human Escalation      |
| `release_authorized`       | valid `release_reason` ∧ actor authorized (`actor_authorized`). This is a _reason plus authorization_ check, **not** a source-state check.                                                                                                | Orchestrator          |
| `actor_authorized`         | role ∧ jurisdiction ∧ data-class OK                                                                                                                                                                                                       | Orchestrator (policy) |
| `retry_budget_left(agent)` | `retry_count[agent] < N[agent]`                                                                                                                                                                                                           | Orchestrator          |
| `acuity_agree`             | `acuity_gap == 0`                                                                                                                                                                                                                         | Orchestrator          |
| `acuity_gap_minor`         | `acuity_gap == 1`                                                                                                                                                                                                                         | Orchestrator          |
| `acuity_gap_major`         | `acuity_gap >= 2`                                                                                                                                                                                                                         | Orchestrator          |
| `actor_is_charge`          | `actor_authorized` ∧ role ∈ {charge_nurse, shift_lead}                                                                                                                                                                                    | Orchestrator          |

---

## Actions

These are the side effects that a transition can perform. Only the Orchestrator is allowed to perform them, following the writer rule. The actions added in this project—acuity resolution, release sign-off, and the two parts of agent-failure handling (manual fallback and technician alert)—are listed in the last five rows.

| Action                                      | Performed by                    | Side-effecting?    | Idempotent?                 | Output               | Notes / Arrow                                                                                                                          |
| ------------------------------------------- | ------------------------------- | ------------------ | --------------------------- | -------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `write_state(plane, value)`                 | **Orchestrator only**           | yes                | _(to confirm)_              | —                    | the single write path                                                                                                                  |
| `invoke_intake_parser`                      | Orchestrator                    | no                 | yes                         | ParseResult          | 3                                                                                                                                      |
| `redact_pii` + `score_urgency`              | Orchestrator → Input Normalizer | no                 | yes                         | RedactedMsg + scores | 5                                                                                                                                      |
| `invoke_acuity_classifier`                  | Orchestrator                    | no                 | yes                         | {acuity, confidence} | 7                                                                                                                                      |
| `invoke_safety_validation`                  | Orchestrator                    | no                 | yes                         | {verdict, reasons}   | 9                                                                                                                                      |
| `invoke_human_escalation`                   | Orchestrator                    | yes (queues human) | _(to confirm)_              | ApprovalRequest      | 9c / 11                                                                                                                                |
| `start_reassessment_timer`                  | Orchestrator                    | yes                | must be (dedupe by patient) | TimerHandle          | 13                                                                                                                                     |
| `notify_user(reason)`                       | Orchestrator                    | yes                | _(to confirm)_              | Notification         | reassessment / missing / rescan / wrong-doc / accepted / approval                                                                      |
| `emit_event_log`                            | Orchestrator → Audit            | yes (append)       | yes                         | TraceRecord          | every transition                                                                                                                       |
| `fetch_patient_data` / `patch_patient_data` | Orchestrator → CRM              | patch = yes        | patch _(to confirm)_        | PatientRecord        | Knowledge & Memory                                                                                                                     |
| `auto_resolve_acuity_upward`                | Orchestrator                    | yes                | yes                         | acuity               | `acuity ← more acute of {nurse, system}` (lower ESI); `acuity_source ← auto_resolved`; recompute `order_key`; log both inputs + choice |
| `apply_human_acuity(choice)`                | Orchestrator                    | yes                | yes                         | acuity               | `acuity ← choice`; `acuity_source ← human_confirmed`; recompute `order_key`; log resolver + role                                       |
| `sign_release(reason)`                      | Orchestrator ← nurse            | yes                | yes                         | ReleaseRecord        | record reason + actor; → `case_closed`                                                                                                 |
| `alert_technician(agent)`                   | Orchestrator → Technician       | yes                | yes                         | Alert                | fires ops alert; does not block degraded flow                                                                                          |
| `fallback_manual(agent)`                    | Orchestrator                    | yes                | yes                         | —                    | switch a fail-open agent to its human/manual substitute                                                                                |

### Per-agent failure model (`AGENT_FAILED`)

This section explains how each agent behaves when it errors or times out, making the fail-operational approach clear. The two key columns are "Critical?" (halt or keep working) and "Fail direction" (open means degrade and continue, closed means stop the line). Retries are attempted first, using the retry-budget guard, and the exhaustion action happens only after retries are used up. Note that this refers to the agent crashing, which is different from demo case 3 where the document had nothing to parse.

When an error or timeout occurs, the Orchestrator retries up to the agent's own retry budget `N` (`retry_budget_left`). After that, it follows the agent's declared direction. The two consequences, fallback and technician alert, run in parallel and do not block each other.

| Agent                   | Critical? | Fail direction     | Retry `N`      | On exhaustion                                                                                                                                                                               |
| ----------------------- | --------- | ------------------ | -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Intake Parser           | No        | **open (degrade)** | _(to confirm)_ | `fallback_manual`: present **all expected fields as a blank form** for the nurse to fill **AND** `alert_technician`. Flow continues via manual entry.                                       |
| Acuity Classifier       | No        | **open (degrade)** | _(to confirm)_ | Drop the system acuity; **fall back to `nurse_proposed_acuity`**; **discrepancy gate is disabled for the outage. Flag these cases as "cross-check off, review later"**; `alert_technician`. |
| Safety Validation       | Yes       | degrade-to-human   | _(to confirm)_ | Do **not** hard-halt: route **every** case to charge nurse (`awaiting_human_approval`) so the no-approval-bypass rule still holds; `alert_technician`.                                      |
| PII filter              | **Yes**   | **closed (halt)**  | _(to confirm)_ | **Stop the line**. Continuing would leak raw PII (the no-raw-PII-downstream rule). `alert_technician`; no degraded path.                                                                    |
| Human Escalation bridge | Yes       | degrade-to-human   | _(to confirm)_ | `alert_technician`; fall back to a manual notification channel for the gate.                                                                                                                |

> **Principle:** For non-critical issues, continue via fallback and notify. For critical-open, degrade to human and notify. For critical-closed, halt and notify.

---

## Transitions (complete table)

This table is the core of the state machine. Each edge is shown as **current state + `EVENT` [guard] → next state / actions**. The **Arrow** column matches the diagram, keeping this table and the Whimsical board aligned. After the table, you’ll find the named scenario paths (regression cases) and the forbidden sequences that the safety layer must block.

| Arrow         | Current (control)          | Event                                             | Guard                                                | Actions                                                                                  | Next (control)               | World effect                              |
| ------------- | -------------------------- | ------------------------------------------------- | ---------------------------------------------------- | ---------------------------------------------------------------------------------------- | ---------------------------- | ----------------------------------------- |
| 1a            | —                          | `CASE_SUBMITTED`                                  | —                                                    | route channel; **assign `order_key`**                                                    | `intake_received`            | —                                         |
| 2             | `intake_received`          | `MESSAGE_NORMALIZED`                              | —                                                    | `emit_event_log`                                                                         | `parsing`                    | —                                         |
| 3             | `parsing`                  | _(auto)_                                          | —                                                    | `invoke_intake_parser`                                                                   | `parsing`                    | —                                         |
| 4             | `parsing`                  | `DATA_PARSED`                                     | `required_fields_complete` ∧ `doc_is_valid_template` | `emit_event_log`                                                                         | `data_parsed`                | —                                         |
| 16            | `parsing`                  | `MISSING_FIELDS_DETECTED`                         | ¬`required_fields_complete`                          | `notify_user("request fields")`                                                          | `missing_fields_requested`   | —                                         |
| 17            | `parsing`                  | `PARSE_FAILED_RESCAN`                             | — (demo case 3)                                      | `notify_user("rescan or manual")`                                                        | `scan_failed`                | —                                         |
| 18            | `parsing`                  | `WRONG_DOC_DETECTED`                              | ¬`doc_is_valid_template`                             | `notify_user("wrong doc")`                                                               | `erroneous_file_rejected`    | —                                         |
| AF·parser     | `parsing`                  | `AGENT_FAILED` (Intake Parser)                    | ¬`retry_budget_left`                                 | `fallback_manual` + `alert_technician`                                                   | `parsing` (manual entry)     | —                                         |
| 1b.x          | `missing_fields_requested` | `FIELDS_SUBMITTED`                                | —                                                    | —                                                                                        | `parsing`                    | —                                         |
| 1a·rescan     | `scan_failed`              | _(auto / manual)_                                 | —                                                    | `notify_user`                                                                            | `intake_received`            | —                                         |
| 1a·wrong-doc  | `erroneous_file_rejected`  | _(auto)_                                          | —                                                    | `notify_user`                                                                            | `intake_received`            | —                                         |
| 5             | `data_parsed`              | _(auto)_                                          | —                                                    | `redact_pii` + `score_urgency`                                                           | `redacting_routing`          | —                                         |
| AF·PII        | `redacting_routing`        | `AGENT_FAILED` (PII filter)                       | ¬`retry_budget_left`                                 | `alert_technician`                                                                       | `agent_failed` (halt)        | —                                         |
| 6             | `redacting_routing`        | `REDACT_ROUTE_DONE`                               | —                                                    | —                                                                                        | `classifying`                | —                                         |
| 7             | `classifying`              | _(auto)_                                          | —                                                    | `invoke_acuity_classifier`                                                               | `classifying`                | —                                         |
| AF·classifier | `classifying`              | `AGENT_FAILED` (Acuity Classifier)                | ¬`retry_budget_left`                                 | `fallback_manual` (use `nurse_proposed_acuity`; disable gate; flag) + `alert_technician` | `safety_validating`          | —                                         |
| 8             | `classifying`              | `ACUITY_PROPOSED`                                 | —                                                    | `emit_event_log`                                                                         | `acuity_proposed`            | —                                         |
| 9a            | `acuity_proposed`          | _(auto)_                                          | `acuity_agree`                                       | set `acuity_source = human_confirmed`                                                    | `safety_validating`          | —                                         |
| 9b            | `acuity_proposed`          | _(auto)_                                          | `acuity_gap_minor`                                   | `auto_resolve_acuity_upward`                                                             | `safety_validating`          | —                                         |
| 9c            | `acuity_proposed`          | _(auto)_                                          | `acuity_gap_major`                                   | `invoke_human_escalation` ("show discrepancy + system rationale; request resolution")    | `awaiting_human_approval`    | → `human_review`                          |
| 10            | `safety_validating`        | `VERDICT_PROPOSED`                                | `safety_pass`                                        | `emit_event_log`                                                                         | `verdict_proposed`           | —                                         |
| 10·fail       | `safety_validating`        | `VERDICT_PROPOSED`                                | ¬`safety_pass`                                       | `invoke_human_escalation`                                                                | `awaiting_human_approval`    | → `human_review`                          |
| AF·safety     | `safety_validating`        | `AGENT_FAILED` (Safety Validation)                | ¬`retry_budget_left`                                 | `invoke_human_escalation` (route all to charge) + `alert_technician`                     | `awaiting_human_approval`    | → `human_review`                          |
| 11            | `verdict_proposed`         | _(auto)_                                          | `escalation_needed`                                  | `invoke_human_escalation`                                                                | `awaiting_human_approval`    | → `human_review`                          |
| 11·pass       | `verdict_proposed`         | _(auto)_                                          | ¬`escalation_needed`                                 | `start_reassessment_timer`                                                               | `monitoring`                 | → `waiting`                               |
| 12            | `awaiting_human_approval`  | `ESCALATION_PROPOSED`                             | —                                                    | `emit_event_log`                                                                         | `awaiting_human_approval`    | —                                         |
| 20            | `awaiting_human_approval`  | _(auto)_                                          | escalation = true                                    | `notify_user("request approval")`                                                        | `awaiting_human_approval`    | → `human_review`                          |
| 20a           | `awaiting_human_approval`  | `GATE_TIMER_1`                                    | —                                                    | notify assigned charge nurse (UI)                                                        | `awaiting_human_approval`    | —                                         |
| 20b           | `awaiting_human_approval`  | `GATE_TIMER_2`                                    | —                                                    | re-alert / widen to any charge-role nurse                                                | `awaiting_human_approval`    | —                                         |
| 1b.z·acuity   | `awaiting_human_approval`  | `APPROVAL_RESPONSE_RECEIVED`                      | `actor_is_charge` (acuity branch)                    | `apply_human_acuity` → `emit_event_log`                                                  | `safety_validating`          | —                                         |
| 1b.z·safety   | `awaiting_human_approval`  | `APPROVAL_RESPONSE_RECEIVED`                      | (safety-fail branch)                                 | **see Open design question**                                                             | **see Open design question** | **see Open design question**              |
| 13            | `monitoring`               | _(auto)_                                          | —                                                    | `start_reassessment_timer`                                                               | `monitoring`                 | → `waiting`                               |
| 14            | `monitoring`               | `REASSESSMENT_TIMEOUT` ∨ `DETERIORATION_DETECTED` | —                                                    | `notify_user("reassessment")`                                                            | `reassessment_required`      | → `reassessment_required`                 |
| 15            | `reassessment_required`    | _(nurse re-files)_                                | —                                                    | —                                                                                        | **`parsing`**                | —                                         |
| 1b.y          | `monitoring`               | `MOVE_REQUESTED`                                  | `release_authorized` (target = treatment_started)    | `emit_event_log`                                                                         | `monitoring`                 | → `treatment_started`                     |
| 19            | `monitoring`               | `TRANSITION_ACCEPTED`                             | `release_authorized`                                 | `notify_user("accepted")`                                                                | `monitoring`                 | → `treatment_started`                     |
| FV            | `monitoring`               | _(auto after treatment)_                          | —                                                    | `emit_event_log`                                                                         | `monitoring`                 | `treatment_started` → `formal_validation` |
| REL           | _(any active state)_       | `RELEASE_REQUESTED`                               | `release_authorized` (reason ∧ `actor_authorized`)   | `sign_release`                                                                           | `case_closed`                | → `patient_released`                      |

> **About the arrow labels:** The numbered arrows like `1a`, `1b.x`, `1b.y`, `1b.z`, and `2` to `20` are shown on the diagram and used in the paths below. The lettered or suffixed rows (`9a` to `9c`, `10·fail`, `11·pass`, the `AF·…` agent-failure edges, `20a`/`20b`, `FV`, `REL`) are internal details of those same edges, included here for completeness.

> **Documented scenario paths (regression cases):**
>
> - **Normal entry (demo case 1):** `1a → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9a → 10 → 11·pass → 13`
> - **Acuity gap ≥2 → charge nurse:** `… → 8 → 9c → 1b.z·acuity → 10 → 11·pass → 13`
> - **Acuity gap 1 auto-resolve:** `… → 8 → 9b → 10 → 11·pass → 13`
> - **Safety fail → gate:** `… → 10·fail → [see Open design question]`
> - **Reassessment:** `13 → 14 → 15 → 3 → …` (full front-door rerun)
> - **Move to treatment:** `1b.y → 19` (→ `treatment_started`) → `FV` → `REL`
> - **AMA / release from waiting:** `… → 13 → REL` (with `reason = ama`, nurse sign-off)
> - **Missing fields (demo case 2):** `1a → … → 16 → 1b.x`
> - **Unreadable document (demo case 3):** `1a → … → 17 → 1a·rescan` (or full manual entry)
> - **Wrong / injection (demo case 4):** `1a → … → 18 → 1a·wrong-doc`
> - **Parser down (degrade):** `AF·parser` → manual entry, continue
> - **Classifier down (degrade):** `AF·classifier` → nurse acuity, gate off, continue
> - **PII filter down (halt):** `AF·PII` → `agent_failed`, line stops
> - **Safety validator down (degrade-to-human):** `AF·safety` → every case to charge nurse

> **Forbidden sequences (must be provably blocked):**
>
> - **Patient starves:** a queued patient sits in `waiting` past the escalation ceiling with no reassessment/escalation. Blocked by the **wait-liveness** rule.
> - **Served out of order:** a queued patient pulled ahead of an emergent one. Blocked by the **bucket-ordering** rule.
> - **Clock rewrites acuity:** `REASSESSMENT_TIMEOUT` mutates acuity directly instead of forcing a re-look. Blocked by the **acuity-write-authority** rule (the timer only _triggers_ 14→15; the nurse's re-filed form changes acuity).
> - **Approval bypass:** reaching `treatment_started` via the system without safety/approval. Blocked by the **no-approval-bypass** rule.
> - **Acuity livelock:** deny → `waiting` → timer → same fight forever. Blocked by the **anti-livelock** rule.
> - **Unauthorized release:** a release with no reason or no authorized actor. Blocked by the **release-authorization** rule.
> - **Injection reaches the model:** a prompt-injection document (demo case 4) proceeds past intake to the classifier. Blocked by the **injection-rejected** rule.

---

## Context / State variables (Data plane)

This section lists the data the machine keeps for each case. Here you’ll find the acuity model, including `nurse_proposed_acuity` and system `acuity`, their `acuity_gap`, and the `acuity_source` lock. It also includes the queue key and the retry counters for each agent.

| Variable                | Plane   | Type                                         | Set by                       | Notes                                                                                                       |
| ----------------------- | ------- | -------------------------------------------- | ---------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `case_id`               | Control | id                                           | intake                       | —                                                                                                           |
| `channel`               | Control | enum(pdf, website)                           | Channel Router               | `pdf` = uploaded questionnaire document (mock-parsed in this build)                                         |
| `parsed_fields`         | Data    | struct                                       | Intake Parser                | schema _(to define)_                                                                                        |
| `nurse_proposed_acuity` | Data    | enum/level                                   | nurse (via questionnaire)    | **mandatory**; absent → demo case 2 / arrow 16; **always nurse-supplied, never inferred from the document** |
| `redacted_payload`      | Data    | struct                                       | PII filter                   | no raw PII downstream                                                                                       |
| `urgency_scores`        | Data    | {sentiment, distress, pain}                  | Input Normalizer             | —                                                                                                           |
| `acuity`                | Data    | enum/level                                   | Acuity Classifier            | scale _(to confirm — e.g. ESI 1–5)_                                                                         |
| `acuity_gap`            | Data    | int                                          | derived `\|nurse − system\|` | drives the band (see Queue ordering rule)                                                                   |
| `acuity_source`         | Data    | enum(system, auto_resolved, human_confirmed) | Orchestrator                 | **the lock** — classifier alone cannot overwrite `human_confirmed`                                          |
| `confidence`            | Data    | float                                        | Acuity Classifier            | threshold for `confidence_ok`                                                                               |
| `safety_verdict`        | Data    | {pass/fail, reasons}                         | Safety Validation            | —                                                                                                           |
| `clinical_status`       | World   | enum (see World plane)                       | Orchestrator                 | board column                                                                                                |
| `acuity_bucket`         | Data    | enum(emergent[1–2], queued[3–5])             | derived from `acuity`        | primary sort key                                                                                            |
| `arrival_time`          | Data    | timestamp                                    | intake                       | tiebreaker within bucket                                                                                    |
| `order_key`             | World   | (bucket, arrival_time)                       | Orchestrator                 | **assigned at system entry**; persists across state changes                                                 |
| `release_reason`        | Data    | enum(discharge, ama, transfer, admit)        | nurse                        | recorded at close                                                                                           |
| `wait_timer`            | World   | timer                                        | Orchestrator                 | per-acuity interval; drives `REASSESSMENT_TIMEOUT`                                                          |
| `gate_timer`            | World   | timer                                        | Orchestrator                 | drives `GATE_TIMER_1`/`GATE_TIMER_2` notify-ladder (distinct from `wait_timer`)                             |
| `retry_count[agent]`    | Control | int per agent                                | Orchestrator                 | per-agent budget for agent-failure handling                                                                 |

---

## Queue ordering rule

This section explains how the waiting queue is sorted, and just as importantly, what does not affect your place in line. The main idea is that only a real acuity change will reorder you. State labels and the clock do not change your position. Fairness comes from the sorting, while liveness is handled by the escalation path, not by the sort.

`order_key` sorts the waiting queue by two keys, in order:

1. **Bucket** — `emergent` (acuity 1–2) always above `queued` (acuity 3–5).
2. **Arrival time** — within a bucket, earlier arrival first.

Consequences:

- A 1 and a 2 are peers (bucket sorts them, arrival breaks the tie); same for 3/4/5.
- `order_key` is assigned at system entry and **persists across state changes** — going into `reassessment_required` or `human_review` does not move you in line.
- Only a real **acuity** change (via reassessment/deterioration/override) moves you between buckets. State labels and the clock never move you.
- **Timer firing forces a reassessment (14→15); it does not re-sort the queue.**
- Under overload, ordering guarantees _fairness_, not _service_. Liveness is delivered by the escalation path (the wait-liveness rule), not the sort.

---

## Safety invariants

These are the properties enforced by the symbolic layer (Prolog, Datalog, Z3, OPA) and the runtime monitor. This is the "governance" part of the neurosymbolic split. Each property is written as a checkable proposition with a target formalism. The last three (anti-livelock, acuity write-authority, release authorization) were added during this project’s design work and are the most valuable to formalize for the capstone demo.

| Property               | Family           | Statement                                                                                                                                                                                                                                                        | Formalism     |
| ---------------------- | ---------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------- |
| No bypass of approval  | Safety           | never reach `treatment_started` via system without passing safety/approval                                                                                                                                                                                       | _(to define)_ |
| Sole writer            | Safety           | no state write originates outside the Orchestrator                                                                                                                                                                                                               | _(to define)_ |
| No raw PII downstream  | Safety           | after `redacting_routing`, payload contains no ID/name/surname                                                                                                                                                                                                   | OPA/Rego      |
| Reassessment bound     | Bounded          | after `waiting`, reassessment within ≤ T                                                                                                                                                                                                                         | LTL `F≤t`     |
| Escalation liveness    | Liveness         | every `human_review` eventually resolved                                                                                                                                                                                                                         | LTL `F`       |
| Wait liveness          | Liveness/Bounded | `G(waiting → F≤T(reassessed ∨ escalated))`                                                                                                                                                                                                                       | LTL `F≤t`     |
| Injection rejected     | Safety           | `WRONG_DOC_DETECTED` ⇒ never proceeds to classify                                                                                                                                                                                                                | _(to define)_ |
| Bucket ordering        | Safety           | `G(¬(queued.order_key < emergent.order_key))`                                                                                                                                                                                                                    | Z3 / Datalog  |
| Anti-livelock (acuity) | Safety           | after the gate resolves, `acuity_source ∈ {auto_resolved, human_confirmed}`; a `human_confirmed` acuity cannot re-enter the gate on `ACUITY_PROPOSED` alone — only a reassessment (new human form) can. `G(human_confirmed(acuity) → ¬ classifier_reopens_gate)` | LTL / Datalog |
| Acuity write-authority | Safety           | `acuity` is written only by (a) initial-classification resolution, (b) reassessment carrying new clinical evidence, or (c) human override. The timer alone never changes `acuity`; the classifier alone never overrides `human_confirmed`.                       | Datalog       |
| Release authorization  | Safety           | every `case_closed` via release carries a valid `release_reason` and an authorized nurse actor                                                                                                                                                                   | OPA/Rego      |

---

## Temporal logic rules

The rubric requires the temporal rules to be listed separately, so they are collected here. Most of these are the same properties as in the Safety invariants table, but restated as temporal-logic rules. Some are pure liveness or until rules that only apply over a sequence of events. Each rule describes what must always hold, what must eventually happen, or what must hold until something else occurs. These are checked over a case’s event trace, not just a single step.

| Kind          | Rule (LTL)                                               | In words                                                                                     | Where it bites                               |
| ------------- | -------------------------------------------------------- | -------------------------------------------------------------------------------------------- | -------------------------------------------- |
| Always        | `G(reach_treatment → safety_passed ∧ approved)`          | never start treatment before safety validation **and** approval                              | guards on arrows 1b.y / 19 / REL — no-bypass |
| Always (next) | `G(¬safety_pass → X awaiting_human_approval)`            | a safety failure must move straight to the human gate on the next step                       | arrow 10·fail                                |
| Eventually    | `G(awaiting_human_approval → F resolved)`                | every gated case must eventually be resolved by a human                                      | the gate — escalation liveness               |
| Bounded       | `G(waiting → F≤T (reassessed ∨ escalated))`              | no patient waits past the ceiling **T** without a re-look or an escalation                   | arrows 13 / 14 — wait liveness               |
| Until         | `G(sensitive_data → ¬external_call U redaction_done)`    | raw PII must not leave the system until redaction has passed                                 | arrow 5→6 — no raw PII downstream            |
| Eventually    | `G(action_executed → F audit_log_created)`               | every state-changing action is eventually written to the audit log                           | `emit_event_log` on every transition         |
| Always (next) | `G(WRONG_DOC_DETECTED → X ¬classifying)`                 | an injection / wrong document never reaches the classifier                                   | arrow 18 — injection rejected                |
| Always        | `G(human_confirmed(acuity) → ¬ classifier_reopens_gate)` | once a human sets acuity, the classifier alone cannot re-open the discrepancy gate           | arrow 9c — anti-livelock                     |
| Always        | `G(¬(queued.order_key < emergent.order_key))`            | an emergent patient is never ordered behind a queued one                                     | queue sort — bucket ordering                 |
| Always (next) | `G(agent_failed → X(degrade ∨ halt))`                    | an agent failure must move to a degraded/manual path or a controlled halt — never to nowhere | the `AF·…` edges — per-agent failure model   |

> `resolved` = a human records a decision at the gate: `apply_human_acuity` on the acuity branch, or the safety-branch outcome once it is defined (see **Open design question**).
>
> **How they are checked.** Rules that can be decided at a single step (no-bypass, injection-rejected, safety-fail routing, agent-failure routing) are enforced as transition guards. The liveness and bounded rules (_eventually_ / _within T_) are checked by the runtime governance monitor against the running event trace (the `emit_event_log` stream), which raises a violation if the deadline passes.

---

## Symbolic governance layer — OPA, Z3, Prolog, Datalog

This section puts the neurosymbolic split into practice. The neural components (the Intake Parser and Acuity Classifier LLMs) only make proposals. Before the Orchestrator writes State, each proposal passes through four symbolic layers, with each one guarding a different invariant from the Safety invariants table. If any layer fails, the transition moves to `awaiting_human_approval` or `agent_failed`, and never directly to `treatment_started`. In this governance half of the split, the model proposes, the logic sets limits, and the final decision is written only after every layer has approved it.

**OPA — policy and authorization.** A code-as-policy layer, checked at two points: the human-approval gate (arrows 11/20) and patient release (REL). It decides three questions — whether the actor holds a role authorized for the action, whether a `sign_release` carries a valid `release_reason`, and whether the case may move from redaction to classification (arrow 6), a move blocked if raw PII remains in the payload. Backs the **No raw PII downstream** and **Release authorization** invariants. When policy returns "not allowed," the Orchestrator simply does not perform the action, and the case stays in place instead of advancing.

**Z3 — constraints and consistency.** Runs in `safety_validating` (arrow 9) to prove properties per-case tests cannot. Two central constraints: that queue ordering cannot be violated (a `queued` patient never receives an `order_key` smaller than an `emergent` one), and that the acuity bands are exhaustive — from `acuity_proposed` there is no gap value falling outside one of transitions 9a/9b/9c (no "hole" in the transition table). The solver tries to construct a forbidden state; an `unsat` result means the violation is impossible, while an `sat` result would be a proof that a path to a forbidden state exists — in which case the Orchestrator moves to Blocked rather than continuing. Backs **Bucket ordering** and reinforces the integrity of the transition table.

**Prolog — symbolic inference and explanation.** Backs the `actor_is_charge` guard and the explainability requirement. Facts (who is on shift, in what role) and rules (which action requires which role, and what is blocked outright) let the engine infer whether a given user may perform an action. Just as important is an explanation layer that returns _why_ — approved because the role has authority, blocked because the role is insufficient, or blocked by an outright rule. If inference fails at arrow 1b.z·acuity, the gate is not resolved and the case stays in `awaiting_human_approval`; the explanation is what the nurse sees in the UI and what is written to the audit log.

**Datalog — relations, information flow, and provenance.** Runs in `safety_validating` (arrow 9) for transitive-relation analysis. Two uses: first, tracing information flow — whether a sensitive field (name, ID number) can reach an external tool (for example an external LLM call) through the chain of nodes without passing through redaction; a query returning such a path exposes a leak. Second, checking the provenance of the acuity value — who wrote it: only initial-classification resolution, reassessment carrying new clinical evidence, or an authorized human override are permitted writers, and a timer alone is not. Backs **No raw PII downstream** and **Acuity write-authority**.

**How the roles divide.** OPA governs permissions and boundaries (who, what, when is allowed); Z3 proves there is no mathematical path to a forbidden state; Prolog infers and explains individual decisions; Datalog traces relations and flows across the graph. Together they cover the four separate symbolic requirements in the grading rubric, and together they are what stops a neural proposal from ever becoming an unchecked state write.

---

## Open decisions

These are the items that still need a decision before the specification is final. The safety-fail branch of the human gate (see Open design question) is the most important one to resolve; the others are more straightforward. The OCR question is already settled, as intake uses mock questionnaire data (see Document intake & demo data).

1. Acuity scale (ESI 1–5?) and the `confidence_ok` threshold.
2. `confidence_ok`: **wire in** (low confidence → gate) **or delete** it (see Guards).
3. Per-agent retry budgets `N` (see Per-agent failure model).
4. Formalisms for the **no-approval-bypass**, **sole-writer**, and **injection-rejected** rules.
5. `in_transition` status: real status or transient?
6. `parsed_fields` schema (see Context / State variables).
7. Safety-fail branch of the human gate — see Open design question.

---

## Open design question — safety-fail branch of the human gate

This is the one part of the core logic that is still not designed. The system enters `awaiting_human_approval` for two different reasons, each asking the human a different question. The acuity-discrepancy reason is resolved, but the safety-failure reason is not. This section explains what needs to be decided and the constraints that any answer must meet.

`awaiting_human_approval` is entered by **two situations that ask the human different questions**:

- **Acuity discrepancy** (gap ≥2, arrow 9c → 1b.z·acuity) — **resolved.** Question: "which acuity is right?" Options: `accept_system` / `keep_nurse`.
- **Safety-validation failure** (arrows 10·fail and 11 via `escalation_needed` — verdict fail / low confidence / policy hit, plus `AF·safety` when the validator is down) — **UNRESOLVED** (arrow 1b.z·safety). There is no acuity dispute here.

**To decide:**

- What are the nurse's options on a safety-fail? (override-and-proceed, correct-and-revalidate, reject-case, hold?)
- Where does each option send the case? (`monitoring`/`waiting`? a new `nurse_override` state? back to `safety_validating`? toward `case_closed`?)
- Does resolving a safety-fail require charge role (`actor_is_charge`) or any authorized nurse (`actor_authorized`)?

**Constraints on any answer:**

- Must not violate the **no-approval-bypass** rule (no path to `treatment_started` that skips safety/approval).
- Must not silently return the case to `waiting` unchanged — a resolution must change `acuity`, `clinical_status`, or `safety_verdict`, never nothing (the livelock we designed out).

_Until resolved, arrow 1b.z·safety stays a distinct guarded edge, separate from 1b.z·acuity._
