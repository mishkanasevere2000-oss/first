from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from program.database.connection import DatabaseError, connect, transaction
from program.database.migrations import (
    MIGRATIONS, Migration, check_integrity, initialize, migrate, read_version,
    validate_database, verified_backup,
)
from program.database.operations import Store


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'данные с пробелами' / 'finance.db'
    assert initialize(path)
    return path


@pytest.fixture
def store(database):
    with connect(database) as connection:
        yield Store(connection)


def operation(store, identifier='o1', **changes):
    if not store.get('imports', 'i1'):
        store.create_import('i1', 'synthetic.xlsx', 'Example Bank')
    data = dict(id=identifier, import_id='i1', bank='Example Bank', account_id='test-account',
                operation_date='2026-01-01', description='Synthetic event', amount_kopecks=64798,
                direction='OUT', balance_after_kopecks=-100, source_data={'test': True})
    data.update(changes)
    store.add_operation(**data)
    return identifier


def test_initialize_reopen_and_schema(database):
    with connect(database, readonly=True) as connection:
        assert validate_database(connection) == 1
        assert connection.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {'schema_metadata', 'imports', 'categories', 'own_accounts', 'projects',
                          'operations', 'rules', 'rule_versions', 'classifications', 'manual_overrides',
                          'operation_link_groups', 'operation_link_members', 'audit_log'}
        columns = {row[1] for row in connection.execute('PRAGMA table_info(operations)')}
        assert 'currency' not in columns and 'bank_category' not in columns
        for table in tables - {'schema_metadata'}:
            assert connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
    assert not initialize(database)


def test_idempotent_init_preserves_data(store, database):
    operation(store)
    before = store.get('operations', 'o1')
    assert not initialize(database)
    assert store.get('operations', 'o1') == before
    assert store.connection.execute('SELECT count(*) FROM audit_log').fetchone()[0] == 2


@pytest.mark.parametrize('changes', [
    {'amount_kopecks': 1.25}, {'amount_kopecks': 1.0}, {'amount_kopecks': True},
    {'amount_kopecks': -1}, {'balance_after_kopecks': 1.5}, {'direction': 'SIDE'},
    {'operation_date': '2026-02-30'}, {'processing_date': '2026-1-1'},
    {'description': ''}, {'bank': ' '}, {'account_id': ''}, {'id': ''},
])
def test_operation_invalid_input(store, changes):
    with pytest.raises(ValueError):
        operation(store, **changes)
    assert store.connection.execute('SELECT count(*) FROM operations').fetchone()[0] == 0


def test_money_and_origin(store):
    operation(store)
    row = store.get('operations', 'o1')
    assert row['amount_kopecks'] == 64798
    assert row['balance_after_kopecks'] == -100
    assert store.connection.execute('SELECT typeof(amount_kopecks) FROM operations').fetchone()[0] == 'integer'
    assert row['source_data'] == '{"test": true}'


def test_database_rejects_invalid_direct_writes(store):
    operation(store)
    # These go past Python validation and must still be rejected by SQLite.
    for assignment, value in [('direction', 'SIDE'), ('amount_kopecks', 1.25),
                              ('amount_kopecks', -2), ('description', None), ('bank', '')]:
        columns = ['id', 'import_id', 'bank', 'account_id', 'operation_date', 'description', 'amount_kopecks', 'direction']
        values = ['bad', 'i1', 'Example Bank', 'account', '2026-01-01', 'test', 1, 'OUT']
        values[columns.index(assignment)] = value
        with pytest.raises(sqlite3.IntegrityError):
            store.connection.execute(f"INSERT INTO operations({','.join(columns)}) VALUES (?,?,?,?,?,?,?,?)", values)
    with pytest.raises(sqlite3.IntegrityError):
        operation(store, 'missing', import_id='no-such-import')


