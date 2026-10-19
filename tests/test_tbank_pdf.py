"""T-Bank rows, missing balances and two times; invented fixtures only."""
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
from program.importers.parsers.tbank_card_pdf_v1 import parse_pages,money
from program.importers.service import import_file,preview_lines

FIXTURES=Path(__file__).parent/'fixtures'


def pages():return (FIXTURES/'tbank_synthetic.txt').read_text(encoding='utf-8').split('\f')


def test_pdf_two_times_missing_balances_card_and_fee():
    s=read_statement(FIXTURES/'tbank_synthetic.pdf')
    assert s.parser_name=='tbank_card_pdf' and s.bank=='Т-Банк'
    assert s.account_id=='5'*20 and len(s.operations)==5
    assert (s.incoming_kopecks,s.outgoing_kopecks)==(110000,5500)
    assert s.opening_balance_kopecks is None and s.closing_balance_kopecks is None
    assert s.bank_turnover_totals_present
    assert s.operations[0].description=='TEST CREDIT. TEST WRAPPED CREDIT.'
    assert (s.operations[1].operation_time,s.operations[1].processing_time)==('12:01','12:05')
    assert s.operations[1].card_number_mask=='0001'
    assert s.operations[3].description=='TEST SERVICE FEE. TEST FEE DESCRIPTION.'
    assert s.operations[3].card_number_mask is None and s.operations[3].direction=='OUT'
    assert s.operations[-1].description=='TEST TRANSFER. TEST TRANSFER CONTINUED.'
    assert (s.operations[-1].operation_date,s.operations[-1].processing_date)==('2026-07-31','2026-08-01')
    assert all(row.balance_after_kopecks is None and row.operation_time_zone is None
               and row.bank_transaction_id is None for row in s.operations)
    assert 'совпадают с итогами банка' in '\n'.join(preview_lines(s))


@pytest.mark.parametrize(('value','expected'),[('+1 234.56',123456),('-0.01',-1),('0.00',0)])
def test_exact_money(value,expected):assert money(value)==expected


@pytest.mark.parametrize(('before','after'),[
 ('TBANK.RU','OTHER.BANK'),
 ('55555555555555555555','5555555555555555555'),
 ('с 01.08.2026 по 31.08.2026','с 31.08.2026 по 01.08.2026'),
 ('03.08.2026          03.08.2026','33.08.2026          03.08.2026'),
 ('03.08.2026          03.08.2026','03.08.2026          01.09.2026'),
 ('-25.00 ₽','-26.00 ₽'),
 ('-25.00 ₽','-25.001 ₽'),
 ('-25.00 ₽','-25.00 USD'),
 ('Пополнения: 1 100,00','Пополнения: 1 099,00'),
 ('Расходы: 55,00','Расходы: 54,00'),
 ('12:01','25:01'),
 ('12:05','25:05'),
 ('0001','BADMASK'),
 ('\n1\f','\n2\f'),
 ('в валюте карты','UNKNOWN HEADING'),
 ('TEST BANK SIGNATURE','03.08.2026 03.08.2026 LATE ROW'),
 ('лицензия Банка России № 2673','лицензия OTHER BANK'),
])
def test_reject_inconsistent_or_unknown_layout(before,after):
    text='\f'.join(pages());assert before in text
    with pytest.raises(StatementError):parse_pages(text.replace(before,after).split('\f'))


def test_missing_page_time_and_unknown_continuation():
    with pytest.raises(StatementError):parse_pages(pages()[:1])
    damaged=pages()
    damaged[0]='\n'.join(line for line in damaged[0].splitlines() if not line.startswith('12:01'))
    with pytest.raises(StatementError):parse_pages(damaged)
    damaged=pages();damaged[0]=damaged[0].replace('12:05','12:05\nUNKNOWN TABLE LINE')
    with pytest.raises(StatementError):parse_pages(damaged)


def variant(target,mode):
    writer=PdfWriter(clone_from=PdfReader(FIXTURES/'tbank_synthetic.pdf'))
    removed=False
    for page in writer.pages:
        content=page.get_contents();operations=[];skip_time=False
        for values,operator in content.operations:
            if operator==b'Tj':
                raw=values[0].original_bytes
                if mode=='short' and not removed and raw.startswith(b'02.08.2026') and b'-25.00' in raw:
                    removed=True;skip_time=True;continue
                if skip_time:skip_time=False;continue
                if mode=='short':raw=raw.replace(b'55,00',b'30,00')
                values=[ByteStringObject(raw)]
            operations.append((values,operator))
        content.operations=operations;page.replace_contents(content)
    writer.add_metadata({'/Subject':'Invented '+mode})
    writer.write(target)


