"""Storage for import attempts, content hashes, occurrences and source observations."""
STATEMENTS_V2 = (
    "ALTER TABLE imports ADD COLUMN archive_status TEXT NOT NULL DEFAULT 'LEGACY' CHECK(archive_status IN ('LEGACY','PENDING','ARCHIVED','ERROR','NOT_REQUIRED'))",
    """CREATE TABLE operation_identities (
        operation_id TEXT PRIMARY KEY NOT NULL REFERENCES operations(id),
        bank TEXT NOT NULL,
        account_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('BANK_ID','FINGERPRINT')),
        identity_key TEXT NOT NULL CHECK(length(identity_key)=64 AND identity_key NOT GLOB '*[^0-9a-f]*'),
        occurrence INTEGER NOT NULL CHECK(typeof(occurrence)='integer' AND occurrence>0),
        UNIQUE(bank,account_id,kind,identity_key,occurrence),
        CHECK(kind!='BANK_ID' OR occurrence=1)
    )""",
    """CREATE TABLE source_files (
        sha256 TEXT PRIMARY KEY NOT NULL CHECK(length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
        first_import_id TEXT NOT NULL REFERENCES imports(id),
        source_count INTEGER NOT NULL CHECK(typeof(source_count)='integer' AND source_count>=0),
        archive_path TEXT NOT NULL UNIQUE,
        pending_path TEXT,
        archive_state TEXT NOT NULL CHECK(archive_state IN ('PENDING','ARCHIVED','ERROR')),
        archive_error TEXT,
        metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json) AND json_type(metadata_json)='object')
    )""",
    """CREATE TABLE import_observations (
        import_id TEXT NOT NULL REFERENCES imports(id),
        source_index INTEGER NOT NULL CHECK(typeof(source_index)='integer' AND source_index>=0),
        operation_id TEXT NOT NULL REFERENCES operations(id),
        source_row TEXT,
        observed_json TEXT NOT NULL CHECK(json_valid(observed_json) AND json_type(observed_json)='object'),
        is_new INTEGER NOT NULL CHECK(is_new IN (0,1)),
        PRIMARY KEY(import_id,source_index)
    )""",
    "CREATE INDEX import_file_hash ON imports(sha256)",
)
STATEMENTS_V2 += tuple(
    f"CREATE TRIGGER immutable_{table} BEFORE UPDATE ON {table} "
    "BEGIN SELECT RAISE(ABORT, 'Import history is immutable'); END"
    for table in ('operation_identities','import_observations')
)
STATEMENTS_V2 += tuple(
    f"CREATE TRIGGER preserve_{table} BEFORE DELETE ON {table} "
    "BEGIN SELECT RAISE(ABORT, 'Import history must be preserved'); END"
    for table in ('operation_identities','import_observations','source_files')
)
