"""Deterministic financial projection, read-only previews, verified guarded commits."""
from collections import Counter,defaultdict
from dataclasses import dataclass,asdict
from datetime import date
import hashlib
import json
from pathlib import Path

from program.database.connection import connect,transaction,read_snapshot,DEFAULT_DATABASE
from program.database.migrations import validate_database,verified_backup
from program.database.operations import Store
from program.database.financial import FinancialRepository
from .rules import EngineError,iso,load_rules,choose_rule
from .links import propose,refund_proposal,inspect_groups,CONFIRMED

ENGINE_VERSION='1'
DECISION_FIELDS=('operation_type','include_in_report','category_id','subcategory_id','project_id','source',
                 'manual_override_id','related_group_id','rule_id','rule_version','comment')


def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(value):return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def state_token(connection):
    return digest(FinancialRepository(connection).state())


def decision(kind='undefined',include=False,source='UNDEFINED',comment='',**fields):
    result=dict(operation_type=kind,include_in_report=int(include),source=source,comment=comment,
                category_id=None,subcategory_id=None,project_id=None,manual_override_id=None,
                related_group_id=None,rule_id=None,rule_version=None)
    result.update(fields)
    return result


def compact(row):return {field:row.get(field) for field in DECISION_FIELDS}


def load(connection):
    repository=FinancialRepository(connection)
    if repository.has_unfinished_operations():
        raise EngineError('В базе есть операции из незавершённой или ошибочной загрузки.')
    operations={}
    rows,classifications,overrides,groups=repository.data()
    for row in rows:
        op=dict(row)
        data=json.loads(op['source_data']) if op['source_data'] is not None else {}
        if not isinstance(data,dict):raise EngineError('Исходные данные операции должны быть JSON-объектом.')
        op['source_data']=data;operations[op['id']]=op
    current={row['operation_id']:compact(dict(row)) for row in classifications}
    manuals={}
    for row in overrides:
        value=dict(row)
        manuals[value['operation_id']]=decision(value['operation_type'],value['include_in_report'],'MANUAL',value['comment'],
            category_id=value['category_id'],subcategory_id=value['subcategory_id'],project_id=value['project_id'],manual_override_id=value['id'])
    return operations,current,manuals,groups


def in_period(operation,start,end):return (start is None or operation['operation_date']>=start) and (end is None or operation['operation_date']<=end)


def totals(operations,decisions,groups,manuals,start=None,end=None):
    decisions={**decisions,**manuals}
    _,allocations,link_errors,invalid=inspect_groups(operations,groups,manuals)
    contributions=[];undefined=[];review=[];unlinked_refunds=[];fallback_dates=[]
    by_refund={ret:(original,amount) for original,ret,amount in allocations}
    for identifier,op in operations.items():
        if not in_period(op,start,end):continue
        d=decisions.get(identifier,decision())
        kind=d['operation_type'];include=bool(d['include_in_report'])
        if kind=='undefined':undefined.append(identifier)
        if kind in {'expense','income'} and include and not d['category_id']:review.append(identifier)
        if op['source_data'].get('operation_date_source')=='POSTING_DATE':fallback_dates.append(identifier)
        if not include:continue
        if kind in {'expense','income'}:
            contributions.append(dict(operation_id=identifier,type=kind,amount_kopecks=op['amount_kopecks'],
                category_id=d['category_id'],subcategory_id=d['subcategory_id'],project_id=d['project_id']))
        elif kind=='refund':
            allocation=by_refund.get(identifier)
            if allocation is None:unlinked_refunds.append(identifier);continue
            original,amount=allocation;original_decision=decisions.get(original,decision())
            if original_decision['operation_type']!='expense':unlinked_refunds.append(identifier);continue
            if original_decision['include_in_report']:
                contributions.append(dict(operation_id=identifier,type='expense',amount_kopecks=-amount,
                    category_id=original_decision['category_id'],subcategory_id=original_decision['subcategory_id'],
                    project_id=original_decision['project_id']))
                if not original_decision['category_id']:review.append(identifier)
    buckets=defaultdict(int)
    for row in contributions:
        buckets[(row['type'],row['category_id'],row['subcategory_id'],row['project_id'])]+=row['amount_kopecks']
    breakdown=[dict(type=key[0],category_id=key[1],subcategory_id=key[2],project_id=key[3],amount_kopecks=value,
                    display_group=('Неклассифицированные расходы' if key[0]=='expense' else 'Доходы без категории') if key[1] is None else None)
               for key,value in sorted(buckets.items(),key=lambda item:canonical(item[0]))]
    selected={i for i,o in operations.items() if in_period(o,start,end)}
    relevant_errors=[error for error in link_errors if set(error['operation_ids'])&selected]
    proposed=sum(g['status']=='PROPOSED' and any(i in selected for i,_ in g['members']) for g in groups)
    status='ERROR' if undefined or unlinked_refunds or relevant_errors else (
        'WARNING' if review or fallback_dates or proposed else 'OK')
    return dict(expenses_kopecks=sum(r['amount_kopecks'] for r in contributions if r['type']=='expense'),
                incomes_kopecks=sum(r['amount_kopecks'] for r in contributions if r['type']=='income'),
                undefined=sorted(undefined),review_required=sorted(set(review)),
                unlinked_refunds=sorted(unlinked_refunds),date_fallbacks=sorted(fallback_dates),
                proposed_groups=proposed,link_errors=relevant_errors,status=status,
                breakdown=breakdown,contributions=contributions)


