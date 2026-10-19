"""Invented VTB statements only; no customer PDF or extracted text in tests."""
import hashlib
import json
from pathlib import Path
import shutil

import pytest
from pypdf import PdfReader,PdfWriter
from pypdf.generic import ByteStringObject

from program.database.connection import connect
from program.database.migrations import initialize,validate_database
from program.importers.detector import read_statement
from program.importers.models import StatementError
from program.importers.parsers.vtb_card_pdf_v1 import parse_pages,money
from program.importers.service import import_file
from program.processing.deduplication import identity

FIXTURES=Path(__file__).parent/'fixtures'


def pages():
    return (FIXTURES/'vtb_synthetic.txt').read_text(encoding='utf-8').split('\f')


def test_pdf_extraction_facts_and_account_selection():
    statement=read_statement(FIXTURES/'vtb_synthetic.pdf')
    assert statement.parser_name=='vtb_card_pdf'
    assert statement.bank=='ВТБ'
    assert statement.account_id=='2'*20  # SKS, never the bank's common receiving account.
    assert (statement.incoming_kopecks,statement.outgoing_kopecks)==(60050,15040)
    assert (statement.opening_balance_kopecks,statement.closing_balance_kopecks)==(-100000,-54990)
    assert len(statement.operations)==5
    assert statement.operations[1].direction=='OUT'
    assert statement.operations[1].amount_kopecks==2515
    assert statement.operations[1].operation_time=='12:01:00'
    assert statement.operations[-1].operation_date=='2026-07-31'
    assert statement.operations[-1].processing_date=='2026-08-01'
    assert statement.operations[3].description=='TEST DEPOSIT. TEST WRAPPED DESCRIPTION.'
    assert all(row.operation_time_zone is None and row.balance_after_kopecks is None
               and row.bank_transaction_id is None and row.mcc is None for row in statement.operations)
    assert len(statement.warnings)==2


@pytest.mark.parametrize(('text','expected'),[('0.0',0),('0.01',1),('-12.3',-1230),('123.45',12345)])
def test_exact_decimal_money(text,expected):assert money(text)==expected


@pytest.mark.parametrize(('before','after'),[
 ('Банк ВТБ (ПАО)','OTHER BANK'),
 ('22222222222222222222','2222222222222222222'),
 ('Период выписки 01.08.2026 - 31.08.2026','Период выписки 31.08.2026 - 01.08.2026'),
 ('-25.15 RUB','-25.15 USD'),
 ('-25.15 RUB','-25.151 RUB'),
 ('-25.15 RUB','-26.15 RUB'),
 ('Комиссия банка 0.0 RUB','Комиссия банка 1.0 RUB'),
 ('0.0 RUB  TEST PAYMENT','1.0 RUB  TEST PAYMENT'),
 ('Расходные операции 150.4 RUB','Расходные операции 149.4 RUB'),
 ('Баланс на конец периода -549.9 RUB','Баланс на конец периода -550.0 RUB'),
 ('12:01:00','25:01:00'),
 ('12:01:00','12:01:00\n12:02:00'),
 ('12:01:00',''),
 ('03.08.2026    03.08.2026','33.08.2026    03.08.2026'),
 ('03.08.2026    03.08.2026','03.08.2026    01.09.2026'),
 ('\n2\f','\n3\f'),
 ('валюте операции                 карты','UNKNOWN SUBHEADER'),
])
def test_reject_unsupported_or_inconsistent_source(before,after):
    text='\f'.join(pages());assert before in text
    with pytest.raises(StatementError):parse_pages(text.replace(before,after).split('\f'))


