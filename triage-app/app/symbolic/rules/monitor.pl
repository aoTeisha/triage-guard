%% The monitor's reasoning layer: who may act, and the per-timer decision
%% cross-checked against app/monitor/bthreads.py.
%%
%% Facts about ONE timer are asserted by app/symbolic/prolog.py right before a
%% query and retracted right after, so the process-wide engine never mixes two
%% timers:
%%   timer(Id, Kind, FireState).   pause_active(Id).
%%   notify_count(Id, N).          notify_budget(Id, B).

:- dynamic timer/3, pause_active/1, notify_count/2, notify_budget/2.

%% Who may act. A charge role resolves the gate and signs a release; once a
%% case has been handed up (senior_required), only a shift lead may.
%%
%% Role only, for now — no jurisdiction or data-class check, since no store
%% yet holds who's on shift, in which ward, at what clearance. Those get
%% their own facts and an extra rule when that store exists.
%% Must be kept in sync by hand with `charge_roles` in
%% app/symbolic/policy/monitor.rego — OPA can't call into Prolog, so that
%% file independently restates this same role set rather than delegating to
%% it. See tests/symbolic/test_opa.py (or wherever this file's cross-engine
%% consistency test lives) for the check that catches drift.
charge_role(charge_nurse).
charge_role(shift_lead).
senior_role(shift_lead).

may_resolve_gate(Role, true)  :- senior_role(Role).
may_resolve_gate(Role, false) :- charge_role(Role).

reminder_kind(gate_reminder).
reminder_kind(reassessment_reminder).
reminder_kind(senior_reminder).

%% A fire whose acknowledgment was lost. Never re-dispatched blind: a second,
%% unnecessary delivery could re-notify a nurse twice, or worse.
unacknowledged('UNKNOWN').
unacknowledged('DISPATCHING').

budget_spent(T) :- notify_count(T, N), notify_budget(T, B), N >= B.

%% action(Timer, Action): exactly one action per timer.
action(T, reconcile)   :- timer(T, reassessment, S), unacknowledged(S).
action(T, dispatch)    :- timer(T, reassessment, S), \+ unacknowledged(S).
action(T, cancel)      :- timer(T, K, _), reminder_kind(K), \+ pause_active(T).
action(T, fail_budget) :- timer(T, K, _), reminder_kind(K), pause_active(T), budget_spent(T).
action(T, notify)      :- timer(T, K, _), reminder_kind(K), pause_active(T), \+ budget_spent(T).
%% An unrecognized kind has no pause to check, so it is cancelled rather than delivered blind.
action(T, cancel)      :- timer(T, K, _), K \== reassessment, \+ reminder_kind(K).

%% denial(Event, Timer, Why): the explanation behind every blocked event.
denial(dispatch, T, blind_redispatch_from_unknown) :- timer(T, _, S), unacknowledged(S).
denial(notify, T, reminder_pause_resolved)         :- timer(T, K, _), reminder_kind(K), \+ pause_active(T).
denial(notify, T, notification_budget_exhausted)   :- timer(T, K, _), reminder_kind(K), budget_spent(T).
