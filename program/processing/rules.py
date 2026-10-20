"""Strict data-only rules: no eval, bank keywords, or personal category defaults."""
from dataclasses import dataclass
from datetime import date
import json
import unicodedata
from program.database.financial import FinancialRepository


class EngineError(ValueError):
    pass


TEXT_FIELDS={'bank','account_id','card_number_mask','description','mcc','counterparty','phone',
             'bank_operation_type','link_type'}
DATE_FIELDS={'operation_date','processing_date'}
FIELDS=TEXT_FIELDS|DATE_FIELDS|{'amount_kopecks','direction','has_link'}
OPS={'eq','ne','contains','not_contains','starts_with','ends_with','gt','lt','between','date_between'}
TYPES={'expense','income','technical','refund','undefined'}


def iso(value):
    if not isinstance(value,str):raise EngineError('Ожидается дата YYYY-MM-DD.')
    try:valid=date.fromisoformat(value).isoformat()==value
    except ValueError:valid=False
    if not valid:raise EngineError('Ожидается дата YYYY-MM-DD.')
    return value


def validate_condition(node,depth=0):
    if not isinstance(node,dict) or depth>8:raise EngineError('Неверное дерево условий.')
    if set(node) in ({'and'},{'or'}):
        children=next(iter(node.values()))
        if not isinstance(children,list):raise EngineError('AND/OR требует список условий.')
        if not children and 'or' in node:raise EngineError('Пустой OR не допускается.')
        for child in children:validate_condition(child,depth+1)
        return
    if set(node)-{'field','op','value','case_sensitive'} or not {'field','op','value'}<=set(node):
        raise EngineError('Неизвестные поля условия.')
    field,op,value=node['field'],node['op'],node['value']
    if field not in FIELDS or op not in OPS:raise EngineError('Неизвестный признак или оператор правила.')
    if type(node.get('case_sensitive',False)) is not bool:raise EngineError('case_sensitive должен быть bool.')
    if value is None and op in {'eq','ne'}:return
    if field=='amount_kopecks':
        if op not in {'eq','ne','gt','lt','between'}:raise EngineError('Неверный оператор суммы.')
        values=value if op=='between' else [value]
        if not isinstance(values,list) or (op=='between' and len(values)!=2) or any(type(v) is not int or v<0 for v in values):
            raise EngineError('Сумма задаётся целыми неотрицательными копейками.')
        if op=='between' and values[0]>values[1]:raise EngineError('Неверные границы суммы.')
    elif field in DATE_FIELDS:
        if op not in {'eq','ne','between','date_between'}:raise EngineError('Неверный оператор даты.')
        values=value if op in {'between','date_between'} else [value]
        if not isinstance(values,list) or len(values)!=(2 if op in {'between','date_between'} else 1):
            raise EngineError('Неверные границы дат.')
        for item in values:iso(item)
        if len(values)==2 and values[0]>values[1]:raise EngineError('Неверные границы дат.')
    elif field=='has_link':
        if op not in {'eq','ne'} or type(value) is not bool:raise EngineError('has_link требует bool и eq/ne.')
    else:
        if op not in {'eq','ne','contains','not_contains','starts_with','ends_with'} or not isinstance(value,str):
            raise EngineError('Текстовое условие требует строку и текстовый оператор.')
        if field=='direction' and (op not in {'eq','ne'} or value not in {'IN','OUT'}):
            raise EngineError('Направление должно быть IN/OUT.')


def condition_fields(node):
    if 'and' in node or 'or' in node:
        return set().union(*(condition_fields(c) for c in next(iter(node.values()))))
    return {node['field']}



def requires_semantic_fact(node):
    if 'and' in node:return any(requires_semantic_fact(child) for child in node['and'])
    if 'or' in node:return all(requires_semantic_fact(child) for child in node['or'])
    return node['field'] not in {'operation_date','processing_date','amount_kopecks','direction'}


def normalized(value,sensitive):
    result=unicodedata.normalize('NFKC',value)
    return result if sensitive else result.casefold()


