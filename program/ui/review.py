"""Russian console review: explicit choices, separate decisions and rules."""
from datetime import date
import json
from pathlib import Path
import sqlite3

from program.database.connection import DatabaseError
from program.importers.service import format_kopecks
from program.processing import review as service
from program.processing.engine import decision
from program.processing.rules import FIELDS,TEXT_FIELDS,DATE_FIELDS,EngineError,validate_condition

KINDS={'expense':'Расход','income':'Доход','technical':'Техническое движение','refund':'Возврат','undefined':'Не определён'}
FIELDS_RU={'description':'Исходное описание','bank':'Банк','account_id':'Счёт','card_number_mask':'Маска карты',
 'mcc':'MCC','counterparty':'Контрагент','phone':'Телефон','bank_operation_type':'Тип операции банка',
 'operation_date':'Дата совершения','processing_date':'Дата проводки','amount_kopecks':'Сумма в копейках',
 'direction':'Направление IN/OUT','has_link':'Есть подтверждённая связь','link_type':'Тип связи'}
SOURCES={'MANUAL':'ручное решение','LINK':'подтверждённая связь','RULE':'правило','UNDEFINED':'не определён'}
STATUSES={'ACTIVE':'действует','DISABLED':'отключено','ARCHIVED':'в архиве','PROPOSED':'предложена','USER_CONFIRMED':'подтверждена вами','AUTO_CONFIRMED':'подтверждена автоматически','REJECTED':'отклонена'}
LINKS={'OWN_TRANSFER':'Собственный перевод','REFUND':'Покупка и возврат','CHAIN':'Цепочка операций'}
OPS={'eq':'равно','ne':'не равно','contains':'содержит','not_contains':'не содержит',
 'starts_with':'начинается с','ends_with':'заканчивается на','gt':'больше','lt':'меньше',
 'between':'между','date_between':'дата между'}


def section(title):print('\n'+'─'*56+'\n'+title+'\n')
def yes(prompt):return input(prompt+' Введите ДА: ').strip().casefold()=='да'
def choose(rows,label,zero='Без выбора'):
    for n,row in enumerate(rows,1):print(f'{n}. {label(row)}')
    print(f'0. {zero}')
    value=input('Выберите номер: ').strip()
    if value=='0':return None
    if not value.isdigit() or not 1<=int(value)<=len(rows):raise EngineError('Нет такого номера. Действие отменено.')
    return rows[int(value)-1]


def category_label(row,state):
    parent=next((r['name'] for r in state['references']['categories'] if r['id']==row['parent_id']),None)
    return f"{parent} / {row['name']}" if parent else row['name']


def show_operation(op,state):
    d=state['current'].get(op['id'],decision());cats={r['id']:r for r in state['references']['categories']}
    section(f"Операция {op['id']}")
    print(f"Дата: {op['operation_date']}. Банк: {op['bank']}. Счёт: …{op['account_id'][-4:]}.")
    print(f"{'Списание' if op['direction']=='OUT' else 'Поступление'}: {format_kopecks(op['amount_kopecks'])} ₽.")
    print(f"Описание: {op['description']}")
    print(f"Дата проводки: {op['processing_date'] or 'не указана'}. MCC: {op['mcc'] or 'не указан'}.")
    print(f"Тип: {KINDS[d['operation_type']]}. В итогах: {'да' if d['include_in_report'] else 'нет'}.")
    print('Категория: '+(' / '.join(cats[i]['name'] for i in (d['category_id'],d['subcategory_id']) if i in cats) or 'не выбрана'))
    print(f"Источник решения: {SOURCES[d['source']]}"+(f"; правило {d['rule_id']}, версия {d['rule_version']}" if d['rule_id'] else ''))
    if d['comment']:print('Комментарий: '+d['comment'])
    if op['source_data'].get('operation_date_source')=='POSTING_DATE':print('Вместо даты совершения банк предоставил только дату проводки.')


def action_wizard(state,op=None):
    kinds=['expense','income','technical','refund','undefined']
    if op:kinds=[k for k in kinds if k not in ({'income','refund'} if op['direction']=='OUT' else {'expense'})]
    kind=choose(kinds,lambda k:KINDS[k],zero='Отмена')
    if kind is None:return None
    include=kind in {'expense','income','refund'}
    if include:include=yes('Включать в финансовые итоги?')
    category=sub=project=None
    if kind in {'expense','income'}:
        cats=[r for r in state['references']['categories'] if r['status']=='ACTIVE' and r['parent_id'] is None]
        if cats:
            section('Категория');category=choose(cats,lambda r:r['name'],zero='Без категории')
            if category:
                children=[r for r in state['references']['categories'] if r['parent_id']==category['id'] and r['status']=='ACTIVE']
                if children:sub=choose(children,lambda r:r['name'],zero='Без подкатегории')
        else:print('Категорий пока нет. Их можно создать в пункте 6 → Настроить справочники.')
        if kind=='expense' and include and category is None:print('Расход войдёт в «Неклассифицированные расходы» до выбора категории.')
        projects=[r for r in state['references']['projects'] if r['status']=='ACTIVE']
        if projects:project=choose(projects,lambda r:r['name'],zero='Без проекта')
    if kind=='refund':print('Возврат уменьшит расходы только после подтверждения связи с покупкой (пункт 6 → Связи).')
    comment=input('Комментарий (можно Enter): ').strip() or 'Решение пользователя в консоли.'
    return dict(operation_type=kind,include_in_report=include,category_id=category['id'] if category else None,
                subcategory_id=sub['id'] if sub else None,project_id=project['id'] if project else None,comment=comment)


