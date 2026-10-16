"""Schema 1: banking facts and analytical decisions have separate storage."""

SCHEMA_VERSION = 2

STATEMENTS = (
    """CREATE TABLE schema_metadata (
        singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
        schema_version INTEGER NOT NULL CHECK(typeof(schema_version) = 'integer' AND schema_version > 0)
    )""",
    """CREATE TABLE imports (
        id TEXT PRIMARY KEY NOT NULL CHECK(length(trim(id)) > 0),
        filename TEXT NOT NULL CHECK(length(trim(filename)) > 0),
        bank TEXT NOT NULL CHECK(length(trim(bank)) > 0),
        sha256 TEXT,
        parser_name TEXT,
        parser_version TEXT,
        status TEXT NOT NULL CHECK(status IN ('PENDING','SUCCESS','FAILED')),
        source_count INTEGER CHECK(source_count IS NULL OR (typeof(source_count)='integer' AND source_count>=0)),
        new_count INTEGER CHECK(new_count IS NULL OR (typeof(new_count)='integer' AND new_count>=0)),
        repeated_count INTEGER CHECK(repeated_count IS NULL OR (typeof(repeated_count)='integer' AND repeated_count>=0)),
        error TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE categories (
        id TEXT PRIMARY KEY NOT NULL CHECK(length(trim(id)) > 0),
        name TEXT NOT NULL CHECK(length(trim(name)) > 0),
        name_key TEXT NOT NULL CHECK(length(name_key) > 0),
        parent_id TEXT REFERENCES categories(id),
        status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','ARCHIVED')),
        CHECK(parent_id IS NULL OR parent_id != id)
    )""",
    "CREATE UNIQUE INDEX category_name_per_parent ON categories(coalesce(parent_id, ''), name_key)",
    """CREATE TABLE own_accounts (
        id TEXT PRIMARY KEY NOT NULL CHECK(length(trim(id)) > 0),
        bank TEXT NOT NULL CHECK(length(trim(bank)) > 0),
        account TEXT NOT NULL CHECK(length(trim(account)) > 0),
        owner_name TEXT,
        phone TEXT,
        status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','ARCHIVED')),
        UNIQUE(bank, account)
    )""",
    """CREATE TABLE projects (
        id TEXT PRIMARY KEY NOT NULL CHECK(length(trim(id)) > 0),
        name TEXT NOT NULL CHECK(length(trim(name)) > 0),
        start_date TEXT,
        end_date TEXT,
        status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','ARCHIVED')),
        CHECK(start_date IS NULL OR end_date IS NULL OR start_date <= end_date)
    )""",
    """CREATE TABLE operations (
        id TEXT PRIMARY KEY NOT NULL CHECK(length(trim(id)) > 0),
        import_id TEXT NOT NULL REFERENCES imports(id),
        bank TEXT NOT NULL CHECK(length(trim(bank)) > 0),
        account_id TEXT NOT NULL CHECK(length(trim(account_id)) > 0),
        operation_date TEXT NOT NULL CHECK(length(operation_date)=10),
        processing_date TEXT CHECK(processing_date IS NULL OR length(processing_date)=10),
        description TEXT NOT NULL CHECK(length(trim(description)) > 0),
        amount_kopecks INTEGER NOT NULL CHECK(typeof(amount_kopecks)='integer' AND amount_kopecks>=0),
        direction TEXT NOT NULL CHECK(direction IN ('IN','OUT')),
        mcc TEXT,
        counterparty TEXT,
        bank_operation_type TEXT,
        balance_after_kopecks INTEGER CHECK(balance_after_kopecks IS NULL OR typeof(balance_after_kopecks)='integer'),
        source_row TEXT,
        source_data TEXT CHECK(source_data IS NULL OR json_valid(source_data)),
        bank_transaction_id TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE rules (
        id TEXT PRIMARY KEY NOT NULL CHECK(length(trim(id)) > 0),
        name TEXT NOT NULL CHECK(length(trim(name)) > 0),
        status TEXT NOT NULL DEFAULT 'DISABLED' CHECK(status IN ('ACTIVE','DISABLED')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE rule_versions (
        rule_id TEXT NOT NULL REFERENCES rules(id),
        version INTEGER NOT NULL CHECK(typeof(version)='integer' AND version>0),
        conditions_json TEXT NOT NULL CHECK(json_valid(conditions_json)),
        action_json TEXT NOT NULL CHECK(json_valid(action_json)),
        priority INTEGER NOT NULL CHECK(typeof(priority)='integer'),
        effective_from TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(rule_id,version)
    )""",
    """CREATE TABLE operation_link_groups (
        id TEXT PRIMARY KEY NOT NULL CHECK(length(trim(id)) > 0),
        link_type TEXT NOT NULL CHECK(link_type IN ('OWN_TRANSFER','REFUND','CHAIN')),
        status TEXT NOT NULL CHECK(status IN ('PROPOSED','AUTO_CONFIRMED','USER_CONFIRMED','REJECTED')),
        comment TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE operation_link_members (
        group_id TEXT NOT NULL REFERENCES operation_link_groups(id),
        operation_id TEXT NOT NULL REFERENCES operations(id),
        role TEXT NOT NULL CHECK(length(trim(role)) > 0),
        PRIMARY KEY(group_id,operation_id)
    )""",
    """CREATE TABLE manual_overrides (
        id INTEGER PRIMARY KEY,
        operation_id TEXT NOT NULL REFERENCES operations(id),
        previous_id INTEGER UNIQUE REFERENCES manual_overrides(id),
        operation_type TEXT NOT NULL CHECK(operation_type IN ('expense','income','technical','refund','undefined')),
        include_in_report INTEGER NOT NULL CHECK(include_in_report IN (0,1)),
        category_id TEXT REFERENCES categories(id),
        subcategory_id TEXT REFERENCES categories(id),
        project_id TEXT REFERENCES projects(id),
        comment TEXT NOT NULL CHECK(length(trim(comment)) > 0),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE classifications (
        operation_id TEXT PRIMARY KEY NOT NULL REFERENCES operations(id),
        operation_type TEXT NOT NULL CHECK(operation_type IN ('expense','income','technical','refund','undefined')),
        include_in_report INTEGER NOT NULL CHECK(include_in_report IN (0,1)),
        category_id TEXT REFERENCES categories(id),
        subcategory_id TEXT REFERENCES categories(id),
        project_id TEXT REFERENCES projects(id),
        source TEXT NOT NULL CHECK(source IN ('MANUAL','LINK','RULE','UNDEFINED')),
        manual_override_id INTEGER REFERENCES manual_overrides(id),
        related_group_id TEXT REFERENCES operation_link_groups(id),
        rule_id TEXT,
        rule_version INTEGER,
        comment TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(rule_id,rule_version) REFERENCES rule_versions(rule_id,version),
        CHECK((source='RULE' AND rule_id IS NOT NULL AND rule_version IS NOT NULL)
           OR (source!='RULE' AND rule_id IS NULL AND rule_version IS NULL)),
        CHECK((source='MANUAL' AND manual_override_id IS NOT NULL)
           OR (source!='MANUAL' AND manual_override_id IS NULL)),
        CHECK((source='LINK' AND related_group_id IS NOT NULL)
           OR (source!='LINK' AND related_group_id IS NULL)),
        CHECK(operation_type NOT IN ('technical','undefined') OR include_in_report=0)
    )""",
    """CREATE TABLE audit_log (
        id INTEGER PRIMARY KEY,
        event TEXT NOT NULL,
        entity TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        before_json TEXT CHECK(before_json IS NULL OR json_valid(before_json)),
        after_json TEXT CHECK(after_json IS NULL OR json_valid(after_json)),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
)

# These records are historical facts. Status changes go through the repository.
IMMUTABLE = ('operations', 'manual_overrides', 'rule_versions', 'audit_log')
NO_DELETE = IMMUTABLE + ('imports', 'categories', 'own_accounts', 'projects', 'rules',
                         'operation_link_groups', 'operation_link_members', 'classifications')
STATEMENTS += tuple(
    f"CREATE TRIGGER immutable_{table} BEFORE UPDATE ON {table} "
    "BEGIN SELECT RAISE(ABORT, 'Historical record is immutable'); END"
    for table in IMMUTABLE
)
STATEMENTS += tuple(
    f"CREATE TRIGGER preserve_{table} BEFORE DELETE ON {table} "
    "BEGIN SELECT RAISE(ABORT, 'Physical deletion is not supported'); END"
    for table in NO_DELETE
)
STATEMENTS += (
    """CREATE TRIGGER manual_previous_operation BEFORE INSERT ON manual_overrides
    WHEN NEW.previous_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM manual_overrides WHERE id=NEW.previous_id AND operation_id=NEW.operation_id
    ) BEGIN SELECT RAISE(ABORT, 'Previous decision belongs to another operation'); END""",
    """CREATE TRIGGER manual_classification_insert BEFORE INSERT ON classifications
    WHEN NEW.source='MANUAL' AND NOT EXISTS (
        SELECT 1 FROM manual_overrides WHERE id=NEW.manual_override_id AND operation_id=NEW.operation_id
    ) BEGIN SELECT RAISE(ABORT, 'Manual decision belongs to another operation'); END""",
    """CREATE TRIGGER manual_classification_update BEFORE UPDATE ON classifications
    WHEN NEW.source='MANUAL' AND NOT EXISTS (
        SELECT 1 FROM manual_overrides WHERE id=NEW.manual_override_id AND operation_id=NEW.operation_id
    ) BEGIN SELECT RAISE(ABORT, 'Manual decision belongs to another operation'); END""",
)
