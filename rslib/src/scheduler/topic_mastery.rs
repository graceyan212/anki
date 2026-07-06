// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! GMAT fork (T2): per-topic mastery query.
//!
//! Topics are parsed from `notes.tags` using the shared taxonomy contract
//! (`content/taxonomy.md`):
//!
//! ```text
//! Section::Topic::Subtopic
//! ```
//!
//! A note carries exactly one such topic tag plus orthogonal `difficulty::*` /
//! `split::*` / `type::*` tags. We group review-eligible cards by a tag prefix
//! of configurable depth and report, per group: total cards, how many are
//! "mastered", the mastery fraction, and the average recall from the revlog.
//!
//! This is read-only: it does a single plain storage read, so it records no new
//! undo step and leaves the existing undo history intact (a stats query must
//! not cost the student their pending undo). The heavy lifting is a single
//! aggregate SQL query (`storage::topic_stats`); here we only parse tags and
//! fold the rows. The points-at-stake reorder it also powers mutates no cards,
//! so that path leaves the undo history untouched too.

use std::collections::BTreeMap;
use std::collections::HashMap;

use anki_proto::scheduler::DifficultyBand;
use anki_proto::scheduler::GetTopicBreakdownRequest;
use anki_proto::scheduler::GetTopicBreakdownResponse;
use anki_proto::scheduler::GetTopicMasteryStatsRequest;
use anki_proto::scheduler::GetTopicMasteryStatsResponse;
use anki_proto::scheduler::TopicDifficultyBreakdown;
use anki_proto::scheduler::TopicMasteryStat;
use fsrs::current_retrievability;
use fsrs::MemoryState;
use fsrs::FSRS5_DEFAULT_DECAY;

use crate::prelude::*;
use crate::scheduler::adaptive::note_difficulty;
use crate::storage::topic_stats::TopicCardRow;

/// Default interval (days) at/above which a card is "mastered" — the SM-2
/// "mature card" boundary, also where Anki paints reviews green.
const DEFAULT_MASTERED_INTERVAL_DAYS: u32 = 21;
/// Default FSRS retrievability threshold for the alternate mastery path.
const DEFAULT_MASTERED_RETRIEVABILITY: f32 = 0.9;
/// The tag-prefix depth used by the taxonomy's mastery contract
/// (`Section::Topic`), used when the caller does not specify one.
const DEFAULT_TOPIC_DEPTH: u32 = 2;

/// Running totals for a single topic group while folding rows.
#[derive(Default)]
struct Accumulator {
    total_cards: u32,
    mastered_cards: u32,
    passed_reviews: u64,
    total_reviews: u64,
}

/// The three difficulty bands a card's resolved 0–100 difficulty falls into.
/// Boundaries match the T4 contract: 0–33 easy, 34–66 medium, 67–100 hard —
/// splitting the coarse levels (20/50/80) cleanly and putting the band edges
/// halfway between them.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Band {
    Easy,
    Medium,
    Hard,
}

impl Band {
    /// Band a resolved 0–100 difficulty. Values are already clamped to [0,100]
    /// by `note_difficulty`; anything ≤33 is easy, 34–66 medium, ≥67 hard.
    fn from_difficulty(difficulty: f32) -> Band {
        if difficulty <= 33.0 {
            Band::Easy
        } else if difficulty <= 66.0 {
            Band::Medium
        } else {
            Band::Hard
        }
    }
}

/// Running per-band totals while folding rows. `correct`/`reviews` accumulate
/// revlog counts (correct = passes, i.e. ease > 1); accuracy is derived at the
/// end as `correct / reviews`.
#[derive(Default)]
struct BandAcc {
    total: u32,
    attempted: u32,
    correct: u32,
    reviews: u32,
}

impl BandAcc {
    fn into_proto(self) -> DifficultyBand {
        let accuracy = if self.reviews > 0 {
            self.correct as f64 / self.reviews as f64
        } else {
            0.0
        };
        DifficultyBand {
            total: self.total,
            attempted: self.attempted,
            correct: self.correct,
            accuracy,
        }
    }
}

/// Running totals for a single topic while building the per-difficulty
/// breakdown: the three bands plus a distinct-reviewed-card count.
#[derive(Default)]
struct TopicBreakdownAcc {
    reviewed_cards: u32,
    easy: BandAcc,
    medium: BandAcc,
    hard: BandAcc,
}

impl TopicBreakdownAcc {
    fn band_mut(&mut self, band: Band) -> &mut BandAcc {
        match band {
            Band::Easy => &mut self.easy,
            Band::Medium => &mut self.medium,
            Band::Hard => &mut self.hard,
        }
    }
}

