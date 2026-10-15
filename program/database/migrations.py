"""Transactional schema migrations with verified SQLite backups."""
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import tempfile
from typing import Callable

from .connection import DEFAULT_DATABASE, DatabaseError, connect, transaction
from .schema import SCHEMA_VERSION, STATEMENTS


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
    with sqlite3.connect(':memory:') as expected:
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


MIGRATIONS = (Migration(1, create_v1, validate_v1),)


def validate_database(connection):
    version = read_version(connection)
    if version != SCHEMA_VERSION:
        raise DatabaseError(f'Версия базы {version} не поддерживается программой (ожидается {SCHEMA_VERSION}).')
    validate_v1(connection)
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
        with sqlite3.connect(path) as target:
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
        with connect(path, readonly=True) as connection:
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
