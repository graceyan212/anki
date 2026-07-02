# Why this belongs in Rust, not Python (T2)

The GMAT fork adds two engine features — a **points-at-stake review ordering**
and a **per-topic mastery query** — to Anki's Rust scheduler (`rslib`). Both
deliberately live below the Python (`pylib`) layer. Here is why.

## 1. The review order is produced _inside_ the queue builder

The order a study session shows cards in is decided in
`rslib/src/scheduler/queue/builder`, not in Python. Review cards are gathered
straight from SQLite and, historically, are **not re-sorted in Rust** — their
order is whatever the DB returns under the configured `review_order`. The
points-at-stake feature reorders that gathered `Vec<DueCard>` in place
(`sort_review`), right after `gather_cards` populates it and before the queue is
materialized and handed to the reviewer.

Python never sees this intermediate list. `col.sched.getCard()` already returns
a fully-built queue; by the time Python is involved, the ordering decision is
made and frozen. To influence the order from Python we would have to either
rebuild the queue ourselves in Python (duplicating the deck-limit, burying, and
interday-learning logic) or re-fetch and re-sort cards on every draw. Doing it
in Rust is one stable sort over an already-in-memory vector — effectively free,
and it composes with the existing new/learning/interday merge.

## 2. The mastery query is a single aggregate over the whole collection

`GetTopicMasteryStats` reads **every review-eligible card**, joins it to its
note's tags and to a grouped revlog aggregate, and folds the result by topic.
On a ~50k-card deck this is one prepared SQL statement returning ~50k rows that
we group in a tight Rust loop.

In Python the same work would be N+1 round-trips across the
Python↔Rust↔SQLite boundary (fetch cards, then per-card fetch tags and revlog),
each crossing the protobuf/PyO3 marshalling layer. The Rust path runs the
aggregation in the same process and language as the storage layer, with
`prepare_cached` statement reuse and zero per-row serialization. The `cards.data`
FSRS blob is parsed with SQLite's native `json_extract`, and the revlog
pass/total counts are computed by the database engine itself — none of that data
ever has to be shipped to Python.

## 3. Correctness, undo, and transactions are Rust concerns

The mastery query is a plain, read-only storage read (`all_topic_card_rows`): it
opens no transaction and records no undo step, and — unlike `transact_no_undo`
(the wrapper Anki uses for db-check, which resets the undo history) — it leaves
the existing undo history intact, so opening the readiness dashboard never costs
the student a pending undo (proven by a unit test). The points-at-stake reorder mutates **no**
`card.due` and writes nothing — it is a pure in-memory permutation of a
transient vector — so the undo history is left completely intact across a queue
build (also unit-tested) and there is no integrity risk. These invariants ("don't break
undo", "don't dirty the collection") are enforced by the Rust type/transaction
machinery; replicating them safely from Python would mean re-implementing the
transaction discipline that `rslib` already guarantees.

## 4. One implementation, every client

`rslib` backs the desktop client, AnkiMobile, AnkiDroid, and AnkiWeb. Putting
the logic in Rust and exposing it over the existing protobuf service means the
mastery query and the new ordering are available to every client through the
generated bindings, with no per-platform reimplementation. The Python binding
(`col._backend.get_topic_mastery_stats(...)`) is generated automatically from
`scheduler.proto`.

In short: the data lives in Rust/SQLite, the ordering decision is made in Rust,
the performance budget demands Rust, and the safety guarantees are Rust's to
keep. Python is the right place to _call_ this feature, not to _implement_ it.
