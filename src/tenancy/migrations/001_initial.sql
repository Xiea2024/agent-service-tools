CREATE TABLE sp_users (
    id UUID PRIMARY KEY, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE sp_tenants (
    id UUID PRIMARY KEY, name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE sp_memberships (
    tenant_id UUID NOT NULL REFERENCES sp_tenants(id),
    user_id UUID NOT NULL REFERENCES sp_users(id),
    role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
    PRIMARY KEY (tenant_id, user_id)
);
CREATE TABLE sp_sessions (
    token_hash TEXT PRIMARY KEY, user_id UUID NOT NULL REFERENCES sp_users(id),
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX sp_sessions_expiry ON sp_sessions(expires_at);
CREATE TABLE sp_threads (
    id TEXT PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 128),
    checkpoint_id UUID UNIQUE NOT NULL,
    tenant_id UUID NOT NULL, user_id UUID NOT NULL,
    agent_id TEXT NOT NULL, title TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, user_id) REFERENCES sp_memberships(tenant_id, user_id),
    UNIQUE (id, tenant_id, user_id)
);
CREATE INDEX sp_threads_owner_time ON sp_threads(tenant_id, user_id, updated_at DESC);
CREATE TABLE sp_runs (
    id UUID PRIMARY KEY, thread_id TEXT NOT NULL,
    tenant_id UUID NOT NULL, user_id UUID NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running','succeeded','failed','interrupted')),
    question TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    FOREIGN KEY (thread_id, tenant_id, user_id) REFERENCES sp_threads(id, tenant_id, user_id),
    UNIQUE (id, tenant_id, user_id)
);
CREATE UNIQUE INDEX sp_runs_one_active_thread ON sp_runs(thread_id) WHERE status='running';
CREATE TABLE sp_answers (
    run_id UUID PRIMARY KEY, tenant_id UUID NOT NULL, user_id UUID NOT NULL,
    answer TEXT NOT NULL, evidence JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (run_id, tenant_id, user_id) REFERENCES sp_runs(id, tenant_id, user_id),
    UNIQUE (run_id, tenant_id, user_id)
);
CREATE TABLE sp_feedback (
    run_id UUID PRIMARY KEY, tenant_id UUID NOT NULL, user_id UUID NOT NULL,
    helpful BOOLEAN NOT NULL, comment TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (run_id, tenant_id, user_id) REFERENCES sp_answers(run_id, tenant_id, user_id)
);
CREATE TABLE sp_documents (
    tenant_id UUID NOT NULL REFERENCES sp_tenants(id), document_id TEXT NOT NULL,
    title TEXT NOT NULL, source_url TEXT NOT NULL, fingerprint TEXT NOT NULL,
    chunk_count INTEGER NOT NULL CHECK (chunk_count > 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, document_id)
);
