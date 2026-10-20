"""Evidence-backed transfer proposals and validation of explicit economic groups."""
from collections import defaultdict
from datetime import date
import json
from uuid import UUID,uuid5

from .rules import EngineError

NAMESPACE=UUID('8f75b9a1-4d0c-42a9-bbb1-6960bd4bbabc')
CONFIRMED={'AUTO_CONFIRMED','USER_CONFIRMED'}


def group_id(kind,members):
    return uuid5(NAMESPACE,json.dumps([kind,sorted(members)],sort_keys=True)).hex


def propose(operations,accounts,groups,max_days=3):
    """Date/amount only nominates a proposal; never a confirmed technical move."""
    own={(row['bank'],row['account']) for row in accounts if row['status']=='ACTIVE'}
    existing={frozenset(member[0] for member in group['members']) for group in groups}
    occupied={identifier for group in groups if group['status'] in CONFIRMED for identifier,_ in group['members']}
    eligible=[op for op in operations.values() if (op['bank'],op['account_id']) in own and op['id'] not in occupied]
    references=defaultdict(list)
    for op in eligible:
        reference=op['source_data'].get('transfer_reference')
        # A network-qualified reference is a bank fact supplied by a parser, not
        # an authorization code, amount, person's name or fuzzy text match.
        if (isinstance(reference,str) and ':' in reference and all(reference.split(':',1))
                and not any(character.isspace() for character in reference)):
            references[reference].append(op)
    proposals=[];paired=set()
    for reference,ops in sorted(references.items()):
        outs=[o for o in ops if o['direction']=='OUT'];ins=[o for o in ops if o['direction']=='IN']
        if len(outs)!=1 or len(ins)!=1:continue
        out,inc=outs[0],ins[0]
        members=sorted([(out['id'],'OUT'),(inc['id'],'IN')])
        pair=frozenset((out['id'],inc['id']))
        if ((out['bank'],out['account_id'])==(inc['bank'],inc['account_id'])
                or out['amount_kopecks']!=inc['amount_kopecks'] or pair in existing):continue
        proposals.append(dict(id=group_id('OWN_TRANSFER',members),link_type='OWN_TRANSFER',status='AUTO_CONFIRMED',
                              members=members,comment='Совпавший сетевой идентификатор и два подтверждённых собственных счёта.'))
        existing.add(pair);paired.update(pair)
    # Bucket candidates rather than compare every operation with every other one.
    by_amount=defaultdict(list)
    for op in eligible:
        if op['id'] not in paired:by_amount[op['amount_kopecks']].append(op)
    for amount,ops in sorted(by_amount.items()):
        for out in [op for op in ops if op['direction']=='OUT']:
            for inc in [op for op in ops if op['direction']=='IN']:
                pair=frozenset((out['id'],inc['id']))
                if pair in existing or (out['bank'],out['account_id'])==(inc['bank'],inc['account_id']):continue
                days=abs((date.fromisoformat(out['operation_date'])-date.fromisoformat(inc['operation_date'])).days)
                if days>max_days:continue
                members=sorted([(out['id'],'OUT'),(inc['id'],'IN')])
                proposals.append(dict(id=group_id('OWN_TRANSFER',members),link_type='OWN_TRANSFER',status='PROPOSED',
                                      members=members,comment='Совпали сумма и близкие даты; связь требует подтверждения.'))
                existing.add(pair)
    return sorted(proposals,key=lambda g:g['id'])


def refund_proposal(original,refund,existing):
    members=sorted([(original,'PURCHASE'),(refund,'REFUND')])
    identifier=group_id('REFUND',members)
    if any(frozenset(op for op,_ in g['members'])==frozenset((original,refund)) for g in existing):return None
    return dict(id=identifier,link_type='REFUND',status='PROPOSED',members=members,
                comment='Правило предлагает исходную покупку; требуется подтверждение связи.')


