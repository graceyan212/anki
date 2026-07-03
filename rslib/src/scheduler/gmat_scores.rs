// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! GMAT fork (T3): the three scores — memory, performance, readiness.
//!
//! Read-only: computed from a single plain storage read (`all_topic_card_rows`,
//! carrying FSRS stability/decay, revlog pass/total tallies, last-review time,
//! and tags), with NO transaction and NO card mutation — same discipline as
//! `topic_mastery`, so the student's undo history stays intact (asserted by a
//! test).
//!
//! The three are deliberately distinct measurements:
//!   * memory      — current FSRS recall probability across studied cards
//!                   (can they remember it right now?).
//!   * performance — Rasch/1PL ability θ estimated from answer history vs. each
//!                   item's difficulty (can they answer a new, exam-style
//!                   question?). Difficulty comes from the AI `aidiff::NN` tag
//!                   when present, else the coarse `difficulty::` tag.
//!   * readiness   — θ mapped onto the GMAT 205–805 scale, discounted by topic
//!                   coverage, with a confidence band (what would they score?).
//!
//! Each score carries a range and an independent give-up rule: it abstains with
//! a `missing` list until it has enough of its own data.

use fsrs::current_retrievability;
use fsrs::MemoryState;
use fsrs::FSRS5_DEFAULT_DECAY;

use anki_proto::scheduler::GetGmatScoresRequest;
use anki_proto::scheduler::GmatScores;
use anki_proto::scheduler::ScoreValue;

use crate::prelude::*;
use crate::storage::topic_stats::TopicCardRow;

// --- Give-up thresholds (spec: "set a clear line and state it"). ---
const MIN_MEMORY_REVIEWS: u32 = 30;
const MIN_PERF_ANSWERS: u32 = 20;
const MIN_READINESS_REVIEWS: u32 = 200;
const MIN_READINESS_COVERAGE: f64 = 0.50;

const MS_PER_DAY: f64 = 86_400_000.0;
const GMAT_MIN: f64 = 205.0;
const GMAT_SPAN: f64 = 600.0; // 805 - 205
/// 4-choice guess baseline: expected proportion correct on material the student
/// has not studied (used to discount readiness by coverage).
const GUESS_FLOOR: f64 = 0.25;
/// Maps a 0–100 difficulty onto ±2 logits (SCALE = 4).
const DIFFICULTY_SCALE: f64 = 4.0;

/// GMAT Focus coverage outline (Section::Topic[::Subtopic]). Mirrors
/// `pylib/anki/gmat_readiness.COVERAGE_OUTLINE` so the engine and the desktop
/// dashboard agree on what "covered" means.
const OUTLINE_TOPICS: &[&str] = &[
    "Quant::Arithmetic::PropertiesOfIntegers",
    "Quant::Arithmetic::FractionsDecimals",
    "Quant::Arithmetic::Percents",
    "Quant::Arithmetic::RatiosProportions",
    "Quant::Arithmetic::PowersRoots",
    "Quant::Arithmetic::Statistics",
    "Quant::Algebra::LinearEquations",
    "Quant::Algebra::Quadratics",
    "Quant::Algebra::Inequalities",
    "Quant::Algebra::FunctionsExponents",
    "Quant::WordProblems::RateWorkMixtureInterest",
    "Verbal::CriticalReasoning::Assumption",
    "Verbal::CriticalReasoning::Strengthen",
    "Verbal::CriticalReasoning::Weaken",
    "Verbal::CriticalReasoning::Inference",
    "Verbal::CriticalReasoning::Evaluate",
    "Verbal::CriticalReasoning::Paradox",
    "Verbal::CriticalReasoning::Boldface",
    "Verbal::ReadingComprehension::MainIdea",
    "Verbal::ReadingComprehension::Detail",
    "Verbal::ReadingComprehension::Inference",
    "Verbal::ReadingComprehension::Function",
    "Verbal::ReadingComprehension::Tone",
    "DataInsights::DataSufficiency",
    "DataInsights::MultiSourceReasoning",
    "DataInsights::TableAnalysis",
    "DataInsights::GraphicsInterpretation",
    "DataInsights::TwoPartAnalysis",
];