@dataclass(frozen=True)
class Plan:
    engine_version:str
    token:str
    mode:str
    as_of:str
    start:str|None
    end:str|None
    max_days:int
    changes:dict
    proposals:list
    before:dict
    after:dict

    @property
    def id(self):return digest(asdict(self))


def _preview(connection,mode,as_of,start,end,max_days):
    store=Store(connection)
    if not isinstance(mode,str) or mode not in {'new','history'}:raise EngineError('Режим должен быть new или history.')
    iso(as_of)
    if start is not None:iso(start)
    if end is not None:iso(end)
    if start and end and start>end:raise EngineError('Неверный период.')
    if type(max_days) is not int or not 0<=max_days<=31:raise EngineError('Окно предложений должно быть 0–31 день.')
    operations,current,manuals,groups=load(connection)
    rules=load_rules(connection,as_of)
    proposals=propose({i:op for i,op in operations.items() if i not in manuals and in_period(op,start,end)},FinancialRepository(connection).accounts(),groups,max_days)
    all_groups=groups+proposals
    meanings,allocations,errors,invalid=inspect_groups(operations,all_groups,manuals)
    result=dict(current)
    contexts={identifier:{'has_link':False,'link_type':None} for identifier in operations}
    for group in all_groups:
        if group['status'] in CONFIRMED:
            for identifier,_ in group['members']:
                if identifier in meanings:contexts[identifier]={'has_link':True,'link_type':group['link_type']}
    for identifier,op in operations.items():
        if not in_period(op,start,end):continue
        saved=current.get(identifier)
        if identifier in manuals:
            result[identifier]=manuals[identifier];continue
        if identifier in invalid:
            result[identifier]=decision(comment='Противоречивые подтверждённые связи требуют проверки.');continue
        link=meanings.get(identifier)
        if link:
            keep={key:saved[key] for key in ('category_id','subcategory_id','project_id')} if saved and saved['operation_type']=='expense' and link['operation_type']=='expense' else {}
            if saved and saved['source']=='LINK' and saved['related_group_id']==link['group_id'] and saved['operation_type']==link['operation_type']:
                result[identifier]=saved
            else:result[identifier]=decision(link['operation_type'],link['include_in_report'],'LINK',
                ('Подтверждённая экономическая связь.'+
                 (f" Категория сохранена из правила {saved['rule_id']}/v{saved['rule_version']}." if keep and saved['source']=='RULE' else '')),
                related_group_id=link['group_id'],**keep)
            continue
        if saved and mode=='new' and saved['source']!='LINK':
            result[identifier]=saved;continue
        rule,conflicts=choose_rule(rules,op,contexts[identifier])
        if conflicts:
            result[identifier]=decision(comment='Конфликт равноприоритетных правил: '+', '.join(f'{r.id}/v{r.version}' for r in conflicts));continue
        if rule is None:
            result[identifier]=decision(comment='Нет подтверждённой связи или однозначного правила.');continue
        a=rule.action;kind=a['operation_type']
        if (kind=='expense' and op['direction']!='OUT') or (kind in {'income','refund'} and op['direction']!='IN'):
            result[identifier]=decision(comment=f'Правило {rule.id}/v{rule.version} противоречит направлению.');continue
        target=a['original_operation_id']
        if target is not None:
            original=operations.get(target)
            if original is None or original['direction']!='OUT' or original['amount_kopecks']<op['amount_kopecks'] or target==identifier:
                raise EngineError(f'Правило {rule.id}: неверная исходная покупка возврата.')
            candidate=refund_proposal(target,identifier,all_groups)
            if candidate:proposals.append(candidate);all_groups.append(candidate)
        result[identifier]=decision(kind,a['include_in_report'],'RULE',a['comment'],rule_id=rule.id,rule_version=rule.version,
            category_id=a['category_id'],subcategory_id=a['subcategory_id'],project_id=a['project_id'])
    changes={identifier:value for identifier,value in sorted(result.items()) if current.get(identifier)!=value}
    return Plan(ENGINE_VERSION,state_token(connection),mode,as_of,start,end,max_days,changes,
                sorted(proposals,key=lambda group:group['id']),
                totals(operations,current,groups,manuals,start,end),
                totals(operations,result,all_groups,manuals,start,end))