impl Collection {
    /// Read-only per-topic mastery aggregate. See module docs.
    pub fn get_topic_mastery_stats(
        &mut self,
        req: GetTopicMasteryStatsRequest,
    ) -> Result<GetTopicMasteryStatsResponse> {
        let depth = if req.topic_depth == 0 {
            DEFAULT_TOPIC_DEPTH
        } else {
            req.topic_depth
        } as usize;
        let mastered_ivl = if req.mastered_interval_days == 0 {
            DEFAULT_MASTERED_INTERVAL_DAYS
        } else {
            req.mastered_interval_days
        };
        let mastered_retr = if req.mastered_retrievability <= 0.0 {
            DEFAULT_MASTERED_RETRIEVABILITY
        } else {
            req.mastered_retrievability
        };

        // Read-only: a plain storage read. It records no undo step AND leaves the
        // existing undo history intact, so opening the readiness dashboard never
        // costs the student their pending undo (same pattern as
        // points_at_stake_weights below).
        let rows = self.storage.all_topic_card_rows()?;

        let mut groups: BTreeMap<String, Accumulator> = BTreeMap::new();
        for row in &rows {
            let Some(topic) = topic_prefix(&row.tags, depth) else {
                continue;
            };
            let acc = groups.entry(topic).or_default();
            acc.total_cards += 1;
            if is_mastered(row, mastered_ivl, mastered_retr) {
                acc.mastered_cards += 1;
            }
            acc.passed_reviews += row.passed as u64;
            acc.total_reviews += row.total as u64;
        }

        let topics = groups
            .into_iter()
            .map(|(topic, acc)| {
                let mastery = if acc.total_cards > 0 {
                    acc.mastered_cards as f32 / acc.total_cards as f32
                } else {
                    0.0
                };
                let average_recall = if acc.total_reviews > 0 {
                    acc.passed_reviews as f32 / acc.total_reviews as f32
                } else {
                    0.0
                };
                TopicMasteryStat {
                    topic,
                    total_cards: acc.total_cards,
                    mastered_cards: acc.mastered_cards,
                    average_recall,
                    mastery,
                }
            })
            .collect();

        Ok(GetTopicMasteryStatsResponse { topics })
    }

    /// Read-only per-topic × per-difficulty-band breakdown. See module docs and
    /// [`Band`]. Bands each card by its resolved 0–100 difficulty
    /// (`note_difficulty`, shared with the adaptive selector) and reports, per
    /// band, how many cards there are, how many have been attempted (≥1 revlog
    /// entry), how many reviews were correct (ease > 1), and the resulting
    /// accuracy. `reviewed_cards` flags whether the student has hit the topic
    /// at all (distinct cards with ≥1 review, across every band).
    ///
    /// Same single-aggregate-read discipline as `get_topic_mastery_stats`: it
    /// runs one `all_topic_card_rows` pass, records no undo step, and leaves
    /// the existing undo history intact (a stats query must not cost the
    /// student their pending undo).
    pub fn get_topic_breakdown(
        &mut self,
        req: GetTopicBreakdownRequest,
    ) -> Result<GetTopicBreakdownResponse> {
        let depth = if req.topic_depth == 0 {
            DEFAULT_TOPIC_DEPTH
        } else {
            req.topic_depth
        } as usize;

        // Single read-only aggregate pass; see method docs (mirrors
        // get_topic_mastery_stats — no undo step, undo history untouched).
        let rows = self.storage.all_topic_card_rows()?;

        let mut groups: BTreeMap<String, TopicBreakdownAcc> = BTreeMap::new();
        for row in &rows {
            let Some(topic) = topic_prefix(&row.tags, depth) else {
                continue;
            };
            // A card with no difficulty tag cannot be banded, so it does not
            // belong in any easy/medium/hard bucket. It is intentionally
            // excluded (the breakdown is strictly per-band).
            let Some(difficulty) = note_difficulty(&row.tags) else {
                continue;
            };
            let acc = groups.entry(topic).or_default();
            let reviewed = row.total > 0;
            if reviewed {
                // Distinct reviewed card in this topic (across all bands).
                acc.reviewed_cards += 1;
            }
            let band = acc.band_mut(Band::from_difficulty(difficulty));
            band.total += 1;
            if reviewed {
                band.attempted += 1;
                band.correct += row.passed;
                band.reviews += row.total;
            }
        }

        let topics = groups
            .into_iter()
            .map(|(topic, acc)| TopicDifficultyBreakdown {
                topic,
                reviewed_cards: acc.reviewed_cards,
                easy: Some(acc.easy.into_proto()),
                medium: Some(acc.medium.into_proto()),
                hard: Some(acc.hard.into_proto()),
            })
            .collect();

        Ok(GetTopicBreakdownResponse { topics })
    }

