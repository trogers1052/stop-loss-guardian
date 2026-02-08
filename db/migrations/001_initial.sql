-- Stop Loss Guardian Database Schema
-- Run this on the trading_platform database

-- Stop loss tracking for open positions
CREATE TABLE IF NOT EXISTS stop_loss_tracking (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,
    position_id INTEGER,
    entry_price DECIMAL(12, 4) NOT NULL,
    quantity DECIMAL(12, 4) NOT NULL,

    -- Stop loss configuration
    stop_loss_price DECIMAL(12, 4),
    stop_loss_type VARCHAR(20),  -- 'manual', 'atr', 'percentage', 'support'
    stop_loss_pct DECIMAL(6, 2),
    stop_loss_set_at TIMESTAMPTZ,

    -- Current state
    current_price DECIMAL(12, 4),
    current_drawdown_pct DECIMAL(6, 2),
    price_updated_at TIMESTAMPTZ,

    -- Alert state
    missing_stop_alert_sent BOOLEAN DEFAULT false,
    alert_count INTEGER DEFAULT 0,
    alert_escalation_level VARCHAR(20) DEFAULT 'none',  -- 'none', 'telegram', 'sms', 'phone_call'
    last_alert_sent TIMESTAMPTZ,

    -- Acknowledgment
    acknowledged BOOLEAN DEFAULT false,
    acknowledged_at TIMESTAMPTZ,
    acknowledged_reason TEXT,

    -- Earnings tracking
    next_earnings_date DATE,

    -- Timestamps
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),

    -- Unique constraint for upsert
    UNIQUE (symbol, position_id)
);

-- Index for common queries
CREATE INDEX IF NOT EXISTS idx_stop_loss_tracking_symbol ON stop_loss_tracking(symbol);
CREATE INDEX IF NOT EXISTS idx_stop_loss_tracking_no_stop ON stop_loss_tracking(symbol)
    WHERE stop_loss_price IS NULL AND acknowledged = false;

-- Urgent alerts log
CREATE TABLE IF NOT EXISTS urgent_alerts (
    id SERIAL PRIMARY KEY,
    alert_type VARCHAR(50) NOT NULL,  -- 'missing_stop_loss', 'critical_drawdown', 'stop_triggered', etc.
    symbol VARCHAR(20),
    position_id INTEGER,
    stop_loss_tracking_id INTEGER REFERENCES stop_loss_tracking(id),

    -- Alert details
    severity VARCHAR(20) NOT NULL,  -- 'info', 'warning', 'critical', 'emergency'
    escalation_level INTEGER DEFAULT 0,
    message TEXT NOT NULL,
    details JSONB,

    -- Delivery
    channel VARCHAR(20) NOT NULL,  -- 'telegram', 'sms', 'phone_call'
    twilio_sid VARCHAR(50),
    delivered_at TIMESTAMPTZ DEFAULT NOW(),

    -- Response
    acknowledged BOOLEAN DEFAULT false,
    acknowledged_at TIMESTAMPTZ,
    response_action TEXT,

    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index for alert queries
CREATE INDEX IF NOT EXISTS idx_urgent_alerts_symbol ON urgent_alerts(symbol);
CREATE INDEX IF NOT EXISTS idx_urgent_alerts_type ON urgent_alerts(alert_type);
CREATE INDEX IF NOT EXISTS idx_urgent_alerts_created ON urgent_alerts(created_at DESC);
