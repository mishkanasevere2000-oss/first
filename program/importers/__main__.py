"""Read-only preview, explicit import, and explicit archive recovery."""
import argparse
from pathlib import Path
import sqlite3

from program.database.connection import DEFAULT_DATABASE, DatabaseError
from .detector import read_statement
from .models import StatementError
from .service import import_file, preview_lines, recover_imports, result_lines


def main(argv=None):
    parser = argparse.ArgumentParser(description='Банковский PDF-импорт без финансовой классификации.')
    parser.add_argument('command', choices=('preview', 'import', 'recover'))
    parser.add_argument('file', type=Path, nargs='?')
    parser.add_argument('--database', type=Path, default=DEFAULT_DATABASE)
    parser.add_argument('--confirm', action='store_true', help='Подтвердить запись и архивирование источника')
    args = parser.parse_args(argv)
    if args.command != 'recover' and args.file is None:
        parser.error('Укажите PDF-файл.')
    if args.command == 'import' and not args.confirm:
        parser.error('Для записи нужен --confirm. Сначала выполните preview.')
    try:
        if args.command == 'recover':
            warnings = recover_imports(args.database)
            for warning in warnings:
                print('ПРЕДУПРЕЖДЕНИЕ: ' + warning)
            print('Восстановление проверено.' if not warnings else 'Часть архивных действий требует внимания.')
            return 0 if not warnings else 1
        if args.command == 'preview':
            for line in preview_lines(read_statement(args.file)):
                print(line)
            print('Предпросмотр завершён. База данных не изменялась.')
        else:
            for line in result_lines(import_file(args.file, args.database)):
                print(line)
        return 0
    except (StatementError, DatabaseError, sqlite3.Error, OSError, ValueError) as error:
        print(f'Импорт не выполнен: {error}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
