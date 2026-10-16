from dataclasses import replace
from pathlib import Path
import json
import shutil
import sqlite3
import subprocess
import sys

import pytest
from pdf_variants import variant

from program.database.connection import DatabaseError, connect
from program.database.migrations import (copy_database, initialize, read_version, validate_database, validate_v1)
from program.database.operations import Store
from program.importers import archive
from program.importers.detector import read_statement
from program.importers.service import (import_file, import_statement, operation_records, recover_imports)
from program.processing.deduplication import match_occurrences

FIXTURES = Path(__file__).parent / 'fixtures'
PDF = FIXTURES / 'sber_credit_synthetic.pdf'
LEGACY = FIXTURES / 'legacy_stage2_synthetic.db'


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'database' / 'finance.db'
    initialize(path)
    return path


def copy_pdf(tmp_path, name='statement.pdf', mode=None):
    path = tmp_path / 'input' / name
    path.parent.mkdir(exist_ok=True)
    if mode:
        variant(PDF,path,mode)
    else:
        shutil.copyfile(PDF,path)
    return path


def operation_rows(database):
    with connect(database,readonly=True) as connection:
        return [dict(row) for row in connection.execute('SELECT * FROM operations ORDER BY rowid')]


def test_real_pdf_overlap_and_occurrence_increase(database, tmp_path):
    short = import_file(copy_pdf(tmp_path,'short.pdf','short'),database)
    assert (short.new_count,short.repeated_count)==(3,0)
    complete = import_file(copy_pdf(tmp_path,'complete.pdf'),database)
    assert (complete.new_count,complete.repeated_count)==(1,3)
    prior = {row['id']:row for row in operation_rows(database)}
    extended = import_file(copy_pdf(tmp_path,'extended.pdf','extended'),database)
    assert (extended.new_count,extended.repeated_count)==(1,4)
    rows=operation_rows(database)
    assert len(rows)==5
    for row in rows:
        if row['id'] in prior:
            assert row==prior[row['id']]
    shorter_again = import_file(copy_pdf(tmp_path,'short-copy.pdf','short'),database)
    assert (shorter_again.new_count,shorter_again.repeated_count)==(0,3)
    assert len(operation_rows(database))==5
    with connect(database,readonly=True) as connection:
        assert connection.execute('SELECT count(*) FROM import_observations').fetchone()[0]==15
        assert connection.execute('SELECT count(*) FROM classifications').fetchone()[0]==0
        assert connection.execute('SELECT count(*) FROM operation_identities WHERE occurrence=2').fetchone()[0]==1


def test_same_rows_new_pdf_bytes_do_not_duplicate(database,tmp_path):
    first=import_file(copy_pdf(tmp_path),database)
    before=operation_rows(database)
    second=import_file(copy_pdf(tmp_path,'reencoded.pdf','reencoded'),database)
    assert not second.known_file
    assert (second.new_count,second.repeated_count)==(0,4)
    assert operation_rows(database)==before
    assert first.archive_path!=second.archive_path


def test_same_file_new_name_is_known_and_archive_not_overwritten(database,tmp_path):
    first=import_file(copy_pdf(tmp_path),database)
    original_stat=first.archive_path.stat()
    second=import_file(copy_pdf(tmp_path,'renamed.pdf'),database)
    assert second.known_file and second.new_count==0 and second.repeated_count==4
    assert first.archive_path==second.archive_path
    assert first.archive_path.stat().st_mtime_ns==original_stat.st_mtime_ns
    assert len(operation_rows(database))==4


def test_existing_balance_keeps_specific_observation_on_old_id(database,tmp_path):
    import_file(copy_pdf(tmp_path),database)
    original=next(row for row in operation_rows(database) if row['balance_after_kopecks']==134985)
    result=import_file(copy_pdf(tmp_path,'extended.pdf','extended'),database)
    with connect(database,readonly=True) as connection:
        observations=connection.execute('SELECT * FROM import_observations WHERE import_id=? ORDER BY source_index',
                                        (result.import_id,)).fetchall()
        # The new identical row has a different balance, the old row is still present.
        assert observations[1]['operation_id']==original['id']
        assert observations[0]['is_new']==1


