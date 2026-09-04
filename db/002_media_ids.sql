-- Meta hosts uploaded media and returns an id we send instead of a URL.
-- Cache the id so each photo is uploaded exactly once.
ALTER TABLE listing_media
  ADD COLUMN IF NOT EXISTS meta_media_id   text,
  ADD COLUMN IF NOT EXISTS meta_uploaded_at timestamptz;

-- Meta media ids expire (~30 days), so we re-upload when stale.
CREATE INDEX IF NOT EXISTS listing_media_meta_idx
  ON listing_media (listing_id, meta_uploaded_at DESC)
  WHERE meta_media_id IS NOT NULL;
