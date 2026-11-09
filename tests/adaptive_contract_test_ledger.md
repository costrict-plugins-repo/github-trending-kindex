# Independent adaptive Sim acceptance ledger

Authority: `/Users/jmcentire/kinbase-assessment-20261015/implementation/adaptive-contract.md` and `adaptive-design.md`, authorization confirmed by Validator. No implementation files or peer worktrees were inspected. Repository guidance, existing tests, and Validator-supplied shared interface facts were read. The Validator owns execution and verdict. Author performed AST syntax checks only.

## Assertion and falsifiability mapping

Each test docstring names its contract paragraph (C1–C8; exact paragraph anchors are defined in the core test module) and the implementation mutation that makes the test fail. Assertions within a test inherit that test's cited clause; helper setup assertions cite their own requirements. This is the per-test falsifiability ledger, maintained beside each oracle to avoid a second stale list.

Core suite covers real enqueue/drain/pickup and Store metadata: minimum due and no idle work; duplicate stability; hysteresis/cap/reset; explicit and unknown events; override scope, persistence, duration, precedence, and disabled-adaptive behavior; stable bounded jitter and backward clocks; restart; real SQL queue/generation/pickup faults; exact advice lifecycle, TTL, bounded retention, scoped IDs, legacy missing timestamps, and stale/superseded advice; real configuration validation; store circuit threshold/cooldown/probe and nonfailure dispositions; live worker control recheck; corrupt-state fail-closed; earliest store due.

Surface suite covers actual CLI subprocess parsing/persistence and manual-check backoff, MCP discovery registration and selected-store mutations/ack, rejection of ambiguous scope, real money/native-count admission precedence, admitted provider snapshot retention, deferred hook spawn suppression, opt-out precedence, cross-store ack refusal, concurrent workers at failed cooldown probe, and explicit changed-goal derivation through supervisor_tick.

Guard labels: new capability tests are red-now. Existing hard-gate tests (real money/native counts, runtime kill switch, pinned provider snapshot) preserve prior authorities while invoking new attention controls; their whole composite tests are red-now because the new public API is absent at base. No existing green-now guards were reclassified. Existing queue/admission/trust suites remain the Validator's separate compatibility evidence.

## Mock ledger

- `adaptive_sim.now`: deterministic wall-clock only; direct float, no unset attributes. CLI tests use actual wall time in both parent and child to avoid mismatched expiry.
- `sim.call_sim`: core structured provider success/quiet/advice or named existing failure disposition; records every call. Queue, storage, control, validation, terminal commits, and pickup remain real. Failure classification tests intentionally enter this provider boundary.
- `sim.build_sim_grounding`: returns empty synthetic context. Grounding quality is outside controller scope. No permissive mock attributes.
- `sim.spawn_background_drain`: records calls or explicitly returns False in supervisor tests, preventing subprocesses. Deferred-respawn oracle asserts no spawn request; it does not claim real subprocess-loop observation.
- MCP `_store`/`_config`: select real isolated Store/config using established repository fixture conventions; tools and decorators remain real.
- Native `_run_provider`: structured synthetic response/usage at existing transport seam. Real native allowance ledger, preflight, call_sim and queue remain active. `shutil.which` supplies synthetic native executable availability only for codex/tmux, so no native command is launched.
- Synthetic command provider: an actual Python subprocess with stdin, JSON stdout and call log. Real call_sim and budget admission remain active; no network or credentials.
- SQL failure triggers: actual SQLite ABORT on real meta writes. Queue/Store functions are not mocked.

## Reachability

Control rejection tests compare persisted metadata before/after. Worker opt-out/backoff tests first successfully enqueue. Native and money tests first prove a successful transport effect through actual hard admission before exhausting limits. Ack tests first generate and emit real advice, then attempt wrong scope/store/unknown IDs. MCP registration is checked through FastMCP's public list_tools boundary. SQL commit fault tests force real provider completion or valid pending data before injecting write failures. The legacy fixture starts with genuine generated pending, strips only generation timestamps, and removes synthetic controller metadata to represent a pre-controller database.

## Explicit gaps and limits

