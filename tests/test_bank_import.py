from dataclasses import replace
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter

from program.database.connection import connect, DatabaseError
from program.database.migrations import initialize, validate_database
from program.importers.detector import read_statement
from program.importers.models import StatementError, bank_date, kopecks
from program.importers.parsers.sber_credit_pdf_v1 import parse_pages
from program.importers.service import format_kopecks, import_statement, preview_lines

FIXTURE = Path(__file__).parent / 'fixtures' / 'sber_credit_synthetic.pdf'
TEXT = FIXTURE.with_suffix('.txt')


@pytest.fixture
def pages():
    return TEXT.read_text(encoding='utf-8').split('\f')


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'test.db'
    initialize(path)
    return path


def counts(path):
    with connect(path, readonly=True) as connection:
        return tuple(connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                     for table in ('imports', 'operations', 'audit_log', 'classifications'))


def test_actual_synthetic_pdf_extraction_dates_totals_and_duplicates():
    statement = read_statement(FIXTURE)
    assert statement.parser_name == 'sber_credit_pdf' and statement.parser_version == '1'
    assert statement.account_id == '0' * 20
    assert len(statement.operations) == 4
    assert (statement.incoming_count, statement.outgoing_count) == (1, 3)
    assert (statement.incoming_kopecks, statement.outgoing_kopecks) == (50025, 15040)
    assert statement.operations[-1].operation_date == '2026-07-31'
    assert statement.operations[-1].processing_date == '2026-08-01'
    assert statement.operations[0].amount_kopecks == statement.operations[1].amount_kopecks == 2515
    assert statement.operations[0].description == statement.operations[1].description
    assert statement.operations[2].description.endswith('Операция по карте ****0000')
    assert len(statement.operations[2].description_lines) == 2
    assert len(statement.warnings) == 1 and 'вне периода' in statement.warnings[0]


@pytest.mark.parametrize(('source', 'expected'), [
    ('1 234,56', 123456), ('+123,00', 12300), ('-0,01', -1),
    ('1\xa0234,56', 123456), ('1\u202f234,56', 123456), ('0,00', 0),
])
def test_exact_money(source, expected):
    assert kopecks(source) == expected


@pytest.mark.parametrize('value', ['1.25', '1,2', '12 34,56', '1e3', 'NaN', '', '100,000'])
def test_bad_money(value):
    with pytest.raises(StatementError):
        kopecks(value)


@pytest.mark.parametrize('amount,expected', [(0, '0,00'), (123456, '1 234,56'), (123456789, '1 234 567,89'), (-1, '-0,01')])
def test_display_money(amount, expected):
    assert format_kopecks(amount) == expected


@pytest.mark.parametrize('text', ['31.02.2026', '00.01.2026', '31.13.2026'])
def test_bad_date(text):
    with pytest.raises(StatementError):
        bank_date(text)


@pytest.mark.parametrize(('old', 'new'), [
    ('25,15', '25,1X'), ('12:02', '25:99'),
    ('000003', 'ABCDEF'), ('02.08.2026', '31.02.2026'),
    ('500,25', '500,26'), ('Российский рубль', 'Доллар США'),
    ('ДАТА ОПЕРАЦИИ (МСК)', 'ДРУГАЯ ТАБЛИЦА'), ('www.sberbank.ru', 'unknown.invalid'),
])
def test_bad_source_stops_parsing(pages, old, new):
    pages[0] = pages[0].replace(old, new, 1)
    with pytest.raises(StatementError):
        parse_pages(pages)


def test_missing_second_page_is_rejected(pages):
    # First page still carries four-operation summary, so missing rows cannot pass.
    with pytest.raises(StatementError):
        parse_pages(pages[:1])


def test_reordered_and_wrong_page_count(pages):
    pages[1] = pages[1].replace('Страница 2 из 2', 'Страница 3 из 3')
    with pytest.raises(StatementError):
        parse_pages(pages)