def test_preview_snapshot_change_rejects_and_records_failure(database,tmp_path):
    source=copy_pdf(tmp_path)
    statement=read_statement(source)
    source.write_bytes(source.read_bytes()+b'\n% changed since preview\n')
    with pytest.raises(ValueError,match='изменился после предпросмотра'):
        import_statement(statement,source,database)
    assert not operation_rows(database)
    assert source.exists()
    with connect(database,readonly=True) as connection:
        row=connection.execute('SELECT * FROM imports').fetchone()
        assert row['status']=='FAILED' and row['sha256']
        assert connection.execute('SELECT count(*) FROM source_files').fetchone()[0]==0


def test_snapshot_error_prevents_financial_write(database,tmp_path,monkeypatch):
    source=copy_pdf(tmp_path)
    def fail(*args):raise OSError('Snapshot unavailable')
    monkeypatch.setattr(archive,'snapshot',fail)
    with pytest.raises(OSError):import_file(source,database)
    assert source.exists() and not operation_rows(database)
    with connect(database,readonly=True) as connection:
        assert connection.execute('SELECT status FROM imports').fetchone()[0]=='FAILED'
        assert connection.execute('SELECT count(*) FROM import_observations').fetchone()[0]==0


def test_archive_failure_retains_source_then_recovers_without_duplicate(database,tmp_path,monkeypatch):
    source=copy_pdf(tmp_path)
    real_open=Path.open
    def blocked(path,mode='r',*args,**kwargs):
        if mode=='xb' and 'sberbank' in path.parts:
            raise PermissionError('Archive unavailable')
        return real_open(path,mode,*args,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path,'open',blocked)
        first=import_file(source,database)
    assert first.new_count==4 and first.archive_path is None
    assert source.exists() and first.warnings
    with connect(database,readonly=True) as connection:
        assert connection.execute('SELECT archive_status FROM imports').fetchone()[0]=='ERROR'
    assert recover_imports(database)==[]
    retry=import_file(source,database)
    assert retry.known_file and retry.new_count==0 and retry.repeated_count==4
    assert retry.source_removed and not source.exists()
    assert len(operation_rows(database))==4


def test_crash_after_commit_recovers_pending_archive(database,tmp_path,monkeypatch):
    source=copy_pdf(tmp_path)
    def crash(*args):raise SystemExit('Simulated termination after COMMIT')
    with monkeypatch.context() as patch:
        patch.setattr(archive,'finalize',crash)
        with pytest.raises(SystemExit):import_file(source,database)
    assert len(operation_rows(database))==4 and source.exists()
    before=operation_rows(database)
    with connect(database,readonly=True) as connection:
        state=connection.execute('SELECT status,archive_status FROM imports').fetchone()
        assert tuple(state)==('SUCCESS','PENDING')
    assert recover_imports(database)==[]
    assert operation_rows(database)==before
    # Recovery preserves an original without a currently confirmed import action.
    assert source.exists()
    repeat=import_file(source,database)
    assert repeat.new_count==0 and repeat.source_removed


def test_crash_with_completed_archive_and_pending_db_state(database,tmp_path,monkeypatch):
    source=copy_pdf(tmp_path)
    def crash(*args):raise SystemExit
    with monkeypatch.context() as patch:
        patch.setattr(archive,'finalize',crash)
        with pytest.raises(SystemExit):import_file(source,database)
    root=tmp_path/'archive'/'statements'
    with connect(database,readonly=True) as connection:
        row=connection.execute('SELECT * FROM source_files').fetchone()
        final=archive.safe_path(root,row['archive_path'])
        pending=archive.safe_path(root,row['pending_path'])
        final.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(pending,final)
    assert recover_imports(database)==[]
    assert final.exists() and not pending.exists()
    assert len(operation_rows(database))==4


def test_conflicting_archive_never_overwritten(database,tmp_path):
    source=copy_pdf(tmp_path)
    sha=archive.digest(source.read_bytes())
    target=tmp_path/'archive'/'statements'/'2026'/'sberbank'/(sha+'.pdf')
    target.parent.mkdir(parents=True)
    target.write_bytes(b'unexpected user file')
    result=import_file(source,database)
    assert result.archive_path is None and result.new_count==4
    assert source.exists() and target.read_bytes()==b'unexpected user file'
    assert recover_imports(database)
    target.rename(target.with_suffix('.preserved'))
    assert recover_imports(database)==[]
    assert archive.digest(target.read_bytes())==sha


