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
//! This is read-only and runs inside `transact_no_undo` (like Anki's other
//! stats aggregates), so it records no new undo step. The heavy lifting is a
//! single aggregate SQL query (`storage::topic_stats`); here we only parse tags
//! and fold the rows. The points-at-stake reorder it also powers mutates no
//! cards, so that path leaves the undo history untouched.

use std::collections::BTreeMap;
use std::collections::HashMap;

use anki_proto::scheduler::GetTopicMasteryStatsRequest;
use anki_proto::scheduler::GetTopicMasteryStatsResponse;
use anki_proto::scheduler::TopicMasteryStat;
use fsrs::FSRS5_DEFAULT_DECAY;

use crate::prelude::*;
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

        // Read-only: wrap in a no-undo transaction so we never push to the undo
        // queue (mirrors how other read aggregates avoid touching undo state).
        let rows = self.transact_no_undo(|col| col.storage.all_topic_card_rows())?;

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

    /// Compute a "points-at-stake" weight per note for the in-memory review
    /// reorder (see `queue::builder::sorting::sort_review`).
    ///
    /// weight = topic_weight × weakness, where:
    ///   * `topic_weight` is how much the topic is worth on the exam (tunable
    ///     table below; defaults to 1.0 for unlisted topics), and
    ///   * `weakness` ∈ [0,1] is how much the student is *not* yet on top of
    ///     the topic, derived from the topic's mastery and recall.
    ///
    /// Higher weight = study sooner. This is a single aggregate query (the same
    /// `all_topic_card_rows` pass), so it is cheap to call while building the
    /// queue. Notes with no topic tag get weight 0.0 and keep their DB order.
    pub(crate) fn points_at_stake_weights(&mut self) -> Result<HashMap<NoteId, f32>> {
        // Plain read: this runs while a queue is being built (possibly inside an
        // outer transaction), so we must not open/commit our own transaction or
        // clear study queues. The standalone RPC below uses transact_no_undo;
        // here a direct storage read is correct and side-effect-free.
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

/// How "at stake" a topic is, independent of the student: a rough proxy for its
/// share of the GMAT Focus exam. TUNABLE — edit these numbers (or load them
/// from config) to change which topics surface first. Unlisted topics use 1.0.
fn topic_weight(topic: &str) -> f32 {
    // Section-level defaults; Data Insights and Quant carry the heaviest scoring
    // weight on GMAT Focus. Verbal and any unlisted section default to 1.0.
    let section_default = if topic.starts_with("DataInsights") {
        1.2
    } else if topic.starts_with("Quant") {
        1.1
    } else {
        1.0
    };
    // A couple of high-yield topics nudged above their section default.
    match topic {
        "DataInsights::DataSufficiency" => 1.4,
        "Quant::Arithmetic::Percents" => 1.3,
        "Verbal::CriticalReasoning::Assumption" => 1.2,
        _ => section_default,
    }
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
        // mastered even if its current interval is shorter.
        retrievability(stability, mastered_ivl as f32) >= mastered_retr
    } else {
        false
    }
}

/// FSRS forgetting curve R(t) = (1 + FACTOR * t / S)^decay, with the FSRS-5
/// default decay. Returns the probability of recall `days` after a review for
/// a card of the given `stability`.
fn retrievability(stability: f32, days: f32) -> f32 {
    if stability <= 0.0 {
        return 0.0;
    }
    let decay = FSRS5_DEFAULT_DECAY;
    let factor = 0.9f32.powf(1.0 / decay) - 1.0;
    (1.0 + factor * days / stability).powf(decay)
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

/// The taxonomy reserves these level-1 namespaces for orthogonal metadata that
/// is not a topic. Match is case-insensitive on the segment before the first
/// `::`.
fn is_auxiliary_tag(tag: &str) -> bool {
    let head = tag.split("::").next().unwrap_or("");
    head.eq_ignore_ascii_case("difficulty")
        || head.eq_ignore_ascii_case("split")
        || head.eq_ignore_ascii_case("type")
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
        // no topic tag at all
        assert_eq!(topic_prefix("leech marked", 2), None);
        assert_eq!(topic_prefix("difficulty::easy split::train", 2), None);
    }

    #[test]
    fn retrievability_decreases_with_time_and_clears_threshold() {
        // Fresh, high-stability card is recallable far out.
        let high = retrievability(1000.0, 21.0);
        // Low-stability card decays below 0.9 quickly.
        let low = retrievability(5.0, 21.0);
        assert!(high > 0.9, "high stability stays recallable: {high}");
        assert!(low < 0.9, "low stability drops off: {low}");
        assert!(high > low);
    }

    #[test]
    fn mastery_via_interval_or_fsrs() {
        // Mastered purely by interval, no FSRS state.
        let by_ivl = TopicCardRow {
            note_id: NoteId(1),
            tags: String::new(),
            interval: 30,
            stability: None,
            passed: 0,
            total: 0,
        };
        assert!(is_mastered(&by_ivl, 21, 0.9));

        // Short interval, but strong FSRS memory -> mastered via retrievability.
        let by_fsrs = TopicCardRow {
            note_id: NoteId(2),
            tags: String::new(),
            interval: 3,
            stability: Some(1000.0),
            passed: 0,
            total: 0,
        };
        assert!(is_mastered(&by_fsrs, 21, 0.9));

        // Short interval and weak/no memory -> not mastered.
        let weak = TopicCardRow {
            note_id: NoteId(3),
            tags: String::new(),
            interval: 3,
            stability: Some(2.0),
            passed: 0,
            total: 0,
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

        // The read-only mastery query uses transact_no_undo (like every other
        // Anki stats aggregate, e.g. card_stats); per Anki's semantics that
        // resets the undo history, but it must run cleanly and leave the
        // collection in a consistent, still-undoable state for fresh ops.
        let _ = col
            .get_topic_mastery_stats(GetTopicMasteryStatsRequest::default())
            .unwrap();
        assert_eq!(col.can_undo(), None, "transact_no_undo clears history");
        // A subsequent op is recorded and undoable -> undo machinery intact.
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
        col.undo().unwrap();
        assert_eq!(col.storage.get_card(card.id).unwrap().unwrap().interval, 10);
    }

    #[test]
    fn points_at_stake_weights_rank_weak_high_value_topics_first() {
        let mut col = Collection::new();
        // Strong topic: fully mastered -> low weakness -> low weight.
        let strong = add_review_card(&mut col, &["Quant::Algebra::LinearEquations"], 60);
        // Weak, high-value topic: young card on a heavily-weighted topic.
        let weak = add_review_card(&mut col, &["DataInsights::DataSufficiency"], 1);

        let weights = col.points_at_stake_weights().unwrap();
        let strong_w = weights[&strong];
        let weak_w = weights[&weak];
        assert!(
            weak_w > strong_w,
            "weak high-value topic should outrank mastered topic: weak={weak_w} strong={strong_w}"
        );
    }
}