impl Collection {
    /// Compute the three GMAT scores. Read-only (no transaction, no card
    /// mutation), so opening a score view never costs the student their undo.
    pub fn get_gmat_scores(&mut self, _req: GetGmatScoresRequest) -> Result<GmatScores> {
        let rows = self.storage.all_topic_card_rows()?;
        // Epoch-ms "now" (same clock as revlog ids), matching the bridge.
        let now_ms = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis() as i64)
            .unwrap_or(0);
        Ok(compute_scores(&rows, now_ms))
    }
}

/// Pure scoring over the storage rows — testable without a Collection.
fn compute_scores(rows: &[TopicCardRow], now_ms: i64) -> GmatScores {
    let perf = PerfEstimate::from_rows(rows);
    GmatScores {
        memory: Some(memory_score(rows, now_ms)),
        performance: Some(perf.to_score_value()),
        readiness: Some(readiness_score(rows, &perf)),
    }
}

// ---------------------------------------------------------------------------
// Memory: current FSRS recall probability across studied cards.
// ---------------------------------------------------------------------------

fn memory_score(rows: &[TopicCardRow], now_ms: i64) -> ScoreValue {
    let total_reviews: u32 = rows.iter().map(|r| r.total).sum();
    if total_reviews < MIN_MEMORY_REVIEWS {
        return abstain(format!(
            "Answer at least {MIN_MEMORY_REVIEWS} cards to estimate memory (have {total_reviews})."
        ));
    }
    let mut recalls: Vec<f64> = Vec::new();
    for r in rows {
        let (Some(stability), Some(last)) = (r.stability, r.last_review_ms) else {
            continue;
        };
        if stability <= 0.0 {
            continue;
        }
        let days = ((now_ms - last) as f64 / MS_PER_DAY).max(0.0) as f32;
        let decay = r.decay.unwrap_or(FSRS5_DEFAULT_DECAY);
        let rec = current_retrievability(
            MemoryState {
                stability,
                difficulty: 0.0,
            },
            days,
            decay,
        ) as f64;
        recalls.push(rec);
    }
    if recalls.is_empty() {
        return abstain("No cards with an FSRS memory state yet.".to_string());
    }
    let (mean, sd) = mean_sd(&recalls);
    let score = mean * 100.0;
    let half = sd * 100.0; // ±1 SD of per-card recall = the honest spread
    scored(
        score,
        (score - half).max(0.0),
        (score + half).min(100.0),
        "pct",
        "",
        vec![format!(
            "current FSRS recall across {} studied cards",
            recalls.len()
        )],
    )
}

// ---------------------------------------------------------------------------
// Performance: Rasch/1PL ability θ from answers joined with item difficulty.
// ---------------------------------------------------------------------------

struct PerfEstimate {
    theta: f64,
    se: f64,
    n_answers: u32,
    ok: bool,
    missing: String,
}

