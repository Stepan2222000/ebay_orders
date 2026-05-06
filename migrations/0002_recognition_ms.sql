ALTER TABLE raw_ocr
    ADD COLUMN IF NOT EXISTS recognition_ms integer NULL;

ALTER TABLE raw_ocr
    DROP CONSTRAINT IF EXISTS raw_ocr_recognition_ms_check;
ALTER TABLE raw_ocr
    ADD CONSTRAINT raw_ocr_recognition_ms_check
        CHECK (recognition_ms IS NULL OR recognition_ms >= 0);
