"""Financial acceptance scenarios with invented bank facts and user rules only."""
from dataclasses import replace
from pathlib import Path
import json
import shutil
import sqlite3
import subprocess
import sys

import pytest

from program.database.connection import connect
from program.database.migrations import initialize,validate_database
from program.database.operations import Store
from program.processing.engine import preview,apply,save_plan,read_plan,load,totals
from program.processing.rules import EngineError,validate_condition,matches
from program.processing.catalog import install


@pytest.fixture
def db(tmp_path):
    path=tmp_path/'data with spaces'/'finance.db';initialize(path);return path


def op(db,identifier,amount=10000,direction='OUT',day='2026-08-01',**changes):
    with connect(db) as c:
        s=Store(c)
        if not s.get('imports','i'):
            s.create_import('i','invented.pdf','TEST');s.set_import_status('i','SUCCESS')
        data=dict(id=identifier,import_id='i',bank='TEST A',account_id='account-a',
                  operation_date=day,processing_date=day,description='TEST EVENT',amount_kopecks=amount,
                  direction=direction,source_data={})
        data.update(changes);s.add_operation(**data)


def condition(field='description',operator='contains',value='TEST'):
    return {'field':field,'op':operator,'value':value}


def rule(identifier='r',kind='expense',priority=20,version=1,conditions=None,**action):
    return dict(id=identifier,name='TEST RULE '+identifier,version=version,
                conditions=conditions if conditions is not None else condition(),
                action={'operation_type':kind,**action},priority=priority,effective_from='2026-01-01',status='ACTIVE')


def rules(db,records):
    with connect(db) as c:install(c,records,confirmed=True,backup_dir=db.parent/'backups')


def link(db,identifier,kind,members,status='USER_CONFIRMED'):
    with connect(db) as c:Store(c).create_link_group(identifier,kind,status,members)


def manual(db,identifier,kind='income',include=True,**kwargs):
    with connect(db) as c:Store(c).record_manual_decision(identifier,operation_type=kind,include_in_report=include,comment='TEST explicit decision',**kwargs)


def own(db):
    with connect(db) as c:
        c.executemany('INSERT INTO own_accounts(id,bank,account) VALUES (?,?,?)',
                      [('a','TEST A','account-a'),('b','TEST B','account-b')])


def dump(db):
    with connect(db,readonly=True) as c:return '\n'.join(c.iterdump())


def test_readonly_preview_unknown_is_not_sign_based_and_normal_start_does_not_process(db):
    op(db,'out');op(db,'in',direction='IN')
    before=dump(db);p=preview(db)
    assert p.after['expenses_kopecks']==p.after['incomes_kopecks']==0
    assert len(p.after['undefined'])==2 and p.after['status']=='ERROR'
    assert dump(db)==before and p.id==preview(db).id
    assert not (db.parent/'backup').exists()
    backup=apply(p,db,confirmed=True)
    assert backup.exists()
    with connect(backup,readonly=True) as c:
        validate_database(c);assert c.execute('SELECT count(*) FROM classifications').fetchone()[0]==0
    assert not preview(db).changes
    before=dump(db);assert apply(preview(db),db,confirmed=True) is None
    assert dump(db)==before


def test_expense_income_uncategorized_and_no_other_default_bucket(db):
    op(db,'expense');op(db,'income',4000,'IN',description='TEST SALARY')
    rules(db,[rule(conditions=condition('direction','eq','OUT')),
              rule('salary','income',conditions=condition('description','eq','TEST SALARY'))])
    p=preview(db)
    assert p.after['expenses_kopecks']==10000 and p.after['incomes_kopecks']==4000
    assert p.after['status']=='WARNING' and len(p.after['review_required'])==2
    assert all(item['category_id'] is None for item in p.after['breakdown'])
    apply(p,db,confirmed=True)
    with connect(db) as c:
        assert c.execute('SELECT count(*) FROM categories').fetchone()[0]==0
        assert c.execute("SELECT count(*) FROM classifications WHERE source='RULE' AND rule_id IS NOT NULL AND rule_version=1").fetchone()[0]==2
        assert c.execute('SELECT count(*) FROM operations').fetchone()[0]==2


