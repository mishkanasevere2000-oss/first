"""Cumulative import with immutable observations, file hashes and durable archives."""
from dataclasses import dataclass
import json
from pathlib import Path
from uuid import uuid4

from program.database.connection import DEFAULT_DATABASE, PROJECT_ROOT, connect
from program.database.operations import Store
from program.database.imports import ImportLedger
from . import archive
from .detector import read_statement_bytes
from .models import StatementError


@dataclass(frozen=True)
class ImportResult:
    import_id: str
    new_count: int
    repeated_count: int
    source_count: int
    archive_path: Path | None
    source_removed: bool
    warnings: tuple[str, ...]
    known_file: bool


def storage_root(database):
    path = Path(database).resolve()
    if path == DEFAULT_DATABASE:
        return PROJECT_ROOT
    return path.parent.parent if path.parent.name == 'database' else path.parent


def operation_records(statement, identifier):
    metadata = dict(period_start=statement.period_start, period_end=statement.period_end,
                    opening_balance_kopecks=statement.opening_balance_kopecks,
                    closing_balance_kopecks=statement.closing_balance_kopecks,
                    incoming_kopecks=statement.incoming_kopecks, outgoing_kopecks=statement.outgoing_kopecks,
                    warnings=list(statement.warnings),
                    bank_turnover_totals_present=statement.bank_turnover_totals_present)
    records = [dict(
        id=uuid4().hex, import_id=identifier, bank=statement.bank,
        account_id=statement.account_id, operation_date=row.operation_date,
        processing_date=row.processing_date, description=row.description,
        amount_kopecks=row.amount_kopecks, direction=row.direction,
        mcc=row.mcc, bank_transaction_id=row.bank_transaction_id,
        balance_after_kopecks=row.balance_after_kopecks,
        source_row=f'page={row.page};layout_line={row.line}',
        source_data=dict(parser_name=statement.parser_name, parser_version=statement.parser_version,
                         **({'operation_time_moscow': row.operation_time} if row.operation_time_zone == 'Europe/Moscow'
                            else {'operation_time_local': row.operation_time, 'operation_time_zone': row.operation_time_zone}),
                         authorization_code=row.authorization_code,
                         **({'bank_operation_code': row.bank_operation_code} if row.bank_operation_code is not None else {}),
                         **({'operation_date_source': row.operation_date_source} if row.operation_date_source is not None else {}),
                         **({'card_number_mask': row.card_number_mask} if row.card_number_mask else {}),
                         **({'processing_time_local': row.processing_time} if row.processing_time is not None else {}),
                         description_lines=list(row.description_lines), statement=metadata),
    ) for row in statement.operations]
    return metadata, records


def import_statement(statement, source_name, database=DEFAULT_DATABASE, *, archive_root=None):
    return import_file(source_name, database, archive_root=archive_root,
                       expected_sha256=statement.source_sha256, expected_statement=statement)


