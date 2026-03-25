-- PostgreSQL 초기 스키마 (gonggo-radar)
-- pgvector/pgvector:pg16 이미지에서 자동 실행됨

-- pgvector 확장 활성화
CREATE EXTENSION IF NOT EXISTS vector;

-- 공고 테이블
CREATE TABLE IF NOT EXISTS announcements (
    id              SERIAL PRIMARY KEY,
    source          TEXT    NOT NULL,
    source_id       TEXT    NOT NULL,
    title           TEXT    NOT NULL,
    summary         TEXT    DEFAULT '',
    url             TEXT    NOT NULL,
    author          TEXT    DEFAULT '',
    category        TEXT    DEFAULT '',
    target          TEXT    DEFAULT '',
    period_start    TEXT,
    period_end      TEXT,
    relevance_score DOUBLE PRECISION DEFAULT 0.0,
    relevance_reason TEXT   DEFAULT '',
    matched_keywords TEXT   DEFAULT '[]',
    is_notified     INTEGER DEFAULT 0,
    raw_data        TEXT    DEFAULT '',
    business_domain TEXT    DEFAULT '',
    domain_confidence DOUBLE PRECISION DEFAULT 0.0,
    obsidian_path   TEXT    DEFAULT '',
    embedding_id    INTEGER DEFAULT NULL,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    UNIQUE(source, source_id)
);

-- 키워드 테이블
CREATE TABLE IF NOT EXISTS keywords (
    id        SERIAL PRIMARY KEY,
    keyword   TEXT    NOT NULL UNIQUE,
    category  TEXT    NOT NULL DEFAULT 'boost',
    weight    DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    is_active INTEGER NOT NULL DEFAULT 1
);

-- 실행 이력 테이블
CREATE TABLE IF NOT EXISTS run_history (
    id            SERIAL PRIMARY KEY,
    started_at    TEXT    NOT NULL,
    finished_at   TEXT,
    source        TEXT    NOT NULL,
    total_fetched INTEGER DEFAULT 0,
    new_count     INTEGER DEFAULT 0,
    relevant_count INTEGER DEFAULT 0,
    notified_count INTEGER DEFAULT 0,
    status        TEXT    DEFAULT 'running',
    error_message TEXT    DEFAULT ''
);

-- 임베딩 테이블
CREATE TABLE IF NOT EXISTS embeddings (
    id              SERIAL PRIMARY KEY,
    announcement_id INTEGER NOT NULL REFERENCES announcements(id),
    model_name      TEXT    NOT NULL DEFAULT 'text-embedding-3-small',
    embedding       BYTEA   NOT NULL,
    dimensions      INTEGER NOT NULL DEFAULT 256,
    created_at      TEXT    NOT NULL
);

-- 벡터 검색 테이블 (pgvector)
CREATE TABLE IF NOT EXISTS vec_announcements (
    id        SERIAL PRIMARY KEY,
    embedding vector(256)
);

-- 신청 이력 테이블
CREATE TABLE IF NOT EXISTS application_history (
    id                SERIAL PRIMARY KEY,
    announcement_id   INTEGER NOT NULL REFERENCES announcements(id),
    status            TEXT    NOT NULL DEFAULT 'discovered',
    applied_date      TEXT,
    result_date       TEXT,
    result            TEXT    DEFAULT '',
    prepared_docs     TEXT    DEFAULT '[]',
    notes             TEXT    DEFAULT '',
    assigned_domain   TEXT    DEFAULT '',
    priority          INTEGER DEFAULT 0,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL
);

-- 리서치 문서 테이블
CREATE TABLE IF NOT EXISTS research_documents (
    id              SERIAL PRIMARY KEY,
    title           TEXT    NOT NULL,
    doc_type        TEXT    NOT NULL DEFAULT 'quarterly',
    period_start    TEXT,
    period_end      TEXT,
    content         TEXT    NOT NULL DEFAULT '',
    metadata        TEXT    DEFAULT '{}',
    obsidian_path   TEXT    DEFAULT '',
    version         INTEGER DEFAULT 1,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

-- 스키마 버전 테이블
CREATE TABLE IF NOT EXISTS schema_version (
    version      INTEGER PRIMARY KEY,
    applied_at   TEXT    NOT NULL,
    description  TEXT    NOT NULL DEFAULT ''
);

-- 인덱스
CREATE INDEX IF NOT EXISTS idx_ann_source ON announcements(source);
CREATE INDEX IF NOT EXISTS idx_ann_notified ON announcements(is_notified);
CREATE INDEX IF NOT EXISTS idx_ann_score ON announcements(relevance_score);
CREATE INDEX IF NOT EXISTS idx_ann_created ON announcements(created_at);
CREATE INDEX IF NOT EXISTS idx_kw_category ON keywords(category);
CREATE INDEX IF NOT EXISTS idx_emb_ann ON embeddings(announcement_id);
CREATE INDEX IF NOT EXISTS idx_apphist_ann ON application_history(announcement_id);
CREATE INDEX IF NOT EXISTS idx_apphist_status ON application_history(status);

-- pgvector 인덱스 (IVFFlat - 데이터가 충분히 쌓인 후 성능 향상)
-- 주의: IVFFlat은 최소 lists * 10 이상의 행이 필요
-- CREATE INDEX IF NOT EXISTS idx_vec_ann_embedding ON vec_announcements
--     USING ivfflat (embedding vector_cosine_ops) WITH (lists = 10);

-- 마이그레이션 기록 (init-db.sql로 생성 시 migration 1-4 완료 상태)
INSERT INTO schema_version (version, applied_at, description) VALUES
    (1, NOW()::text, 'Add business_domain, domain_confidence, obsidian_path, embedding_id to announcements'),
    (2, NOW()::text, 'Create embeddings table and vec_announcements pgvector table'),
    (3, NOW()::text, 'Create application_history table'),
    (4, NOW()::text, 'Create research_documents table')
ON CONFLICT (version) DO NOTHING;
