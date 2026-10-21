"""Interactive stage-6 acceptance on invented facts; no personal data."""
from pathlib import Path
import json
import sqlite3

import pytest

from test_financial_engine import db,op,rule
from program.database.connection import connect
from program.database.migrations import initialize
from program.processing import review as r
from program.processing.engine import preview,apply
from program.processing.rules import EngineError
from program.ui import review as ui


def token(db):return r.snapshot(db)['token']
def action(kind='expense',**extra):
    return dict(operation_type=kind,include_in_report=kind in {'expense','income','refund'},comment='TEST USER',**extra)
def answer(monkeypatch,values):
    answers=iter(values);monkeypatch.setattr('builtins.input',lambda prompt='':next(answers))
def count(db,table):
    with connect(db) as c:return c.execute('SELECT count(*) FROM '+table).fetchone()[0]


def test_manual_persists_and_preserves_immutable_facts(db):
    op(db,'a')
    with connect(db) as c:before=[tuple(x) for x in c.execute('SELECT * FROM operations')]
    r.manual(db,token(db),'a',action(),confirmed=True)
    s=r.snapshot(db);assert s['current']['a']['source']=='MANUAL'
    assert s['summary']['expenses_kopecks']==10000
    assert s['summary']['review_required']==['a']
    assert count(db,'rules')==0
    r.manual(db,token(db),'a',action('technical'),confirmed=True)
    history=r.history(db,'a');assert len(history)==2 and history[1]['previous_id']==history[0]['id']
    with connect(db) as c:assert [tuple(x) for x in c.execute('SELECT * FROM operations')]==before


@pytest.mark.parametrize('kind',['expense','income','technical','refund','undefined'])
def test_manual_types_direction_and_confirmation(db,kind):
    op(db,'a',direction='IN' if kind in {'income','refund'} else 'OUT')
    with pytest.raises(EngineError):r.manual(db,token(db),'a',action(kind))
    assert count(db,'manual_overrides')==0
    r.manual(db,token(db),'a',action(kind),confirmed=True)
    assert r.snapshot(db)['current']['a']['operation_type']==kind


def test_stale_manual_and_wrong_direction_do_not_write(db):
    op(db,'a');old=token(db);r.add_category(db,old,'TEST',confirmed=True)
    with pytest.raises(EngineError):r.manual(db,old,'a',action(),confirmed=True)
    with pytest.raises(EngineError):r.manual(db,token(db),'a',action('income'),confirmed=True)
    assert count(db,'manual_overrides')==0


def test_invalid_category_and_comment_are_atomic(db):
    op(db,'a')
    for a in (action(category_id='missing'),{**action(),'comment':''}):
        with pytest.raises(ValueError):r.manual(db,token(db),'a',a,confirmed=True)
    assert count(db,'manual_overrides')==count(db,'classifications')==0


def test_categories_subcategories_and_duplicates(db):
    parent=r.add_category(db,token(db),'TEST Parent',confirmed=True)
    child=r.add_category(db,token(db),'TEST Child',parent,confirmed=True)
    with pytest.raises(ValueError):r.add_category(db,token(db),'test parent',confirmed=True)
    with pytest.raises(ValueError):r.add_category(db,token(db),'TEST Grandchild',child,confirmed=True)
    op(db,'a');r.manual(db,token(db),'a',action(category_id=parent,subcategory_id=child),confirmed=True)
    assert r.snapshot(db)['current']['a']['subcategory_id']==child
    assert r.snapshot(db)['summary']['review_required']==[]


def test_account_requires_explicit_ownership_and_imported_pair(db):
    op(db,'a')
    with pytest.raises(EngineError):r.add_account(db,token(db),'TEST A','account-a')
    with pytest.raises(EngineError):r.add_account(db,token(db),'OTHER','missing',confirmed=True)
    r.add_account(db,token(db),'TEST A','account-a',confirmed=True)
    assert count(db,'own_accounts')==1 and count(db,'classifications')==0
    with pytest.raises(ValueError):r.add_account(db,token(db),'TEST A','account-a',confirmed=True)