def test_source_changed_during_import_is_not_deleted(database,tmp_path,monkeypatch):
    source=copy_pdf(tmp_path)
    original=archive.remove_original
    def changed(path,archived,sha):
        Path(path).write_bytes(b'new user file')
        return original(path,archived,sha)
    monkeypatch.setattr(archive,'remove_original',changed)
    result=import_file(source,database)
    assert not result.source_removed
    assert source.read_bytes()==b'new user file'
    assert result.archive_path and result.warnings
    assert len(operation_rows(database))==4


def test_pending_attempt_marked_failed_on_explicit_recovery(database):
    with connect(database) as connection:
        connection.execute("INSERT INTO imports(id,filename,bank,status,archive_status) VALUES ('pending','fake.pdf','UNKNOWN','PENDING','NOT_REQUIRED')")
    assert recover_imports(database)==[]
    with connect(database,readonly=True) as connection:
        row=connection.execute("SELECT * FROM imports WHERE id='pending'").fetchone()
        assert row['status']=='FAILED' and 'Прерванный' in row['error']
    assert not operation_rows(database)


def legacy_snapshot(connection):
    return {table:[tuple(row) for row in connection.execute(f'SELECT * FROM {table} ORDER BY rowid')]
            for table in ('operations','classifications','manual_overrides','rules','rule_versions','audit_log')}


def test_actual_released_stage2_database_migrates_without_losing_history(tmp_path):
    source=tmp_path/'old'/'finance.db';source.parent.mkdir();shutil.copyfile(LEGACY,source)
    original_bytes=source.read_bytes()
    with connect(source,readonly=True) as connection:
        validate_v1(connection)
        before=legacy_snapshot(connection)
    destination=tmp_path/'new'/'database'/'finance.db'
    assert copy_database(source,destination)==1
    assert source.read_bytes()==original_bytes
    assert not initialize(destination)
    with connect(destination,readonly=True) as connection:
        assert validate_database(connection)==2
        assert legacy_snapshot(connection)==before
        assert connection.execute('SELECT count(*) FROM operation_identities').fetchone()[0]==4
    backups=list((destination.parent/'backup').glob('*.db'))
    assert len(backups)==1
    with connect(backups[0],readonly=True) as connection:
        validate_v1(connection)
        assert legacy_snapshot(connection)==before
    source_pdf=copy_pdf(tmp_path)
    result=import_file(source_pdf,destination)
    assert (result.new_count,result.repeated_count)==(0,4)
    with connect(destination,readonly=True) as connection:
        after=legacy_snapshot(connection)
        for table in before.keys()-{'audit_log'}:
            assert after[table]==before[table]
    assert source.read_bytes()==original_bytes


def test_schema2_migration_failure_keeps_schema1_and_backup(tmp_path,monkeypatch):
    import program.database.migrations as module
    path=tmp_path/'finance.db';shutil.copyfile(LEGACY,path)
    with connect(path,readonly=True) as connection:
        before=legacy_snapshot(connection)
    original=module.attach_identity
    calls=0
    def fail(connection,row):
        nonlocal calls
        calls+=1
        original(connection,row)
        if calls==2:raise RuntimeError('Injected identity backfill failure')
    monkeypatch.setattr(module,'attach_identity',fail)
    with pytest.raises(RuntimeError):initialize(path)
    with connect(path,readonly=True) as connection:
        validate_v1(connection)
        assert read_version(connection)==1 and legacy_snapshot(connection)==before
    assert len(list((tmp_path/'backup').glob('*.db')))==1


def test_copy_never_overwrites_existing_destination_or_source(tmp_path):
    source=tmp_path/'old.db';shutil.copyfile(LEGACY,source)
    target=tmp_path/'new.db';target.write_bytes(b'existing user file')
    with pytest.raises(FileExistsError):copy_database(source,target)
    with pytest.raises(FileExistsError):copy_database(source,source)
    assert target.read_bytes()==b'existing user file'
    with connect(source,readonly=True) as connection:validate_v1(connection)


def test_cli_copy_migrate_check_info_and_recover(tmp_path):
    target=tmp_path/'database'/'finance.db'
    def run(module,*args):
        return subprocess.run([sys.executable,'-X','utf8','-m',module,*map(str,args)],capture_output=True,text=True,encoding='utf-8',timeout=20)
    copied=run('program.database','copy','--source',LEGACY,'--database',target)
    assert copied.returncode==0 and 'Схема 1' in copied.stdout
    for command in ('init','check','info'):
        result=run('program.database',command,'--database',target)
        assert result.returncode==0,result.stderr+result.stdout
        if command=='info':assert 'Операций в базе: 4' in result.stdout
    result=run('program.importers','recover','--database',target)
    assert result.returncode==0 and 'Восстановление проверено' in result.stdout