def matches(node,operation,context):
    if 'and' in node:return all(matches(child,operation,context) for child in node['and'])
    if 'or' in node:return any(matches(child,operation,context) for child in node['or'])
    field,op,expected=node['field'],node['op'],node['value']
    data=operation.get('source_data') or {}
    actual=context.get(field) if field in {'has_link','link_type'} else (
        data.get(field) if field in {'phone','card_number_mask'} else operation.get(field))
    if expected is None:return actual is None if op=='eq' else actual is not None
    if actual is None:return False  # Missing facts do not satisfy negative predicates.
    if isinstance(actual,str) and isinstance(expected,str):
        actual=normalized(actual,node.get('case_sensitive',False));expected=normalized(expected,node.get('case_sensitive',False))
    if op=='eq':return type(actual) is type(expected) and actual==expected
    if op=='ne':return type(actual) is type(expected) and actual!=expected
    if op in {'between','date_between'}:return expected[0]<=actual<=expected[1]
    if op=='gt':return actual>expected
    if op=='lt':return actual<expected
    if op=='contains':return expected in actual
    if op=='not_contains':return expected not in actual
    if op=='starts_with':return actual.startswith(expected)
    return actual.endswith(expected)


def validate_action(raw,conditions,categories,projects):
    allowed={'type','operation_type','include_in_report','category_id','subcategory_id','project_id','comment','original_operation_id'}
    if not isinstance(raw,dict) or set(raw)-allowed or ('type' in raw and 'operation_type' in raw):
        raise EngineError('Неизвестные или противоречивые поля действия.')
    action=dict(raw);kind=action.pop('type',action.get('operation_type'))
    if kind not in TYPES:raise EngineError('Неизвестный тип действия.')
    action['operation_type']=kind
    include=action.setdefault('include_in_report',kind in {'expense','income','refund'})
    if type(include) is not bool or (kind in {'technical','undefined'} and include):
        raise EngineError('Неверный признак включения в отчёт.')
    for key in ('category_id','subcategory_id','project_id','original_operation_id'):
        value=action.setdefault(key,None)
        if value is not None and (not isinstance(value,str) or not value.strip()):raise EngineError(f'Неверное поле {key}.')
    if not isinstance(action.setdefault('comment',''),str):raise EngineError('Комментарий должен быть строкой.')
    category,sub=action['category_id'],action['subcategory_id']
    for identifier in (category,sub):
        if identifier and (identifier not in categories or categories[identifier]['status']!='ACTIVE'):
            raise EngineError('Категория правила отсутствует или архивирована.')
    if sub and (not category or categories[sub]['parent_id']!=category):raise EngineError('Подкатегория не принадлежит категории.')
    project=action['project_id']
    if project and (project not in projects or projects[project]['status']!='ACTIVE'):raise EngineError('Проект отсутствует или архивирован.')
    if kind in {'technical','undefined'} and (category or sub):raise EngineError('Для этого типа категория не назначается.')
    if action['original_operation_id'] and kind!='refund':raise EngineError('Исходная покупка допустима только для возврата.')
    if kind=='technical' and not requires_semantic_fact(conditions):
        raise EngineError('Дата и сумма не доказывают технический характер операции.')
    return action


@dataclass(frozen=True)
class Rule:
    id:str
    version:int
    priority:int
    conditions:dict
    action:dict


def load_rules(connection,as_of):
    repository=FinancialRepository(connection)
    categories,projects=repository.references()
    result=[]
    for row in repository.active_rule_versions(as_of):
        try:
            conditions=json.loads(row['conditions_json']);validate_condition(conditions)
            action=validate_action(json.loads(row['action_json']),conditions,categories,projects)
            result.append(Rule(row['rule_id'],row['version'],row['priority'],conditions,action))
        except (ValueError,TypeError,KeyError) as error:raise EngineError(f"Правило {row['rule_id']} v{row['version']}: {error}") from error
    return result


def choose_rule(rules,operation,context):
    applicable=[rule for rule in rules if matches(rule.conditions,operation,context)]
    if not applicable:return None,[]
    priority=max(rule.priority for rule in applicable)
    top=[rule for rule in applicable if rule.priority==priority]
    # Comments do not change financial meaning. Equal results have a stable provenance.
    actions={json.dumps({k:v for k,v in rule.action.items() if k!='comment'},sort_keys=True) for rule in top}
    return (None,top) if len(actions)>1 else (top[0],[])