    /// Compute a "points-at-stake" weight per note for the in-memory review
    /// reorder (see `queue::builder::sorting::sort_review`).
    ///
    /// weight = topic_weight × weakness, where:
    ///   * `topic_weight` is a tunable per-topic multiplier, UNIFORM (1.0) by
    ///     default — GMAT Focus weights its sections equally and there is no
    ///     defensible per-topic frequency source, so this currently reduces the
    ///     ordering to weakness alone ("weakest topics first"); and
    ///   * `weakness` ∈ [0,1] is how much the student is *not* yet on top of
    ///     the topic, derived from the topic's mastery and recall.
    ///
    /// Higher weight = study sooner. This is a single aggregate query (the same
    /// `all_topic_card_rows` pass), so it is cheap to call while building the
    /// queue. Notes with no topic tag get weight 0.0 and keep their DB order.
    pub(crate) fn points_at_stake_weights(&mut self) -> Result<HashMap<NoteId, f32>> {
        // Plain read: this runs while a queue is being built (possibly inside an
        // outer transaction), so we must not open/commit our own transaction or
        // clear study queues. (The standalone mastery RPC above does a plain read
        // for the same reason — a direct storage read is correct and
        // side-effect-free.)
        let rows = self.storage.all_topic_card_rows()?;

        // First pass: per-topic weakness, grouped at the contract depth.
        let mut acc: BTreeMap<String, Accumulator> = BTreeMap::new();
        for row in &rows {
            if let Some(topic) = topic_prefix(&row.tags, DEFAULT_TOPIC_DEPTH as usize) {
                let entry = acc.entry(topic).or_default();
                entry.total_cards += 1;
                if is_mastered(
                    row,
                    DEFAULT_MASTERED_INTERVAL_DAYS,
                    DEFAULT_MASTERED_RETRIEVABILITY,
                ) {
                    entry.mastered_cards += 1;
                }
                entry.passed_reviews += row.passed as u64;
                entry.total_reviews += row.total as u64;
            }
        }
        let weakness: HashMap<&str, f32> = acc
            .iter()
            .map(|(topic, a)| (topic.as_str(), topic_weakness(a)))
            .collect();

        // Second pass: assign each note its topic's points-at-stake weight.
        let mut weights = HashMap::with_capacity(rows.len());
        for row in &rows {
            let w = topic_prefix(&row.tags, DEFAULT_TOPIC_DEPTH as usize)
                .map(|topic| {
                    topic_weight(&topic) * weakness.get(topic.as_str()).copied().unwrap_or(0.0)
                })
                .unwrap_or(0.0);
            weights.insert(row.note_id, w);
        }
        Ok(weights)
    }
}

/// How "at stake" a topic is, independent of the student. This is a TUNABLE
/// hook for weighting topics by their share of the exam — but GMAT Focus scores
/// its three sections EQUALLY (per GMAC), and there is no published per-topic
/// question-frequency distribution we can defensibly cite, so every topic gets
/// a uniform 1.0. With uniform weights, `weight = topic_weight × weakness`
/// reduces to ordering by student weakness alone ("weakest topics first"). If a
/// defensible per-topic distribution ever exists, replace the body (or load it
/// from config) — but do NOT reintroduce guessed multipliers.
fn topic_weight(_topic: &str) -> f32 {
    1.0
}

/// Student weakness for a topic ∈ [0,1]: 1.0 = totally unstudied/failing,
/// 0.0 = fully mastered with perfect recall. Blends mastery fraction and recall
/// so a topic the student keeps failing outranks one they have simply not
/// reached yet. TUNABLE: the 0.6/0.4 blend.
fn topic_weakness(a: &Accumulator) -> f32 {
    let mastery = if a.total_cards > 0 {
        a.mastered_cards as f32 / a.total_cards as f32
    } else {
        0.0
    };
    // No reviews yet -> treat recall as unknown-but-poor (0.0) so brand-new
    // topics still register as weak.
    let recall = if a.total_reviews > 0 {
        a.passed_reviews as f32 / a.total_reviews as f32
    } else {
        0.0
    };
    let strength = 0.6 * mastery + 0.4 * recall;
    (1.0 - strength).clamp(0.0, 1.0)
}

