"""Alfa posting layout and inherited-resource pagination; invented data only."""
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
from program.importers.parsers.alfa_account_pdf_v1 import parse_pages,pagination_from_forms,card_facts
from program.importers.service import import_file
from program.processing.deduplication import identity

FIXTURES=Path(__file__).parent/'fixtures'


def pages():return (FIXTURES/'alfa_synthetic.txt').read_text(encoding='utf-8').split('\f')


def test_real_pdf_inherited_resources_only_invoked_forms_and_no_mutation():
    reader=PdfReader(FIXTURES/'alfa_synthetic.pdf')
    for index,page in enumerate(reader.pages,1):
        assert 'Страница' not in page.extract_text(extraction_mode='layout')
        assert len(page['/Resources']['/XObject'])==2  # Includes an unused footer.
        forms=[ref.get_object() for ref in page['/Resources']['/XObject'].values()]
        assert all('/Resources' not in form for form in forms)
        assert pagination_from_forms(page)==f'Страница {index} из 2'
        assert all('/Resources' not in form for form in forms)
    s=read_statement(FIXTURES/'alfa_synthetic.pdf')
    assert s.parser_name=='alfa_account_pdf' and len(s.operations)==6
    assert (s.incoming_kopecks,s.outgoing_kopecks)==(56000,6000)
    assert (s.opening_balance_kopecks,s.closing_balance_kopecks)==(10000,60000)
    assert s.operations[0].operation_date=='2026-07-31'
    assert s.operations[0].processing_date=='2026-08-01'
    assert s.operations[0].mcc=='5411' and s.operations[0].card_number_mask=='000000++++++0001'
    assert s.operations[0].operation_date_source=='DESCRIPTION'
    assert s.operations[1].operation_date_source=='POSTING_DATE'
    assert s.operations[1].operation_date==s.operations[1].processing_date
    assert s.operations[0].bank_operation_code==s.operations[4].bank_operation_code=='CRD_SAME'
    assert s.operations[2].bank_operation_code=='CRD_'
    assert all(row.bank_transaction_id is None and row.operation_time is None and row.balance_after_kopecks is None for row in s.operations)


@pytest.mark.parametrize(('before','after'),[
 ('77777777777777777777','7777777777777777777'),
 ('Валюта счета RUR','Валюта счета USD'),
 ('с 01.08.2026 по 31.08.2026','с 31.08.2026 по 01.08.2026'),
 ('Поступления 560,00','Поступления 559,00'),
 ('Исходящий остаток 600,00','Исходящий остаток 599,00'),
 ('31.07.26','32.07.26'),
 ('31.07.26','31.07.24'),
 ('31.07.26','02.08.26'),
 ('25.00 RUR','26.00 RUR'),
 ('MCC5411','MCC541'),
 ('Страница 2 из 2','Страница 3 из 3'),
 ('подпись сотрудника АО «АЛЬФА-БАНК»','OTHER BANK'),
 ('Уполномоченное лицо','UNKNOWN FOOTER'),
 ('Код операции','UNKNOWN COLUMN'),
 ('в валюте счета','USD'),
])
def test_reject_inconsistent_or_unknown_layout(before,after):
    text='\f'.join(pages());assert before in text
    with pytest.raises(StatementError):parse_pages(text.replace(before,after).split('\f'))


def test_missing_page_unknown_line_and_bad_posting_date():
    with pytest.raises(StatementError):parse_pages(pages()[:1])
    damaged=pages();damaged[0]=damaged[0].replace('TEST TRANSFER IN','TEST TRANSFER IN\nUNKNOWN ROW')
    with pytest.raises(StatementError):parse_pages(damaged)
    text='\f'.join(pages()).replace('01.08.2026        ','01.09.2026        ',1)
    assert text!='\f'.join(pages())
    with pytest.raises(StatementError):parse_pages(text.split('\f'))


def test_short_year_context_handles_year_boundary_without_arbitrary_century():
    description='Операция по карте: 000000++++++0001, на сумму: 25.00 RUR, дата совершения операции: 31.12.25, место совершения операции: TEST MCC5411'
    assert card_facts(description,'2026-01-02')[0]=='2025-12-31'
    with pytest.raises(StatementError):card_facts(description.replace('31.12.25','31.12.24'),'2026-01-02')


def test_operation_code_is_composite_fact_not_unique_id_and_old_hash_unchanged():
    record=dict(bank='TEST',account_id='TEST',operation_date='2026-08-01',processing_date='2026-08-01',
                amount_kopecks=100,direction='OUT',description='TEST',source_data={})
    prior=identity(record)
    record['source_data']['bank_operation_code']=None
    assert identity(record)==prior
    record['source_data']['bank_operation_code']='SAME'
    first=identity(record)
    assert first[2]=='FINGERPRINT'
    record['amount_kopecks']=200
    assert identity(record)!=first
    record['amount_kopecks']=100;record['source_data']['bank_operation_code']='OTHER'
    assert identity(record)!=first


def variant(target,mode):
    writer=PdfWriter(clone_from=PdfReader(FIXTURES/'alfa_synthetic.pdf'))
    for page in writer.pages:
        content=page.get_contents();operations=[]
        for values,operator in content.operations:
            if operator==b'Tj':
                raw=values[0].original_bytes
                if mode=='short' and b'B_TEST2' in raw:continue
                if mode=='short':raw=raw.replace(b'560,00',b'510,00').replace(b'600,00',b'550,00')
                values=[ByteStringObject(raw)]
            operations.append((values,operator))
        content.operations=operations;page.replace_contents(content)
    writer.add_metadata({'/Subject':'Invented '+mode})
    writer.write(target)


def test_overlap_repeated_codes_reencoding_and_all_banks(tmp_path):
    db=tmp_path/'finance.db';initialize(db)
    source=tmp_path/'source.pdf';variant(source,'short')
    assert import_file(source,db).new_count==5
    shutil.copyfile(FIXTURES/'alfa_synthetic.pdf',source)
    second=import_file(source,db)
    assert (second.new_count,second.repeated_count)==(1,5)
    assert second.archive_path.parent.name=='alfabank' and not source.exists()
    variant(source,'reencoded');third=import_file(source,db)
    assert not third.known_file and (third.new_count,third.repeated_count)==(0,6)
    shutil.copyfile(FIXTURES/'alfa_synthetic.pdf',source)
    assert import_file(source,db).new_count==0
    with connect(db) as c:
        rows=[json.loads(row[0]) for row in c.execute('SELECT source_data FROM operations')]
        assert sum(row['bank_operation_code']=='CRD_SAME' for row in rows)==2
        assert all(row['operation_time_local'] is None for row in rows)
        assert {row['operation_date_source'] for row in rows}=={'DESCRIPTION','POSTING_DATE'}
        assert c.execute("SELECT count(*) FROM operation_identities WHERE kind='BANK_ID'").fetchone()[0]==0
    for name,count in [('sber_credit_synthetic.pdf',4),('sber_debit_synthetic.pdf',5),('ozon_synthetic.pdf',4),('vtb_synthetic.pdf',5),('rshb_synthetic.pdf',5),('tbank_synthetic.pdf',5)]:
        shutil.copyfile(FIXTURES/name,source);assert import_file(source,db).new_count==count
    with connect(db) as c:
        assert c.execute('SELECT count(*) FROM operations').fetchone()[0]==34
        assert c.execute('SELECT count(*) FROM classifications').fetchone()[0]==0
        validate_database(c)
