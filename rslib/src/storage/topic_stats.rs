// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! GMAT fork (T2): storage-level aggregate feeding the per-topic mastery query.
//!
//! This exposes a single SQL pass that returns one row per review-eligible
//! card (interval, FSRS stability, and per-card revlog pass/total counts). The
//! topic grouping itself happens in Rust (see `scheduler::topic_mastery`),
//! because parsing the `Section::Topic::Subtopic` tag out of a space-separated
//! tags string is far cleaner in Rust than in SQL. Crucially there is no
//! per-card query loop: the revlog totals are computed once in a grouped
//! sub-select, so this stays fast on large (~50k card) collections.

use super::SqliteStorage;
use crate::error::Result;
use crate::notes::NoteId;

/// One review-eligible card's raw inputs for the mastery aggregate.
#[derive(Debug, Clone)]
pub(crate) struct TopicCardRow {
    /// The card's note id (used to weight the in-memory review reorder).
    pub note_id: NoteId,
    /// The note's full, space-separated tags string.
    pub tags: String,
    /// Current interval in days.
    pub interval: u32,
    /// FSRS stability (days) from the cards.data JSON blob, if present.
    pub stability: Option<f32>,
    /// FSRS per-card decay from the cards.data JSON blob, if present. Stored
    /// positive (matching Anki's convention); `None` for legacy/FSRS-5 cards,
    /// in which case callers fall back to `FSRS5_DEFAULT_DECAY`.
    pub decay: Option<f32>,
    /// Number of passed reviews (ease >= 2) in the revlog.
    pub passed: u32,
    /// Total genuine reviews in the revlog.
    pub total: u32,
}

impl SqliteStorage {
    /// Return one [`TopicCardRow`] per review-eligible card (non-new,
    /// non-suspended) in the whole collection, in a single aggregate query.
    pub(crate) fn all_topic_card_rows(&self) -> Result<Vec<TopicCardRow>> {
        self.db
            .prepare_cached(include_str!("topic_stats.sql"))?
            .query_and_then([], |row| -> Result<TopicCardRow> {
                Ok(TopicCardRow {
                    note_id: row.get(0)?,
                    tags: row.get(1)?,
                    interval: row.get::<_, i64>(2)?.max(0) as u32,
                    stability: row.get(3)?,
                    decay: row.get(4)?,
                    passed: row.get::<_, i64>(5)?.max(0) as u32,
                    total: row.get::<_, i64>(6)?.max(0) as u32,
                })
            })?
            .collect()
    }
}