def test_manual_decisions_are_history_and_facts_unchanged(store, database):
    operation(store)
    before = store.get('operations', 'o1')
    store.record_manual_decision('o1', operation_type='expense', include_in_report=True, comment='First decision')
    first = store.get('classifications', 'o1')['manual_override_id']
    store.record_manual_decision('o1', operation_type='technical', include_in_report=False, comment='Correction')
    assert store.get('operations', 'o1') == before
    rows = store.connection.execute('SELECT * FROM manual_overrides ORDER BY id').fetchall()
    assert len(rows) == 2 and rows[1]['previous_id'] == first
    assert rows[0]['operation_type'] == 'expense'
    with connect(database) as connection:
        assert Store(connection).get('classifications', 'o1')['operation_type'] == 'technical'
    with pytest.raises(DatabaseError):
        store.set_classification('o1', source='UNDEFINED', operation_type='undefined', include_in_report=False)
    for table in ['operations', 'manual_overrides', 'audit_log']:
        with pytest.raises(sqlite3.IntegrityError):
            store.connection.execute(f'DELETE FROM {table}')
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute("UPDATE operations SET description='changed'")
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute("UPDATE manual_overrides SET comment='changed'")


def test_rule_versions_preserve_exact_reference(store):
    operation(store)
    store.create_rule('r1', 'Synthetic rule')
    for version in [1, 2]:
        store.add_rule_version('r1', version, {'and': []}, {'type': 'expense'}, 20, '2026-01-01')
    store.set_classification('o1', source='RULE', operation_type='expense', include_in_report=True, rule_id='r1', rule_version=1)
    assert store.get('classifications', 'o1')['rule_version'] == 1
    assert store.connection.execute('SELECT count(*) FROM rule_versions').fetchone()[0] == 2
    with pytest.raises(sqlite3.IntegrityError):
        store.add_rule_version('r1', 1, {}, {}, 20, '2026-01-01')
    with pytest.raises(sqlite3.IntegrityError):
        store.set_classification('o1', source='RULE', operation_type='expense', include_in_report=True, rule_id='r1', rule_version=3)
    assert store.get('classifications', 'o1')['rule_version'] == 1
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute('UPDATE rule_versions SET priority=50')
    store.set_status('rules', 'r1', 'DISABLED')
    assert store.get('classifications', 'o1')['rule_version'] == 1


def test_groups_support_three_members_and_rollback_bad_links(store):
    for index in range(3):
        operation(store, f'o{index}')
    store.create_link_group('g1', 'CHAIN', 'USER_CONFIRMED', [(f'o{i}', 'member') for i in range(3)])
    assert store.connection.execute('SELECT count(*) FROM operation_link_members').fetchone()[0] == 3
    store.set_classification('o0', source='LINK', operation_type='technical', include_in_report=False, related_group_id='g1')
    with pytest.raises(sqlite3.IntegrityError):
        store.create_link_group('bad', 'CHAIN', 'PROPOSED', [('o0', 'member'), ('absent', 'member')])
    assert store.get('operation_link_groups', 'bad') is None
    store.create_link_group('g2', 'CHAIN', 'PROPOSED', [('o1', 'member'), ('o2', 'member')])
    with pytest.raises(ValueError):
        store.set_classification('o1', source='LINK', operation_type='technical', include_in_report=False, related_group_id='g2')


def test_projects_import_status_and_soft_archive(store):
    store.create_project('p1', 'Example project', '2026-01-01', '2026-01-31')
    store.set_status('projects', 'p1', 'ARCHIVED')
    assert store.get('projects', 'p1')['status'] == 'ARCHIVED'
    store.create_import('i1', 'synthetic.xlsx', 'Example Bank')
    store.set_import_status('i1', 'FAILED')
    assert store.get('imports', 'i1')['status'] == 'FAILED'
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute("DELETE FROM projects WHERE id='p1'")


def test_transaction_rollback(store):
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(store.connection):
            store.connection.execute("INSERT INTO rules(id,name) VALUES ('r1','Example')")
            store.connection.execute("INSERT INTO rules(id,name) VALUES ('r1','Duplicate')")
    assert store.get('rules', 'r1') is None


def test_unknown_schema_is_not_changed(database):
    with connect(database) as connection:
        connection.execute('UPDATE schema_metadata SET schema_version=999')
    before = database.read_bytes()
    with pytest.raises(DatabaseError):
        initialize(database)
    assert database.read_bytes() == before


