# Why this belongs in Rust, not Python

The GMAT fork adds several things to Anki's Rust engine (rslib): three GMAT
scores (memory, performance, and readiness), a review order that puts the
student's weakest topics first, computer-adaptive card selection,
confidence-based grading, and a per-topic mastery report. All of them are written
in Rust instead of Python, for four reasons.

## 1. The card order is decided inside the engine, where Python can't reach it.

Anki decides what order to show cards deep inside the Rust queue builder. Our code
sorts the due cards right after they're gathered and before the session starts —
weakest topics first — and, when adaptive mode is on, picks the next card nearest
the student's estimated ability. (The current build weights every topic equally,
so "weakest first" is a plain sort; the same hook can weight topics by exam
importance instead.)

By the time Python is involved the queue is already built and ordered — Python
never sees the raw list to reorder it. Doing this from Python would mean
rebuilding the whole queue ourselves (re-implementing Anki's deck limits, card
burying, and learning-card logic) or re-sorting the cards every time one is drawn.
In Rust it's one quick sort of a list that's already in memory, right beside the
code that builds the queue.

## 2. The scores and the report are one database read, not hundreds.

The mastery report, the per-topic breakdown, and all three scores read the same
thing: every review card in the collection, with its tags, FSRS memory state, and
review history — gathered in a single query that stays fast even on a 50,000-card
deck. From that one read the engine computes memory (FSRS recall), performance (a
Rasch / 1PL ability estimate from item-response theory), and readiness (that
ability projected onto the GMAT 205–805 scale). In Python the same work would be
hundreds of separate trips between Python and the database; in Rust the data never
has to leave the engine at all.

## 3. Undo has to keep working, and that's the engine's job.

The scores and the reports only read — they never write — so opening them never
wipes out the student's ability to undo their last action. The reordering just
rearranges a temporary list, so building the queue leaves the undo history
untouched, and confidence grading records a review exactly the way answering a
card normally does. All of this is backed by tests.

## 4. Writing it once covers every device, including the phone.

rslib is the shared engine behind the desktop app, the iPhone/iPad app, AnkiDroid,
and the web version. Because the logic lives in Rust and is exposed through Anki's
normal engine interface, the exact same code ships everywhere with nothing to
rewrite — the Python, Swift, and Kotlin versions of each call are generated
automatically. So the phone shows the _same_ three scores and applies the _same_
confidence grading as the desktop, from a single implementation. Written in
Python, it would only ever run on desktop.

In short: the data lives in the Rust engine; the ordering, scoring, and grading
all happen there; it's the only place fast enough; and it's the only place that
can keep undo safe.