def preview(database=DEFAULT_DATABASE,*,mode='new',as_of=None,start=None,end=None,max_days=3):
    with connect(database,readonly=True) as connection:
        # A consistent read snapshot, with no database writes or backup side effects.
        with read_snapshot(connection):
            return _preview(connection,mode,as_of or date.today().isoformat(),start,end,max_days)



def apply(plan,database=DEFAULT_DATABASE,*,confirmed=False,backup_dir=None):
    if confirmed is not True:raise EngineError('Нужно явное подтверждение предварительного расчёта.')
    if not isinstance(plan,Plan) or plan.engine_version!=ENGINE_VERSION:raise EngineError('Несовместимый предварительный расчёт.')
    database=Path(database)
    with connect(database) as connection:
        fresh=_preview(connection,plan.mode,plan.as_of,plan.start,plan.end,plan.max_days)
        if fresh.id!=plan.id:raise EngineError('Данные или предварительный расчёт изменились. Выполните новый просмотр.')
        if not plan.changes and not plan.proposals:return None
        folder=Path(backup_dir) if backup_dir is not None else (
            database.parent.parent/'backup' if database.parent.name=='database' else database.parent/'backup')
        backup=verified_backup(connection,folder,validate_database)
        with transaction(connection):
            locked=_preview(connection,plan.mode,plan.as_of,plan.start,plan.end,plan.max_days)
            if locked.id!=plan.id:raise EngineError('Данные изменились после просмотра. Выполните новый просмотр.')
            store=Store(connection)
            repository=FinancialRepository(connection)
            for group in plan.proposals:repository.insert_group(group,store)
            for identifier,value in plan.changes.items():repository.save_decision(identifier,value,store)
            store._audit('FINANCIAL_RUN','classifications',plan.id,None,
                         {'mode':plan.mode,'changed':len(plan.changes),'new_groups':len(plan.proposals),
                          'period':[plan.start,plan.end],'as_of':plan.as_of,'status':plan.after['status']})
            validate_database(connection)
        return backup


def save_plan(plan,path):
    with Path(path).open('x',encoding='utf-8') as output:json.dump(asdict(plan),output,ensure_ascii=False,sort_keys=True,indent=2)


def read_plan(path):
    with Path(path).open(encoding='utf-8') as source:raw=json.load(source)
    try:return Plan(**raw)
    except TypeError as error:raise EngineError('Неверный файл предварительного расчёта.') from error