def test_search_and_review_filter(db):
    op(db,'a',day='2026-08-01',description='TEST SHOP')
    op(db,'b',day='2026-09-01',description='TEST SALARY',direction='IN')
    assert [o['id'] for o in r.find(r.snapshot(db),text='shop',start='2026-08-01',end='2026-08-31')]==['a']
    r.manual(db,token(db),'a',action('technical'),confirmed=True)
    assert [o['id'] for o in r.find(r.snapshot(db),review=True)]==['b']
    assert [o['id'] for o in r.find(r.snapshot(db),bank='test a',account='account',kind='technical')]==['a']
    with pytest.raises(EngineError):r.find(r.snapshot(db),start='2026-09-01',end='2026-08-01')


def test_rule_preview_cancel_activation_and_manual_priority(db):
    op(db,'a');r.manual(db,token(db),'a',action(),confirmed=True)
    record=rule(kind='technical');t,matched=r.rule_preview(db,record)
    assert len(matched)==1 and count(db,'rules')==0
    with pytest.raises(EngineError):r.save_rule(db,t,record)
    r.save_rule(db,t,record,confirmed=True)
    assert count(db,'rules')==1
    assert preview(db,mode='history').changes=={}
    assert r.snapshot(db)['current']['a']['operation_type']=='expense'


def test_rule_edit_immutable_versions_and_status(db):
    op(db,'a');first=rule();r.save_rule(db,token(db),first,confirmed=True)
    second=r.rule_record(first['name'],first['conditions'],action('technical'),70,'2026-01-01',existing=first)
    r.save_rule(db,token(db),second,confirmed=True)
    assert count(db,'rule_versions')==2
    s=r.snapshot(db);assert json.loads(s['rules'][0]['action_json'])['operation_type']=='expense'
    r.set_rule_status(db,s['token'],'r','DISABLED',confirmed=True)
    assert r.snapshot(db)['rules'][-1]['status']=='DISABLED'
    assert count(db,'classifications')==0
    r.set_rule_status(db,token(db),'r','ACTIVE',confirmed=True)
    assert preview(db).changes['a']['operation_type']=='technical'


def test_rule_stale_after_backup_rejected(db,monkeypatch):
    op(db,'a');record=rule();t,_=r.rule_preview(db,record)
    from program.processing import catalog
    original=catalog.verified_backup
    def concurrent(c,d,v):
        result=original(c,d,v)
        r.add_category(db,token(db),'TEST concurrent',confirmed=True)
        return result
    monkeypatch.setattr(catalog,'verified_backup',concurrent)
    with pytest.raises(EngineError):r.save_rule(db,t,record,confirmed=True)
    assert count(db,'rules')==count(db,'rule_versions')==0


def test_ui_manual_only_no_rule_and_restart(db,monkeypatch,capsys):
    op(db,'a');answer(monkeypatch,['1','1','да','','ДА','нет'])
    ui.edit_operation(db,'a')
    assert count(db,'manual_overrides')==1 and count(db,'rules')==0
    assert r.snapshot(db)['current']['a']['operation_type']=='expense'
    assert 'Правило не создавалось' in capsys.readouterr().out


def test_ui_manual_cancel_no_write(db,monkeypatch):
    op(db,'a');answer(monkeypatch,['1','1','да','','нет'])
    ui.edit_operation(db,'a');assert count(db,'manual_overrides')==0


def test_ui_rule_formal_preview_and_cancel(db,monkeypatch,capsys):
    op(db,'a')
    def condition(op=None):return {'field':'description','op':'contains','value':'TEST'}
    monkeypatch.setattr(ui,'condition_wizard',condition)
    answer(monkeypatch,['TEST RULE','','','нет'])
    ui.rule_wizard(db,action=action())
    assert count(db,'rules')==0
    output=capsys.readouterr().out
    assert 'Формальная запись' in output and 'Правило не сохранено' in output


def test_ui_rule_separate_activation(db,monkeypatch):
    op(db,'a');monkeypatch.setattr(ui,'condition_wizard',lambda op=None:{'field':'description','op':'eq','value':'TEST EVENT'})
    answer(monkeypatch,['TEST RULE','','','ДА']);ui.rule_wizard(db,action=action())
    assert count(db,'rules')==1 and count(db,'manual_overrides')==0


def test_ui_paginated_search_does_not_write(db,monkeypatch,capsys):
    for i in range(12):op(db,str(i))
    before=token(db);answer(monkeypatch,['','нет','н','п','0'])
    ui.browse(db,review=True)
    assert token(db)==before
    assert 'Страница 2 из 2' in capsys.readouterr().out


def test_ui_eof_safe(db,monkeypatch,capsys):
    monkeypatch.setattr('builtins.input',lambda prompt:(_ for _ in ()).throw(EOFError()))
    ui.run(db.parent.parent,'search')
    assert 'отменено' in capsys.readouterr().out


