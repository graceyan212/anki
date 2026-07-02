# Why this belongs in Rust, not Python

The GMAT fork adds two things to Anki's Rust engine (rslib): a review order that
puts the student's weakest topics first, and a per-topic mastery report. Both are
written in Rust instead of Python because:

## 1. The card order is decided inside the engine, where Python can't reach it.

Anki decides what order to show cards deep inside the Rust queue builder. Our code
sorts the list of due cards right after it's gathered and before the study session
starts — putting the weakest topics first. (Since the new version of the GMAT
weight every topic/section equally, so this is simply "weakest first" however the
code can be changed to weigh topics by exam importance)

By the time Python is involved, the queue is already built and ordered, Python
never sees the raw list to reorder it. To do this from Python we'd have to rebuild
the whole queue ourselves (copying Anki's deck limits, card burying, and
learning-card logic), or re-sort the cards every single time one is drawn. In Rust
it's one quick sort of a list that's already in memory, and it fits neatly into the
code that's already there.

## 2. The mastery report is one database query while in Python it would be hundreds.

The report reads every review card in the collection, looks at its tags and its
review history, and groups the results by topic — all in a single database query
that stays fast even on a 50,000-card deck. In Python, the same work would mean
hundreds of separate trips between Python and the database. Doing it in Rust keeps
everything next to the database, so the data never has to be handed over to Python
at all.

## 3. Undo has to keep working, and that's the engine's job.

The mastery report only reads data and it never writes anything, so opening the
report never wipes out the student's ability to undo their last action. The
reordering doesn't change any cards, it just rearranges a temporary list, so
building the queue leaves the undo history untouched. Both of these are backed by
tests.

## 4. Writing it once covers every device, including the phone.

rslib is the shared engine behind the desktop app, the iPhone/iPad app, AnkiDroid,
and the web version. Because the logic lives in Rust and is exposed through Anki's
normal engine interface, the exact same code ships to the phone with nothing to
rewrite — the Python, Swift, and Kotlin versions of the call are all generated
automatically. If we'd written this in Python, it would only ever work on desktop.

In short: the data lives in the Rust engine, the ordering happens there, it's the
only place fast enough, and it's the only place that can keep undo safe.
