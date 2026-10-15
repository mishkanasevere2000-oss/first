"""Explicit single-file import. No deduplication or financial classification."""
from pathlib import Path
from uuid import uuid4

from program.database.connection import DEFAULT_DATABASE, connect
from program.database.operations import Store


def import_statement(statement, source_name, database=DEFAULT_DATABASE):
    identifier = uuid4().hex
    metadata = dict(period_start=statement.period_start, period_end=statement.period_end,
                    opening_balance_kopecks=statement.opening_balance_kopecks,
                    closing_balance_kopecks=statement.closing_balance_kopecks,
                    incoming_kopecks=statement.incoming_kopecks, outgoing_kopecks=statement.outgoing_kopecks,
                    warnings=list(statement.warnings))
    operations = [dict(
        id=uuid4().hex, account_id=statement.account_id, operation_date=row.operation_date,
        processing_date=row.processing_date, description=row.description,
        amount_kopecks=row.amount_kopecks, direction=row.direction,
        balance_after_kopecks=row.balance_after_kopecks,
        source_row=f'page={row.page};layout_line={row.line}',
        source_data=dict(parser_name=statement.parser_name, parser_version=statement.parser_version,
                         operation_time_moscow=row.operation_time, authorization_code=row.authorization_code,
                         description_lines=list(row.description_lines), statement=metadata),
    ) for row in statement.operations]
    with connect(database) as connection:
        return Store(connection).add_first_bank_import(identifier, Path(source_name).name, statement.bank,
                                                      statement.parser_name, statement.parser_version, operations)


def format_kopecks(amount):
    sign = '-' if amount < 0 else ''
    whole, fraction = divmod(abs(amount), 100)
    grouped = f'{whole:,}'.replace(',', ' ')
    return f'{sign}{grouped},{fraction:02}'


def preview_lines(statement):
    lines = [f'Формат: {statement.parser_name}, версия {statement.parser_version}.',
             f'Банк: {statement.bank}. Счёт: …{statement.account_id[-4:]}.',
             f'Период в шапке: {statement.period_start} — {statement.period_end}.',
             f'Прочитано операций: {len(statement.operations)}.',
             f'Пополнения по выписке: {statement.incoming_count}, сумма {format_kopecks(statement.incoming_kopecks)} ₽.',
             f'Списания по выписке: {statement.outgoing_count}, сумма {format_kopecks(statement.outgoing_kopecks)} ₽.',
             'Суммы строк совпадают с итогами банка. Это банковские обороты, а не расчёт доходов и расходов.']
    lines.extend(f'ПРЕДУПРЕЖДЕНИЕ: {warning}' for warning in statement.warnings)
    return lines
