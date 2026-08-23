-- Real ASR health measurements per transcript. `asr_confidence` is only the
-- share of chunks that returned; this records what came back: empty-chunk
-- count, chars per second of audio (near-zero = lost speech even when every
-- chunk "succeeded"), and repetition detection (a decoder stuck in a loop).
ALTER TABLE transcripts
    ADD COLUMN IF NOT EXISTS asr_metrics jsonb NOT NULL DEFAULT '{}'::jsonb;