def test_confirmed_link_own_registry_and_manual_conflict(db):
    op(db,'a');op(db,'b',direction='IN',bank='TEST B',account_id='account-b')
    group=r.new_link('OWN_TRANSFER',[('a','OUT'),('b','IN')],'TEST own')
    with pytest.raises(EngineError):r.link_change(db,token(db),group,confirmed=True)
    for bank,account in [('TEST A','account-a'),('TEST B','account-b')]:r.add_account(db,token(db),bank,account,confirmed=True)
    r.link_change(db,token(db),group,confirmed=True)
    assert count(db,'classifications')==0
    plan=preview(db);assert plan.after['expenses_kopecks']==plan.after['incomes_kopecks']==0
    apply(plan,db,confirmed=True)
    assert r.snapshot(db)['current']['a']['operation_type']=='technical'
    r.manual(db,token(db),'a',action(),confirmed=True)
    with pytest.raises(EngineError):r.link_change(db,token(db),group,confirmed=True)
    group={**group,'status':'REJECTED'};r.link_change(db,token(db),group,confirmed=True)
    assert r.snapshot(db)['groups'][0]['status']=='REJECTED'


def test_refund_group_multiple_returns_and_reject_overpayment(db):
    op(db,'buy');op(db,'return1',3000,'IN');op(db,'return2',2000,'IN')
    group=r.new_link('REFUND',[('buy','PURCHASE'),('return1','REFUND'),('return2','REFUND')],'TEST')
    r.link_change(db,token(db),group,confirmed=True)
    plan=preview(db);assert plan.after['expenses_kopecks']==5000 and plan.after['incomes_kopecks']==0
    op(db,'return3',7000,'IN')
    invalid=r.new_link('REFUND',[('buy','PURCHASE'),('return3','REFUND')],'TEST over')
    with pytest.raises(EngineError):r.link_change(db,token(db),invalid,confirmed=True)
    assert count(db,'operation_link_groups')==1


def test_manual_audit_failure_rolls_back_whole_decision(db,monkeypatch):
    op(db,'a')
    from program.database.operations import Store
    def fail(*args,**kw):raise RuntimeError('TEST audit failure')
    monkeypatch.setattr(Store,'_audit',fail)
    with pytest.raises(RuntimeError):r.manual(db,token(db),'a',action(),confirmed=True)
    assert count(db,'manual_overrides')==count(db,'classifications')==0


def test_real_menu_search_edit_and_exit(tmp_path,monkeypatch,capsys):
    root=tmp_path/'program with spaces';database=root/'database'/'finance.db';initialize(database);op(database,'a')
    from program import main
    monkeypatch.setattr(main,'PROJECT_ROOT',root)
    answer(monkeypatch,['3','','нет','1','1','1','да','','ДА','нет','0','0'])
    assert main.main()==0
    assert r.snapshot(database)['current']['a']['source']=='MANUAL'
    assert 'Ручное решение сохранено' in capsys.readouterr().out


@pytest.mark.parametrize(('field','operator','values'),[
    ('operation_date','date_between',['2026-08-01','2026-08-31']),
    ('amount_kopecks','between',['100','10000']),
    ('description','contains',['TEST']),
    ('has_link','eq',['да']),
])
def test_condition_wizard_supported_fields(monkeypatch,field,operator,values):
    from program.processing.rules import FIELDS,TEXT_FIELDS,DATE_FIELDS,validate_condition
    operators=['eq','ne']
    if field in TEXT_FIELDS:operators+=['contains','not_contains','starts_with','ends_with']
    if field=='amount_kopecks':operators+=['gt','lt','between']
    if field in DATE_FIELDS:operators+=['between','date_between']
    answer(monkeypatch,[str(sorted(FIELDS).index(field)+1),str(operators.index(operator)+1),*values,'нет'])
    conditions=ui.condition_wizard();validate_condition(conditions)
    assert conditions['and'][0]['field']==field


def test_ui_create_own_category_no_preseed(db,monkeypatch):
    answer(monkeypatch,['1','TEST PERSONAL CATEGORY','ДА','0'])
    ui.references(db)
    assert len(r.snapshot(db)['references']['categories'])==1
    assert r.snapshot(db)['references']['categories'][0]['name']=='TEST PERSONAL CATEGORY'
