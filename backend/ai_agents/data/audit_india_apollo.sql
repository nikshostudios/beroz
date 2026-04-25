-- Apollo data-quality audit for the Indian market (Phase A).
--
-- Five read-only queries against the live `candidates` table for the
-- subset where market='IN' AND source='apollo'. Each query answers one
-- question; together they decide whether Apollo is a viable source for
-- India or whether we need to augment / replace it.
--
-- Run from Supabase SQL Editor. No DDL, no writes.
--
-- See plan: nimbalyst-local/plans/alright-so-honestly-this-quirky-eclipse.md
-- Verdict goes to ~/Downloads/2026-04-25.md (daily chronicle).

-- ── A1. Volume + recency ──────────────────────────────────────
-- "How many India-Apollo rows do we have, and how fresh are they?"
SELECT COUNT(*)                                                              AS total,
       COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '30 days')       AS last_30d,
       COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '7 days')        AS last_7d
FROM candidates
WHERE market = 'IN' AND source = 'apollo';

-- ── A2. Reachability fill rates ───────────────────────────────
-- "Of those rows, what fraction can we actually contact this week?"
SELECT
  COUNT(*)                                                                                AS total,
  ROUND(100.0 * COUNT(*) FILTER (WHERE email IS NOT NULL AND email <> '') / NULLIF(COUNT(*), 0), 1)        AS pct_with_email,
  ROUND(100.0 * COUNT(*) FILTER (WHERE phone IS NOT NULL AND phone <> '') / NULLIF(COUNT(*), 0), 1)        AS pct_with_phone,
  ROUND(100.0 * COUNT(*) FILTER (WHERE linkedin_url IS NOT NULL) / NULLIF(COUNT(*), 0), 1)                 AS pct_with_linkedin,
  ROUND(100.0 * COUNT(*) FILTER (WHERE enriched_at IS NOT NULL) / NULLIF(COUNT(*), 0), 1)                  AS pct_reveal_attempted
FROM candidates
WHERE market = 'IN' AND source = 'apollo';

-- ── A3. Location distribution ─────────────────────────────────
-- "Top 20 current_location values. We expect the literal string 'India'
--  to dominate, which is the proxy for our fabricated-location problem
--  in core.py:705-709."
SELECT current_location, COUNT(*) AS n
FROM candidates
WHERE market = 'IN' AND source = 'apollo'
GROUP BY current_location
ORDER BY n DESC
LIMIT 20;

-- ── A4. Top employers ─────────────────────────────────────────
-- "Top 25 current_employer values. Eyeball check: do these look like
--  India-HQ companies, or US-headquartered tech with India presence?"
SELECT current_employer, COUNT(*) AS n
FROM candidates
WHERE market = 'IN' AND source = 'apollo' AND current_employer IS NOT NULL
GROUP BY current_employer
ORDER BY n DESC
LIMIT 25;

-- ── A5. Duplicate density ─────────────────────────────────────
-- "Same linkedin_url appearing on >1 row — proxy for upsert key drift
--  or Apollo returning the same person across queries."
SELECT linkedin_url, COUNT(*) AS dup_count
FROM candidates
WHERE market = 'IN' AND source = 'apollo' AND linkedin_url IS NOT NULL
GROUP BY linkedin_url
HAVING COUNT(*) > 1
ORDER BY dup_count DESC
LIMIT 10;
