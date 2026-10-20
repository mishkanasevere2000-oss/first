"""Existing-file connections; importing this module never touches the filesystem."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE = PROJECT_ROOT / 'database' / 'finance.db'


class DatabaseError(Exception):
    """A recoverable, user-facing database error."""


@contextmanager
def connect(path=DEFAULT_DATABASE, *, readonly=False):
    """Open an existing database only; callers explicitly initialize new files."""
    uri = Path(path).resolve().as_uri() + ('?mode=ro' if readonly else '?mode=rw')
    connection = sqlite3.connect(uri, uri=True, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA foreign_keys=ON')
        if connection.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
            raise DatabaseError('Не удалось включить проверку связей базы.')
        yield connection
    finally:
        connection.close()


@contextmanager
def transaction(connection):
    if connection.in_transaction:
        raise DatabaseError('Вложенная транзакция не поддерживается.')
    connection.execute('BEGIN IMMEDIATE')
    try:
        yield
        connection.execute('COMMIT')
    except BaseException:
        if connection.in_transaction:
            connection.execute('ROLLBACK')
        raise


@contextmanager
def read_snapshot(connection):
    """A consistent read transaction; callers use a read-only connection."""
    if connection.in_transaction:
        raise DatabaseError('Вложенный снимок чтения не поддерживается.')
    connection.execute('BEGIN')
    try:
        yield
    finally:
        if connection.in_transaction:
            connection.execute('ROLLBACK')
