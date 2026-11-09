# Independent test ledger

Authority is the isolated experiment contract v1, S1-S10 and its
pre-implementation clarifications, supplied by the Validator. This is not a
signed factory promotion verdict. Source and query files retain their original
separate freezes. No implementation code or generated output was inspected.
Only AGENTS/REVIEW, existing public tests and the supplied contract were read.

## Behavior claims and falsifying mutations

All tests are in `tests/test_recall_cues_contract.py`. The table names the
behavior and a concrete regression each test should reject.

| Test | Contract | Falsifying mutation |
|---|---|---|
| default_config_and_strict_bounds | S1 | Enable by default, coerce string limits, accept NaN |
| spans_origins_distinctness_and_determinism | S3 | Invent support, drop alias, remove negation, emit >3 or duplicate cues |
| long_and_empty_source_inputs | S3 | Read content tail past 12000 or emit unsupported empty cue |
| fingerprint_tracks_authority_but_not_usage | S4 | Omit provenance/extra/scope fields or include usage counters |
| build_rejects_invalid_limits | S2 | Coerce or ignore invalid bounds |
| bounded_selection_idempotence_and_no_source_writes | S1/S2/S4 | Generate before cap, rewrite stable metadata, mutate source timestamps, use get_node |
| changed_source_is_immediately_unusable | S4/S5 | Trust stored sidecar without source fingerprint comparison |
| inactive_sources_never_redirect_cue_votes | S4/S6 | Emit retired source or transfer cue vote to successor |
| compare_and_swap_rejects_source_changed_during_generation | S4 | Publish stale generated cues after intervening source edit |
| build_failure_cannot_publish_half_a_sidecar | S4/S5 | Commit metadata separately from FTS, swallow explicit build failure |
| unique_scored_candidates_filter_before_limit_and_read_only | S5 | Limit before access filter, multiply votes per cue, mutate metadata on reads |
| disabled_parity_and_enabled_source_only_evidence | S1/S6 | Apply sidecar while disabled or expose generated text as evidence |
| explicit_predicate_and_current_source_fences | S6 | Admit private, archived, expired or unverified source through cue channel |
| sidecar_failure_falls_back_with_private_warning | S5/S6 | Propagate optional SQL failure, log query/source wording, omit warning |
| v15_upgrade_and_required_columns | S4 | Omit v16 migration, alter existing facts, omit drift verification |
| cli_and_mcp_share_explicit_bounded_administration | S7 | Wire one surface to another store or ignore limits |
| malformed_ids_fail_before_work | S2 clarification | Accept malformed/oversized list or generate before validation |
| retired_selection_and_missing_index_repair | S2 clarification | Select archived by default or label missing FTS unchanged |
| fts_indexes_support_not_labels_and_uses_or_tokens | S5 clarification | Index framing labels, use AND, retain tokens after first 32 |
| explicit_scope_filters_neighbor_titles_and_answer_gather | S6 clarification | Leak neighbor title or ignore answer scope |

Parameterizations add independent bounds, invalidation and lifecycle cases.
Assertions carry S# references in test docstrings; they were authored against
contract promises rather than observed output. These are new-feature contract
checks, not repair labels claiming every one fails on historical main. Validator
may use a disposable no-op shim to establish behavioral red; only Validator owns
those observations.

## Reachability and mocks

- Source/access tests first establish a usable indexed positive source. The
  predicate test records IDs passed to the access predicate, including allowed
  and denied candidates. Results assert actual IDs and absence of denied title.
- CAS uses a wrapper around the real public generator to edit the source after
  generation input is captured. The wrapper records invocation and delegates all
  cue work. This injects timing, not expected generator behavior.
- Bounded build wraps the real public generator to count source IDs; it replaces
  access-mutating `get_node` with a fail-fast function. No permissive mock values.
- Atomic publication uses a SQLite authorizer to refuse real FTS writes and
  asserts the refusal was reached. Both persisted tables must retain the old
  snapshot. Because write order is deliberately not learned from code, this does
  not prove the refusal occurs strictly between the two writes on every design;
  the CAS test independently covers stale publication.
- Optional-channel failure drops the disposable sidecar FTS table. Ordinary
  retrieval and logging remain real. No mock fallback result.
- MCP store injection follows existing test conventions; the real helper and
  real temporary SQLite state are exercised. CLI is a real subprocess.
- Standard repository conftest disables provider credentials/schedulers. The
  evaluator additionally blocks sockets, DNS, and known vector function entry
  points, records attempts, and fails even if optional code catches a denial.
- Evaluator fixed datetime/time substitutions affect only the evaluation clock;
  elapsed latency uses monotonic perf_counter. Sources receive identical fixed
  persisted timestamps before indexing. PYTHONHASHSEED=0 is required at launch.

## Evaluation design

`future_recall_ablation.py` creates a new disposable directory and refuses an
existing one. Source facts seed two isolated stores with normal one-time
supersession state. Both arms use identical explicit scope/audience filtering,
no vectors and no graph expansion. Each top-k budget has its own search call.