/// Is a single card "mastered"? Interval at/above the threshold counts, and
/// (independently) an FSRS card whose retrievability at the threshold horizon
/// clears `mastered_retr` also counts.
fn is_mastered(row: &TopicCardRow, mastered_ivl: u32, mastered_retr: f32) -> bool {
    if row.interval >= mastered_ivl {
        return true;
    }
    if let Some(stability) = row.stability {
        // Project retrievability out to the mastery horizon. A card whose
        // memory is strong enough to still be recallable that far out is
        // mastered even if its current interval is shorter. Use the card's own
        // stored decay when present, falling back to FSRS5_DEFAULT_DECAY — the
        // same convention Anki uses everywhere else (see stats/card.rs,
        // stats/graphs/retrievability.rs, browser_table.rs, storage/sqlite.rs).
        let decay = row.decay.unwrap_or(FSRS5_DEFAULT_DECAY);
        retrievability(stability, mastered_ivl as f32, decay) >= mastered_retr
    } else {
        false
    }
}

/// FSRS forgetting curve: the probability of recall `days` after a review for a
/// card of the given `stability` and (positive) `decay`. Delegates to the
/// `fsrs` crate's `current_retrievability` so this matches Anki's scheduler
/// exactly rather than re-deriving a variant. `decay` is the value stored on
/// the card (positive; the crate applies `-decay` internally), so
/// `R(stability) == 0.9` and R decreases as `days` grows.
fn retrievability(stability: f32, days: f32, decay: f32) -> f32 {
    if stability <= 0.0 {
        return 0.0;
    }
    // Difficulty is unused by the forgetting curve; only stability matters.
    current_retrievability(
        MemoryState {
            stability,
            difficulty: 0.0,
        },
        days,
        decay,
    )
}

/// Extract a topic tag prefix at the requested `::`-separated `depth` from a
/// space-separated tags string. Returns the first tag that (a) contains `::`
/// and (b) is not an orthogonal `difficulty::` / `split::` / `type::` tag, then
/// truncates it to `depth` segments. `None` if no topic tag is present.
fn topic_prefix(tags: &str, depth: usize) -> Option<String> {
    tags.split_whitespace()
        .filter(|tag| tag.contains("::"))
        .find(|tag| !is_auxiliary_tag(tag))
        .map(|tag| {
            tag.split("::")
                .take(depth.max(1))
                .collect::<Vec<_>>()
                .join("::")
        })
}

/// A tag is a topic tag only when its level-1 namespace is a known GMAT section
/// (Quant / Verbal / DataInsights); everything else is auxiliary. Using a
/// section allowlist (rather than a blocklist of difficulty::/split::/type::)
/// means orthogonal metadata like `id::`, `kind::`, `of::` and `aidiff::` is
/// ignored too — a blocklist missed those, so a leading `id::` tag was mistaken
/// for the topic and cards were mis-grouped by id. Case-insensitive on the
/// segment before the first `::`.
fn is_auxiliary_tag(tag: &str) -> bool {
    let head = tag.split("::").next().unwrap_or("");
    !(head.eq_ignore_ascii_case("Quant")
        || head.eq_ignore_ascii_case("Verbal")
        || head.eq_ignore_ascii_case("DataInsights"))
}

#[cfg(test)]
mod test {
    use super::*;

    #[test]
    fn parses_topic_at_various_depths() {
        let tags = "Quant::Arithmetic::Percents difficulty::medium split::train";
        assert_eq!(
            topic_prefix(tags, 1).as_deref(),
            Some("Quant"),
            "depth 1 -> section"
        );
        assert_eq!(
            topic_prefix(tags, 2).as_deref(),
            Some("Quant::Arithmetic"),
            "depth 2 -> section::topic (the contract default)"
        );
        assert_eq!(
            topic_prefix(tags, 3).as_deref(),
            Some("Quant::Arithmetic::Percents"),
            "depth 3 -> full leaf"
        );
    }

    #[test]
    fn skips_auxiliary_tags_and_tagless_notes() {
        // leading auxiliary tag must not be mistaken for the topic
        assert_eq!(
            topic_prefix("difficulty::hard Verbal::CriticalReasoning::Assumption", 2).as_deref(),
            Some("Verbal::CriticalReasoning"),
        );
        // a two-segment DataInsights tag still groups sensibly at depth 2
        assert_eq!(
            topic_prefix("DataInsights::DataSufficiency split::gold", 2).as_deref(),
            Some("DataInsights::DataSufficiency"),
        );
        // id::/kind::/of:: are auxiliary too: with the real-deck tag ordering,
        // a leading id:: must NOT be picked as the topic (the bug this fixes).
        assert_eq!(
            topic_prefix(
                "difficulty::easy id::Q-PS-001 Quant::Arithmetic::Percents split::train",
                3
            )
            .as_deref(),
            Some("Quant::Arithmetic::Percents"),
        );
        assert_eq!(
            topic_prefix(
                "id::Q-PS-001-p1 kind::paraphrase of::Q-PS-001 Quant::Arithmetic::Percents",
                2
            )
            .as_deref(),
            Some("Quant::Arithmetic"),
        );
        // no topic tag at all
        assert_eq!(topic_prefix("leech marked", 2), None);
        assert_eq!(topic_prefix("difficulty::easy split::train", 2), None);
    }