def test_overlap_two_times_repeats_null_balances_and_mixed_banks(tmp_path):
    db=tmp_path/'finance.db';initialize(db)
    source=tmp_path/'source.pdf';variant(source,'short')
    assert import_file(source,db).new_count==4
    with connect(db) as c:
        old=c.execute('SELECT id FROM operations WHERE amount_kopecks=2500').fetchone()[0]
    shutil.copyfile(FIXTURES/'tbank_synthetic.pdf',source)
    second=import_file(source,db)
    assert (second.new_count,second.repeated_count)==(1,4)
    assert second.archive_path.parent.name=='tbank' and not source.exists()
    with connect(db) as c:
        rows=[dict(row) for row in c.execute('SELECT * FROM operations WHERE amount_kopecks=2500')]
        times={json.loads(row['source_data'])['operation_time_local']:row['id'] for row in rows}
        assert set(times)=={'12:01','12:02'} and times['12:02']==old
        data=json.loads(rows[0]['source_data'])
        assert data['processing_time_local'] in {'12:05','12:06'}
        assert data['statement']['opening_balance_kopecks'] is None
        assert data['statement']['closing_balance_kopecks'] is None
        assert data['statement']['bank_turnover_totals_present'] is True
        assert 'operation_time_moscow' not in data
        assert c.execute("SELECT count(*) FROM operations WHERE description LIKE 'TEST SERVICE FEE%'").fetchone()[0]==1
    variant(source,'reencoded');third=import_file(source,db)
    assert not third.known_file and (third.new_count,third.repeated_count)==(0,5)
    shutil.copyfile(FIXTURES/'tbank_synthetic.pdf',source)
    assert import_file(source,db).new_count==0
    for name,count in [('sber_credit_synthetic.pdf',4),('sber_debit_synthetic.pdf',5),('ozon_synthetic.pdf',4),('vtb_synthetic.pdf',5),('rshb_synthetic.pdf',5)]:
        shutil.copyfile(FIXTURES/name,source);assert import_file(source,db).new_count==count
    with connect(db) as c:
        assert c.execute('SELECT count(*) FROM operations').fetchone()[0]==28
        assert c.execute('SELECT count(*) FROM classifications').fetchone()[0]==0
        validate_database(c)


def separate_summary(target, *, other_account=False):
    """Split the invented fixture into two table pages and a summary-only page."""
    writer=PdfWriter(clone_from=PdfReader(FIXTURES/'tbank_synthetic.pdf'))
    writer.add_page(PdfReader(FIXTURES/'tbank_synthetic.pdf').pages[-1])
    text=(FIXTURES/'tbank_synthetic.txt').read_text(encoding='utf-8').split('\f')[-1].splitlines()
    summary_index=next(i for i,line in enumerate(text) if line.startswith('Пополнения:'))
    footer_index=next(i for i,line in enumerate(text) if line.startswith('АО «ТБанк»'))
    for page_index,page in enumerate(writer.pages):
        content=page.get_contents();operations=[];text_index=-1
        for values,operator in content.operations:
            if operator==b'Tj':
                text_index+=1
                if page_index==1 and summary_index<=text_index<footer_index:continue
                if page_index==2 and text_index<summary_index:continue
                raw=values[0].original_bytes
                if page_index==2 and text_index==len(text)-1:raw=b'3'
                if other_account:raw=raw.replace(b'55555555555555555555',b'66666666666666666666')
                values=[ByteStringObject(raw)]
            operations.append((values,operator))
        content.operations=operations;page.replace_contents(content)
    writer.write(target)


def test_summary_only_page_actual_pdf(tmp_path):
    source=tmp_path/'summary.pdf';separate_summary(source)
    s=read_statement(source)
    old=read_statement(FIXTURES/'tbank_synthetic.pdf')
    assert len(s.operations)==len(old.operations)==5
    assert s.operations==old.operations
    assert (s.incoming_kopecks,s.outgoing_kopecks)==(110000,5500)
    assert len(PdfReader(source).pages)==3


@pytest.mark.parametrize('damage',['number','order','missing','totals','prefix','late_row','currency','footer'])
def test_summary_only_page_rejects_damage(tmp_path,damage):
    source=tmp_path/'summary.pdf';separate_summary(source)
    pages=[p.extract_text(extraction_mode='layout') for p in PdfReader(source).pages]
    if damage=='number':pages[-1]=pages[-1].rstrip()[:-1]+'4'
    elif damage=='order':pages[-1]=pages[-1].replace('Пополнения:','TEMP:').replace('Расходы:','Пополнения:').replace('TEMP:','Расходы:')
    elif damage=='missing':pages.pop()
    elif damage=='totals':pages[-1]=pages[-1].replace('55,00','54,00')
    elif damage=='prefix':pages[-1]='UNKNOWN LINE\n'+pages[-1]
    elif damage=='late_row':pages[-1]=pages[-1].replace('TEST BANK SIGNATURE','03.08.2026 03.08.2026 LATE ROW')
    elif damage=='currency':pages[-1]=pages[-1].replace('₽','USD')
    elif damage=='footer':pages[-1]=pages[-1].replace('лицензия Банка России','UNKNOWN')
    with pytest.raises(StatementError):parse_pages(pages)


def test_summary_only_repeats_and_independent_second_account(tmp_path):
    db=tmp_path/'finance.db';initialize(db)
    source=tmp_path/'source.pdf';shutil.copyfile(FIXTURES/'tbank_synthetic.pdf',source)
    assert import_file(source,db).new_count==5
    with connect(db) as c:original=[tuple(row) for row in c.execute('SELECT * FROM operations ORDER BY id')]
    separate_summary(source)
    r=import_file(source,db);assert not r.known_file and (r.new_count,r.repeated_count)==(0,5)
    separate_summary(source,other_account=True)
    r=import_file(source,db);assert (r.new_count,r.repeated_count)==(5,0)
    separate_summary(source,other_account=True)
    r=import_file(source,db);assert (r.new_count,r.repeated_count)==(0,5)
    with connect(db) as c:
        assert c.execute('SELECT count(*) FROM operations').fetchone()[0]==10
        assert original==[tuple(row) for row in c.execute('SELECT * FROM operations WHERE account_id=? ORDER BY id',('5'*20,))]
        validate_database(c)