impl PerfEstimate {
    fn from_rows(rows: &[TopicCardRow]) -> Self {
        // (difficulty logit b, correct c, total n) per answered card.
        let items: Vec<(f64, u32, u32)> = rows
            .iter()
            .filter(|r| r.total > 0)
            .map(|r| (difficulty_logit(&r.tags), r.passed, r.total))
            .collect();
        let n_answers: u32 = items.iter().map(|(_, _, n)| *n).sum();
        let n_correct: u32 = items.iter().map(|(_, c, _)| *c).sum();

        if n_answers < MIN_PERF_ANSWERS {
            return Self::unavailable(format!(
                "Answer at least {MIN_PERF_ANSWERS} questions (with a mix of right and wrong) to estimate performance (have {n_answers})."
            ));
        }
        if n_correct == 0 || n_correct == n_answers {
            return Self::unavailable(
                "Need both correct and incorrect answers to estimate ability.".to_string(),
            );
        }

        // Newton–Raphson MLE of θ for P(correct) = σ(θ − b).
        let mut theta = 0.0_f64;
        for _ in 0..50 {
            let mut grad = 0.0;
            let mut info = 0.0; // Fisher information = −(2nd derivative)
            for (b, c, n) in &items {
                let p = sigmoid(theta - b);
                grad += *c as f64 - *n as f64 * p;
                info += *n as f64 * p * (1.0 - p);
            }
            if info < 1e-9 {
                break;
            }
            let step = grad / info;
            theta += step;
            if step.abs() < 1e-6 {
                break;
            }
        }
        let theta = theta.clamp(-6.0, 6.0);
        let info: f64 = items
            .iter()
            .map(|(b, _, n)| {
                let p = sigmoid(theta - b);
                *n as f64 * p * (1.0 - p)
            })
            .sum();
        let se = if info > 1e-9 { 1.0 / info.sqrt() } else { 3.0 };
        Self {
            theta,
            se,
            n_answers,
            ok: true,
            missing: String::new(),
        }
    }

    fn unavailable(missing: String) -> Self {
        Self {
            theta: 0.0,
            se: 0.0,
            n_answers: 0,
            ok: false,
            missing,
        }
    }

    fn to_score_value(&self) -> ScoreValue {
        if !self.ok {
            return abstain(self.missing.clone());
        }
        let mid = sigmoid(self.theta) * 100.0;
        let lo = sigmoid(self.theta - 1.96 * self.se) * 100.0;
        let hi = sigmoid(self.theta + 1.96 * self.se) * 100.0;
        scored(
            mid,
            lo,
            hi,
            "pct",
            "",
            vec![format!(
                "Rasch ability from {} answers weighted by item difficulty",
                self.n_answers
            )],
        )
    }
}

/// Map a card's tags to a difficulty logit. `aidiff::NN` (0–100) wins; else the
/// coarse `difficulty::easy|medium|hard` (20/50/80); else neutral 50.
fn difficulty_logit(tags: &str) -> f64 {
    (difficulty_0_100(tags) / 100.0 - 0.5) * DIFFICULTY_SCALE
}

fn difficulty_0_100(tags: &str) -> f64 {
    for tag in tags.split_whitespace() {
        if let Some(rest) = tag.strip_prefix("aidiff::") {
            if let Ok(n) = rest.parse::<f64>() {
                return n.clamp(0.0, 100.0);
            }
        }
    }
    for tag in tags.split_whitespace() {
        let low = tag.to_ascii_lowercase();
        if let Some(level) = low.strip_prefix("difficulty::") {
            if level.contains("easy") {
                return 20.0;
            }
            if level.contains("medium") {
                return 50.0;
            }
            if level.contains("hard") {
                return 80.0;
            }
        }
    }
    50.0
}

// ---------------------------------------------------------------------------
// Readiness: θ → GMAT 205–805, discounted by coverage, with confidence.
// ---------------------------------------------------------------------------

