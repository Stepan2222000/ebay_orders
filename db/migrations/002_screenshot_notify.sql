-- Stage 4 — оповещение о смене статусов снимков для real-time счётчиков.
-- Триггер statement-level: одно NOTIFY на стейтмент, payload пустой —
-- подписчик в /api/status/stream сам пересчитывает counts.

CREATE OR REPLACE FUNCTION notify_status_change() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('status_changed', '');
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER screenshots_status_notify_iud
    AFTER INSERT OR DELETE ON screenshots
    FOR EACH STATEMENT EXECUTE FUNCTION notify_status_change();

CREATE TRIGGER screenshots_status_notify_upd
    AFTER UPDATE OF ocr_status, agent_status ON screenshots
    FOR EACH STATEMENT EXECUTE FUNCTION notify_status_change();