def describe_action(action,state):
    action={**action,'operation_type':action.get('operation_type',action.get('type'))}
    action.setdefault('include_in_report',action['operation_type'] in {'expense','income','refund'})
    names={r['id']:r['name'] for r in state['references']['categories']}
    print(f"Результат: {KINDS[action['operation_type']]}; в итогах: {'да' if action['include_in_report'] else 'нет'}.")
    print('Категория: '+(' / '.join(names[i] for i in (action.get('category_id'),action.get('subcategory_id')) if i in names) or 'не выбрана'))


def edit_operation(database,identifier):
    state=service.snapshot(database);op=state['operations'][identifier];show_operation(op,state)
    print('\n1. Изменить решение\n2. Показать историю ручных изменений\n3. Предложить правило\n0. Назад')
    choice=input('Выберите действие: ').strip()
    if choice=='0':return
    if choice=='2':
        history=service.history(database,identifier)
        for row in history:
            print(f"\n{row['created_at']} | решение №{row['id']} | предыдущее: {row['previous_id'] or 'нет'}")
            describe_action(row,state);print(row['comment'])
        if not history:print('Ручных изменений пока нет.')
        return
    if choice=='3':rule_wizard(database,op=op);return
    if choice!='1':raise EngineError('Неизвестное действие.')
    action=action_wizard(state,op)
    if action is None:return
    section('Решение только для этой операции');describe_action(action,state)
    if not yes('Сохранить это ручное решение?'):print('Отменено. База не изменена.');return
    service.manual(database,state['token'],identifier,action,confirmed=True)
    print('\nРучное решение сохранено. Правило не создавалось.')
    if yes('Хотите отдельно подготовить правило для будущих операций?'):rule_wizard(database,op=op,action=action)