def inspect_groups(operations,groups,manuals):
    """Return per-operation meanings, refund allocations and blocking diagnostics."""
    meanings={};refunds=[];errors=[];invalid=set();used={}
    for group in sorted(groups,key=lambda g:g['id']):
        if group['status'] not in CONFIRMED:continue
        ids={identifier for identifier,_ in group['members']}
        try:
            if len(ids)!=len(group['members']) or len(ids)<2:raise EngineError('Нужны различные участники группы.')
            members=[(operations[identifier],role) for identifier,role in group['members']]
            kind=group['link_type'];local={};allocations=[]
            if kind=='OWN_TRANSFER':
                if any(role!=op['direction'] for op,role in members):raise EngineError('Для перевода нужны роли OUT/IN по направлениям.')
                if not any(op['direction']=='IN' for op,_ in members):raise EngineError('Нет входящей части перевода.')
                if sum(op['amount_kopecks']*(1 if op['direction']=='IN' else -1) for op,_ in members)!=0:
                    raise EngineError('Суммы собственного перевода не сходятся.')
                local={op['id']:('technical',False) for op,_ in members}
            elif kind in {'REFUND','CHAIN'}:
                allowed={'PURCHASE','REFUND'} if kind=='REFUND' else {'OUT','IN','PURCHASE','REFUND'}
                if any(role not in allowed for _,role in members):raise EngineError('Неизвестные роли экономической группы.')
                technical=[(op,role) for op,role in members if role in {'OUT','IN'}]
                if technical:
                    if any(op['direction']!=role for op,role in technical):raise EngineError('Неверное направление части цепочки.')
                    if sum(op['amount_kopecks']*(1 if role=='IN' else -1) for op,role in technical)!=0:
                        raise EngineError('Техническая часть цепочки не сбалансирована.')
                    local.update({op['id']:('technical',False) for op,_ in technical})
                purchases=[op for op,role in members if role=='PURCHASE']
                returns=[op for op,role in members if role=='REFUND']
                if any(op['direction']!='OUT' for op in purchases) or any(op['direction']!='IN' for op in returns):
                    raise EngineError('Покупка должна быть OUT, возврат IN.')
                if returns and len(purchases)!=1:raise EngineError('Возвратам нужна одна явно указанная исходная покупка.')
                if kind=='REFUND' and (not purchases or not returns):raise EngineError('Нужны покупка и возврат.')
                if not purchases and not technical:raise EngineError('У цепочки нет экономического содержания.')
                local.update({op['id']:('expense',True) for op in purchases})
                local.update({op['id']:('refund',True) for op in returns})
                for ret in returns:
                    original=purchases[0]
                    if (ret['source_data'].get('operation_date_source')!='POSTING_DATE'
                            and original['source_data'].get('operation_date_source')!='POSTING_DATE'
                            and ret['operation_date']<original['operation_date']):raise EngineError('Возврат датирован раньше покупки.')
                    allocations.append((original['id'],ret['id'],ret['amount_kopecks']))
                if returns and sum(ret['amount_kopecks'] for ret in returns)>purchases[0]['amount_kopecks']:
                    raise EngineError('Возвраты превышают исходную покупку.')
            else:raise EngineError('Неизвестный тип группы.')
            for identifier,(kind,include) in local.items():
                if identifier in used:raise EngineError('Операция входит в несколько подтверждённых групп.')
                manual=manuals.get(identifier)
                if manual and manual['operation_type']!=kind:
                    raise EngineError('Ручное решение противоречит подтверждённой связи; оно не изменено.')
            for identifier,(kind,include) in local.items():
                meanings[identifier]=dict(operation_type=kind,include_in_report=include,group_id=group['id'])
                used[identifier]=group['id']
            refunds.extend(allocations)
        except (EngineError,KeyError) as error:
            invalid.update(ids)
            errors.append(dict(group_id=group['id'],operation_ids=sorted(ids),reason=str(error)))
    # If a later group conflicts with an earlier one, neither may silently win.
    affected=True
    while affected:
        affected=False
        for group in groups:
            ids={identifier for identifier,_ in group['members']}
            if group['status'] in CONFIRMED and ids&invalid and not ids<=invalid:
                invalid.update(ids);affected=True
    meanings={k:v for k,v in meanings.items() if k not in invalid}
    refunds=[row for row in refunds if row[0] not in invalid and row[1] not in invalid]
    return meanings,refunds,errors,invalid
