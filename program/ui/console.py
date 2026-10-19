"""Sequential file import: preview and explicit consent for each source."""
from pathlib import Path
import sqlite3

from program.database.connection import DatabaseError
from program.importers.detector import read_statement
from program.importers.models import StatementError
from program.importers.service import import_statement, preview_lines, record_failed_preview, result_lines


def import_first_pdf(root):
    root = Path(root)
    try:
        folder = root / 'input'
        files = sorted((path for path in folder.iterdir() if path.is_file() and path.suffix.lower() == '.pdf'),
                       key=lambda path: path.name) if folder.is_dir() else []
    except OSError as error:
        print(f'Не удалось прочитать input: {error}')
        return
    if not files:
        print('Поместите PDF-выписку кредитной или дебетовой карты Сбера, справку Ozon Банка либо выписку карты ВТБ или РСХБ в папку input. Ничего не импортировано.\n')
        return
    print(f'Найдено PDF-файлов: {len(files)}. Каждый файл подтверждается отдельно.')
    for source in files:
        preview_complete = False
        try:
            print(f'Файл: {source.name}')
            statement = read_statement(source)
            preview_complete = True
            for line in preview_lines(statement):
                print(line)
            try:
                answer = input('Сохранить новые операции и перенести PDF в архив? Введите ДА: ').strip().casefold()
            except (EOFError, KeyboardInterrupt):
                print('Импорт отменён.\n')
                return
            if answer != 'да':
                print('Импорт отменён. База не изменена.\n')
                continue
            result = import_statement(statement, source, root / 'database' / 'finance.db')
            for line in result_lines(result):
                print(line)
        except (StatementError, DatabaseError, sqlite3.Error, OSError, ValueError) as error:
            if not preview_complete:
                try:
                    record_failed_preview(source, root / 'database' / 'finance.db', error)
                except (DatabaseError,sqlite3.Error,OSError,ValueError):
                    pass  # The original error is more useful when the database is unavailable.
            print(f'Импорт не выполнен: {error}\n')
