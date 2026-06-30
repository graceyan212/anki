-- GMAT fork (T2): per-card inputs for the per-topic mastery aggregate.
--
-- One row per review-eligible card (type != new), carrying everything the
-- Rust grouping pass needs:
--   * the note's space-separated tags string (topic is parsed in Rust),
--   * the card's interval in days,
--   * the FSRS stability from the cards.data JSON blob (NULL if absent),
--   * the count of passed reviews and total reviews from the revlog.
--
-- The revlog counts are produced by a single grouped sub-select that is joined
-- once, so the whole thing is one aggregate query rather than a per-card loop.
-- A "pass" is any genuine review answer (ease >= 2, i.e. Hard/Good/Easy);
-- ease = 1 is Again (a lapse). type = 4 (manual) and ease = 0 rows are skipped
-- so reschedules/sets don't pollute recall.
SELECT c.nid,
  n.tags,
  cast(c.ivl AS integer) AS ivl,
  -- cards.data is usually "{}" or an FSRS JSON blob, but legacy/empty rows may
  -- hold "" or non-JSON text; json_valid() guards json_extract from erroring.
  CASE
    WHEN json_valid(c.data) THEN json_extract(c.data, '$.s')
    ELSE NULL
  END AS stability,
  coalesce(r.passed, 0) AS passed,
  coalesce(r.total, 0) AS total
FROM cards c
  JOIN notes n ON c.nid = n.id
  LEFT JOIN (
    SELECT cid,
      sum(ease >= 2) AS passed,
      COUNT(*) AS total
    FROM revlog
    WHERE ease > 0
      AND type != 4
    GROUP BY cid
  ) r ON r.cid = c.id
WHERE c.type != 0
  AND c.queue != -1