def test_rule_conflict_same_priority_requires_user_and_equal_result_is_stable(db):
    op(db,'o')
    rules(db,[rule('a'),rule('b',kind='technical')])
    p=preview(db);assert p.changes['o']['operation_type']=='undefined'
    assert 'Конфликт' in p.changes['o']['comment']
    rules(db,[rule('b',version=2)])
    p=preview(db);assert p.changes['o']['rule_id']=='a'
    rules(db,[rule('b',version=3,kind='technical',priority=100)])
    assert preview(db).changes['o']['operation_type']=='technical'


def test_new_rule_versions_never_silently_rewrite_history_and_manual_is_absolute(db):
    op(db,'old');rules(db,[rule()]);apply(preview(db),db,confirmed=True)
    old=Store_read(db,'old')
    rules(db,[rule(version=2,kind='technical')]);op(db,'new')
    p=preview(db);assert 'old' not in p.changes and p.changes['new']['operation_type']=='technical'
    assert Store_read(db,'old')==old
    manual(db,'old','expense',category_id=None)
    before=dump(db)
    p=preview(db,mode='history');assert 'old' not in p.changes
    apply(p,db,confirmed=True)
    assert Store_read(db,'old')['source']=='MANUAL'
    with connect(db) as c:
        assert c.execute('SELECT count(*) FROM manual_overrides').fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM rule_versions').fetchone()[0]==2


def Store_read(db,identifier):
    with connect(db,readonly=True) as c:return Store(c).get('classifications',identifier)


def test_manual_history_restores_projection_without_new_manual_or_rule(db):
    op(db,'o');manual(db,'o','technical',False)
    with connect(db) as c:
        c.execute("UPDATE classifications SET source='UNDEFINED',manual_override_id=NULL,operation_type='undefined' WHERE operation_id='o'")
    p=preview(db,mode='history');assert p.changes['o']['source']=='MANUAL'
    apply(p,db,confirmed=True)
    with connect(db) as c:
        assert c.execute('SELECT count(*) FROM rules').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM manual_overrides').fetchone()[0]==1


def test_activation_is_for_new_imports_not_operation_date_and_future_version_waits(db):
    op(db,'late',day='2025-01-01')
    r=rule();r['effective_from']='2026-08-01'
    r2=rule(version=2,kind='technical');r2['effective_from']='2027-01-01'
    rules(db,[r,r2])
    assert preview(db,as_of='2026-07-31').changes['late']['source']=='UNDEFINED'
    assert preview(db,as_of='2026-08-01').changes['late']['rule_version']==1
    assert preview(db,as_of='2027-01-01').changes['late']['rule_version']==2


@pytest.mark.parametrize(('operator','value','expected'),[
    ('eq','test event',True),('ne','OTHER',True),('contains','EVENT',True),
    ('not_contains','UNKNOWN',True),('starts_with','TEST',True),('ends_with','EVENT',True),
])
def test_text_operators(operator,value,expected):
    node=condition('description',operator,value);validate_condition(node)
    assert matches(node,{'description':'TEST EVENT'}, {}) is expected


@pytest.mark.parametrize(('operator','value'),[('gt',50),('lt',150),('between',[50,150])])
def test_amount_operators(operator,value):
    node=condition('amount_kopecks',operator,value);validate_condition(node)
    assert matches(node,{'amount_kopecks':100},{})


def test_and_or_date_between_phone_and_missing_negative_predicates():
    node={'and':[condition('operation_date','date_between',['2026-07-01','2026-07-31']),
                 {'or':[condition('phone','eq','TEST PHONE'),condition('description','contains','OTHER')]}]}
    validate_condition(node)
    assert matches(node,{'operation_date':'2026-07-31','source_data':{'phone':'TEST PHONE'}},{})
    assert not matches(condition('counterparty','ne','TEST'),{'counterparty':None},{})
    assert not matches(condition('mcc','not_contains','5411'),{'mcc':None},{})