@pytest.mark.parametrize('contents', [b'not a database', b''])
def test_corrupt_or_empty_existing_file_is_not_recreated(tmp_path, contents):
    path = tmp_path / 'broken.db'
    path.write_bytes(contents)
    with pytest.raises((DatabaseError, sqlite3.DatabaseError)):
        initialize(path)
    assert path.read_bytes() == contents


def test_missing_table_or_trigger_rejected(database):
    with connect(database) as connection:
        connection.execute('DROP TRIGGER immutable_operations')
        with pytest.raises(DatabaseError):
            validate_database(connection)
    before = database.read_bytes()
    with pytest.raises(DatabaseError):
        initialize(database)
    assert database.read_bytes() == before


def test_missing_fk_data_is_reported(database):
    connection = sqlite3.connect(database)
    try:
        connection.execute("INSERT INTO rule_versions(rule_id,version,conditions_json,action_json,priority,effective_from) VALUES ('missing',1,'{}','{}',1,'2026-01-01')")
        connection.commit()
    finally:
        connection.close()
    with connect(database, readonly=True) as connection:
        with pytest.raises(DatabaseError):
            validate_database(connection)


def second_migration(fail=False):
    def apply(connection):
        connection.execute('CREATE TABLE test_new_table(id INTEGER PRIMARY KEY)')
        connection.execute("UPDATE projects SET name='changed by migration'")
        if fail:
            raise RuntimeError('Injected migration failure')
    def validate(connection):
        assert read_version(connection) == 2
        connection.execute('SELECT id FROM test_new_table').fetchall()
        check_integrity(connection)
    return (*MIGRATIONS, Migration(2, apply, validate))


def test_successful_migration_backup_and_repeat(database, tmp_path):
    with connect(database) as connection:
        Store(connection).create_project('p1', 'Original')
        migrations = second_migration()
        backup = migrate(connection, migrations=migrations, backup_dir=tmp_path / 'backups')
        assert read_version(connection) == 2
        assert migrate(connection, migrations=migrations) is None
    with connect(backup, readonly=True) as connection:
        assert validate_database(connection) == 1
        assert Store(connection).get('projects', 'p1')['name'] == 'Original'


def test_failed_migration_rolls_back_version_schema_and_data(database, tmp_path):
    with connect(database) as connection:
        Store(connection).create_project('p1', 'Original')
        with pytest.raises(RuntimeError):
            migrate(connection, migrations=second_migration(fail=True), backup_dir=tmp_path / 'backups')
        assert validate_database(connection) == 1
        assert Store(connection).get('projects', 'p1')['name'] == 'Original'
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='test_new_table'").fetchone() is None
    backups = list((tmp_path / 'backups').glob('*.db'))
    assert len(backups) == 1
    with connect(backups[0], readonly=True) as connection:
        assert validate_database(connection) == 1


def test_backup_failure_blocks_migration(database, tmp_path, monkeypatch):
    import program.database.migrations as module
    def fail(*args):
        raise OSError('Backup storage unavailable')
    monkeypatch.setattr(module, 'verified_backup', fail)
    with connect(database) as connection:
        with pytest.raises(OSError):
            migrate(connection, migrations=second_migration(), backup_dir=tmp_path)
        assert validate_database(connection) == 1


def test_invalid_backup_removed(database, tmp_path):
    def reject(_):
        raise DatabaseError('Bad backup')
    with connect(database) as connection:
        with pytest.raises(DatabaseError):
            verified_backup(connection, tmp_path / 'copies', reject)
    assert list((tmp_path / 'copies').iterdir()) == []


def test_initial_migration_once(database):
    with connect(database) as connection:
        assert migrate(connection) is None
        with pytest.raises(DatabaseError):
            migrate(connection, new=True)
        assert validate_database(connection) == 1


def test_check_missing_database_never_creates_file(tmp_path):
    path = tmp_path / 'absent.db'
    with pytest.raises(sqlite3.OperationalError):
        with connect(path, readonly=True):
            pass
    assert not path.exists()