fn readiness_score(rows: &[TopicCardRow], perf: &PerfEstimate) -> ScoreValue {
    let coverage = coverage_fraction(rows);
    let total_reviews: u32 = rows.iter().map(|r| r.total).sum();

    let mut missing = Vec::new();
    if total_reviews < MIN_READINESS_REVIEWS {
        missing.push(format!(
            "Answer at least {MIN_READINESS_REVIEWS} cards (have {total_reviews})."
        ));
    }
    if coverage < MIN_READINESS_COVERAGE {
        missing.push(format!(
            "Cover at least {}% of exam topics (currently {}%).",
            (MIN_READINESS_COVERAGE * 100.0) as u32,
            (coverage * 100.0).round() as u32
        ));
    }
    if !perf.ok {
        missing.push(perf.missing.clone());
    }
    if !missing.is_empty() {
        return ScoreValue {
            abstained: true,
            missing,
            ..Default::default()
        };
    }

    // Discount the expected proportion-correct toward the guess floor by the
    // fraction of the exam left uncovered, then map to the GMAT scale.
    let to_gmat = |p: f64| {
        let p_adj = p * coverage + GUESS_FLOOR * (1.0 - coverage);
        GMAT_MIN + p_adj * GMAT_SPAN
    };
    let score = round_to_10(to_gmat(sigmoid(perf.theta)));
    // Range from θ's 95% interval, widened by the uncovered fraction.
    let widen = (1.0 - coverage) * 60.0;
    let low = round_to_10((to_gmat(sigmoid(perf.theta - 1.96 * perf.se)) - widen).max(GMAT_MIN));
    let high = round_to_10((to_gmat(sigmoid(perf.theta + 1.96 * perf.se)) + widen).min(GMAT_MAX_CLAMP));
    let confidence = if coverage >= 0.8 {
        "high"
    } else if coverage >= 0.5 {
        "medium"
    } else {
        "low"
    };
    scored(
        score,
        low,
        high,
        "gmat",
        confidence,
        vec![format!(
            "projected from ability across {}% topic coverage",
            (coverage * 100.0).round() as u32
        )],
    )
}

const GMAT_MAX_CLAMP: f64 = GMAT_MIN + GMAT_SPAN; // 805

fn coverage_fraction(rows: &[TopicCardRow]) -> f64 {
    if OUTLINE_TOPICS.is_empty() {
        return 0.0;
    }
    let card_tags: Vec<&str> = rows.iter().filter_map(|r| card_topic_tag(&r.tags)).collect();
    let covered = OUTLINE_TOPICS
        .iter()
        .filter(|entry| card_tags.iter().any(|t| topic_covers(entry, t)))
        .count();
    covered as f64 / OUTLINE_TOPICS.len() as f64
}

/// The card's topic tag: the first `::`-bearing tag that is not an orthogonal
/// `difficulty::` / `split::` / `type::` / `aidiff::` tag.
fn card_topic_tag(tags: &str) -> Option<&str> {
    tags.split_whitespace()
        .filter(|t| t.contains("::"))
        .find(|t| {
            let head = t.split("::").next().unwrap_or("");
            !head.eq_ignore_ascii_case("difficulty")
                && !head.eq_ignore_ascii_case("split")
                && !head.eq_ignore_ascii_case("type")
                && !head.eq_ignore_ascii_case("aidiff")
        })
}

/// Does a card's topic tag cover an outline entry (either is a prefix of the
/// other, handling the 2-segment DataInsights entries vs. 3-segment card tags)?
fn topic_covers(outline_entry: &str, card_tag: &str) -> bool {
    card_tag == outline_entry
        || card_tag.starts_with(&format!("{outline_entry}::"))
        || outline_entry.starts_with(&format!("{card_tag}::"))
}

// ---------------------------------------------------------------------------
// Small numeric helpers.
// ---------------------------------------------------------------------------

fn sigmoid(x: f64) -> f64 {
    1.0 / (1.0 + (-x).exp())
}

fn mean_sd(xs: &[f64]) -> (f64, f64) {
    let n = xs.len() as f64;
    let mean = xs.iter().sum::<f64>() / n;
    let var = xs.iter().map(|x| (x - mean).powi(2)).sum::<f64>() / n;
    (mean, var.sqrt())
}

fn round_to_10(x: f64) -> f64 {
    (x / 10.0).round() * 10.0
}

fn abstain(missing: String) -> ScoreValue {
    ScoreValue {
        abstained: true,
        missing: vec![missing],
        ..Default::default()
    }
}