Before parsing query content, the runner writes PRE_QUERY_FREEZE.json including
source/query hashes, runner hash, generator-module hash and canonical logical
sidecar hash. Read-time mutation of sidecar metadata fails evaluation. JSON
contains all IDs/ranks, per-condition recall and truncated MRR, answerable and
no-answer candidate rates, timing and storage. Markdown states limitations and
budget differences. Generated labels never define relevance or answer labels.

The separate wrong-hint fixture rotates existing hints by seven pairs before
any implementation results. It does not replace or edit the 72 main trials.
Its source text is deliberately independent of generator output but the hints
are artificial wrong-context stress, not measured user-memory errors.

## Gaps and ambiguities reported to Validator

- Exact RRF/ensemble arithmetic is specified, but the contract does not declare
  the public configuration selector or a candidate-channel injection API. No
  implementation-derived seam was invented. Existing public fixture patterns
  did not supply these settings; focused coverage remains a Validator gap.
- Final-hydration concurrent edit coverage requires a declared acquisition/
  hydration injection point. The current suite covers generation CAS and
  immediate before-query invalidation, not a race after cue acquisition.
- The evaluator verifies sources, not provider-generated answers. No-answer
  candidate rate is explicitly not answer correctness or hallucination rate.
- Full migration-history immutability, absence of provider/background wiring,
  tool inventory and documentation completeness require Validator code/diff
  review. The upgrade test models the v15 pre-sidecar shape via the repository's
  existing drop/reopen migration fixture convention.
- MCP output format is only described as existing conventions. The test checks
  shared state mutations and CLI JSON, but does not invent an exact MCP payload.
- Bounded refill is covered by private distractors before allowed records, but
  the exact maximum 4096 examined rows and query-plan boundedness need a declared
  query-observation seam or Validator review.
- This corpus deliberately includes historical sources retired by normal policy;
  their misses are policy/corpus mismatch evidence, not altered labels.

No tests or evaluation were executed by Tester. Only standalone fixture shape/
hash checks and Python AST parsing were performed. No provider credentials were
read, live profiles opened, commits created, or Coder contacted.

## Contract-based oracle corrections and completed seams

Before accepting any implementation fix, Validator identified two test-oracle
issues. Both were resolved directly from the contract without implementation
inspection:

- S4 requires an actual fingerprinted source change. The audience-invalidation
  fixture now starts explicitly `team` and changes to `private`; leaving an
  existing private value private did not exercise invalidation.
- S4 promises idempotent persisted outcomes, not a prohibition on recomputing
  unchanged inputs. Removed only the assertion requiring generation to skip an
  unchanged source. First-build ordering/bounds, persisted metadata equality,
  source equality and built/unchanged counters remain asserted.

Validator supplied existing public seams, closing two earlier gaps:

| Added test | Contract | Falsifying mutation |
|---|---|---|
| final_hydration_removes_obsolete_cue_contribution | S6 clarification | Keep cue vote after source changes between acquisition and final hydration |
| rrf_uses_rank_units_and_configured_weight | S6 clarification | Add raw BM25 to cue RRF, ignore configured weight or rrf_k |
| disabled_never_calls_sidecar_but_enabled_does | S6 | Consult sidecar while disabled |

The final-hydration race wraps real public `cue_candidates`, confirms the source
was acquired, then edits it. A wrapper around real `Store.fts_search` either
retains or removes that one source to construct both boundaries deterministically.
It does not fabricate cue scores or hydration results. The ordinary-contribution
case compares the surviving contribution against its pre-edit ordinary-only
control, not a captured golden answer. Public module aliases are instrumented
where exposed. The exact RRF oracle is independently calculated for one source
at rank one in both channels, using `store.config.ranking.rrf_k` and
`ranking='rrf'`. Ensemble exact arithmetic remains a review gap.

The runner passes public `recency_time` and `evaluation_time` explicitly as well
as freezing datetime/time. Future provider caps are left to the final integration
report so this independent evaluator does not establish a competing budget.

## Additional independent S3/S6 review regressions

The Validator supplied three behavioral review findings and a minimal public
Kinbase association fixture, without implementation text or retrieval-quality
outputs. Added these checks before corresponding repair execution:

| Added test | Contract | Falsifying mutation |
|---|---|---|
| generated_spans_preserve_decimal_address_and_abbreviation_context | S3 | Split `1.5`, a domain/email or `Dr. Vale` at an internal dot and emit a partial source fact |
| trusted_cue_expiry_uses_explicit_evaluation_clock | S6/S9 | Use wall-clock expiry for cue trust while ordinary retrieval uses the requested evaluation time |
| scope_filter_also_bounds_linked_kinbase_question_metadata | S6 | Attach denied question content/owner to an allowed fact through `kinbase_unknowns` |

Span cases use independently authored short single-instruction source text;
protected fragments and offsets come from those facts, not generator output.
The clock test establishes ordinary trusted retrieval and real cue eligibility
at a date before the authored expiry, then suppresses only the ordinary FTS
source to reach the cue path. It also checks a date after expiry. The linked
question test establishes real unfiltered enrichment using the supplied public
`extra.kinbase` repo/logical_key relationship, then applies the explicit source
predicate and checks the complete emitted result for denied content and owner.

These additions do not modify frozen ablation sources, queries, stress cases,
or evaluator behavior. Tester did not execute the tests or inspect code.