def test_cli_developer_workflow(tmp_path):
    path = tmp_path / 'новая база' / 'finance.db'
    for command, expected in [('check', 1), ('init', 0), ('init', 0), ('check', 0)]:
        result = subprocess.run([sys.executable, '-X', 'utf8', '-m', 'program.database', command, '--database', str(path)],
                                capture_output=True, text=True, encoding='utf-8', timeout=15)
        assert result.returncode == expected, result.stdout + result.stderr
        assert 'Traceback' not in result.stderr
    path.write_bytes(b'broken')
    result = subprocess.run([sys.executable, '-X', 'utf8', '-m', 'program.database', 'init', '--database', str(path)],
                            capture_output=True, text=True, encoding='utf-8', timeout=15)
    assert result.returncode == 1 and 'Ошибка' in result.stdout
    assert path.read_bytes() == b'broken'


def test_imports_and_menu_do_not_create_default_database(tmp_path):
    import shutil
    root = Path(__file__).resolve().parents[1]
    installation = tmp_path / 'installation'
    shutil.copytree(root / 'program', installation / 'program', ignore=shutil.ignore_patterns('__pycache__'))
    code = ('import program.database.connection; import program.database.migrations; '
            'import program.database.operations; import program.database.references; '
            'import program.database.__main__; from program.main import build_menu; '
            'assert "Версия" in build_menu()')
    result = subprocess.run([sys.executable, '-X', 'utf8', '-c', code], cwd=installation,
                            capture_output=True, text=True, encoding='utf-8', timeout=15)
    assert result.returncode == 0, result.stderr
    assert not (installation / 'database').exists()


def test_manual_decision_failure_keeps_previous_history_and_audit(store, monkeypatch):
    operation(store)
    store.record_manual_decision('o1', operation_type='expense', include_in_report=True, comment='Original')
    before = store.get('classifications', 'o1')
    audit_count = store.connection.execute('SELECT count(*) FROM audit_log').fetchone()[0]
    def fail(*args):
        raise RuntimeError('Injected audit failure')
    monkeypatch.setattr(store, '_audit', fail)
    with pytest.raises(RuntimeError):
        store.record_manual_decision('o1', operation_type='technical', include_in_report=False, comment='Failed change')
    assert store.get('classifications', 'o1') == before
    assert store.connection.execute('SELECT count(*) FROM manual_overrides').fetchone()[0] == 1
    assert store.connection.execute('SELECT count(*) FROM audit_log').fetchone()[0] == audit_count


def test_cli_bad_reference_is_clear_error(database, tmp_path):
    path = tmp_path / 'broken.xlsx'
    path.write_bytes(b'broken')
    result = subprocess.run([sys.executable, '-X', 'utf8', '-m', 'program.database', 'import-categories',
                             '--database', str(database), '--file', str(path)],
                            capture_output=True, text=True, encoding='utf-8', timeout=15)
    assert result.returncode == 1
    assert 'Ошибка' in result.stdout and 'Traceback' not in result.stderr


@pytest.mark.parametrize('reject_backup', [False, True])
def test_backup_closes_all_owned_sqlite_connections(database, tmp_path, monkeypatch, reject_backup):
    """A real open SQLite handle blocks unlink on Windows, even after with exits."""
    original_connect = sqlite3.connect
    owned = []

    class TrackedConnection(sqlite3.Connection):
        closed = False

        def close(self):
            try:
                super().close()
            finally:
                self.closed = True

    def tracked_connect(*args, **kwargs):
        kwargs['factory'] = TrackedConnection
        connection = original_connect(*args, **kwargs)
        owned.append(connection)
        return connection

    with connect(database) as source:
        monkeypatch.setattr(sqlite3, 'connect', tracked_connect)

        def validator(connection):
            validate_database(connection)
            if reject_backup:
                raise DatabaseError('Rejected backup')

        try:
            if reject_backup:
                with pytest.raises(DatabaseError, match='Rejected backup'):
                    verified_backup(source, tmp_path / 'copies', validator)
                assert list((tmp_path / 'copies').iterdir()) == []
            else:
                backup = verified_backup(source, tmp_path / 'copies', validator)
                assert backup.is_file()
            assert owned
            assert all(connection.closed for connection in owned)
            for connection in owned:
                with pytest.raises(sqlite3.ProgrammingError):
                    connection.execute('SELECT 1')
        finally:
            for connection in owned:
                connection.close()
