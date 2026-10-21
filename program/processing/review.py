"""Explicit user decisions, optimistic guards, searchable immutable facts."""
from datetime import date
from pathlib import Path
from uuid import uuid4
import json

from program.database.connection import connect,read_snapshot,transaction
from program.database.migrations import validate_database
from program.database.operations import Store
from program.database.review import ReviewRepository
from .engine import load,state_token,decision,totals
from .rules import EngineError,validate_action,matches,iso
from .catalog import install,prepare
from .links import inspect_groups,group_id


def snapshot(database):
    with connect(database,readonly=True) as c,read_snapshot(c):
        validate_database(c);ops,current,manuals,groups=load(c);repo=ReviewRepository(c)
        return dict(token=state_token(c),operations=ops,current={**current,**manuals},manuals=manuals,
                    groups=groups,references=repo.references(),rules=repo.rules(),
                    summary=totals(ops,current,groups,manuals))


def guard(c,token):
    if state_token(c)!=token:raise EngineError('База изменилась. Откройте операцию заново.')


def find(state,*,text='',start=None,end=None,bank='',account='',kind=None,review=False):
    if start:iso(start)
    if end:iso(end)
    if start and end and start>end:raise EngineError('Начало периода позже конца.')
    need=set(state['summary']['undefined'])|set(state['summary']['review_required'])|set(state['summary']['unlinked_refunds'])
    for error in state['summary']['link_errors']:need.update(error['operation_ids'])
    result=[]
    for identifier,op in state['operations'].items():
        d=state['current'].get(identifier,decision())
        if review and identifier not in need:continue
        if start and op['operation_date']<start or end and op['operation_date']>end:continue
        if bank and bank.casefold() not in op['bank'].casefold():continue
        if account and account not in op['account_id']:continue
        if kind and d['operation_type']!=kind:continue
        if text and text.casefold() not in (' '.join(str(op.get(k) or '') for k in ('id','description','counterparty','mcc'))).casefold():continue
        result.append(op)
    return sorted(result,key=lambda op:(op['operation_date'],op['bank'],op['id']))


def manual(database,token,identifier,action,*,confirmed=False):
    if confirmed is not True:raise EngineError('Ручное решение требует подтверждения.')
    with connect(database) as c,transaction(c):
        guard(c,token);ops,current,manuals,groups=load(c)
        if identifier not in ops:raise EngineError('Операция не найдена.')
        refs=ReviewRepository(c).references()
        # The user explicitly confirms this single operation; the automatic-rule
        # evidence guard is therefore supplied with its exact immutable description.
        value=validate_action(action,{'field':'description','op':'eq','value':ops[identifier]['description']},
                              {r['id']:r for r in refs['categories']},{r['id']:r for r in refs['projects']})
        if value['original_operation_id']:raise EngineError('Связь возврата задаётся отдельно.')
        kind=value['operation_type'];op=ops[identifier]
        if kind=='expense' and op['direction']!='OUT' or kind in {'income','refund'} and op['direction']!='IN':
            raise EngineError('Выбранный тип противоречит направлению банковской операции.')
        Store(c)._record_manual_decision(identifier,operation_type=kind,include_in_report=value['include_in_report'],
            category_id=value['category_id'],subcategory_id=value['subcategory_id'],project_id=value['project_id'],comment=value['comment'])


def history(database,identifier):
    with connect(database,readonly=True) as c:return ReviewRepository(c).history(identifier)


def add_category(database,token,name,parent=None,*,confirmed=False):
    if confirmed is not True:raise EngineError('Создание категории требует подтверждения.')
    with connect(database) as c,transaction(c):
        guard(c,token);return ReviewRepository(c).add_category(name,parent)


def add_account(database,token,bank,account,owner='',phone='',*,confirmed=False):
    if confirmed is not True:raise EngineError('Нужно подтвердить, что счёт ваш.')
    with connect(database) as c,transaction(c):
        guard(c,token)
        if not ReviewRepository(c).has_operation_account(bank,account):
            raise EngineError('Выберите счёт из загруженных операций.')
        return ReviewRepository(c).add_account(bank,account,owner,phone)