def import_file(source, database=DEFAULT_DATABASE, *, archive_root=None,
                expected_sha256=None, expected_statement=None):
    source = Path(source)
    root = Path(archive_root).resolve() if archive_root else storage_root(database) / 'archive' / 'statements'
    identifier = uuid4().hex
    data, source_error = None, None
    try:
        data = source.read_bytes()
    except OSError as error:
        source_error = error
    sha256 = archive.digest(data) if data is not None else None
    pending = None
    created_snapshot = False
    committed = False
    with connect(database) as connection:
        store = Store(connection)
        ledger = ImportLedger(connection,store)
        recovery_warnings = archive.recover(connection, root, skip_sha256=sha256)
        ledger.start(identifier,source.name)
        try:
            if source_error is not None:
                raise source_error
            if source.suffix.lower() != '.pdf':
                raise StatementError('Поддерживаются PDF-источники.')
            if expected_sha256 is not None and sha256 != expected_sha256:
                raise StatementError('Файл изменился после предпросмотра. Нужен новый предпросмотр.')
            known = ledger.source(sha256)
            if known:
                original = ledger.attempt(known['first_import_id'])
                bank, parser_name, parser_version = original['bank'], original['parser_name'], original['parser_version']
                metadata = json.loads(known['metadata_json'])
                count = known['source_count']
                records = None
                final_path = archive.safe_path(root, known['archive_path'])
                if not final_path.exists() or archive.digest(final_path.read_bytes()) != sha256:
                    previous = archive.safe_path(root,known['pending_path']) if known['pending_path'] else None
                    if previous and previous.exists() and archive.digest(previous.read_bytes()) == sha256:
                        pending = known['pending_path']
                    else:
                        pending = archive.snapshot(root, data, sha256)
                        created_snapshot = True
            else:
                statement = read_statement_bytes(data, suffix=source.suffix)
                if expected_statement is not None and statement != expected_statement:
                    raise StatementError('Предпросмотр не соответствует текущему файлу. Повторите предпросмотр.')
                bank, parser_name, parser_version = statement.bank, statement.parser_name, statement.parser_version
                metadata, records = operation_records(statement, identifier)
                records = [store._validated_operation(**record) for record in records]
                count = len(records)
                if not count:
                    raise StatementError('Нет операций для записи.')
                pending = archive.snapshot(root, data, sha256)
                created_snapshot = True
            new_count,count = ledger.commit(identifier,sha256,bank,parser_name,parser_version,
                                             metadata,records,pending,known)
            committed = True
        except BaseException as error:
            ledger.fail(identifier,sha256,error)
            if created_snapshot and pending and not committed:
                try:
                    archive.safe_path(root,pending).unlink(missing_ok=True)
                except OSError:
                    pass  # Failed-attempt history remains; preserve any unremovable snapshot.
            raise
        destination, warning = archive.finalize(connection, root, sha256)
        warnings = recovery_warnings + list(metadata.get('warnings', []))
        removed = False
        if warning:
            warnings.append(warning)
        else:
            removed, warning = archive.remove_original(source, destination, sha256)
            if warning:
                warnings.append(warning)
        return ImportResult(identifier,new_count,count-new_count,count,destination,removed,tuple(warnings),known is not None)


def recover_imports(database=DEFAULT_DATABASE, *, archive_root=None):
    root = Path(archive_root).resolve() if archive_root else storage_root(database) / 'archive' / 'statements'
    with connect(database) as connection:
        Store(connection)
        return archive.recover(connection, root)


def result_lines(result):
    lines = [f'Новых операций: {result.new_count}. Уже известных: {result.repeated_count}. Всего в файле: {result.source_count}.',
             'Категории не назначались.']
    if result.archive_path:
        lines.append('Архивная копия проверена.' + (' Исходный PDF перенесён в архив.' if result.source_removed else ' Исходник не удалялся.'))
    else:
        lines.append('Архивирование ожидает завершения. Исходный PDF не удаляйте.')
    lines.extend('ПРЕДУПРЕЖДЕНИЕ: ' + message for message in result.warnings)
    return lines


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
             ('Суммы строк совпадают с итогами банка. Это банковские обороты, а не расчёт доходов и расходов.'
              if statement.bank_turnover_totals_present else
              'Суммы пополнений/списаний рассчитаны по строкам: отдельных итогов банка нет. Изменение остатка сверено.')]
    if not statement.bank_turnover_totals_present:
        lines = [line.replace('по выписке:', 'по строкам:') for line in lines]
    lines.extend(f'ПРЕДУПРЕЖДЕНИЕ: {warning}' for warning in statement.warnings)
    return lines


def record_failed_preview(source, database, error):
    """A menu-selected unreadable source gets an error record, never a financial write."""
    source = Path(source)
    try:
        sha256 = archive.digest(source.read_bytes())
    except OSError:
        sha256 = None
    with connect(database) as connection:
        ledger=ImportLedger(connection)
        identifier=uuid4().hex
        ledger.start(identifier,source.name)
        ledger.fail(identifier,sha256,error)