@pytest.mark.parametrize('node',[condition('amount_kopecks','eq',1.5),condition('amount_kopecks','eq',True),
    condition('unknown','eq','X'),condition('description','regex','.*'),condition('operation_date','date_between',['2026-02-30','2026-03-01']),
    {'or':[]},condition('amount_kopecks','between',[200,100])])
def test_bad_rules_rejected_before_backup_or_writes(db,node):
    before=dump(db)
    with pytest.raises((EngineError,ValueError)):rules(db,[rule(conditions=node)])
    assert dump(db)==before


def test_confirmed_transfer_and_chain_exclude_technical_but_keep_purchase(db):
    op(db,'out',50000);op(db,'in',50000,'IN',bank='TEST B',account_id='account-b')
    op(db,'purchase',30000,bank='TEST B',account_id='account-b')
    link(db,'chain','CHAIN',[('out','OUT'),('in','IN'),('purchase','PURCHASE')])
    rules(db,[rule('too_broad','income')])
    p=preview(db);assert p.after['expenses_kopecks']==30000 and p.after['incomes_kopecks']==0
    assert p.changes['out']['source']==p.changes['in']['source']=='LINK'
    assert p.changes['purchase']['operation_type']=='expense'
    apply(p,db,confirmed=True)
    assert not preview(db).changes


def test_only_date_amount_even_two_owned_accounts_is_proposed_not_excluded(db):
    own(db);op(db,'out');op(db,'in',direction='IN',bank='TEST B',account_id='account-b')
    p=preview(db)
    assert len(p.proposals)==1 and p.proposals[0]['status']=='PROPOSED'
    assert all(d['operation_type']=='undefined' for d in p.changes.values())
    apply(p,db,confirmed=True)
    assert not preview(db).proposals
    with connect(db) as c:Store(c).set_status('operation_link_groups',p.proposals[0]['id'],'REJECTED')
    assert not preview(db).proposals


def test_auto_transfer_needs_network_reference_two_owned_accounts_and_uniqueness(db):
    own(db)
    op(db,'out',source_data={'transfer_reference':'TEST-NETWORK:1'})
    op(db,'in',direction='IN',bank='TEST B',account_id='account-b',source_data={'transfer_reference':'TEST-NETWORK:1'})
    p=preview(db)
    assert len(p.proposals)==1 and p.proposals[0]['status']=='AUTO_CONFIRMED'
    assert all(d['operation_type']=='technical' for d in p.changes.values())
    apply(p,db,confirmed=True);assert preview(db).after['expenses_kopecks']==0
    op(db,'amb-out',20000,source_data={'transfer_reference':'TEST-NETWORK:2'})
    op(db,'amb-in1',20000,'IN',bank='TEST B',account_id='account-b',source_data={'transfer_reference':'TEST-NETWORK:2'})
    op(db,'amb-in2',20000,'IN',bank='TEST B',account_id='account-b',source_data={'transfer_reference':'TEST-NETWORK:2'})
    p=preview(db)
    assert not any(g['status']=='AUTO_CONFIRMED' for g in p.proposals)


@pytest.mark.parametrize('returns',[[10000],[3000],[3000,2000]])
def test_full_partial_multiple_refunds_have_no_income(db,returns):
    op(db,'purchase');members=[('purchase','PURCHASE')]
    for index,amount in enumerate(returns):
        identifier=f'refund{index}';op(db,identifier,amount,'IN',day='2026-08-02');members.append((identifier,'REFUND'))
    link(db,'refunds','REFUND',members)
    p=preview(db)
    assert p.after['expenses_kopecks']==10000-sum(returns) and p.after['incomes_kopecks']==0
    apply(p,db,confirmed=True)
    assert preview(db).after['expenses_kopecks']==10000-sum(returns)


def test_period_uses_operation_date_and_refund_date_not_posting_date(db):
    op(db,'purchase',day='2026-07-31',processing_date='2026-08-02')
    op(db,'refund',3000,'IN',day='2026-08-03')
    link(db,'r','REFUND',[('purchase','PURCHASE'),('refund','REFUND')])
    apply(preview(db),db,confirmed=True)
    july=preview(db,start='2026-07-01',end='2026-07-31').after
    august=preview(db,start='2026-08-01',end='2026-08-31').after
    assert july['expenses_kopecks']==10000
    assert august['expenses_kopecks']==-3000 and august['incomes_kopecks']==0