    #[test]
    fn retrievability_decreases_with_time_and_clears_threshold() {
        // Fresh, high-stability card is recallable far out.
        let high = retrievability(1000.0, 21.0, FSRS5_DEFAULT_DECAY);
        // Low-stability card decays below 0.9 quickly.
        let low = retrievability(5.0, 21.0, FSRS5_DEFAULT_DECAY);
        assert!(high > 0.9, "high stability stays recallable: {high}");
        assert!(low < 0.9, "low stability drops off: {low}");
        assert!(high > low);
    }

    #[test]
    fn retrievability_matches_fsrs_convention() {
        // At days == stability, retrievability is exactly the 0.9 desired
        // retention, for both the fallback and a real per-card decay.
        for decay in [FSRS5_DEFAULT_DECAY, 0.1542_f32, 0.3_f32] {
            let at_stability = retrievability(10.0, 10.0, decay);
            assert!(
                (at_stability - 0.9).abs() < 1e-4,
                "R(stability) should be 0.9 for decay={decay}: {at_stability}"
            );
            // Curve must be strictly decreasing as days grow.
            let mut prev = retrievability(10.0, 0.0, decay);
            for days in [1.0, 5.0, 10.0, 21.0, 60.0, 200.0] {
                let r = retrievability(10.0, days, decay);
                assert!(
                    r < prev,
                    "R must decrease with days (decay={decay}, days={days}): {r} !< {prev}"
                );
                prev = r;
            }
        }
    }

    #[test]
    fn is_mastered_uses_stored_per_card_decay_not_default() {
        // A card whose retrievability at the horizon straddles the threshold
        // depending on which decay is used proves the stored decay is honoured.
        // In the far-out regime (days >> stability) the FSRS-6 decay (0.1542)
        // forgets more slowly than the FSRS-5 default (0.5), so the same
        // (stability, horizon) clears the bar under the stored decay but not
        // under the fallback.
        let stability = 7.0;
        let horizon_days = 21;
        let threshold = 0.8;

        // Sanity: the two conventions genuinely disagree for this card.
        let with_stored = retrievability(stability, horizon_days as f32, 0.1542);
        let with_default = retrievability(stability, horizon_days as f32, FSRS5_DEFAULT_DECAY);
        assert!(
            with_stored >= threshold && with_default < threshold,
            "test setup must straddle the threshold: stored={with_stored} default={with_default}"
        );

        // Card carrying the real FSRS-6 decay -> mastered via retrievability.
        let stored = TopicCardRow {
            note_id: NoteId(1),
            tags: String::new(),
            interval: 3,
            stability: Some(stability),
            decay: Some(0.1542),
            passed: 0,
            total: 0,
            last_review_ms: None,
        };
        assert!(
            is_mastered(&stored, horizon_days, threshold),
            "card with stored decay must use it (not the default) and be mastered"
        );

        // Same card but with no stored decay -> falls back to the default and
        // is NOT mastered. This is the regression the fix addresses: previously
        // both branches hardcoded the default and ignored card.decay.
        let no_decay = TopicCardRow {
            decay: None,
            ..stored
        };
        assert!(
            !is_mastered(&no_decay, horizon_days, threshold),
            "card without stored decay falls back to FSRS5_DEFAULT_DECAY"
        );
    }

    #[test]
    fn mastery_via_interval_or_fsrs() {
        // Mastered purely by interval, no FSRS state.
        let by_ivl = TopicCardRow {
            note_id: NoteId(1),
            tags: String::new(),
            interval: 30,
            stability: None,
            decay: None,
            passed: 0,
            total: 0,
            last_review_ms: None,
        };
        assert!(is_mastered(&by_ivl, 21, 0.9));

        // Short interval, but strong FSRS memory -> mastered via retrievability.
        let by_fsrs = TopicCardRow {
            note_id: NoteId(2),
            tags: String::new(),
            interval: 3,
            stability: Some(1000.0),
            decay: None,
            passed: 0,
            total: 0,
            last_review_ms: None,
        };
        assert!(is_mastered(&by_fsrs, 21, 0.9));

        // Short interval and weak/no memory -> not mastered.
        let weak = TopicCardRow {
            note_id: NoteId(3),
            tags: String::new(),
            interval: 3,
            stability: Some(2.0),
            decay: None,
            passed: 0,
            total: 0,
            last_review_ms: None,
        };
        assert!(!is_mastered(&weak, 21, 0.9));
    }

