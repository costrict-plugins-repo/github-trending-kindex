# Storage integrity contract test ledger

Authority: validator's delegated behavioral contract, confirmed as ratified user scope in this run. T1–T4 and A1–A2 are quoted in the test module docstring. Expected outcomes were fixed without reading implementation files or coder material.

| Test | Contract / guard | Mutation that makes the test fail |
|---|---|---|
| invalid_storage_type_preserves_graph | T1; red target | Admit an invalid type, or replace the original before validation. Full node, edge and activity snapshots catch writes. |
| valid_storage_types_remain_supported | T4; green guard | Restrict storage to public-add types, omit an existing built-in, or rewrite the type. |
| import_type_rejection_is_atomic | T2; red target | Skip type validation during dry-run; accept malformed types; commit a valid prefix before rejecting a later record. |
| invalid_archive_type_is_retained | T3; red target | Restore malformed type from legacy columns or whole-row JSON, remove rejected archive evidence, or partially write live rows. |
| reopen_preserves_legacy_type_but_supersede_refuses_copy | T3; reopen is green guard, copy is red target | Rewrite existing types on open, or create a new malformed successor/change original lifecycle before rejection. Read-access clock excluded from final comparison. |
| archive_node_collision_preserves_both_and_continues | A1; red target | Replace an archived same-ID owner (current or rotated archive), delete its live counterpart, stop the batch, suppress failure accounting, or expose either evidence body/title in the error. |
| archive_edge_collision_preserves_complete_evidence | A1; red target | Replace same-ID divergent or unverifiable edge evidence, delete live source, leave a partial node copy, or stop the batch. The selected second endpoint has no archived node collision, ensuring the edge guard is reached. |
| legacy_restore_edge_identity_collision_preserves_both_stores | A1; red target | Restore legacy edge ID 7 as a new edge ID when live ID 7 belongs to different endpoints; replace the live edge; partially restore the node; or discard archive evidence on refusal. Full snapshots of both stores are required. |
| public_add_keeps_its_narrower_type_policy | T4; green guard | Replace public admission rules with the broader storage allowlist, admitting managed/internal types. |
| archive_two_endpoints_roundtrips_original_edge | A2; green guard | Reject identical shared-edge evidence, drop an endpoint, or restore incomplete edge provenance/weight/clocks. |

Mocks: none. All boundaries use real Store, archive files and CLI subprocesses; every database is under pytest tmp_path. Direct SQL constructs historical/collision evidence only. Snapshot capture is used to assert nonmutation, not to invent expected semantic outputs. Archive setup invokes successful public archive operations before injecting corruption.

No authorization boundary is in scope. No production/live graph, external API, coder implementation, or judging-suite execution was used. Static syntax parsing is the only planned local validation; validator owns baseline and patched runs.

Gaps: concurrency and crash injection are not claimed; conservative duplicate refusal is explicitly permitted. Exact-identical duplicate-node retry success is not required. CLI public admission is covered; validator should also run existing MCP admission tests. No unresolved contract contradiction was found.

Fixture correction: validator supplied the public archive layout `data_path/archive/current.db`; the path helper now searches `data/archive/*.db` and requires exactly one real archive. Earlier path-discovery failures are fixture failures, not behavioral red evidence. No implementation was read or tests executed for this correction.