def test_inconsistent_excess_refund_and_manual_link_conflict_are_blocking(db):
    op(db,'purchase');op(db,'refund',12000,'IN')
    link(db,'r','REFUND',[('purchase','PURCHASE'),('refund','REFUND')])
    p=preview(db);assert p.after['link_errors'] and p.after['status']=='ERROR'
    assert all(d['operation_type']=='undefined' for d in p.changes.values())
    with connect(db) as c:Store(c).set_status('operation_link_groups','r','REJECTED')
    link(db,'own','OWN_TRANSFER',[('purchase','OUT'),('refund','IN')])
    manual(db,'purchase','expense')
    p=preview(db,mode='history')
    assert p.after['status']=='ERROR' and 'purchase' not in p.changes
    assert Store_read(db,'purchase')['source']=='MANUAL'


def test_rule_refund_is_never_income_and_requires_confirmation_to_reduce_purchase(db):
    op(db,'purchase');op(db,'refund',3000,'IN',description='TEST REFUND')
    rules(db,[rule('p',conditions=condition('direction','eq','OUT')),
              rule('r','refund',conditions=condition('description','eq','TEST REFUND'),original_operation_id='purchase')])
    p=preview(db)
    assert p.after['expenses_kopecks']==10000 and p.after['incomes_kopecks']==0
    assert p.after['unlinked_refunds']==['refund'] and p.after['status']=='ERROR'
    proposal=next(g for g in p.proposals if g['link_type']=='REFUND')
    apply(p,db,confirmed=True)
    with connect(db) as c:Store(c).set_status('operation_link_groups',proposal['id'],'USER_CONFIRMED')
    p=preview(db);assert p.after['expenses_kopecks']==7000 and p.after['incomes_kopecks']==0


def test_stale_or_tampered_plan_and_unconfirmed_apply_never_write(db,tmp_path):
    op(db,'o');p=preview(db);before=dump(db)
    with pytest.raises(EngineError):apply(p,db)
    with pytest.raises(EngineError):apply(replace(p,changes={}),db,confirmed=True)
    assert dump(db)==before
    file=tmp_path/'preview.json';save_plan(p,file)
    assert read_plan(file).id==p.id
    with pytest.raises(FileExistsError):save_plan(p,file)
    op(db,'later');before=dump(db)
    with pytest.raises(EngineError):apply(p,db,confirmed=True)
    assert dump(db)==before and not (db.parent/'backup').exists()


def test_backup_failure_or_failure_mid_commit_rolls_back_everything(db,monkeypatch):
    import program.processing.engine as engine
    op(db,'a');op(db,'b');p=preview(db);before=dump(db)
    original=engine.verified_backup
    def fail(*args):raise OSError('TEST BACKUP FAILURE')
    monkeypatch.setattr(engine,'verified_backup',fail)
    with pytest.raises(OSError):apply(p,db,confirmed=True)
    assert dump(db)==before
    monkeypatch.setattr(engine,'verified_backup',original)
    save=Store._save_classification
    def fail_second(self,identifier,values):
        if identifier=='b':raise sqlite3.IntegrityError('TEST COMMIT FAILURE')
        return save(self,identifier,values)
    monkeypatch.setattr(Store,'_save_classification',fail_second)
    with pytest.raises(sqlite3.IntegrityError):apply(p,db,confirmed=True)
    assert dump(db)==before
    assert list((db.parent/'backup').glob('*.db'))


def test_rule_import_explicit_confirmation_append_only_atomic_and_no_implicit_history(db):
    before=dump(db)
    with connect(db) as c:
        with pytest.raises(EngineError):install(c,[rule()],backup_dir=db.parent/'backups')
    assert dump(db)==before
    rules(db,[rule()]);before=dump(db)
    rules(db,[rule()]);assert dump(db)==before
    with pytest.raises(EngineError):rules(db,[rule(kind='income')])
    with pytest.raises(EngineError):rules(db,[rule('new'),rule('bad',conditions=condition('amount_kopecks','eq',1.5))])
    assert dump(db)==before


