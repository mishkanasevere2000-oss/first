"""Preview is read-only; writing requires an explicit command and confirmation flag."""
import argparse
from pathlib import Path
import sqlite3

from program.database.connection import DEFAULT_DATABASE, DatabaseError
from .detector import read_statement
from .models import StatementError
from .service import import_statement, preview_lines


def main(argv=None):
    parser = argparse.ArgumentParser(description='Первый банковский PDF-импорт без классификации.')
    parser.add_argument('command', choices=('preview', 'import'))
    parser.add_argument('file', type=Path)
    parser.add_argument('--database', type=Path, default=DEFAULT_DATABASE)
    parser.add_argument('--confirm', action='store_true', help='Подтвердить запись в пустую базу')
    args = parser.parse_args(argv)
    if args.command == 'import' and not args.confirm:
        parser.error('Для записи нужен --confirm. Сначала выполните preview.')
    try:
        statement = read_statement(args.file)
        for line in preview_lines(statement):
            print(line)
        if args.command == 'import':
            count = import_statement(statement, args.file, args.database)
            print(f'Сохранено операций: {count}. Категории не назначались. Исходный PDF не перемещён.')
        else:
            print('Предпросмотр завершён. База данных не изменялась.')
        return 0
    except (StatementError, DatabaseError, sqlite3.Error, OSError, ValueError) as error:
        print(f'Импорт не выполнен: {error}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
