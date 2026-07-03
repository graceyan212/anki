// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! GMAT fork (T3): the three scores — memory, performance, readiness.
//!
//! Read-only: computed from a plain storage read (FSRS memory state, revlog
//! tallies, and difficulty/topic tags), with NO transaction and NO card
//! mutation — same discipline as `topic_mastery`, so the student's undo history
//! stays intact (asserted by a test).
//!
//! The three are deliberately distinct measurements:
//!   * memory      — current FSRS recall probability (can they remember it now?)
//!   * performance — Rasch/1PL ability vs. item difficulty (can they answer a
//!                   new, exam-style question?)
//!   * readiness   — performance mapped to the GMAT 205-805 scale, discounted by
//!                   topic coverage, with a confidence band.
//!
//! Each score carries a range and an independent give-up rule (abstains with a
//! `missing` list until it has enough of its own data).

use anki_proto::scheduler::GetGmatScoresRequest;
use anki_proto::scheduler::GmatScores;
use anki_proto::scheduler::ScoreValue;

use crate::prelude::*;

impl Collection {
    /// Compute the three GMAT scores. Read-only (no transaction, no card
    /// mutation), so opening a score view never costs the student their undo.
    pub fn get_gmat_scores(&mut self, _req: GetGmatScoresRequest) -> Result<GmatScores> {
        // STUB (Task 1): everything abstains so the RPC -> bridge -> UI pipe can
        // be proven end-to-end before the real math lands (Tasks 3-7).
        Ok(GmatScores {
            memory: Some(abstain("stub: memory score not yet computed")),
            performance: Some(abstain("stub: performance score not yet computed")),
            readiness: Some(abstain("stub: readiness score not yet computed")),
        })
    }
}

/// An abstaining `ScoreValue` (give-up state) that says what is still missing.
fn abstain(missing: &str) -> ScoreValue {
    ScoreValue {
        abstained: true,
        missing: vec![missing.to_string()],
        ..Default::default()
    }
}
