"""Debit layout and common Sber parser regressions; invented customer data only."""
from pathlib import Path
import shutil

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ByteStringObject

from program.database.connection import connect
from program.database.migrations import initialize, validate_database
from program.importers.detector import read_statement
from program.importers.models import StatementError
from program.importers.parsers import sber_credit_pdf_v1 as credit, sber_debit_pdf_v1 as debit
from program.importers.service import import_file

FIXTURES=Path(__file__).parent/'fixtures'


def pages():
    return (FIXTURES/'sber_debit_synthetic.txt').read_text(encoding='utf-8').split('\f')


def test_actual_pdf_extraction_and_format_detection():
    statement=read_statement(FIXTURES/'sber_debit_synthetic.pdf')
    assert statement.parser_name=='sber_debit_pdf'
    assert statement.account_id=='1'*20
    assert (statement.incoming_kopecks,statement.outgoing_kopecks)==(120000,20000)
    assert len(statement.operations)==5
    assert [row.description for row in statement.operations]==[
        'TEST PAYMENT.','TEST PAYMENT.','TEST TRANSFER. TEST WRAPPED DESCRIPTION.',
        'TEST SERVICE.','TEST CREDIT.']
    assert statement.operations[0].bank_transaction_id is None
    assert statement.operations[0].authorization_code=='000001'
    assert statement.operations[1].authorization_code=='000001'
    assert statement.operations[-1].operation_date=='2026-07-31'
    assert statement.operations[-1].processing_date=='2026-08-01'
    assert statement.operations[0].operation_time_zone=='Europe/Moscow'
    assert len(statement.warnings)==1
    assert read_statement(FIXTURES/'sber_credit_synthetic.pdf').parser_name=='sber_credit_pdf'
    with pytest.raises(StatementError):credit.parse_pages(pages())
    with pytest.raises(StatementError):debit.parse_pages((FIXTURES/'sber_credit_synthetic.txt').read_text().split('\f'))


@pytest.mark.parametrize(('before','after'),[
 ('СУММА В ВАЛЮТЕ СЧЁТА','СУММА В ДРУГОЙ ВАЛЮТЕ'),
 ('Российский рубль','USD'),
 ('11111 111 1 1111 1111111','11111 111 1 1111 111111'),
 ('Страница 2 из 2','Страница 3 из 3'),
 ('Списание                     200,00','Списание                     199,00'),
 ('03.08.2026    12:01','33.08.2026    12:01'),
 ('03.08.2026    12:01','03.08.2026    24:01'),
 ('03.08.2026    000001','03.08.2026    BADCODE'),
 ('TEST WRAPPED DESCRIPTION.','TEST WRAPPED DESCRIPTION.'+' '*100+'1,00'),
 ('Продолжение на следующей странице','UNKNOWN END'),
 ('и код авторизации','UNKNOWN HEADER'),
])
def test_reject_damaged_source(before,after):
    source='\f'.join(pages());assert before in source
    with pytest.raises(StatementError):debit.parse_pages(source.replace(before,after).split('\f'))


def test_missing_page_or_processing_date_rejected():
    with pytest.raises(StatementError):debit.parse_pages(pages()[:1])
    damaged=pages()
    damaged[0]='\n'.join(line for line in damaged[0].splitlines() if '03.08.2026    000001' not in line)
    with pytest.raises(StatementError):debit.parse_pages(damaged)


def test_header_markers_only_allowed_before_rows():
    damaged=pages()
    damaged[0]=damaged[0].replace('TEST PAYMENT.','TEST PAYMENT.\n1',1)
    with pytest.raises(StatementError):debit.parse_pages(damaged)


def test_balance_mismatch_preserves_source_with_warning():
    damaged=pages()
    damaged[0]=damaged[0].replace('Остаток на 31.08.2026       1 000,00','Остаток на 31.08.2026       999,00')
    statement=debit.parse_pages(damaged)
    assert statement.closing_balance_kopecks==99900
    assert any('остаток' in warning for warning in statement.warnings)


def variant(target,mode):
    writer=PdfWriter(clone_from=PdfReader(FIXTURES/'sber_debit_synthetic.pdf'))
    removed=False
    for page in writer.pages:
        content=page.get_contents();operations=[];remove_description=False
        for values,operator in content.operations:
            if operator==b'Tj':
                raw=values[0].original_bytes
                if mode=='short' and not removed and raw.startswith(b'03.08.2026') and b'12:01' in raw:
                    removed=True;remove_description=True
                    continue
                if remove_description:
                    remove_description=False
                    continue
                if mode=='short':
                    raw=raw.replace(b'200,00',b'150,00') if b'+200,00' not in raw and b'1 200,00' not in raw else raw
                    # Closing header uses a distinctive spacing in this fixture.
                    if b'31.08.2026       1 000,00' in raw:
                        raw=raw.replace(b'1 000,00',b'1 050,00')
                values=[ByteStringObject(raw)]
            operations.append((values,operator))
        content.operations=operations;page.replace_contents(content)
    writer.add_metadata({'/Subject':'Synthetic '+mode})
    writer.write(target)


def test_overlap_multiplicity_archive_and_other_accounts(tmp_path):
    db=tmp_path/'finance.db';initialize(db)
    source=tmp_path/'source.pdf';variant(source,'short')
    first=import_file(source,db)
    assert (first.new_count,first.repeated_count)==(4,0)
    shutil.copyfile(FIXTURES/'sber_debit_synthetic.pdf',source)
    second=import_file(source,db)
    assert (second.new_count,second.repeated_count)==(1,4)
    assert second.archive_path.parent.name=='sberbank' and not source.exists()
    variant(source,'reencoded')
    third=import_file(source,db)
    assert not third.known_file and (third.new_count,third.repeated_count)==(0,5)
    shutil.copyfile(FIXTURES/'sber_debit_synthetic.pdf',source)
    fourth=import_file(source,db)
    assert fourth.known_file and fourth.new_count==0
    for name in ('sber_credit_synthetic.pdf','ozon_synthetic.pdf'):
        shutil.copyfile(FIXTURES/name,source);assert import_file(source,db).new_count==4
    with connect(db) as c:
        assert c.execute('SELECT count(*) FROM operations').fetchone()[0]==13
        assert c.execute("SELECT count(*) FROM operations WHERE account_id=? AND description='TEST PAYMENT.'",('1'*20,)).fetchone()[0]==2
        assert c.execute('SELECT count(*) FROM classifications').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM manual_overrides').fetchone()[0]==0
        assert c.execute("SELECT count(*) FROM operation_identities WHERE kind='FINGERPRINT'").fetchone()[0]==9
        validate_database(c)
