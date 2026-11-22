-- 实查标签、模型版本与训练任务治理
CREATE TABLE IF NOT EXISTS inspection_finding (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    loan_id bigint NOT NULL REFERENCES exhibit_loan,
    packing_hazard boolean NOT NULL,
    observation text NOT NULL CHECK (length(btrim(observation)) BETWEEN 8 AND 600),
    feature_snapshot jsonb NOT NULL,
    label_version integer NOT NULL DEFAULT 1 CHECK (label_version >= 1),
    superseded_by bigint,
    superseded_at timestamptz,
    recorded_by text NOT NULL REFERENCES staff,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS finding_loan_history ON inspection_finding (loan_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS finding_current_one ON inspection_finding (loan_id)
    WHERE superseded_at IS NULL;

CREATE TABLE IF NOT EXISTS model_version (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version text NOT NULL UNIQUE,
    status text NOT NULL CHECK (status IN ('candidate', 'active', 'retired')),
    retired_reason text CHECK (retired_reason IS NULL OR
        retired_reason IN ('label_corrected', 'superseded_by_publish')),
    retired_at timestamptz,
    metrics jsonb NOT NULL,
    parameters jsonb NOT NULL,
    training_rows_sha256 text NOT NULL,
    training_job_id bigint,
    published_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS training_job (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    status text NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'halted', 'failed')),
    stage text NOT NULL DEFAULT 'queued' CHECK (stage IN
        ('queued', 'manifest', 'encoding', 'fitting', 'metrics', 'persisted', 'done')),
    halt_reason text,
    error text,
    manifest jsonb NOT NULL,
    checkpoint integer NOT NULL DEFAULT 0 CHECK (checkpoint BETWEEN 0 AND 100),
    resumed_count integer NOT NULL DEFAULT 0,
    requested_by text NOT NULL REFERENCES staff,
    model_version_id bigint REFERENCES model_version,
    started_at timestamptz,
    finished_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE triage_case DROP CONSTRAINT IF EXISTS triage_case_loan_id_key;
ALTER TABLE triage_case DROP CONSTRAINT IF EXISTS triage_case_status_check;
ALTER TABLE triage_case ADD CONSTRAINT triage_case_status_check
    CHECK (status IN ('queued', 'scored', 'failed', 'voided'));
ALTER TABLE triage_case ADD COLUMN IF NOT EXISTS model_version_id bigint REFERENCES model_version;
ALTER TABLE triage_case ADD COLUMN IF NOT EXISTS score_state text NOT NULL DEFAULT 'current'
    CHECK (score_state IN ('current', 'invalidated', 'frozen'));
ALTER TABLE triage_case ADD COLUMN IF NOT EXISTS invalidated_reason text;
ALTER TABLE triage_case ADD COLUMN IF NOT EXISTS invalidated_at timestamptz;
ALTER TABLE triage_case ADD COLUMN IF NOT EXISTS source_case_id bigint REFERENCES triage_case;
CREATE INDEX IF NOT EXISTS triage_case_loan_latest ON triage_case (loan_id, id DESC);

CREATE TABLE IF NOT EXISTS triage_score_history (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id bigint NOT NULL REFERENCES triage_case,
    model_version_id bigint REFERENCES model_version,
    version_label text NOT NULL,
    probability double precision NOT NULL,
    risk text NOT NULL,
    evidence jsonb NOT NULL,
    score_state text NOT NULL,
    invalidated_reason text,
    computed_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS score_history_case ON triage_score_history (case_id, id);

ALTER TABLE unpack_review DROP CONSTRAINT IF EXISTS unpack_review_pkey;
ALTER TABLE unpack_review ADD COLUMN IF NOT EXISTS review_id bigint GENERATED ALWAYS AS IDENTITY;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'unpack_review_pkey') THEN
        ALTER TABLE unpack_review ADD CONSTRAINT unpack_review_pkey PRIMARY KEY (review_id);
    END IF;
END $$;
ALTER TABLE unpack_review DROP CONSTRAINT IF EXISTS unpack_review_status_check;
ALTER TABLE unpack_review ADD CONSTRAINT unpack_review_status_check
    CHECK (status IN ('awaiting_witness', 'awaiting_supervisor', 'sampling', 'held', 'cleared', 'voided'));
CREATE UNIQUE INDEX IF NOT EXISTS unpack_review_current_one ON unpack_review (case_id)
    WHERE status <> 'voided';