fn scored(
    score: f64,
    low: f64,
    high: f64,
    unit: &str,
    confidence: &str,
    reasons: Vec<String>,
) -> ScoreValue {
    ScoreValue {
        abstained: false,
        score,
        low,
        high,
        unit: unit.to_string(),
        confidence: confidence.to_string(),
        reasons,
        missing: Vec::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Build a synthetic storage row. `last_days_ago` is how long ago the card
    /// was last reviewed, relative to the `now_ms` passed to `memory_score`.
    fn row(tags: &str, stability: Option<f32>, passed: u32, total: u32, last_ms: Option<i64>) -> TopicCardRow {
        TopicCardRow {
            note_id: crate::notes::NoteId(0),
            tags: tags.to_string(),
            interval: 10,
            stability,
            decay: None,
            passed,
            total,
            last_review_ms: last_ms,
        }
    }

    const NOW: i64 = 1_000_000_000_000; // fixed "now" (epoch ms) for determinism

    #[test]
    fn memory_abstains_below_min_reviews() {
        let rows = vec![row("Quant::Arithmetic::Percents", Some(50.0), 3, 5, Some(NOW))];
        let sv = memory_score(&rows, NOW);
        assert!(sv.abstained);
        assert!(!sv.missing.is_empty());
    }

    #[test]
    fn memory_rises_with_higher_stability() {
        let last = NOW - MS_PER_DAY as i64; // reviewed 1 day ago
        let strong: Vec<_> = (0..4)
            .map(|_| row("Quant::Arithmetic::Percents", Some(200.0), 10, 10, Some(last)))
            .collect();
        let weak: Vec<_> = (0..4)
            .map(|_| row("Quant::Arithmetic::Percents", Some(1.5), 10, 10, Some(last)))
            .collect();
        let s = memory_score(&strong, NOW);
        let w = memory_score(&weak, NOW);
        assert!(!s.abstained && !w.abstained);
        assert!(s.score > w.score, "strong {} should beat weak {}", s.score, w.score);
        assert!(s.low <= s.score && s.score <= s.high);
    }

    #[test]
    fn performance_ability_rises_on_correct() {
        // 5 cards × 5 answers = 25 (≥ MIN_PERF_ANSWERS), all on hard items.
        let mostly_right: Vec<_> = (0..5)
            .map(|_| row("Quant::Algebra::Quadratics difficulty::hard", None, 4, 5, None))
            .collect();
        let mostly_wrong: Vec<_> = (0..5)
            .map(|_| row("Quant::Algebra::Quadratics difficulty::hard", None, 1, 5, None))
            .collect();
        let r = PerfEstimate::from_rows(&mostly_right).to_score_value();
        let w = PerfEstimate::from_rows(&mostly_wrong).to_score_value();
        assert!(!r.abstained && !w.abstained);
        assert!(r.score > w.score, "right {} should beat wrong {}", r.score, w.score);
    }

    #[test]
    fn performance_range_brackets_estimate() {
        let rows: Vec<_> = (0..5)
            .map(|_| row("Verbal::CriticalReasoning::Assumption difficulty::medium", None, 3, 5, None))
            .collect();
        let sv = PerfEstimate::from_rows(&rows).to_score_value();
        assert!(!sv.abstained);
        assert!(sv.low < sv.score && sv.score < sv.high, "{:?}", sv);
    }

    #[test]
    fn performance_abstains_when_all_correct() {
        let rows: Vec<_> = (0..5)
            .map(|_| row("Quant::Algebra::Quadratics difficulty::hard", None, 5, 5, None))
            .collect();
        let sv = PerfEstimate::from_rows(&rows).to_score_value();
        assert!(sv.abstained);
    }

    #[test]
    fn difficulty_prefers_aidiff_then_falls_back_to_coarse() {
        assert_eq!(difficulty_0_100("topic::x difficulty::hard"), 80.0);
        assert_eq!(difficulty_0_100("topic::x difficulty::easy"), 20.0);
        // aidiff wins over the coarse tag when both are present.
        assert_eq!(difficulty_0_100("aidiff::78 difficulty::easy"), 78.0);
        // Neither present -> neutral.
        assert_eq!(difficulty_0_100("topic::x"), 50.0);
    }

    #[test]
    fn readiness_abstains_below_coverage_and_reviews() {
        let rows = vec![row("Quant::Arithmetic::Percents difficulty::medium", None, 3, 5, None)];
        let perf = PerfEstimate::from_rows(&rows);
        let sv = readiness_score(&rows, &perf);
        assert!(sv.abstained);
        assert!(sv.missing.len() >= 1);
    }

    #[test]
    fn readiness_on_gmat_scale_when_enough_data() {
        // 14 distinct outline topics (= 50% coverage) × 15 answers = 210 (≥ 200),
        // each mixed right/wrong so θ is estimable.
        let rows: Vec<_> = OUTLINE_TOPICS
            .iter()
            .take(14)
            .map(|t| row(&format!("{t} difficulty::medium"), Some(20.0), 9, 15, Some(NOW)))
            .collect();
        let perf = PerfEstimate::from_rows(&rows);
        let sv = readiness_score(&rows, &perf);
        assert!(!sv.abstained, "should be scored: {:?}", sv);
        assert_eq!(sv.unit, "gmat");
        assert!((GMAT_MIN..=GMAT_MAX_CLAMP).contains(&sv.score), "score {}", sv.score);
        assert_eq!(sv.score % 10.0, 0.0, "GMAT scores step by 10");
        assert!(sv.low <= sv.score && sv.score <= sv.high);
    }

    #[test]
    fn three_scores_are_distinct() {
        // High stability (strong memory) but mostly WRONG on hard items (weak
        // performance): the two must diverge, and readiness is on a different
        // unit entirely.
        let last = NOW - MS_PER_DAY as i64;
        let rows: Vec<_> = (0..5)
            .map(|_| row("Quant::Algebra::Quadratics difficulty::hard", Some(300.0), 1, 6, Some(last)))
            .collect();
        let scores = compute_scores(&rows, NOW);
        let mem = scores.memory.unwrap();
        let perf = scores.performance.unwrap();
        assert!(!mem.abstained && !perf.abstained);
        assert!(
            mem.score - perf.score > 20.0,
            "memory {} should clearly exceed performance {}",
            mem.score,
            perf.score
        );
        // Readiness abstains here (far under 200 reviews) — but still reports the
        // GMAT unit is its scale, distinct from the two pct scores.
        assert!(scores.readiness.unwrap().abstained);
    }

    // --- end-to-end against a real Collection: read-only, undo intact ---------
    use crate::card::CardQueue;
    use crate::card::CardType;

    /// Add a single review card with the given tags and interval to the default
    /// deck, returning its note id. (Mirrors the `topic_mastery` test helper.)
    fn add_review_card(col: &mut Collection, tags: &[&str], interval: u32) -> NoteId {
        let nt = col.get_notetype_by_name("Basic").unwrap().unwrap();
        let mut note = nt.new_note();
        note.set_field(0, "front").unwrap();
        note.tags = tags.iter().map(|s| s.to_string()).collect();
        col.add_note(&mut note, DeckId(1)).unwrap();

        let mut card = col.storage.get_card_by_ordinal(note.id, 0).unwrap().unwrap();
        card.interval = interval;
        card.due = 0;
        card.ctype = CardType::Review;
        card.queue = CardQueue::Review;
        col.update_cards_maybe_undoable(vec![card], false).unwrap();
        note.id
    }

    #[test]
    fn scores_are_read_only_undo_intact() {
        let mut col = Collection::new();
        let nid = add_review_card(&mut col, &["Quant::Arithmetic::Percents"], 10);
        let card = col.storage.get_card_by_ordinal(nid, 0).unwrap().unwrap();

        // Establish a genuine undoable op to protect.
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

        // The scores query is a plain storage read: it must NOT clear undo.
        let _ = col
            .get_gmat_scores(GetGmatScoresRequest::default())
            .unwrap();
        assert_eq!(
            col.can_undo(),
            Some(&Op::UpdateCard),
            "get_gmat_scores (plain read) must not clear the undo history"
        );
        col.undo().unwrap();
        assert_eq!(col.storage.get_card(card.id).unwrap().unwrap().interval, 10);
    }
}
