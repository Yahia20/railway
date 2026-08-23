-- GENERATED from n8n/workflows/02-calls-ingest-evaluate.json, node 'Load stored transcript'.
-- Do not edit here: edit the workflow JSON and re-run
--   python scripts/check_workflow_json.py n8n/workflows/02-calls-ingest-evaluate.json --dump-sql scripts/sql
-- $n parameters: ={{ [ $json.interaction_id ] }}
-- The ONE renderer. Both paths converge here so that a judge retry scores a
-- byte-identical transcript to the first run: the transcribe path reads back
-- what it just wrote instead of rendering the ASR response itself.
--
-- It is deliberately equivalent to CallTranscript.as_dialogue()
-- (services/worker/app/asr/cohere_arabic.py). The three places where the first
-- version was NOT equivalent, all of which change what the judge reads:
--
--   1. Order.  Python iterates the segment LIST. Ordering by (s->>'seq')::int
--      silently reordered a transcript whose seq was missing (NULL sorts last)
--      or non-monotonic, and threw on a non-integer seq. WITH ORDINALITY
--      preserves array order, which is what the reference does.
--   2. Empty text.  Python skips only FALSEY text (NULL / ''). btrim(...) <> ''
--      also dropped a whitespace-only segment that the reference keeps -- one
--      more line of difference between first run and retry.
--   3. Minutes.  lpad(x, 2, '0') TRUNCATES when x is longer than 2, so a call
--      at 01:43:07 rendered as [10:07]. Python's {:02d} pads and never
--      truncates. greatest(2, length(...)) reproduces that.
--
-- trunc() rather than floor() matches Python's int(); they differ only for a
-- negative start_sec, which ASR does not emit (Segment.start_sec is a byte
-- offset into the audio). Golden cases for all four live in
-- scripts/sql/acceptance_pr1a.sql, section A10.
SELECT $1::uuid                                        AS interaction_id,
       (t.interaction_id IS NOT NULL)                  AS has_transcript,
       coalesce((
         SELECT string_agg(
                  format('[%s:%s]%s %s',
                         lpad(d.mm::text, greatest(2, length(d.mm::text)), '0'),
                         lpad(d.ss::text, greatest(2, length(d.ss::text)), '0'),
                         CASE WHEN d.speaker = 'unknown'
                              THEN '' ELSE ' ' || upper(d.speaker) || ':' END,
                         d.txt),
                  E'\n' ORDER BY d.ord)
         FROM (
           SELECT e.ord,
                  coalesce(e.seg->>'text', '')                                  AS txt,
                  coalesce(e.seg->>'speaker', 'unknown')                        AS speaker,
                  trunc(coalesce((e.seg->>'start_sec')::numeric, 0))::bigint / 60 AS mm,
                  trunc(coalesce((e.seg->>'start_sec')::numeric, 0))::bigint % 60 AS ss
           FROM jsonb_array_elements(coalesce(t.segments, '[]'::jsonb))
                WITH ORDINALITY AS e(seg, ord)
         ) d
         WHERE d.txt <> ''
       ), '')                                          AS dialogue,
       t.asr_confidence,
       t.diarization,
       t.duration_seconds,
       t.channels,
       coalesce(t.asr_metrics->>'asr_quality_status', 'green') AS asr_quality_status,
       coalesce(t.asr_metrics->'quality', '{}'::jsonb)         AS asr_quality
FROM (SELECT 1) one
LEFT JOIN transcripts t ON t.interaction_id = $1::uuid;