def test_missing_pages_unknown_lines_and_late_rows():
    with pytest.raises(StatementError):parse_pages(pages()[:-1])
    damaged=pages();del damaged[1]
    with pytest.raises(StatementError):parse_pages(damaged)
    damaged=pages();damaged[0]=damaged[0].replace('12:01:00','12:01:00\nUNKNOWN TABLE LINE')
    with pytest.raises(StatementError):parse_pages(damaged)
    damaged=pages();row=next(line for line in damaged[2].splitlines() if line.startswith('31.07.2026'))
    damaged[2]=damaged[2].replace('Кредитный лимит по карте',row+'\nКредитный лимит по карте')
    with pytest.raises(StatementError):parse_pages(damaged)


def test_local_time_identity_and_legacy_sber_hash():
    record=dict(bank='TEST',account_id='TEST',operation_date='2026-08-01',processing_date='2026-08-02',
                amount_kopecks=100,direction='OUT',description='TEST',mcc=None)
    record['source_data']={'operation_time_local':'12:01:00','authorization_code':''}
    first=identity(record)
    record['source_data']['operation_time_local']='12:02:00'
    assert identity(record)!=first
    record['source_data']={'operation_time_moscow':'12:01','authorization_code':'000001'}
    legacy_facts=['2026-08-01','2026-08-02',100,'OUT','TEST',None,'12:01','000001']
    expected=hashlib.sha256(json.dumps(legacy_facts,ensure_ascii=False,separators=(',',':'),sort_keys=True,allow_nan=False).encode()).hexdigest()
    assert identity(record)[-1]==expected


def variant(target,mode):
    writer=PdfWriter(clone_from=PdfReader(FIXTURES/'vtb_synthetic.pdf'))
    for page in writer.pages:
        content=page.get_contents();operations=[];skip_time=False
        for values,operator in content.operations:
            if operator==b'Tj':
                raw=values[0].original_bytes
                if mode=='short' and raw.startswith(b'02.08.2026') and b'-25.15 RUB' in raw and page==writer.pages[0]:
                    skip_time=True;continue
                if skip_time:
                    skip_time=False;continue
                if mode=='short':raw=raw.replace(b'150.4 RUB',b'125.25 RUB').replace(b'-549.9 RUB',b'-524.75 RUB')
                values=[ByteStringObject(raw)]
            operations.append((values,operator))
        content.operations=operations;page.replace_contents(content)
    writer.add_metadata({'/Subject':'Invented variant '+mode})
    writer.write(target)


def test_overlap_reencoded_repeat_and_mixed_banks(tmp_path):
    db=tmp_path/'finance.db';initialize(db)
    source=tmp_path/'source.pdf';variant(source,'short')
    assert import_file(source,db).new_count==4
    with connect(db) as c:
        original=c.execute("SELECT id FROM operations WHERE amount_kopecks=2515").fetchone()[0]
    shutil.copyfile(FIXTURES/'vtb_synthetic.pdf',source)
    second=import_file(source,db)
    assert (second.new_count,second.repeated_count)==(1,4)
    assert second.archive_path.parent.name=='vtb' and not source.exists()
    with connect(db) as c:
        times={json.loads(row['source_data'])['operation_time_local']:row['id'] for row in
               c.execute('SELECT * FROM operations WHERE amount_kopecks=2515')}
        assert set(times)=={'12:01:00','12:02:00'}
        assert times['12:02:00']==original
        for row in c.execute('SELECT source_data FROM operations'):
            assert 'operation_time_moscow' not in json.loads(row[0])
    variant(source,'reencoded');third=import_file(source,db)
    assert not third.known_file and (third.new_count,third.repeated_count)==(0,5)
    shutil.copyfile(FIXTURES/'vtb_synthetic.pdf',source)
    assert import_file(source,db).new_count==0
    for name,count in [('sber_credit_synthetic.pdf',4),('sber_debit_synthetic.pdf',5),('ozon_synthetic.pdf',4)]:
        shutil.copyfile(FIXTURES/name,source);assert import_file(source,db).new_count==count
    with connect(db) as c:
        assert c.execute('SELECT count(*) FROM operations').fetchone()[0]==18
        assert c.execute('SELECT count(*) FROM classifications').fetchone()[0]==0
        validate_database(c)