def test_unknown_table_line_is_not_silently_skipped(pages):
    pages[0] = pages[0].replace('Продолжение на следующей странице', 'НЕИЗВЕСТНАЯ СТРОКА\nПродолжение на следующей странице')
    with pytest.raises(StatementError):
        parse_pages(pages)


def test_missing_processing_date_is_error(pages):
    lines = pages[0].splitlines()
    lines = [line for line in lines if '000003' not in line]
    pages[0] = '\n'.join(lines)
    with pytest.raises(StatementError):
        parse_pages(pages)


def test_balance_discrepancy_is_reported_without_changing_rows(pages):
    pages[0] = pages[0].replace('Остаток на 31.08.2026       1 349,85', 'Остаток на 31.08.2026       1 350,85')
    statement = parse_pages(pages)
    assert statement.closing_balance_kopecks == 135085
    assert statement.operations[0].balance_after_kopecks == 134985
    assert any('Конечный остаток' in message for message in statement.warnings)
    assert any('ПРЕДУПРЕЖДЕНИЕ' in line for line in preview_lines(statement))


def test_empty_scan_encrypted_and_invalid_files(tmp_path):
    empty = tmp_path / 'scan.pdf'
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.write(empty)
    with pytest.raises(StatementError, match='нет читаемого текста'):
        read_statement(empty)
    writer.encrypt('test-only')
    encrypted = tmp_path / 'encrypted.pdf'
    writer.write(encrypted)
    with pytest.raises(StatementError, match='паролем'):
        read_statement(encrypted)
    empty.write_bytes(b'not a pdf')
    with pytest.raises(StatementError):
        read_statement(empty)
    with pytest.raises(StatementError):
        read_statement(empty.with_suffix('.xlsx'))


def test_import_records_every_fact_and_no_classification(database):
    source_before = FIXTURE.read_bytes()
    statement = read_statement(FIXTURE)
    assert import_statement(statement, FIXTURE, database) == 4
    assert FIXTURE.read_bytes() == source_before
    assert counts(database) == (1, 4, 5, 0)
    with connect(database, readonly=True) as connection:
        assert validate_database(connection) == 1
        imported = dict(connection.execute('SELECT * FROM imports').fetchone())
        assert (imported['status'], imported['source_count'], imported['new_count']) == ('SUCCESS', 4, 4)
        assert (imported['parser_name'], imported['parser_version']) == ('sber_credit_pdf', '1')
        for table in ('categories', 'rules', 'manual_overrides', 'operation_link_groups'):
            assert connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
        rows = connection.execute('SELECT * FROM operations ORDER BY rowid').fetchall()
        for actual, expected in zip(rows, statement.operations):
            assert (actual['operation_date'], actual['processing_date'], actual['description'],
                    actual['amount_kopecks'], actual['direction'], actual['balance_after_kopecks']) == (
                expected.operation_date, expected.processing_date, expected.description,
                expected.amount_kopecks, expected.direction, expected.balance_after_kopecks)
            assert actual['source_row'].startswith('page=')
            assert actual['mcc'] is None and actual['counterparty'] is None and actual['bank_operation_type'] is None
            assert actual['bank_transaction_id'] is None
            assert 'authorization_code' in actual['source_data']
            assert 'Пример раздела' not in actual['source_data']


def test_repeat_refuses_without_modification(database):
    statement = read_statement(FIXTURE)
    import_statement(statement, FIXTURE, database)
    before = database.read_bytes()
    with pytest.raises(DatabaseError, match='пустую базу'):
        import_statement(statement, FIXTURE, database)
    assert database.read_bytes() == before


def test_sql_failure_rolls_back_whole_import(database, monkeypatch):
    import program.importers.service as module
    monkeypatch.setattr(module, 'uuid4', lambda: SimpleNamespace(hex='duplicate'))
    with pytest.raises(sqlite3.IntegrityError):
        import_statement(read_statement(FIXTURE), FIXTURE, database)
    assert counts(database) == (0, 0, 0, 0)