- No new provider-quality, prompt-quality, or network tests: excluded by contract.
- No separate privacy configuration exists in the shared Sim interface; runtime opt-out is the confirmed privacy/enable boundary and is tested after enqueue and with attention.
- Existing API/native/Ollama hard admission authority is preserved; these additions exercise real money and native limits, while existing Ollama admission tests remain required compatibility evidence.
- No hard process-kill test newly added at the ready-claim boundary. SQL terminal-write failure plus Store reopen exercises persisted-ready recovery without provider replay; existing queue crash acceptance tests remain required.
- Deferred spawn test observes real supervisor hook requests against a suppressed launcher, not a real recurring background child-process loop. Existing bounded-worker acceptance remains required.
- No asserted exact reason-code vocabulary for invalid ack/corrupt state was invented; contract promises a fixed code but omits the code set. Tests require rejection and a code, exact scope, and no mutation. Validator should confirm vocabulary stability separately if names become a public promise.
- Positive integer maxima beyond published max_override_seconds and interval relationships are unspecified. Tests cover nonpositive values, ordering, and explicit jitter limits without inventing numeric caps.

No contradictory product requirements were identified. The undeclared ack reason-code vocabulary and unspecified integer caps are specification precision limitations, not permission to broaden behavior.

## MCP exception projection harness correction

Existing test `tests/test_mcp_privacy.py::test_exception_projection_never_emits_raw_credential` establishes that `_safe_output` projects a ValueError into RuntimeError. The scope-rejection oracle therefore accepts ValueError or RuntimeError, or an explicit structured rejection. It now additionally requires a nonempty rejection diagnostic and still asserts no persisted control write. No controller implementation was read; no judging test was run by the Tester.

## Review-driven recovery additions

`test_adaptive_sim_recovery_contract.py` adds five cases (three functions with parametrization):

- C5 deferred alpha followed by admitted beta: beta must execute immediately, alpha survives restart and executes after finite backoff, and repeated deferred hooks request no new worker. Mutation: break drain at deferred head, drop deferred claim, or respawn on retained deferred work.
- C6/C7 terminal-control one-shot failure for quiet and advisory results: a real SQLite trigger/UDF fails exactly once after provider completion; restart/retry must commit the successful outcome without another provider call. Mutation: generic error fallback finalizes/deletes the ready claim with a neutral result. The UDF's nontransactional one-shot state is deliberate so a secondary error-finalization attempt is not protected by a permanent fault.
- C5 live API enable gate: an enabled/disabled pair uses real call_sim, real get_client, and a fake urllib OpenAI transport from existing test conventions. Only the HTTP boundary is replaced; the key is a fixed synthetic fixture value in a dedicated temporary environment variable. Mutation: pinned enabled configuration bypasses live llm.enabled=False. Positive enabled case proves transport reachability; disabled case requires zero transport calls.

No controller source was inspected. Only AST syntax checks were executed by the Tester.

Recovery API fixture correction: `real_call_sim` is imported at module collection time, before the core `box` fixture replaces `sim.call_sim`. This preserves the real function when the recovery module is collected independently, as well as in the combined suite. The enabled transport reachability assertion is unchanged. Only AST import-placement validation was performed by the Tester.

## Deferred parking fault regression

The recovery suite adds one real SQL/UDF one-shot fault on a post-enqueue `sim.queue` upsert containing deferred alpha. The selector parses the actual queue JSON and matches the exact conversation ID. It is installed only after both admissions and alpha's scoped backoff, so it targets deferred queue persistence. Reopen/retry must process beta while alpha remains deferred, then process alpha exactly once after expiry. Mutation: generic error handling neutrally finalizes/discards alpha after a failed park. The UDF permits subsequent writes so such an incorrect fallback is observable.

The terminal-commit recovery test additionally accepts the published `RuntimeError("review_terminal_commit_failed")` persistence projection, with exact diagnostic validation. This adjusts the exception envelope only; successful-result durability and no-provider-replay assertions are unchanged. Existing core permanent-fault tests remain untouched pending Validator's separate boundary confirmation. No judging tests were run and no implementation source was read.

Validator separately confirmed the core permanent pending-write fault now surfaces the same public safe terminal persistence exception. That single core catch now also accepts RuntimeError with exact `review_terminal_commit_failed` diagnostic; pending/control/restart/replay assertions are unchanged.
