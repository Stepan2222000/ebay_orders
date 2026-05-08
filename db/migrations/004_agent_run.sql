-- Stage 5 — состояние активной агентской сессии.
--
-- agent_run хранит ровно одну строку на сессию агента стадии B (auto-trigger
-- или ответ на user-сообщение). Воркер INSERT'ит при старте, UPDATE'ит thinking
-- вокруг каждого LLM-вызова, выставляет stop_requested при отмене, finished_at
-- по завершению. Триггер NOTIFY 'agent_state' позволяет подпискам в /api/status
-- обновлять Pill «✦ агент думает» и видимость кнопки Stop.
--
-- Триггер chat_messages_user_arrived пробуждает воркер на каждое user-сообщение —
-- единый канал входа для агента (нет двойного диспетчера).

CREATE TABLE agent_run (
    run_id         bigserial PRIMARY KEY,
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    stop_requested boolean NOT NULL DEFAULT false,
    thinking       boolean NOT NULL DEFAULT false,
    message_id     bigint REFERENCES chat_messages(message_id) ON DELETE SET NULL
);

CREATE INDEX agent_run_active_idx ON agent_run(run_id) WHERE finished_at IS NULL;

CREATE OR REPLACE FUNCTION notify_agent_state() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('agent_state', '');
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER agent_run_notify
    AFTER INSERT OR UPDATE OR DELETE ON agent_run
    FOR EACH STATEMENT EXECUTE FUNCTION notify_agent_state();

CREATE OR REPLACE FUNCTION notify_user_message_arrived() RETURNS trigger AS $$
BEGIN
    IF NEW.role = 'user' THEN
        PERFORM pg_notify('user_message_arrived', '');
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER chat_messages_user_arrived
    AFTER INSERT ON chat_messages
    FOR EACH ROW EXECUTE FUNCTION notify_user_message_arrived();