    // --- end-to-end tests against a real Collection ---------------------------

    use crate::card::CardQueue;
    use crate::card::CardType;

    /// Add a single review card with the given tags and interval to the default
    /// deck, returning its note id.
    fn add_review_card(col: &mut Collection, tags: &[&str], interval: u32) -> NoteId {
        let nt = col.get_notetype_by_name("Basic").unwrap().unwrap();
        let mut note = nt.new_note();
        note.set_field(0, "front").unwrap();
        note.tags = tags.iter().map(|s| s.to_string()).collect();
        col.add_note(&mut note, DeckId(1)).unwrap();

        let mut card = col
            .storage
            .get_card_by_ordinal(note.id, 0)
            .unwrap()
            .unwrap();
        card.interval = interval;
        card.due = 0;
        card.ctype = CardType::Review;
        card.queue = CardQueue::Review;
        col.update_cards_maybe_undoable(vec![card], false).unwrap();
        note.id
    }

    #[test]
    fn mastery_query_aggregates_by_topic() {
        let mut col = Collection::new();
        // Quant::Arithmetic: one mature (mastered), one young.
        add_review_card(
            &mut col,
            &["Quant::Arithmetic::Percents", "split::train"],
            40,
        );
        add_review_card(&mut col, &["Quant::Arithmetic::FractionsDecimals"], 5);
        // Verbal::CriticalReasoning: one young only.
        add_review_card(&mut col, &["Verbal::CriticalReasoning::Assumption"], 2);
        // An untagged card must be ignored by the topic grouping.
        add_review_card(&mut col, &[], 99);

        let resp = col
            .get_topic_mastery_stats(GetTopicMasteryStatsRequest {
                topic_depth: 2,
                mastered_interval_days: 21,
                mastered_retrievability: 0.9,
            })
            .unwrap();

        // Two topic groups, untagged card excluded.
        assert_eq!(resp.topics.len(), 2, "topics: {:?}", resp.topics);

        let quant = resp
            .topics
            .iter()
            .find(|t| t.topic == "Quant::Arithmetic")
            .expect("Quant::Arithmetic group present");
        assert_eq!(quant.total_cards, 2);
        assert_eq!(quant.mastered_cards, 1, "only the ivl=40 card is mastered");
        assert!((quant.mastery - 0.5).abs() < 1e-6);

        let verbal = resp
            .topics
            .iter()
            .find(|t| t.topic == "Verbal::CriticalReasoning")
            .expect("Verbal::CriticalReasoning group present");
        assert_eq!(verbal.total_cards, 1);
        assert_eq!(verbal.mastered_cards, 0);
    }

    #[test]
    fn mastery_query_and_queue_build_leave_undo_intact() {
        let mut col = Collection::new();
        let nid = add_review_card(&mut col, &["Quant::Arithmetic::Percents"], 10);

        // Perform a genuine undoable op so there is an undo entry to protect.
        let card = col.storage.get_card_by_ordinal(nid, 0).unwrap().unwrap();
        col.transact(Op::UpdateCard, |col| {
            col.get_and_update_card(card.id, |c| {
                c.interval = 12;
                Ok(())
            })
            .unwrap();
            Ok(())
        })
        .unwrap();
        assert_eq!(col.can_undo(), Some(&Op::UpdateCard));

        // The points-at-stake reorder is the load-bearing claim: building the
        // queue runs points_at_stake_weights + sort_review, which mutate no
        // cards and open no transaction, so the undo entry must survive intact
        // and remain replayable.
        col.build_queues(DeckId(1)).unwrap();
        assert_eq!(
            col.can_undo(),
            Some(&Op::UpdateCard),
            "queue build (points-at-stake reorder) must not clear undo"
        );
        col.undo().unwrap();
        assert_eq!(
            col.storage.get_card(card.id).unwrap().unwrap().interval,
            10,
            "undo restored the pre-op interval after the reorder"
        );

        // The read-only mastery query is a plain storage read, so it must NOT
        // clear the undo history: opening the readiness dashboard should never
        // cost the student their pending undo. Establish a fresh undoable op, run
        // the query, and confirm the op still stands and remains replayable.
        col.transact(Op::UpdateCard, |col| {
            col.get_and_update_card(card.id, |c| {
                c.interval = 99;
                Ok(())
            })
            .unwrap();
            Ok(())
        })
        .unwrap();
        assert_eq!(col.can_undo(), Some(&Op::UpdateCard));
        let _ = col
            .get_topic_mastery_stats(GetTopicMasteryStatsRequest::default())
            .unwrap();
        assert_eq!(
            col.can_undo(),
            Some(&Op::UpdateCard),
            "the mastery query (plain read) must not clear the undo history"
        );
        col.undo().unwrap();
        assert_eq!(col.storage.get_card(card.id).unwrap().unwrap().interval, 10);
    }

