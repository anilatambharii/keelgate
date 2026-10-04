-- Runs once, on first initialisation of the dev Postgres volume.
-- pgvector backs the semantic memory layer.
CREATE EXTENSION IF NOT EXISTS vector;
-- Deterministic UUID generation for audit-chain and run identifiers.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
