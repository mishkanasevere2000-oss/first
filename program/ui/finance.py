"""Stage-5 controls; no rule activation or manual decisions at menu startup."""
from pathlib import Path
import sqlite3

from program.database.connection import connect,DatabaseError
from program.database.migrations import validate_database
from program.processing.engine import preview,apply
from program.processing.__main__ import lines


def control(root):
    database=Path(root)/'database'/'finance.db'
    try:
        print('1. Проверить базу\n2. Обработать новые операции\n3. Пересчитать историю\n0. Назад')
        choice=input('Выберите действие: ').strip()
        if choice=='0':return
        if choice=='1':
            with connect(database,readonly=True) as connection:version=validate_database(connection)
            print(f'База исправна. Схема {version}.');return
        if choice not in {'2','3'}:print('Неизвестное действие.');return
        start=end=None
        if choice=='3':
            start=input('Начало периода YYYY-MM-DD (Enter — вся история): ').strip() or None
            end=input('Конец периода YYYY-MM-DD (Enter — без ограничения): ').strip() or None
        plan=preview(database,mode='history' if choice=='3' else 'new',start=start,end=end)
        print('\n'.join(lines(plan)))
        if not plan.changes and not plan.proposals:print('Изменений нет.');return
        answer=input('Сохранить показанные изменения? Введите ДА: ').strip().casefold()
        if answer!='да':print('Отменено. База не изменена.');return
        backup=apply(plan,database,confirmed=True)
        print(f'Решения сохранены. Проверенная резервная копия: {backup}')
        if plan.after['status']!='OK':print('Есть операции, требующие разбора. Это не готовый финансовый отчёт.')
    except (EOFError,KeyboardInterrupt):print('\nДействие отменено.')
    except (DatabaseError,sqlite3.Error,OSError,ValueError) as error:print(f'Действие не выполнено: {error}')