    /// Record genuine revlog entries for a note's card so the aggregate query
    /// sees `passed`/`total`. A "pass"/"correct" is ease > 1 (Hard/Good/Easy),
    /// a miss is ease 1 (Again); both count toward `total`. Mirrors the helper
    /// in `adaptive.rs` — a plain, non-undoable revlog write.
    fn record_reviews(col: &mut Collection, nid: NoteId, correct: u32, wrong: u32) {
        use crate::revlog::RevlogEntry;
        use crate::revlog::RevlogReviewKind;
        let card = col.storage.get_card_by_ordinal(nid, 0).unwrap().unwrap();
        for i in 0..(correct + wrong) {
            // ease 3 (Good) is a pass; ease 1 (Again) is a miss.
            let ease = if i < correct { 3 } else { 1 };
            let entry = RevlogEntry {
                id: crate::revlog::RevlogId(TimestampMillis::now().0 + i as i64),
                cid: card.id,
                usn: Usn(-1),
                button_chosen: ease,
                interval: 10,
                last_interval: 5,
                ease_factor: 2500,
                taken_millis: 1000,
                review_kind: RevlogReviewKind::Review,
            };
            col.storage.add_revlog_entry(&entry, true).unwrap();
        }
    }

    #[test]
    fn breakdown_bands_by_difficulty_with_revlog_counts() {
        let mut col = Collection::new();
        // Quant::Arithmetic with a card in each band, plus revlog answers.
        // Easy (aidiff::10): 3 correct, 1 wrong -> attempted, accuracy 0.75.
        let easy = add_review_card(&mut col, &["Quant::Arithmetic::Percents", "aidiff::10"], 5);
        record_reviews(&mut col, easy, 3, 1);
        // Medium (coarse difficulty::medium -> 50): 1 correct, 1 wrong.
        let medium = add_review_card(
            &mut col,
            &["Quant::Arithmetic::FractionsDecimals", "difficulty::medium"],
            5,
        );
        record_reviews(&mut col, medium, 1, 1);
        // Hard (aidiff::90): all correct.
        let hard = add_review_card(&mut col, &["Quant::Arithmetic::Roots", "aidiff::90"], 5);
        record_reviews(&mut col, hard, 4, 0);
        // A second hard card with NO reviews -> counts in band total but not
        // attempted, and does not raise reviewed_cards.
        add_review_card(&mut col, &["Quant::Arithmetic::Roots", "aidiff::80"], 5);

        let resp = col
            .get_topic_breakdown(GetTopicBreakdownRequest { topic_depth: 2 })
            .unwrap();

        assert_eq!(resp.topics.len(), 1, "topics: {:?}", resp.topics);
        let t = &resp.topics[0];
        assert_eq!(t.topic, "Quant::Arithmetic");
        // 3 distinct reviewed cards (easy, medium, one hard); the second hard
        // card was never reviewed.
        assert_eq!(t.reviewed_cards, 3);

        let e = t.easy.as_ref().unwrap();
        assert_eq!(e.total, 1);
        assert_eq!(e.attempted, 1);
        assert_eq!(e.correct, 3);
        assert!(
            (e.accuracy - 0.75).abs() < 1e-9,
            "easy accuracy {}",
            e.accuracy
        );

        let m = t.medium.as_ref().unwrap();
        assert_eq!(m.total, 1);
        assert_eq!(m.attempted, 1);
        assert_eq!(m.correct, 1);
        assert!(
            (m.accuracy - 0.5).abs() < 1e-9,
            "medium accuracy {}",
            m.accuracy
        );

        let h = t.hard.as_ref().unwrap();
        assert_eq!(h.total, 2, "two hard cards (aidiff 90 and 80)");
        assert_eq!(h.attempted, 1, "only the reviewed hard card is attempted");
        assert_eq!(h.correct, 4);
        assert!(
            (h.accuracy - 1.0).abs() < 1e-9,
            "hard accuracy {}",
            h.accuracy
        );
    }

