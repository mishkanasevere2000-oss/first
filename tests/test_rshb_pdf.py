"""RSHB layout, truthful validation status and multiplicity; invented data only."""
import json
from dataclasses import replace
from pathlib import Path
import shutil

import pytest
from pypdf import PdfReader,PdfWriter
from pypdf.generic import ByteStringObject

from program.database.connection import connect
from program.database.migrations import initialize,validate_database
from program.importers.detector import read_statement
from program.importers.models import StatementError
from program.importers.parsers.rshb_card_pdf_v1 import parse_pages,money
from program.importers.service import import_file,preview_lines
from program.processing.deduplication import identity

FIXTURES=Path(__file__).parent/'fixtures'


def pages():return (FIXTURES/'rshb_synthetic.txt').read_text(encoding='utf-8').split('\f')


def test_pdf_facts_card_column_and_truthful_preview():
    s=read_statement(FIXTURES/'rshb_synthetic.pdf')
    assert s.parser_name=='rshb_card_pdf' and s.bank=='Россельхозбанк'
    assert s.account_id=='4'*20
    assert len(s.operations)==5
    assert (s.incoming_kopecks,s.outgoing_kopecks)==(12500,12500)
    assert not s.bank_turnover_totals_present
    assert s.operations[0]==replace(s.operations[1],line=s.operations[0].line)
    assert s.operations[2].description=='TEST CREDIT. TEST WRAPPED CREDIT.'
    assert s.operations[3].description=='TEST SHOP. TEST WRAPPED SHOP.'
    assert s.operations[3].card_number_mask=='**0001'
    assert s.operations[3].operation_date=='2026-07-31'
    assert s.operations[3].processing_date=='2026-08-02'
    assert all(row.balance_after_kopecks is None and row.operation_time_zone is None
               and row.bank_transaction_id is None for row in s.operations)
    text='\n'.join(preview_lines(s))
    assert 'Пополнения по строкам:' in text
    assert 'совпадают с итогами банка' not in text
    assert 'полноту PDF' in text
    assert 'совпадают с итогами банка' in '\n'.join(preview_lines(read_statement(FIXTURES/'sber_credit_synthetic.pdf')))


@pytest.mark.parametrize(('value','expected'),[('0.00',0),('-26,990.00',-2699000),('1,234,567.89',123456789),('12.34',1234)])
def test_exact_money(value,expected):assert money(value)==expected


@pytest.mark.parametrize(('before','after'),[
 ('№3349/','№9999/'),
 ('44444444444444444444','4444444444444444444'),
 ('Российский рубль','USD'),
 ('с 01.08.2026 по 31.08.2026','с 31.08.2026 по 01.08.2026'),
 ('03.08.2026     03.08.2026','33.08.2026     03.08.2026'),
 ('03.08.2026     03.08.2026','03.08.2026     01.09.2026'),
 ('-50.00 ₽','-51.00 ₽'),
 ('-50.00 ₽','-50.001 ₽'),
 ('0.00 ₽','1.00 ₽'),
 ('0.00                          0.00','0.00                          1.00'),
 ('00:00','25:00'),
 ('00:00\n','00:00\n00:00\n'),
 ('**0001','BADMASK'),
 ('валюте счета','другая валюта'),
 ('TEST WRAPPED SHOP.','TEST WRAPPED SHOP.'+' '*100+'1.00 ₽'),
])
def test_reject_damaged_or_unsupported_source(before,after):
    text='\f'.join(pages());assert before in text
    with pytest.raises(StatementError):parse_pages(text.replace(before,after).split('\f'))


def test_missing_time_unknown_line_and_page():
    text='\f'.join(pages()).replace('00:00\n','',1)
    with pytest.raises(StatementError):parse_pages(text.split('\f'))
    damaged=pages();damaged[1]+='\nUNKNOWN TABLE LINE'
    with pytest.raises(StatementError):parse_pages(damaged)
    with pytest.raises(StatementError):parse_pages(pages()+['UNKNOWN PAGE'])


def test_balanced_subset_cannot_be_certified_complete():
    # With no page numbers or bank turnover totals, deleting a balanced page
    # cannot be detected arithmetically. The UI must never claim completeness.
    partial=parse_pages(pages()[:1])
    assert len(partial.operations)==3 and not partial.bank_turnover_totals_present
    text='\n'.join(preview_lines(partial))
    assert 'полноту PDF' in text and 'подтвердить нельзя' in text
    assert 'совпадают с итогами банка' not in text


def test_card_mask_identity_does_not_change_prior_hashes():
    record=dict(bank='TEST',account_id='TEST',operation_date='2026-08-01',processing_date=None,
                amount_kopecks=100,direction='OUT',description='TEST',source_data={'operation_time_local':'12:00'})
    prior=identity(record)
    record['source_data']['card_number_mask']=None
    assert identity(record)==prior
    record['source_data']['card_number_mask']='**0001';first=identity(record)
    record['source_data']['card_number_mask']='**0002'
    assert identity(record)!=first and first!=prior


def variant(target,mode):
    writer=PdfWriter(clone_from=PdfReader(FIXTURES/'rshb_synthetic.pdf'))
    removed=False
    for page in writer.pages:
        content=page.get_contents();operations=[];skip_time=False
        for values,operator in content.operations:
            if operator==b'Tj':
                raw=values[0].original_bytes
                if mode=='short' and not removed and raw.startswith(b'03.08.2026') and b'-50.00' in raw:
                    removed=True;skip_time=True;continue
                if skip_time:skip_time=False;continue
                if mode=='short':raw=raw.replace(b'0.00                          0.00',b'0.00                          50.00')
                values=[ByteStringObject(raw)]
            operations.append((values,operator))
        content.operations=operations;page.replace_contents(content)
    writer.add_metadata({'/Subject':'Invented '+mode})
    writer.write(target)


def test_overlap_identical_repayments_repeats_and_mixed_banks(tmp_path):
    db=tmp_path/'finance.db';initialize(db)
    source=tmp_path/'source.pdf';variant(source,'short')
    assert import_file(source,db).new_count==4
    shutil.copyfile(FIXTURES/'rshb_synthetic.pdf',source)
    second=import_file(source,db)
    assert (second.new_count,second.repeated_count)==(1,4)
    assert second.archive_path.parent.name=='rshb' and not source.exists()
    variant(source,'reencoded');third=import_file(source,db)
    assert not third.known_file and (third.new_count,third.repeated_count)==(0,5)
    shutil.copyfile(FIXTURES/'rshb_synthetic.pdf',source)
    assert import_file(source,db).new_count==0
    with connect(db) as c:
        assert c.execute("SELECT count(*) FROM operations WHERE description='TEST REPAYMENT.'").fetchone()[0]==2
        data=json.loads(c.execute("SELECT source_data FROM operations WHERE amount_kopecks=2500 LIMIT 1").fetchone()[0])
        assert data['card_number_mask']=='**0001'
        assert data['statement']['bank_turnover_totals_present'] is False
        assert 'operation_time_moscow' not in data
        for row in c.execute('SELECT metadata_json FROM source_files'):
            assert json.loads(row[0])['bank_turnover_totals_present'] is False
    for name,count in [('sber_credit_synthetic.pdf',4),('sber_debit_synthetic.pdf',5),('ozon_synthetic.pdf',4),('vtb_synthetic.pdf',5)]:
        shutil.copyfile(FIXTURES/name,source);assert import_file(source,db).new_count==count
    with connect(db) as c:
        assert c.execute('SELECT count(*) FROM operations').fetchone()[0]==23
        assert c.execute('SELECT count(*) FROM classifications').fetchone()[0]==0
        validate_database(c)
