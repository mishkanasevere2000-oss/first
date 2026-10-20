"""Explicit previews and financial processing; does not run at ordinary startup."""
import argparse
import json
from pathlib import Path
import sqlite3

from program.database.connection import DEFAULT_DATABASE,DatabaseError,connect
from program.database.migrations import validate_database
from program.importers.service import format_kopecks
from .engine import preview,apply,save_plan,read_plan,load,totals
from .catalog import install,prepare
from .rules import EngineError,iso


def lines(plan):
    after=plan.after
    return [f'Режим: {"пересчёт истории" if plan.mode=="history" else "новые операции и подтверждённые связи"}.',
            f'Период операций: {plan.start or "без ограничения начала"} — {plan.end or "без ограничения конца"}. Правила на дату {plan.as_of}.',
            f'Изменится решений: {len(plan.changes)}. Новых групп: {len(plan.proposals)}.',
            f"Расходы: {format_kopecks(plan.before['expenses_kopecks'])} → {format_kopecks(after['expenses_kopecks'])} ₽.",
            f"Доходы: {format_kopecks(plan.before['incomes_kopecks'])} → {format_kopecks(after['incomes_kopecks'])} ₽.",
            f"Не определён финансовый тип: {len(after['undefined'])}. Требует категории: {len(after['review_required'])}.",
            f"Предполагаемых связей: {after['proposed_groups']}. Возвратов без подтверждения: {len(after['unlinked_refunds'])}.",
            f"Операций с датой проводки вместо даты совершения: {len(after['date_fallbacks'])}.",
            f"Предварительный контроль: {after['status']}. Excel-отчёт не формировался."]


def main(argv=None):
    parser=argparse.ArgumentParser(description='Финансовый двигатель: просмотр перед изменениями.')
    parser.add_argument('command',choices=('preview','apply','status','import-rules'))
    parser.add_argument('--database',type=Path,default=DEFAULT_DATABASE)
    parser.add_argument('--plan',type=Path)
    parser.add_argument('--file',type=Path)
    parser.add_argument('--confirm',action='store_true')
    parser.add_argument('--history',action='store_true')
    parser.add_argument('--as-of');parser.add_argument('--start');parser.add_argument('--end')
    args=parser.parse_args(argv)
    if args.command=='apply' and (args.plan is None or not args.confirm):parser.error('Для apply нужны --plan и --confirm.')
    if args.command=='import-rules' and args.file is None:parser.error('Укажите --file JSON.')
    try:
        if args.command=='preview':
            plan=preview(args.database,mode='history' if args.history else 'new',as_of=args.as_of,start=args.start,end=args.end)
            print('\n'.join(lines(plan)))
            if args.plan:save_plan(plan,args.plan);print('Предварительный расчёт сохранён; база не изменена.')
        elif args.command=='apply':
            plan=read_plan(args.plan)
            backup=apply(plan,args.database,confirmed=args.confirm)
            print(f'Решения сохранены. Проверенный backup: {backup}' if backup else 'Изменений нет. База не изменена.')
        elif args.command=='import-rules':
            with args.file.open(encoding='utf-8') as source:records=json.load(source)
            with connect(args.database) as connection:
                validate_database(connection);prepare(connection,records)
                print(json.dumps(records,ensure_ascii=False,indent=2))
                if args.confirm:
                    root=args.database.parent.parent if args.database.parent.name=='database' else args.database.parent
                    count=install(connection,records,confirmed=True,backup_dir=root/'backup')
                    print(f'Правила сохранены. Изменений: {count}. История операций не пересчитывалась.')
                else:print('Только просмотр. Для установки/активации требуется отдельный запуск с --confirm.')
        else:
            if args.start:iso(args.start)
            if args.end:iso(args.end)
            if args.start and args.end and args.start>args.end:raise EngineError('Неверный период.')
            with connect(args.database,readonly=True) as connection:
                validate_database(connection);ops,current,manuals,groups=load(connection)
                result=totals(ops,current,groups,manuals,args.start,args.end)
                print(f"Операций: {len(ops)}. Решений: {len(current)}. Неопределённых: {len(result['undefined'])}.")
                print(f"Расходы: {format_kopecks(result['expenses_kopecks'])} ₽. Доходы: {format_kopecks(result['incomes_kopecks'])} ₽.")
                print(f"Предварительный контроль: {result['status']}. Excel-отчёт не формировался.")
        return 0
    except (EngineError,DatabaseError,sqlite3.Error,OSError,ValueError,TypeError,KeyError) as error:
        print(f'Финансовый разбор не выполнен: {error}');return 1


if __name__=='__main__':raise SystemExit(main())
