"""Explicit developer commands; normal menu startup does not create a database."""
import argparse
from pathlib import Path
import sqlite3
from zipfile import BadZipFile
from xml.etree.ElementTree import ParseError
from openpyxl.utils.exceptions import InvalidFileException

from .connection import DEFAULT_DATABASE, DatabaseError, connect
from .migrations import initialize, validate_database
from .references import import_reference


def main(argv=None):
    parser = argparse.ArgumentParser(description='Создание и проверка локальной базы данных.')
    parser.add_argument('command', choices=('init', 'check', 'import-categories', 'import-accounts'))
    parser.add_argument('--database', type=Path, default=DEFAULT_DATABASE)
    parser.add_argument('--file', type=Path, help='XLSX-справочник для импорта')
    args = parser.parse_args(argv)
    if args.command.startswith('import-') and args.file is None:
        parser.error('Для импорта укажите --file.')
    try:
        if args.command == 'init':
            created = initialize(args.database)
            print('База создана. Схема 1.' if created else 'База уже существует и проверена. Схема 1.')
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
