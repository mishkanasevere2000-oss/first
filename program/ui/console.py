"""First import through the regular console, with preview and explicit consent."""
from pathlib import Path
import sqlite3

from program.database.connection import DatabaseError
from program.importers.detector import read_statement
from program.importers.models import StatementError
from program.importers.service import import_statement, preview_lines


def import_first_pdf(root):
    root = Path(root)
    folder = root / 'input'
    files = sorted((path for path in folder.iterdir() if path.is_file() and path.suffix.lower() == '.pdf'),
                   key=lambda path: path.name) if folder.is_dir() else []
    if not files:
        print('Поместите одну PDF-выписку кредитной карты Сбера в папку input. Ничего не импортировано.\n')
        return
    if len(files) != 1:
        print('Этап 2: в input должна быть ровно одна PDF-выписка. Пакетный импорт пока не поддерживается.\n')
        return
    try:
        statement = read_statement(files[0])
        for line in preview_lines(statement):
            print(line)
        print('Этап 2: запись только в пустую базу. Повторный импорт пока недоступен.')
        try:
            answer = input('Сохранить операции? Введите ДА, иначе импорт отменится: ').strip().casefold()
        except (EOFError, KeyboardInterrupt):
            answer = ''
        if answer != 'да':
            print('Импорт отменён. База не изменена.\n')
            return
        count = import_statement(statement, files[0], root / 'database' / 'finance.db')
        print(f'Сохранено операций: {count}. Категории не назначались. PDF остался в input.\n')
    except (StatementError, DatabaseError, sqlite3.Error, OSError, ValueError) as error:
        print(f'Импорт не выполнен: {error}\n')