def test_categories_projects_preserve_income_types_and_date_fallback_warning(db):
    with connect(db) as c:
        c.executemany('INSERT INTO categories(id,name,name_key,parent_id) VALUES (?,?,?,?)',
                     [('cat','TEST CATEGORY','test category',None),('sub','TEST SUB','test sub','cat')])
        Store(c).create_project('proj','TEST PROJECT')
    op(db,'one',3000,'IN',description='TEST INCOME ONE',source_data={'operation_date_source':'POSTING_DATE'})
    rules(db,[rule('one','income',conditions=condition('description','eq','TEST INCOME ONE'),category_id='cat',subcategory_id='sub',project_id='proj')])
    apply(preview(db),db,confirmed=True)
    op(db,'two',5000,'IN',description='TEST INCOME TWO')
    rules(db,[rule('two','income',conditions=condition('description','eq','TEST INCOME TWO'))])
    p=preview(db)
    assert p.after['incomes_kopecks']==8000 and p.after['date_fallbacks']==['one']
    assert p.after['status']=='WARNING'
    assert Store_read(db,'one')['subcategory_id']=='sub'


def test_cli_preview_apply_status_and_menu_control_are_real(db,tmp_path,monkeypatch,capsys):
    from program.processing.__main__ import main
    from program.ui.finance import control
    op(db,'o');file=tmp_path/'plan.json'
    assert main(['preview','--database',str(db),'--plan',str(file)])==0
    assert Store_read(db,'o') is None
    assert main(['apply','--database',str(db),'--plan',str(file),'--confirm'])==0
    assert main(['status','--database',str(db)])==0
    assert 'ERROR' in capsys.readouterr().out
    root=tmp_path/'ui';(root/'database').mkdir(parents=True);shutil.copyfile(db,root/'database'/'finance.db')
    answers=iter(['2']);monkeypatch.setattr('builtins.input',lambda _:next(answers))
    control(root);assert 'Изменений нет' in capsys.readouterr().out


def test_technical_rule_cannot_hide_date_amount_only_branch_behind_or(db):
    condition_tree={'or':[condition(),condition('amount_kopecks','eq',10000)]}
    before=dump(db)
    with pytest.raises(EngineError):rules(db,[rule(kind='technical',conditions=condition_tree)])
    assert dump(db)==before


def test_fallback_posting_dates_do_not_invalidate_user_confirmed_refund_order(db):
    op(db,'purchase',day='2026-08-03',source_data={'operation_date_source':'POSTING_DATE'})
    op(db,'refund',3000,'IN',day='2026-08-02')
    link(db,'r','REFUND',[('purchase','PURCHASE'),('refund','REFUND')])
    p=preview(db)
    assert not p.after['link_errors'] and p.after['expenses_kopecks']==7000
    assert p.after['date_fallbacks']==['purchase']


def test_two_conflicting_confirmed_groups_do_not_choose_by_id(db):
    op(db,'purchase');op(db,'r1',3000,'IN');op(db,'r2',4000,'IN')
    link(db,'first','REFUND',[('purchase','PURCHASE'),('r1','REFUND')])
    link(db,'second','REFUND',[('purchase','PURCHASE'),('r2','REFUND')])
    p=preview(db)
    assert p.after['status']=='ERROR'
    assert all(d['operation_type']=='undefined' for d in p.changes.values())


def test_rejected_link_stops_excluding_operations_and_does_not_duplicate_proposals(db):
    own(db);op(db,'out');op(db,'in',direction='IN',bank='TEST B',account_id='account-b')
    link(db,'confirmed','OWN_TRANSFER',[('out','OUT'),('in','IN')])
    apply(preview(db),db,confirmed=True)
    with connect(db) as c:Store(c).set_status('operation_link_groups','confirmed','REJECTED')
    p=preview(db)
    assert not p.proposals
    assert all(d['operation_type']=='undefined' for d in p.changes.values())


