%% Safety validation: does this case's record contradict itself?
%%
%% Not a clinical opinion. SPECIFICATION.md:727 settles that question — ESI
%% treats danger-zone vitals as judgment in context, so no rule here may
%% overturn an acuity. What these rules check is whether the record *can be
%% true*: a level with no provenance, an automatic settle on a gap that was
%% never automatic, a number nobody proposed.
%%
%% Facts are asserted by app/symbolic/prolog.py right before a query and
%% retracted right after, so the process-wide engine never mixes two cases:
%%   acuity(A | none).            acuity_source(S | none).
%%   gap(G | unknown).            nurse_proposal(N | none).
%%   system_proposal(S | none).   human_decided.   classifier_down.
%%
%% Each rule yields violation(Code); app/actors/safety.py turns the code into a
%% sentence carrying the actual values, because a verdict of "safety failed"
%% tells a charge nurse nothing they can act on.

:- dynamic acuity/1, acuity_source/1, gap/1, nurse_proposal/1,
           system_proposal/1, human_decided/0, classifier_down/0.

settled(A) :- acuity(A), A \== none.
proposed(P) :- nurse_proposal(P), P \== none.
proposed(P) :- system_proposal(P), P \== none.

%% 1. A level and its provenance travel together. Either alone is unauditable:
%%    a level nobody can attribute, or an attribution of nothing.
violation(acuity_without_source) :- settled(_), acuity_source(none).
violation(source_without_acuity) :- acuity_source(S), S \== none, acuity(none).

%% 2. The automatic settle exists only for gaps of 0 and 1 (I4). Claiming it on
%%    a wider gap means the case skipped the charge nurse it was owed.
violation(auto_resolved_on_major_gap) :-
    acuity_source(auto_resolved), gap(G), integer(G), G >= 2.

%% 3. A human-confirmed level with no human decision in this triage. The
%%    decision itself is looked for in the audit log by Datalog; this rule fires
%%    on its answer.
violation(human_confirmed_without_a_decision) :-
    acuity_source(human_confirmed), \+ human_decided.

%% 4. The level came from nowhere: neither proposal, and no human chose it.
violation(acuity_matches_no_proposal) :-
    settled(A), \+ human_decided, \+ proposed(A).

%% 5. The degrade path and the data disagree. `fallback_manual` runs because the
%%    classifier is unusable, so a proposal from it cannot also exist.
violation(classifier_down_yet_proposed) :-
    classifier_down, system_proposal(S), S \== none.