def test_invalid_operation_is_rejected_before_write(database):
    statement = read_statement(FIXTURE)
    broken = replace(statement.operations[1], amount_kopecks=1.25)
    with pytest.raises(ValueError):
        import_statement(replace(statement, operations=(statement.operations[0], broken)), FIXTURE, database)
    assert counts(database) == (0, 0, 0, 0)


def test_cli_preview_import_and_failed_parse(database, tmp_path):
    def run(*args):
        return subprocess.run([sys.executable, '-X', 'utf8', '-m', 'program.importers', *map(str, args)],
                              text=True, encoding='utf-8', capture_output=True, timeout=20)
    unused = tmp_path / 'unused.db'
    preview = run('preview', FIXTURE, '--database', unused)
    assert preview.returncode == 0, preview.stdout + preview.stderr
    assert not unused.exists()
    assert 'Прочитано операций: 4.' in preview.stdout
    assert '0' * 20 not in preview.stdout
    unconfirmed = run('import', FIXTURE, '--database', database)
    assert unconfirmed.returncode != 0 and counts(database) == (0, 0, 0, 0)
    written = run('import', FIXTURE, '--database', database, '--confirm')
    assert written.returncode == 0 and 'Сохранено операций: 4.' in written.stdout
    repeat = run('import', FIXTURE, '--database', database, '--confirm')
    assert repeat.returncode == 1 and 'пустую базу' in repeat.stdout
    assert counts(database) == (1, 4, 5, 0)
    broken = tmp_path / 'broken.pdf'
    broken.write_bytes(b'broken')
    rejected = run('import', broken, '--database', database, '--confirm')
    assert rejected.returncode == 1 and 'Traceback' not in rejected.stderr
    assert counts(database) == (1, 4, 5, 0)


def test_menu_empty_input_leaves_files_unchanged(tmp_path, monkeypatch, capsys):
    from program import main
    monkeypatch.setattr(main, 'PROJECT_ROOT', tmp_path)
    answers = iter(['1', '0'])
    monkeypatch.setattr('builtins.input', lambda _: next(answers))
    assert main.main() == 0
    assert 'Поместите одну PDF' in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_menu_cancel_does_not_create_database(tmp_path, monkeypatch, capsys):
    from program.ui.console import import_first_pdf
    (tmp_path / 'input').mkdir()
    shutil.copyfile(FIXTURE, tmp_path / 'input' / 'test.pdf')
    monkeypatch.setattr('builtins.input', lambda _: 'нет')
    import_first_pdf(tmp_path)
    assert 'Импорт отменён' in capsys.readouterr().out
    assert not (tmp_path / 'database').exists()


def test_menu_confirm_then_repeat_and_multiple_files(tmp_path, monkeypatch, capsys):
    from program.ui.console import import_first_pdf
    database = tmp_path / 'database' / 'finance.db'
    initialize(database)
    (tmp_path / 'input').mkdir()
    shutil.copyfile(FIXTURE, tmp_path / 'input' / 'test.pdf')
    monkeypatch.setattr('builtins.input', lambda _: 'ДА')
    import_first_pdf(tmp_path)
    assert 'Сохранено операций: 4' in capsys.readouterr().out
    import_first_pdf(tmp_path)
    assert 'пустую базу' in capsys.readouterr().out
    assert counts(database) == (1, 4, 5, 0)
    shutil.copyfile(FIXTURE, tmp_path / 'input' / 'second.pdf')
    import_first_pdf(tmp_path)
    assert 'ровно одна' in capsys.readouterr().out


def test_description_whitespace_normalized_but_original_lines_retained(pages):
    pages[0] = pages[0].replace('TEST SHOP.', 'TEST  SHOP.', 1)
    statement = parse_pages(pages)
    assert statement.operations[0].description.startswith('TEST SHOP.')
    assert statement.operations[0].description_lines[0].startswith('TEST  SHOP.')


def test_final_signature_footnote_marker_is_not_an_operation(pages):
    pages[1] = pages[1].replace('Дата формирования документа', ' ' * 120 + '*\nДата формирования документа')
    assert len(parse_pages(pages).operations) == 4