def test_bank_id_priority_and_conflict_and_account_scope(database,tmp_path):
    source=read_statement(PDF)
    _,records=operation_records(source,'fake-import')
    with connect(database) as connection:
        store=Store(connection)
        store.create_import('fake-import','synthetic.pdf','Сбербанк')
        record=records[0]
        record['bank_transaction_id']='example-bank-id'
        store.add_operation(**record)
        candidate=dict(record,id='another-id')
        assert match_occurrences(connection,[candidate])==[record['id']]
        with pytest.raises(ValueError):match_occurrences(connection,[candidate,candidate])
        with pytest.raises(ValueError):match_occurrences(connection,[dict(candidate,amount_kopecks=999)])
        assert match_occurrences(connection,[dict(candidate,account_id='different-account')])==[None]
        assert match_occurrences(connection,[dict(candidate,bank='different-bank')])==[None]


def test_menu_invalid_source_records_failure_and_retains_file(database,tmp_path,monkeypatch,capsys):
    from program.ui.console import import_first_pdf
    path=tmp_path/'input'/'broken.pdf';path.parent.mkdir();path.write_bytes(b'broken')
    monkeypatch.setattr('builtins.input',lambda _:pytest.fail('Unreadable file must not ask for write consent'))
    import_first_pdf(tmp_path)
    assert 'Импорт не выполнен' in capsys.readouterr().out
    assert path.read_bytes()==b'broken'
    with connect(database,readonly=True) as connection:
        row=connection.execute('SELECT * FROM imports').fetchone()
        assert row['status']=='FAILED' and row['filename']=='broken.pdf' and row['sha256']
    assert not operation_rows(database)


def test_missing_file_records_failure_but_never_creates_financial_records(database,tmp_path):
    with pytest.raises(FileNotFoundError):import_file(tmp_path/'missing.pdf',database)
    with connect(database,readonly=True) as connection:
        row=connection.execute('SELECT * FROM imports').fetchone()
        assert row['status']=='FAILED' and row['sha256'] is None
    assert not operation_rows(database)


def test_archive_path_escape_is_rejected_and_external_file_untouched(database,tmp_path,monkeypatch):
    source=copy_pdf(tmp_path)
    def crash(*args):raise SystemExit
    with monkeypatch.context() as patch:
        patch.setattr(archive,'finalize',crash)
        with pytest.raises(SystemExit):import_file(source,database)
    outside=tmp_path/'external.pdf';outside.write_bytes(b'external user data')
    with connect(database) as connection:
        connection.execute("UPDATE source_files SET archive_path='../../../external.pdf'")
    assert recover_imports(database)
    assert outside.read_bytes()==b'external user data' and source.exists()


def test_history_counter_corruption_is_detected(database,tmp_path):
    import_file(copy_pdf(tmp_path),database)
    with connect(database) as connection:
        connection.execute('UPDATE imports SET new_count=999')
        with pytest.raises(DatabaseError,match='Счётчики загрузки'):
            validate_database(connection)


def test_recover_reuses_pending_snapshot_on_retry(database,tmp_path,monkeypatch):
    source=copy_pdf(tmp_path)
    real_open=Path.open
    def blocked(path,mode='r',*args,**kwargs):
        if mode=='xb' and 'sberbank' in path.parts:raise PermissionError('Archive blocked')
        return real_open(path,mode,*args,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path,'open',blocked)
        result=import_file(source,database)
        pending=tmp_path/'archive'/'statements'/'.pending'
        assert len(list(pending.iterdir()))==1
        retry=import_file(source,database)
        assert retry.new_count==0 and retry.archive_path is None
        assert len(list(pending.iterdir()))==1
    retry=import_file(source,database)
    assert retry.new_count==0 and retry.archive_path
    assert list(pending.iterdir())==[]


def test_disjoint_dates_and_changed_processing_dates_do_not_collapse(database):
    _,records=operation_records(read_statement(PDF),'example-import')
    with connect(database) as connection:
        store=Store(connection);store.create_import('example-import','synthetic.pdf','Сбербанк')
        row=records[0];store.add_operation(**row)
        assert match_occurrences(connection,[dict(row,operation_date='2026-08-03')])==[None]
        assert match_occurrences(connection,[dict(row,processing_date='2026-08-03')])==[None]
        assert match_occurrences(connection,[dict(row,account_id='other')])==[None]
