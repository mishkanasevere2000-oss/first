"""Transactional schema migrations with verified SQLite backups."""
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import tempfile
from typing import Callable

from .connection import DEFAULT_DATABASE, DatabaseError, connect, transaction
from .schema import SCHEMA_VERSION, STATEMENTS
from .schema_v2 import STATEMENTS_V2
from program.processing.deduplication import attach_identity


def read_version(connection):
    try:
        rows = connection.execute('SELECT singleton,schema_version FROM schema_metadata').fetchall()
    except sqlite3.DatabaseError as error:
        raise DatabaseError('Файл повреждён или не является базой финансового анализатора.') from error
    if len(rows) != 1 or rows[0][0] != 1 or type(rows[0][1]) is not int:
        raise DatabaseError('Некорректные сведения о версии схемы.')
    return rows[0][1]


def check_integrity(connection):
    if [row[0] for row in connection.execute('PRAGMA integrity_check')] != ['ok']:
        raise DatabaseError('Нарушена целостность SQLite. База не изменена.')
    if connection.execute('PRAGMA foreign_key_check').fetchall():
        raise DatabaseError('Нарушены связи между записями базы.')


def schema_objects(connection):
    return {row[0]: (row[1], row[2], row[3]) for row in connection.execute(
        "SELECT name,type,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    )}


def create_v1(connection):
    for statement in STATEMENTS:
        connection.execute(statement)
    connection.execute('INSERT INTO schema_metadata VALUES (1,1)')


def validate_v1(connection):
    with closing(sqlite3.connect(':memory:')) as expected:
        create_v1(expected)
        if schema_objects(connection) != schema_objects(expected):
            raise DatabaseError('Структура базы несовместима со схемой 1; автоматическое исправление запрещено.')
    if read_version(connection) != 1:
        raise DatabaseError('Версия базы несовместима со схемой 1.')
    check_integrity(connection)


@dataclass(frozen=True)
class Migration:
    version: int
    apply: Callable
    validate: Callable


def create_v2(connection):
    for statement in STATEMENTS_V2:
        connection.execute(statement)
    # Existing banking records, IDs and decisions are preserved byte for byte.
    for row in connection.execute('SELECT * FROM operations ORDER BY rowid').fetchall():
        attach_identity(connection, dict(row))


def validate_v2(connection):
    with closing(sqlite3.connect(':memory:')) as expected:
        create_v1(expected)
        create_v2(expected)
        if schema_objects(connection) != schema_objects(expected):
            raise DatabaseError('Структура базы несовместима со схемой 2.')
    if read_version(connection) != 2:
        raise DatabaseError('Ожидается схема 2.')
    check_integrity(connection)
    if connection.execute('SELECT count(*) FROM operations').fetchone()[0] != connection.execute(
            'SELECT count(*) FROM operation_identities').fetchone()[0]:
        raise DatabaseError('Не все операции имеют идентичность для поиска повторов.')
    if connection.execute(
        "SELECT 1 FROM source_files f JOIN imports i ON i.id=f.first_import_id "
        "WHERE i.status!='SUCCESS' OR i.sha256!=f.sha256 OR i.source_count!=f.source_count LIMIT 1"
    ).fetchone():
        raise DatabaseError('Нарушена история исходных файлов.')
    if connection.execute(
        "SELECT 1 FROM imports i WHERE status='SUCCESS' AND sha256 IS NOT NULL AND "
        "(NOT EXISTS(SELECT 1 FROM source_files f WHERE f.sha256=i.sha256) "
        "OR source_count!=new_count+repeated_count "
        "OR source_count!=(SELECT count(*) FROM import_observations o WHERE o.import_id=i.id) "
        "OR new_count!=(SELECT count(*) FROM import_observations o WHERE o.import_id=i.id AND is_new=1)) LIMIT 1"
    ).fetchone():
        raise DatabaseError('Счётчики загрузки не совпадают с её наблюдениями.')



MIGRATIONS = (Migration(1, create_v1, validate_v1), Migration(2, create_v2, validate_v2))


def validate_database(connection):
    version = read_version(connection)
    if version != SCHEMA_VERSION:
        raise DatabaseError(f'Версия базы {version} не поддерживается программой (ожидается {SCHEMA_VERSION}). '
                            'Для старой базы выполните init: обновление создаёт проверенный backup.')
    MIGRATIONS[version-1].validate(connection)
    return version


def verified_backup(connection, directory, validator):
    if connection.in_transaction:
        raise DatabaseError('Резервную копию нельзя создавать внутри изменяющей транзакции.')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    # mkstemp guarantees a new name, never overwriting an existing backup.
    import os
    descriptor, filename = tempfile.mkstemp(prefix='before_migration_', suffix='.db', dir=directory)
    os.close(descriptor)
    path = Path(filename)
    try:
        # sqlite3.Connection.__exit__ handles a transaction, but does not close
        # the handle. Close before validating or removing the backup on Windows.
        with closing(sqlite3.connect(path)) as target:
            connection.backup(target)
        with connect(path, readonly=True) as target:
            validator(target)
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def migrate(connection, *, migrations=MIGRATIONS, backup_dir=None, new=False):
    if [m.version for m in migrations] != list(range(1, len(migrations) + 1)):
        raise DatabaseError('Миграции должны идти последовательно, начиная с 1.')
    if connection.in_transaction:
        raise DatabaseError('Миграция должна начинаться вне транзакции.')
    current = 0 if new else read_version(connection)
    if new and schema_objects(connection):
        raise DatabaseError('Начальная миграция разрешена только для новой пустой базы.')
    if current < 0 or current > len(migrations) or (not new and current == 0):
        raise DatabaseError('Неизвестная версия базы. Файл не изменён.')
    if current:
        migrations[current - 1].validate(connection)
    if current == len(migrations):
        return None
    backup = None
    if current:
        if backup_dir is None:
            raise DatabaseError('Для миграции существующей базы требуется каталог backup.')
        backup = verified_backup(connection, backup_dir, migrations[current - 1].validate)
    with transaction(connection):
        # Recheck the source under the write lock in case another process changed it.
        if current and read_version(connection) != current:
            raise DatabaseError('Версия базы изменилась; повторите операцию.')
        for step in migrations[current:]:
            step.apply(connection)
            connection.execute('UPDATE schema_metadata SET schema_version=? WHERE singleton=1', (step.version,))
            step.validate(connection)
    return backup


def initialize(path=DEFAULT_DATABASE):
    path = Path(path).resolve()
    if path.exists():
        with connect(path) as connection:
            from .connection import PROJECT_ROOT
            backup_dir = (PROJECT_ROOT if path == DEFAULT_DATABASE else path.parent) / 'backup'
            migrate(connection, backup_dir=backup_dir)
            validate_database(connection)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation avoids accidentally initializing an existing file.
    with path.open('xb'):
        pass
    try:
        with connect(path) as connection:
            migrate(connection, new=True)
        return True
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def copy_database(source, destination):
    """Verified SQLite copy; never overwrite either an existing destination or source."""
    destination = Path(destination).resolve()
    with connect(source, readonly=True) as original:
        version = read_version(original)
        if version < 1 or version > len(MIGRATIONS):
            raise DatabaseError('Неизвестная версия исходной базы.')
        validator = MIGRATIONS[version-1].validate
        validator(original)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open('xb'):
            pass
        try:
            with closing(sqlite3.connect(destination)) as target:
                original.backup(target)
            with connect(destination, readonly=True) as target:
                validator(target)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
    return version
