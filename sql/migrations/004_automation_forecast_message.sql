CREATE TABLE IF NOT EXISTS automation_forecast_message (
    id                   bigserial PRIMARY KEY,
    whatsapp_message_id  text UNIQUE NOT NULL,
    client_slug          text,
    raw_payload          jsonb NOT NULL,
    parsed_status        text NOT NULL DEFAULT 'pending'
                            CHECK (parsed_status IN ('pending', 'parsed', 'failed', 'written')),
    parsed_data          jsonb,
    target_sheet_id      bigint,
    target_row_id        bigint,
    error_message        text,
    received_at          timestamptz NOT NULL DEFAULT now(),
    processed_at         timestamptz
);

CREATE INDEX IF NOT EXISTS idx_automation_forecast_message_status
    ON automation_forecast_message (parsed_status);

GRANT SELECT ON automation_forecast_message TO grafana_reader;