def rule_record(name,conditions,action,priority=50,effective_from=None,existing=None):
    return dict(id=existing['id'] if existing else uuid4().hex,name=existing['name'] if existing else name,
                version=existing['version']+1 if existing else 1,conditions=conditions,action=action,priority=priority,
                effective_from=effective_from or date.today().isoformat(),status='ACTIVE')


def rule_preview(database,record):
    with connect(database,readonly=True) as c,read_snapshot(c):
        validate_database(c);prepare(c,[record]);ops,_,manuals,groups=load(c)
        contexts={identifier:{'has_link':False,'link_type':None} for identifier in ops}
        meanings,_,_,_=inspect_groups(ops,groups,manuals)
        for group in groups:
            if group['status'] in {'USER_CONFIRMED','AUTO_CONFIRMED'}:
                for identifier,_ in group['members']:
                    if identifier in meanings:contexts[identifier]={'has_link':True,'link_type':group['link_type']}
        return state_token(c),[op for op in ops.values() if matches(record['conditions'],op,contexts[op['id']])]


def save_rule(database,token,record,*,confirmed=False):
    if confirmed is not True:raise EngineError('Активация правила требует отдельного подтверждения.')
    path=Path(database);root=path.parent.parent if path.parent.name=='database' else path.parent
    with connect(database) as c:
        guard(c,token)
        return install(c,[record],confirmed=True,backup_dir=root/'backup',expected_token=token)


def set_rule_status(database,token,identifier,status,*,confirmed=False):
    if confirmed is not True or status not in {'DISABLED','ACTIVE'}:raise EngineError('Нужно подтвердить статус правила.')
    state=snapshot(database)
    rows=[r for r in state['rules'] if r['id']==identifier]
    if not rows:raise EngineError('Правило не найдено.')
    row=max(rows,key=lambda r:r['version'])
    record={k:row[k] for k in ('id','name','version','priority','effective_from')}
    record.update(conditions=json.loads(row['conditions_json']),action=json.loads(row['action_json']),status=status)
    return save_rule(database,token,record,confirmed=True)


def link_change(database,token,group,*,confirmed=False):
    if confirmed is not True:raise EngineError('Связь требует подтверждения.')
    with connect(database) as c,transaction(c):
        guard(c,token);ops,current,manuals,groups=load(c);store=Store(c)
        old=next((g for g in groups if g['id']==group['id']),None)
        if old and (old['link_type']!=group['link_type'] or sorted(old['members'])!=sorted(group['members'])):
            raise EngineError('Состав существующей связи изменять нельзя. Создайте новую.')
        if group['status'] not in {'USER_CONFIRMED','REJECTED'}:raise EngineError('Недопустимый статус связи.')
        if group['status']=='USER_CONFIRMED':
            technical=[i for i,role in group['members'] if role in {'OUT','IN'}]
            if group['link_type']=='OWN_TRANSFER' or group['link_type']=='CHAIN' and technical:
                own={(r['bank'],r['account']) for r in ReviewRepository(c).references()['own_accounts'] if r['status']=='ACTIVE'}
                if any(i not in ops or (ops[i]['bank'],ops[i]['account_id']) not in own for i in technical):
                    raise EngineError('Сначала подтвердите собственные счета всех участников перевода.')
                if len({(ops[i]['bank'],ops[i]['account_id']) for i in technical})<2:
                    raise EngineError('Перевод должен связывать разные собственные счета.')
            _,_,errors,_=inspect_groups(ops,[g for g in groups if g['id']!=group['id']]+[group],manuals)
            if any(error['group_id']==group['id'] or set(error['operation_ids'])&{i for i,_ in group['members']} for error in errors):
                raise EngineError('Связь противоречит суммам, направлениям, другим связям или ручным решениям.')
        if old:
            ReviewRepository(c).update_group(group,old)
        else:
            from program.database.financial import FinancialRepository
            FinancialRepository(c).insert_group(group,store)
        # Classification is explicitly previewed and confirmed via menu 6 afterward.


def new_link(kind,members,comment):
    return dict(id=group_id(kind,members),link_type=kind,status='USER_CONFIRMED',members=members,comment=comment)