def browse(database,review=False):
    section('Разбор неопределённых' if review else 'Поиск операции')
    text=input('Текст описания, MCC или ID (Enter — все): ').strip()
    start=end=bank=account=None
    if yes('Добавить фильтры по периоду, банку или счёту?'):
        start=input('С даты YYYY-MM-DD (Enter — без ограничения): ').strip() or None
        end=input('По дату YYYY-MM-DD (Enter — без ограничения): ').strip() or None
        bank=input('Банк (Enter — любой): ').strip();account=input('Часть номера счёта (Enter — любой): ').strip()
    page=0
    while True:
        state=service.snapshot(database);rows=service.find(state,text=text,start=start,end=end,bank=bank,account=account,review=review)
        if not rows:print('\nПодходящих операций нет.');return
        page=min(page,(len(rows)-1)//10);part=rows[page*10:(page+1)*10]
        section(f'Найдено: {len(rows)}. Страница {page+1} из {(len(rows)+9)//10}')
        for i,op in enumerate(part,1):
            d=state['current'].get(op['id'],decision())
            print(f"{i}. {op['operation_date']} | {op['bank']} …{op['account_id'][-4:]} | {'−' if op['direction']=='OUT' else '+'}{format_kopecks(op['amount_kopecks'])} ₽ | {KINDS[d['operation_type']]}\n   {op['description'][:110]}")
        value=input('\nНомер — открыть; н — следующая; п — предыдущая; 0 — назад: ').strip().casefold()
        if value=='0':return
        if value=='н':page=min(page+1,(len(rows)-1)//10);continue
        if value=='п':page=max(0,page-1);continue
        if value.isdigit() and 1<=int(value)<=len(part):edit_operation(database,part[int(value)-1]['id'])
        else:print('Выберите номер операции на этой странице.')


def condition_wizard(op=None):
    conditions=[]
    while True:
        section('Условия правила (все условия должны выполняться)')
        field=choose(sorted(FIELDS),lambda k:FIELDS_RU[k],zero='Отмена')
        if field is None:return None
        operators=['eq','ne']
        if field in TEXT_FIELDS:operators+=['contains','not_contains','starts_with','ends_with']
        if field=='amount_kopecks':operators+=['gt','lt','between']
        if field in DATE_FIELDS:operators+=['between','date_between']
        operator=choose(operators,lambda k:OPS[k],zero='Отмена')
        if operator is None:return None
        fact=(op.get('source_data',{}).get(field) if field in {'phone','card_number_mask'} else op.get(field)) if op else None
        if fact is not None:print(f'В открытой операции: {fact}')
        def read_value():
            raw=input('Значение (для дат YYYY-MM-DD, денег — целые копейки): ').strip()
            if field=='amount_kopecks':return int(raw)
            if field=='has_link':
                if raw.casefold() not in {'да','нет'}:raise EngineError('Для признака связи введите да или нет.')
                return raw.casefold()=='да'
            if not raw:raise EngineError('Пустое значение не допускается в мастере правила.')
            return raw
        value=[read_value(),read_value()] if operator in {'between','date_between'} else read_value()
        node=dict(field=field,op=operator,value=value);validate_condition(node);conditions.append(node)
        if not yes('Добавить ещё одно условие И?'):return {'and':conditions}


def show_rule(record,state):
    section(f"Правило «{record['name']}», версия {record['version']}")
    def show(node,indent=''):
        if 'and' in node or 'or' in node:
            key='and' if 'and' in node else 'or';print(indent+('ВСЕ условия:' if key=='and' else 'ХОТЯ БЫ ОДНО условие:'))
            for child in node[key]:show(child,indent+'  ')
        else:print(f"{indent}{FIELDS_RU[node['field']]} {OPS[node['op']]} {json.dumps(node['value'],ensure_ascii=False)}")
    show(record['conditions']);describe_action(record['action'],state)
    print(f"Приоритет: {record['priority']}. Действует с: {record['effective_from']}. Статус: {STATUSES[record['status']]}.")
    print('Формальная запись: '+json.dumps({k:record[k] for k in ('conditions','action')},ensure_ascii=False))


def rule_wizard(database,op=None,action=None,existing=None):
    state=service.snapshot(database)
    section('Новая версия правила' if existing else 'Новое правило для будущих операций')
    name=existing['name'] if existing else input('Название правила (0 — отмена): ').strip()
    if name=='0':return
    conditions=condition_wizard(op)
    if conditions is None:return
    if action is None:action=action_wizard(state,op)
    if action is None:return
    priority=int(input('Числовой приоритет (Enter — 50): ').strip() or '50')
    effective=input(f'Дата активации YYYY-MM-DD (Enter — {date.today().isoformat()}): ').strip() or date.today().isoformat()
    record=service.rule_record(name,conditions,action,priority,effective,existing)
    token,matched=service.rule_preview(database,record);show_rule(record,state)
    print(f'Условия совпадают с {len(matched)} операциями текущей базы. Это просмотр, история не меняется.')
    for row in matched[:5]:print(f"  {row['operation_date']} | {row['bank']} | {format_kopecks(row['amount_kopecks'])} ₽ | {row['description'][:90]}")
    if not yes('Активировать именно это правило?'):print('Правило не сохранено.');return
    service.save_rule(database,token,record,confirmed=True)
    print('\nПравило сохранено. История и ручные решения не пересчитывались. Для прошлого нужен пункт 6 → 3.')


def rules_menu(database):
    while True:
        state=service.snapshot(database);latest={}
        for r in state['rules']:latest[r['id']]=r
        section('Правила')
        print('1. Посмотреть правило и историю версий\n2. Создать правило\n0. Назад')
        choice=input('Выберите действие: ').strip()
        if choice=='0':return
        if choice=='2':rule_wizard(database);continue
        if choice!='1':print('Неизвестное действие.');continue
        row=choose(list(latest.values()),lambda r:f"{r['name']} | v{r['version']} | {STATUSES[r['status']]}",zero='Назад')
        if row is None:continue
        for version in (r for r in state['rules'] if r['id']==row['id']):
            record={**version,'conditions':json.loads(version['conditions_json']),'action':json.loads(version['action_json'])}
            show_rule(record,state)
        print('\n1. Создать новую версию\n2. '+('Отключить' if row['status']=='ACTIVE' else 'Активировать')+'\n0. Назад')
        action=input('Выберите действие: ').strip()
        if action=='1':rule_wizard(database,existing=row)
        elif action=='2':
            status='DISABLED' if row['status']=='ACTIVE' else 'ACTIVE'
            if yes(f"Изменить статус правила «{row['name']}» на {STATUSES[status]}?"):
                service.set_rule_status(database,state['token'],row['id'],status,confirmed=True)
                print('Статус изменён. История не пересчитывалась.')


def references(database):
    while True:
        state=service.snapshot(database);section('Ваши категории и собственные счета')
        for row in state['references']['categories']:print(f"Категория: {category_label(row,state)} | {STATUSES[row['status']]}")
        for row in state['references']['own_accounts']:print(f"Собственный счёт: {row['bank']} …{row['account'][-4:]} | {STATUSES[row['status']]}")
        print('\n1. Создать категорию/подкатегорию\n2. Подтвердить собственный счёт из выписок\n0. Назад')
        choice=input('Выберите действие: ').strip()
        if choice=='0':return
        if choice=='1':
            name=input('Название (0 — отмена): ').strip()
            if name=='0':continue
            parents=[r for r in state['references']['categories'] if r['status']=='ACTIVE' and r['parent_id'] is None]
            parent=choose(parents,lambda r:r['name'],zero='Корневая категория') if parents else None
            print(f"Новая категория: {name}"+(f"; родитель: {parent['name']}" if parent else ''))
            if yes('Создать?'):
                service.add_category(database,state['token'],name,parent['id'] if parent else None,confirmed=True);print('Категория создана.')
        elif choice=='2':
            own={(r['bank'],r['account']) for r in state['references']['own_accounts']}
            pairs=sorted({(op['bank'],op['account_id']) for op in state['operations'].values()}-own)
            pair=choose(pairs,lambda p:f'{p[0]} — {p[1]}',zero='Отмена')
            if pair is None:continue
            owner=input('Ваше имя для справочника (можно Enter): ').strip();phone=input('Подтверждённый телефон (можно Enter): ').strip()
            print(f'Банк: {pair[0]}. Счёт: {pair[1]}.')
            if yes('Подтверждаете, что этот счёт принадлежит вам?'):
                service.add_account(database,state['token'],*pair,owner,phone,confirmed=True);print('Собственный счёт сохранён. Операции не переклассифицированы.')


def links_menu(database):
    state=service.snapshot(database);section('Подтверждение связей')
    print('1. Рассмотреть предложенную/существующую связь\n2. Связать перевод, покупку и возвраты вручную\n0. Назад')
    choice=input('Выберите действие: ').strip()
    if choice=='0':return
    group=None
    if choice=='1':
        group=choose(state['groups'],lambda g:f"{LINKS[g['link_type']]} | {STATUSES[g['status']]} | участников: {len(g['members'])}",zero='Назад')
        if group is None:return
    elif choice=='2':
        kind=choose(['OWN_TRANSFER','REFUND','CHAIN'],lambda k:{'OWN_TRANSFER':'Перевод между своими счетами','REFUND':'Покупка и возврат(ы)','CHAIN':'Цепочка с конечной покупкой'}[k],zero='Отмена')
        if kind is None:return
        members=[]
        print('ID операций можно посмотреть в пункте 3. Для завершения участников — Enter.')
        while True:
            identifier=input('Полный ID участника: ').strip()
            if not identifier:break
            if identifier not in state['operations']:raise EngineError('Операция не найдена.')
            op=state['operations'][identifier];show_operation(op,state)
            roles=[op['direction']] if kind=='OWN_TRANSFER' else (['PURCHASE'] if op['direction']=='OUT' else ['REFUND'])
            if kind=='CHAIN':roles=[op['direction']]+roles
            role=choose(roles,lambda r:{'OUT':'Техническое списание','IN':'Техническое пополнение','PURCHASE':'Конечная покупка','REFUND':'Возврат покупки'}[r],zero='Отмена')
            if role is None:return
            members.append((identifier,role))
        if len(members)<2:raise EngineError('Нужны минимум две различные операции.')
        group=service.new_link(kind,members,input('Комментарий: ').strip() or 'Связь подтверждена пользователем.')
    if group is None:return
    section('Состав связи')
    for identifier,role in group['members']:
        op=state['operations'][identifier];print(f"{role} | {identifier} | {op['operation_date']} | {op['bank']} | {format_kopecks(op['amount_kopecks'])} ₽\n{op['description']}")
    print('\n1. Подтвердить связь\n2. Отклонить связь\n0. Отмена')
    value=input('Выберите действие: ').strip()
    if value not in {'1','2'}:return
    group={**group,'status':'USER_CONFIRMED' if value=='1' else 'REJECTED'}
    if yes('Сохранить это решение о связи?'):
        service.link_change(database,state['token'],group,confirmed=True)
        print('Связь сохранена. Для изменения финансовых решений выполните пункт 6 → 2 с просмотром и подтверждением.')


def run(root,action):
    try:
        database=Path(root)/'database'/'finance.db'
        {'review':lambda:browse(database,True),'search':lambda:browse(database),
         'rules':lambda:rules_menu(database),'references':lambda:references(database),
         'links':lambda:links_menu(database)}[action]()
    except (EOFError,KeyboardInterrupt):print('\nДействие отменено. Уже подтверждённые решения сохранены.')
    except (DatabaseError,sqlite3.Error,OSError,ValueError,KeyError,TypeError) as error:
        print(f'\nДействие не выполнено: {error}')
