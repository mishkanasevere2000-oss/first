"""Explicit, validated, append-only JSON rule import for the stage-5 developer CLI."""
import json
from pathlib import Path

from program.database.connection import transaction
from program.database.migrations import validate_database,verified_backup
from program.database.operations import Store,as_json,require_text,require_integer
from program.database.financial import FinancialRepository
from .rules import EngineError,iso,validate_condition,validate_action


def prepare(connection,records):
    if not isinstance(records,list):raise EngineError('Файл правил должен содержать список.')
    repository=FinancialRepository(connection)
    categories,projects=repository.references()
    seen=set();normalized=[];versions={}
    for row in records:
        keys={'id','name','version','conditions','action','priority','effective_from','status'}
        if not isinstance(row,dict) or set(row)!=keys:raise EngineError('Неверные поля правила в файле.')
        identifier=require_text(row['id'],'id');name=require_text(row['name'],'name')
        version=require_integer(row['version'],'version',minimum=1);require_integer(row['priority'],'priority')
        iso(row['effective_from']);validate_condition(row['conditions'])
        validate_action(row['action'],row['conditions'],categories,projects)
        if row['status'] not in {'ACTIVE','DISABLED'}:raise EngineError('Неверный статус правила.')
        if (identifier,version) in seen:raise EngineError('Повтор версии в файле.')
        seen.add((identifier,version))
        current=repository.rule(identifier)
        if current and current['name']!=name:raise EngineError('Существующее имя правила не меняется импортом версии.')
        if any(r['id']==identifier and (r['name']!=name or r['status']!=row['status']) for r in normalized):
            raise EngineError('Версии одного правила должны иметь одно имя и статус.')
        existing=repository.rule_version(identifier,version)
        if existing:
            same=(json.loads(existing['conditions_json'])==row['conditions'] and json.loads(existing['action_json'])==row['action']
                  and existing['priority']==row['priority'] and existing['effective_from']==row['effective_from'])
            if not same:raise EngineError('Версия правила неизменяема; создайте новую версию.')
        normalized.append({**row,'id':identifier,'name':name,'new_version':existing is None,
                           'new_rule':current is None,'status_changed':current is None or current['status']!=row['status']})
    for row in sorted(normalized,key=lambda r:(r['id'],r['version'])):
        if not row['new_version']:continue
        identifier=row['id']
        latest=versions.get(identifier)
        if latest is None:latest=repository.latest_rule_version(identifier)
        if row['version']!=latest+1:raise EngineError('Новая версия должна следовать за предыдущей без пропуска.')
        versions[identifier]=row['version']
    return normalized


def install(connection,records,*,confirmed=False,backup_dir):
    if confirmed is not True:raise EngineError('Активация/изменение правил требует отдельного подтверждения.')
    store=Store(connection);prepared=prepare(connection,records)
    if not any(row['new_version'] or row['status_changed'] for row in prepared):return 0
    # Even an initial mass activation has a verified rollback point.
    verified_backup(connection,backup_dir,validate_database)
    changed=0
    with transaction(connection):
        prepared=prepare(connection,records)
        repository=FinancialRepository(connection)
        for row in sorted(prepared,key=lambda r:(r['id'],r['version'])):
            changed+=repository.install_rule(row,store)
    return changed
