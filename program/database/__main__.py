"""Explicit developer commands; normal menu startup does not create a database."""
import argparse
from pathlib import Path
import sqlite3
from zipfile import BadZipFile
from xml.etree.ElementTree import ParseError
from openpyxl.utils.exceptions import InvalidFileException

from .connection import DEFAULT_DATABASE, DatabaseError, connect
from .migrations import copy_database, initialize, validate_database
from .schema import SCHEMA_VERSION
from .references import import_reference


def main(argv=None):
    parser = argparse.ArgumentParser(description='Создание и проверка локальной базы данных.')
    parser.add_argument('command', choices=('init', 'check', 'import-categories', 'import-accounts', 'copy', 'info'))
    parser.add_argument('--database', type=Path, default=DEFAULT_DATABASE)
    parser.add_argument('--file', type=Path, help='XLSX-справочник для импорта')
    parser.add_argument('--source', type=Path, help='Исходная база для безопасного копирования')
    args = parser.parse_args(argv)
    if args.command.startswith('import-') and args.file is None:
        parser.error('Для импорта укажите --file.')
    if args.command == 'copy' and args.source is None:
        parser.error('Для copy укажите --source.')
    try:
        if args.command == 'copy':
            version = copy_database(args.source, args.database)
            print(f'База скопирована и проверена. Схема {version}. Исходник не изменён.')
        elif args.command == 'init':
            created = initialize(args.database)
            print(f'База создана. Схема {SCHEMA_VERSION}.' if created else f'База существует, обновлена при необходимости и проверена. Схема {SCHEMA_VERSION}.')
        elif args.command == 'info':
            with connect(args.database,readonly=True) as connection:
                validate_database(connection)
                count=connection.execute('SELECT count(*) FROM operations').fetchone()[0]
                print(f'Операций в базе: {count}.')
                for status in ('SUCCESS','FAILED','PENDING'):
                    total=connection.execute('SELECT count(*) FROM imports WHERE status=?',(status,)).fetchone()[0]
                    print(f'Загрузок {status}: {total}.')
        elif args.command == 'check':
            with connect(args.database, readonly=True) as connection:
                version = validate_database(connection)
            print(f'База исправна. Схема {version}.')
        else:
            entity = 'categories' if args.command == 'import-categories' else 'own_accounts'
            with connect(args.database) as connection:
                changed = import_reference(connection, args.file, entity)
            print(f'Справочник проверен. Изменено записей: {changed}.')
        return 0
    except (DatabaseError, sqlite3.Error, OSError, ValueError, BadZipFile, ParseError, InvalidFileException) as error:
        print(f'Ошибка работы с базой: {error}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
