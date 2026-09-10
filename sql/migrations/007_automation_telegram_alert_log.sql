CREATE TABLE IF NOT EXISTS automation_telegram_alert_log (
    id                bigserial PRIMARY KEY,
    severity          text NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    job_name          text,
    chat_id           text NOT NULL,
    message           text NOT NULL,
    delivered         boolean NOT NULL DEFAULT false,
    delivery_error    text,
    sent_at           timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_automation_telegram_alert_log_sent_at
    ON automation_telegram_alert_log (sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_automation_telegram_alert_log_severity
    ON automation_telegram_alert_log (severity);

CREATE OR REPLACE VIEW automation_alert_volume_daily AS
SELECT date_trunc('day', sent_at) AS day, severity, count(*) AS alert_count,
       count(*) FILTER (WHERE NOT delivered) AS failed_delivery_count
FROM automation_telegram_alert_log
GROUP BY date_trunc('day', sent_at), severity;

GRANT SELECT ON automation_telegram_alert_log TO grafana_reader;
GRANT SELECT ON automation_alert_volume_daily TO grafana_reader;