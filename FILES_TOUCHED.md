# Files touched (T2) + upstream merge-difficulty assessment

Branch: `gmat-rust` (off `gmat-build`). Worktree: `/private/tmp/anki-t2`.

"Merge difficulty" below = risk of conflict when rebasing the GMAT fork onto a
future upstream Anki release. Lower is better. New files cannot conflict; edits
to upstream files can.

## Modified upstream files (4 + 1 trivial)

| File                                           | What changed                                                                                                                                                                                                                                     | Merge difficulty                                                                                                                                                                                                                                                                                                                      |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `proto/anki/scheduler.proto`                   | Added one `rpc GetTopicMasteryStats` line to the `SchedulerService` block (right after `FuzzDelta`) and 4 new messages appended at EOF (`GetTopicMasteryStatsRequest`, `TopicMasteryStat`, `GetTopicMasteryStatsResponse`).                      | **Low.** The RPC line sits at the end of the service; the messages are appended. Upstream adding its own RPCs/messages nearby is the only conflict source, and it resolves by re-stacking lines (no semantic overlap). Proto field numbers are local to the new messages, so no renumbering risk.                                     |
| `rslib/src/scheduler/service/mod.rs`           | (a) Two new `use` lines for the request/response types; (b) one new trait method `get_topic_mastery_stats` appended to the `impl SchedulerService for Collection` block; (c) a `#[path = "../topic_mastery.rs"] mod topic_mastery;` declaration. | **Low–medium.** The method is a 4-line delegator added at the end of the impl. The `use` block is alphabetically ordered (rustfmt-stable). The `#[path]` mod declaration is the only unusual bit — see note below.                                                                                                                    |
| `rslib/src/scheduler/queue/builder/mod.rs`     | `build()` gained a second parameter (`points_at_stake_weights: &HashMap<NoteId, f32>`) and calls `self.sort_review(...)` after `sort_new()`. `build_queues()` computes the weights (one query) and passes them in.                               | **Medium.** This is the only signature change to an existing function. `build()` has a single caller (`build_queues`, same file), so the blast radius is contained, but if upstream refactors `build()`/`build_queues` the hunk will need manual re-application. The added imports already existed (`HashMap`, `NoteId` via prelude). |
| `rslib/src/scheduler/queue/builder/sorting.rs` | Added `sort_review()` + free fn `reorder_reviews_by_weight()` + a `#[cfg(test)] mod test`. New `use` lines for `DueCard` and `NoteId`.                                                                                                           | **Low.** Purely additive; no existing function bodies changed.                                                                                                                                                                                                                                                                        |
| `rslib/src/storage/mod.rs`                     | Added `pub(crate) mod topic_stats;` to the module list.                                                                                                                                                                                          | **Trivial.** One line in an alphabetized `mod` list.                                                                                                                                                                                                                                                                                  |

### Note on the `#[path]` mod declaration

The spec places the mastery Collection method at
`rslib/src/scheduler/topic_mastery.rs`, but the natural module owner
(`rslib/src/scheduler/mod.rs`) was off-limits for this track (another agent owns
it). To declare the module without editing `scheduler/mod.rs`, it is attached
from the already-modified `scheduler/service/mod.rs` via
`#[path = "../topic_mastery.rs"] mod topic_mastery;`. This keeps the file exactly
where the spec wants it and confines all edits to allowed files. When merging,
the clean follow-up is to move the one-line `mod topic_mastery;` declaration into
`scheduler/mod.rs` and drop the `#[path]`; nothing else changes.

## New files (zero merge conflict risk)

| File                                   | Purpose                                                                                                                                                                                                                                                                         |
| -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `rslib/src/scheduler/topic_mastery.rs` | `Collection::get_topic_mastery_stats` (the read-only mastery query) and `Collection::points_at_stake_weights` (the per-note weights powering the reorder). Topic parsing from tags, FSRS retrievability math, tunable topic-weight/weakness model, and unit + end-to-end tests. |
| `rslib/src/storage/topic_stats.rs`     | `SqliteStorage::all_topic_card_rows()` — the single aggregate query wrapper.                                                                                                                                                                                                    |
| `rslib/src/storage/topic_stats.sql`    | The aggregate SQL (cards ⋈ notes ⋈ grouped revlog; `json_extract` for FSRS stability).                                                                                                                                                                                          |
| `pylib/tests/test_topic_mastery.py`    | Python test calling `col._backend.get_topic_mastery_stats(...)`.                                                                                                                                                                                                                |
| `WHY_RUST.md`, `FILES_TOUCHED.md`      | Deliverables.                                                                                                                                                                                                                                                                   |

## Overall

Five upstream edits, four of them additive or one-line; the only behavioural
signature change is contained to a function with a single in-file caller. The
heavy logic is isolated in new files. Rebasing onto a future upstream Anki
should be low-effort, dominated by the `build()`/`build_queues` hunk in
`queue/builder/mod.rs`.
