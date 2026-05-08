-- Stage 4 — оповещение фронта о новых сообщениях в чате.
-- INSERT/UPDATE/DELETE на chat_messages → NOTIFY 'chat_changed', payload пустой.
-- Подписчик в /api/chat/stream сам перечитывает текущую историю и пушит её клиенту.

CREATE OR REPLACE FUNCTION notify_chat_change() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('chat_changed', '');
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER chat_messages_notify
    AFTER INSERT OR UPDATE OR DELETE ON chat_messages
    FOR EACH STATEMENT EXECUTE FUNCTION notify_chat_change();
