"""Only invented data; real customer statements must never enter fixtures."""
import json
from pathlib import Path
from dataclasses import replace
import shutil

import pytest
from pypdf import PdfReader, PdfWriter

from program.database.connection import connect
from program.database.migrations import initialize, validate_database
from program.importers.detector import read_statement
from program.importers.models import StatementError
from program.importers.parsers.ozon_pdf_v1 import parse_pages
from program.importers.service import import_file

FIXTURES=Path(__file__).parent/'fixtures'


def pages():
    return (FIXTURES/'ozon_synthetic.txt').read_text(encoding='utf-8').split('\f')


def test_extract_real_pdf_structure():
    statement=read_statement(FIXTURES/'ozon_synthetic.pdf')
    assert statement==replace(parse_pages(pages()),source_sha256=statement.source_sha256)
    assert len(statement.operations)==4
    assert statement.incoming_kopecks==statement.outgoing_kopecks==10000
    wrapped=statement.operations[0]
    assert wrapped.bank_transaction_id=='12345678901'
    assert wrapped.description=='TEST CREDIT TEST WRAPPED DESCRIPTION'
    assert wrapped.operation_time=='10:00:00'
    assert wrapped.processing_date is None
    assert wrapped.balance_after_kopecks is None
    assert wrapped.operation_time_zone is None
    assert wrapped.mcc is None


@pytest.mark.parametrize(('before','after'),[
 ('РОССИЙСКИЙ РУБЛЬ','ДОЛЛАР США'),
 ('00000000000000000000','0000000000000000000'),
 ('01.08.2026 – 31.08.2026','31.08.2026 – 01.08.2026'),
 ('03.08.2026 10:00:00','32.08.2026 10:00:00'),
 ('03.08.2026 10:00:00','03.08.2026 25:00:00'),
 ('03.08.2026 10:00:00','03.09.2026 10:00:00'),
 ('+ 100.00 ₽  ','+ 101.00 ₽  '),
 ('Итого зачислений за период: 100.00','Итого зачислений за период: 99.00'),
 ('Исходящий остаток: 0.00','Исходящий остаток: 1.00'),
 ('20000000002','20000000003'),
 ('TEST WRAPPED DESCRIPTION','TEST WRAPPED DESCRIPTION'+' '*80+'123.00 ₽'),
 ('Входящий остаток: 0.00 ₽','Входящий остаток: 0.00 ₽\nВходящий остаток: 0.00 ₽'),
 ('TEST BANK SIGNATURE','01.08.2026 01:00:00 BAD LATE ROW'),
 ('Российские рубли','USD'),
 ('\n1\f','\n2\f'),
])
def test_reject_incomplete_or_inconsistent_source(before,after):
    text='\f'.join(pages())
    assert before in text
    with pytest.raises(StatementError):
        parse_pages(text.replace(before,after).split('\f'))


def test_missing_page_and_missing_row():
    with pytest.raises(StatementError):parse_pages(pages()[:1])
    damaged=pages()
    damaged[1]='\n'.join(line for line in damaged[1].splitlines() if '20000000002' not in line)
    with pytest.raises(StatementError):parse_pages(damaged)


def test_import_reencoded_and_overlap_preserve_ids_and_no_classification(tmp_path):
    db=tmp_path/'finance.db';initialize(db)
    source=tmp_path/'first.pdf';shutil.copyfile(FIXTURES/'ozon_synthetic.pdf',source)
    first=import_file(source,db)
    assert (first.new_count,first.repeated_count)==(4,0)
    assert first.archive_path.parent.name=='ozonbank'
    assert not source.exists()
    with connect(db) as c:
        original=[tuple(row) for row in c.execute('SELECT id,description,bank_transaction_id FROM operations ORDER BY id')]
        row=c.execute("SELECT * FROM operations WHERE bank_transaction_id='12345678901'").fetchone()
        data=json.loads(row['source_data'])
        assert data['operation_time_local']=='10:00:00'
        assert data['operation_time_zone'] is None
        assert 'operation_time_moscow' not in data
        assert row['processing_date'] is None and row['balance_after_kopecks'] is None
    # Different bytes must be parsed and matched by document ID, not only hash.
    writer=PdfWriter(clone_from=PdfReader(FIXTURES/'ozon_synthetic.pdf'))
    writer.add_metadata({'/Subject':'Synthetic re-encoding'})
    source=tmp_path/'other.pdf';writer.write(source)
    second=import_file(source,db)
    assert not second.known_file
    assert (second.new_count,second.repeated_count)==(0,4)
    shutil.copyfile(FIXTURES/'ozon_synthetic.pdf',source)
    third=import_file(source,db)
    assert third.known_file and third.new_count==0
    # Existing Sber importer and archive folder must still work in this database.
    shutil.copyfile(FIXTURES/'sber_credit_synthetic.pdf',source)
    sber=import_file(source,db)
    assert sber.new_count==4 and sber.archive_path.parent.name=='sberbank'
    with connect(db) as c:
        assert original==[tuple(row) for row in c.execute("SELECT id,description,bank_transaction_id FROM operations WHERE bank='ОЗОН Банк' ORDER BY id")]
        assert c.execute('SELECT count(*) FROM classifications').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM operations').fetchone()[0]==8
        validate_database(c)


def variant(target, *, shorter=False, conflict=False):
    from pypdf.generic import ByteStringObject
    writer=PdfWriter(clone_from=PdfReader(FIXTURES/'ozon_synthetic.pdf'))
    for page_index,page in enumerate(writer.pages):
        content=page.get_contents()
        text_index=-1
        operations=[]
        for values,operator in content.operations:
            if operator==b'Tj':
                text_index+=1
                raw=values[0].original_bytes
                if shorter and raw.startswith(b'02.08.2026'):
                    continue
                if shorter and page_index==1:
                    # The invented fixture draws one Tj per line; summary rows 3/4.
                    if text_index==3:
                        raw=raw.replace(b'100.00',b'80.00')
                    elif text_index==4:
                        raw=raw.replace(b'0.00',b'20.00')
                if conflict:
                    raw=raw.replace(b'- 30.00',b'- 31.00').replace(b'- 50.00',b'- 49.00')
                values=[ByteStringObject(raw)]
            operations.append((values,operator))
        content.operations=operations
        page.replace_contents(content)
    writer.write(target)


def test_overlapping_pdf_adds_only_new_document(tmp_path):
    db=tmp_path/'finance.db';initialize(db)
    source=tmp_path/'short.pdf';variant(source,shorter=True)
    first=import_file(source,db)
    assert (first.new_count,first.repeated_count)==(3,0)
    shutil.copyfile(FIXTURES/'ozon_synthetic.pdf',source)
    second=import_file(source,db)
    assert (second.new_count,second.repeated_count)==(1,3)
    with connect(db) as c:
        assert c.execute('SELECT count(*) FROM operations').fetchone()[0]==4
        validate_database(c)


def test_conflicting_document_rolls_back_all_and_keeps_source(tmp_path):
    db=tmp_path/'finance.db';initialize(db)
    source=tmp_path/'source.pdf';shutil.copyfile(FIXTURES/'ozon_synthetic.pdf',source)
    import_file(source,db)
    variant(source,conflict=True)
    with pytest.raises(ValueError,match='Банковский ID совпал'):
        import_file(source,db)
    assert source.exists()
    with connect(db) as c:
        assert c.execute('SELECT count(*) FROM operations').fetchone()[0]==4
        assert c.execute("SELECT count(*) FROM imports WHERE status='FAILED'").fetchone()[0]==1
        validate_database(c)
