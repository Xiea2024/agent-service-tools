ALTER TABLE sp_users ADD COLUMN platform_admin BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE sp_users ADD COLUMN disabled BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE sp_users ADD COLUMN email_verified BOOLEAN NOT NULL DEFAULT true;
CREATE TABLE sp_email_tokens (
    token_hash TEXT PRIMARY KEY, email TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('register','reset')),
    password_hash TEXT, expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX sp_email_tokens_expiry ON sp_email_tokens(expires_at);
CREATE TABLE sp_gateway_keys (
    user_id UUID PRIMARY KEY REFERENCES sp_users(id), encrypted_key TEXT NOT NULL
);
CREATE TABLE sp_security_events (
    id BIGSERIAL PRIMARY KEY, actor_id UUID, action TEXT NOT NULL,
    target_id UUID, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX sp_runs_user_time ON sp_runs(user_id, created_at);