def test_stale_change_during_backup_is_detected_under_write_transaction(db,monkeypatch):
    import program.processing.engine as engine
    op(db,'one');p=preview(db);backup_fn=engine.verified_backup
    def concurrent_change(connection,directory,validator):
        backup=backup_fn(connection,directory,validator)
        op(db,'later')
        return backup
    monkeypatch.setattr(engine,'verified_backup',concurrent_change)
    with pytest.raises(EngineError,match='изменились'):apply(p,db,confirmed=True)
    with connect(db) as c:
        assert c.execute('SELECT count(*) FROM classifications').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM operations').fetchone()[0]==2


def test_real_released_database_migrates_and_keeps_manual_decision(tmp_path):
    db=tmp_path/'finance.db'
    shutil.copyfile(Path(__file__).parent/'fixtures'/'legacy_stage2_synthetic.db',db)
    initialize(db)
    with connect(db) as c:
        original=[tuple(row) for row in c.execute('SELECT * FROM operations ORDER BY id')]
        manual_rows=[tuple(row) for row in c.execute('SELECT * FROM manual_overrides')]
        # The released fixture has a deliberately storage-only JSON rule. Leave
        # it disabled rather than pretend an unsupported rule language is valid.
        c.execute("UPDATE rules SET status='DISABLED'")
    p=preview(db);apply(p,db,confirmed=True)
    with connect(db) as c:
        assert original==[tuple(row) for row in c.execute('SELECT * FROM operations ORDER BY id')]
        assert manual_rows==[tuple(row) for row in c.execute('SELECT * FROM manual_overrides')]
        validate_database(c)


def test_history_scope_does_not_create_automatic_links_outside_period(db):
    own(db)
    op(db,'old-out',day='2026-07-01',source_data={'transfer_reference':'TEST-NETWORK:OLD'})
    op(db,'old-in',direction='IN',day='2026-07-01',bank='TEST B',account_id='account-b',source_data={'transfer_reference':'TEST-NETWORK:OLD'})
    op(db,'new',day='2026-08-01')
    p=preview(db,mode='history',start='2026-08-01',end='2026-08-31')
    assert not p.proposals and set(p.changes)=={'new'}


def test_link_purchase_does_not_inherit_category_from_a_different_economic_type(db):
    with connect(db) as c:c.execute("INSERT INTO categories(id,name,name_key) VALUES ('income','TEST INCOME','test income')")
    op(db,'p');op(db,'r',3000,'IN')
    with connect(db) as c:Store(c).set_classification('p',source='UNDEFINED',operation_type='income',include_in_report=True,category_id='income')
    link(db,'g','REFUND',[('p','PURCHASE'),('r','REFUND')])
    p=preview(db)
    assert p.changes['p']['operation_type']=='expense' and p.changes['p']['category_id'] is None
    assert p.after['breakdown'][0]['display_group']=='Неклассифицированные расходы'


def test_cli_rule_import_requires_own_confirmation_and_does_not_classify(db,tmp_path,capsys):
    from program.processing.__main__ import main
    op(db,'o');file=tmp_path/'rules.json';file.write_text(json.dumps([rule()]),encoding='utf-8')
    before=dump(db)
    assert main(['import-rules','--database',str(db),'--file',str(file)])==0
    assert dump(db)==before and Store_read(db,'o') is None
    assert main(['import-rules','--database',str(db),'--file',str(file),'--confirm'])==0
    assert Store_read(db,'o') is None
    assert preview(db).changes['o']['rule_id']=='r'


def test_rule_snapshot_change_stales_plan_and_bank_facts_are_immutable(db):
    op(db,'o');rules(db,[rule()]);p=preview(db)
    with connect(db) as c:before=[tuple(row) for row in c.execute('SELECT * FROM operations')]
    rules(db,[rule(version=2,kind='technical')])
    with pytest.raises(EngineError):apply(p,db,confirmed=True)
    apply(preview(db),db,confirmed=True)
    with connect(db) as c:assert before==[tuple(row) for row in c.execute('SELECT * FROM operations')]


def test_unfinished_import_with_operations_cannot_enter_financial_processing(db):
    op(db,'o')
    with connect(db) as c:Store(c).set_import_status('i','PENDING')
    before=dump(db)
    with pytest.raises(EngineError,match='незавершённой'):preview(db)
    assert dump(db)==before
