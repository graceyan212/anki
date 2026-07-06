# Files touched — upstream merge-difficulty assessment

The upstream Anki files this GMAT fork modifies, and the risk of conflict when
rebasing onto a future upstream Anki release. Lower is better. New files cannot
conflict; edits to upstream files can.

Verified against `git diff --stat b00308e55..HEAD` (`b00308e55` = the last
upstream commit before the fork). **12 upstream files are modified**, split
between the engine change (this doc's original T2 scope) and other fork tracks.

## Engine change (T2): points-at-stake ordering + per-topic mastery query

| File                                           | What changed                                                                                                                                         | Merge difficulty                                                                                                                                                                              |
| ---------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `proto/anki/scheduler.proto`                   | One `rpc GetTopicMasteryStats` added at the end of the `SchedulerService` block + 4 new messages appended at EOF.                                    | **Low.** Appended; only conflicts if upstream adds RPCs/messages at the same spot, resolved by re-stacking. Field numbers are local to the new messages.                                      |
| `rslib/src/scheduler/mod.rs`                   | +1 line: `mod topic_mastery;` in the module list.                                                                                                    | **Trivial.** One line in the module list.                                                                                                                                                     |
| `rslib/src/scheduler/service/mod.rs`           | Two `use` lines + one trait method (`get_topic_mastery_stats`, a delegator) appended to the `impl SchedulerService for Collection` block.            | **Low.** Additive; the `use` block is alphabetized (rustfmt-stable).                                                                                                                          |
| `rslib/src/scheduler/queue/builder/mod.rs`     | `build()` gained a `points_at_stake_weights: &HashMap<NoteId, f32>` parameter; `build_queues()` computes the weights (one query) and passes them in. | **Medium.** The only signature change; `build()` has a single in-file caller (`build_queues`), so the blast radius is contained, but a manual re-apply is needed if upstream refactors these. |
| `rslib/src/scheduler/queue/builder/sorting.rs` | Added `sort_review()` + free fn `reorder_reviews_by_weight()` + a `#[cfg(test)]` module.                                                             | **Low.** Purely additive; no existing bodies changed.                                                                                                                                         |
| `rslib/src/storage/mod.rs`                     | +`pub(crate) mod topic_stats;` in the module list.                                                                                                   | **Trivial.** One line in an alphabetized `mod` list.                                                                                                                                          |

New files backing the above (zero conflict risk): `rslib/src/scheduler/topic_mastery.rs`,
`rslib/src/storage/topic_stats.rs`, `rslib/src/storage/topic_stats.sql`,
`pylib/tests/test_topic_mastery.py`.

### Note on `scheduler/mod.rs`

An earlier revision avoided editing `scheduler/mod.rs` by attaching the module
from `service/mod.rs` via `#[path = "../topic_mastery.rs"] mod topic_mastery;`.
That workaround was **removed** (commit `732ce9f73`): the module is now declared
normally with `mod topic_mastery;` directly in `scheduler/mod.rs`. No `#[path]`
declaration remains anywhere in the fork.

## Other fork tracks (modified upstream files outside T2)

Touched by the readiness feature (T3), the licensing/rebrand, and the
Rust→Python→UI pipeline marker — listed for a complete merge picture:

| File                    | What changed                                                                         | Merge difficulty                                                     |
| ----------------------- | ------------------------------------------------------------------------------------ | -------------------------------------------------------------------- |
| `LICENSE`               | Replaced with AGPL-3.0-or-later.                                                     | **High.** 706-line change; conflicts with any upstream LICENSE edit. |
| `README.md`             | Rebranded to the GMAT Focus Edition study tool + build/architecture notes.           | **High.** Diverges from upstream README.                             |
| `rslib/src/version.rs`  | +`gmat_marker()` (the pipeline-proof marker).                                        | **Low.** Additive.                                                   |
| `pylib/rsbridge/lib.rs` | Exposes `gmat_marker` to Python.                                                     | **Low.** Additive.                                                   |
| `qt/aqt/about.py`       | Shows the GMAT marker in the About box.                                              | **Low.** Small additive edit.                                        |
| `qt/aqt/main.py`        | Registers the "GMAT Readiness" Tools-menu entry and the Bauhaus theme hook in setup. | **Low–medium.** A few added lines in a setup path.                   |

## Overall

The **engine change** is contained: six upstream edits, four of them additive or
one-line, with a single behavioural signature change (`build()`) whose blast
radius is one in-file caller. The heavy logic lives in new files. The largest
rebase surfaces in the fork overall are `LICENSE` and `README.md` (deliberate
divergence), not the engine.