    #[test]
    fn breakdown_topic_with_zero_reviews() {
        let mut col = Collection::new();
        // Two banded cards, neither reviewed.
        add_review_card(
            &mut col,
            &["Verbal::CriticalReasoning::Assumption", "aidiff::20"],
            5,
        );
        add_review_card(
            &mut col,
            &["Verbal::CriticalReasoning::Weaken", "difficulty::hard"],
            5,
        );

        let resp = col
            .get_topic_breakdown(GetTopicBreakdownRequest { topic_depth: 2 })
            .unwrap();

        let t = resp
            .topics
            .iter()
            .find(|t| t.topic == "Verbal::CriticalReasoning")
            .expect("group present");
        // No card reviewed -> the "have you hit this topic" flag stays 0.
        assert_eq!(t.reviewed_cards, 0);
        let e = t.easy.as_ref().unwrap();
        let h = t.hard.as_ref().unwrap();
        // Cards are still counted in their band totals...
        assert_eq!(e.total, 1, "aidiff::20 lands in easy");
        assert_eq!(h.total, 1, "difficulty::hard lands in hard");
        // ...but nothing is attempted and accuracy stays 0.
        for band in [
            t.easy.as_ref().unwrap(),
            t.medium.as_ref().unwrap(),
            t.hard.as_ref().unwrap(),
        ] {
            assert_eq!(band.attempted, 0);
            assert_eq!(band.correct, 0);
            assert_eq!(band.accuracy, 0.0);
        }
    }

    #[test]
    fn breakdown_band_boundaries_and_untagged_difficulty_excluded() {
        let mut col = Collection::new();
        // Boundary cases: 33 -> easy, 34 -> medium, 66 -> medium, 67 -> hard.
        add_review_card(&mut col, &["Quant::Algebra::A", "aidiff::33"], 5);
        add_review_card(&mut col, &["Quant::Algebra::B", "aidiff::34"], 5);
        add_review_card(&mut col, &["Quant::Algebra::C", "aidiff::66"], 5);
        add_review_card(&mut col, &["Quant::Algebra::D", "aidiff::67"], 5);
        // A card with a topic but NO difficulty tag must be excluded from every
        // band (it cannot be banded).
        add_review_card(&mut col, &["Quant::Algebra::E"], 5);

        let resp = col
            .get_topic_breakdown(GetTopicBreakdownRequest { topic_depth: 2 })
            .unwrap();
        let t = resp
            .topics
            .iter()
            .find(|t| t.topic == "Quant::Algebra")
            .expect("group present");
        assert_eq!(t.easy.as_ref().unwrap().total, 1, "33 -> easy");
        assert_eq!(t.medium.as_ref().unwrap().total, 2, "34 and 66 -> medium");
        assert_eq!(t.hard.as_ref().unwrap().total, 1, "67 -> hard");
        // The untagged-difficulty card contributes to no band (1+2+1 == 4).
        let banded = t.easy.as_ref().unwrap().total
            + t.medium.as_ref().unwrap().total
            + t.hard.as_ref().unwrap().total;
        assert_eq!(
            banded, 4,
            "the no-difficulty card is excluded from all bands"
        );
    }

    #[test]
    fn breakdown_query_leaves_undo_intact() {
        let mut col = Collection::new();
        let nid = add_review_card(&mut col, &["Quant::Arithmetic::Percents", "aidiff::50"], 10);
        record_reviews(&mut col, nid, 2, 1);

        // Establish a genuine undoable op to protect.
        let card = col.storage.get_card_by_ordinal(nid, 0).unwrap().unwrap();
        col.transact(Op::UpdateCard, |col| {
            col.get_and_update_card(card.id, |c| {
                c.interval = 42;
                Ok(())
            })
            .unwrap();
            Ok(())
        })
        .unwrap();
        assert_eq!(col.can_undo(), Some(&Op::UpdateCard));

        // The breakdown is a plain read: it must NOT clear the undo history.
        let _ = col
            .get_topic_breakdown(GetTopicBreakdownRequest::default())
            .unwrap();
        assert_eq!(
            col.can_undo(),
            Some(&Op::UpdateCard),
            "the breakdown query (plain read) must not clear the undo history"
        );
        col.undo().unwrap();
        assert_eq!(
            col.storage.get_card(card.id).unwrap().unwrap().interval,
            10,
            "undo restored the pre-op interval after the breakdown query"
        );
    }

    #[test]
    fn points_at_stake_weights_rank_weaker_topics_first() {
        let mut col = Collection::new();
        // Strong topic: fully mastered -> low weakness -> low weight.
        let strong = add_review_card(&mut col, &["Quant::Algebra::LinearEquations"], 60);
        // Weak topic: young, unmastered card.
        let weak = add_review_card(&mut col, &["DataInsights::DataSufficiency"], 1);

        // topic_weight is uniform (1.0), so weight == weakness: the weaker topic
        // must rank first on the grounded weakness signal alone.
        let weights = col.points_at_stake_weights().unwrap();
        let strong_w = weights[&strong];
        let weak_w = weights[&weak];
        assert!(
            weak_w > strong_w,
            "weaker topic should outrank the mastered one: weak={weak_w} strong={strong_w}"
        );
    }
}
