"""Storage operations only: no parser, matching or financial rule execution."""
from datetime import date
import json

from .connection import DatabaseError, transaction
from .migrations import validate_database


def as_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def require_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{field}: требуется непустой текст.')
    return value.strip()


def require_integer(value, field, *, minimum=None):
    if type(value) is not int or (minimum is not None and value < minimum):
        raise ValueError(f'{field}: требуется целое число' + (f' не меньше {minimum}.' if minimum is not None else '.'))
    return value


def require_date(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError('Дата должна быть в формате YYYY-MM-DD.')
    return value


class Store:
    def __init__(self, connection):
        validate_database(connection)
        self.connection = connection

    def get(self, entity, identifier):
        keys = {'imports': 'id', 'operations': 'id', 'categories': 'id',
                'own_accounts': 'id', 'projects': 'id', 'rules': 'id',
                'operation_link_groups': 'id', 'classifications': 'operation_id'}
        if entity not in keys:
            raise ValueError('Неизвестная сущность.')
        row = self.connection.execute(f'SELECT * FROM {entity} WHERE {keys[entity]}=?', (identifier,)).fetchone()
        return dict(row) if row else None

    def _audit(self, event, entity, identifier, before, after):
        self.connection.execute(
            'INSERT INTO audit_log(event,entity,entity_id,before_json,after_json) VALUES (?,?,?,?,?)',
            (event, entity, str(identifier), as_json(before) if before is not None else None,
             as_json(after) if after is not None else None),
        )

    def create_import(self, identifier, filename, bank):
        values = [require_text(value, field) for value, field in
                  [(identifier, 'id'), (filename, 'filename'), (bank, 'bank')]]
        with transaction(self.connection):
            self.connection.execute('INSERT INTO imports(id,filename,bank,status) VALUES (?,?,?,?)', (*values, 'PENDING'))
            self._audit('CREATE', 'imports', identifier, None, self.get('imports', identifier))

    def set_import_status(self, identifier, status):
        if status not in {'PENDING', 'SUCCESS', 'FAILED'}:
            raise ValueError('Неизвестный статус загрузки.')
        self._set_status('imports', identifier, status)

    def _set_status(self, entity, identifier, status):
        with transaction(self.connection):
            before = self.get(entity, identifier)
            if before is None:
                raise ValueError('Запись не найдена.')
            self.connection.execute(f'UPDATE {entity} SET status=? WHERE id=?', (status, identifier))
            self._audit('STATUS', entity, identifier, before, self.get(entity, identifier))

    def set_status(self, entity, identifier, status):
        allowed = {'rules': {'ACTIVE', 'DISABLED'}, 'projects': {'ACTIVE', 'ARCHIVED'},
                   'categories': {'ACTIVE', 'ARCHIVED'}, 'own_accounts': {'ACTIVE', 'ARCHIVED'},
                   'operation_link_groups': {'PROPOSED', 'AUTO_CONFIRMED', 'USER_CONFIRMED', 'REJECTED'}}
        if entity not in allowed or status not in allowed[entity]:
            raise ValueError('Недопустимый статус.')
        self._set_status(entity, identifier, status)

    def _validated_operation(self, *, id, import_id, bank, account_id, operation_date,
                      description, amount_kopecks, direction, **optional):
        allowed = {'processing_date', 'mcc', 'counterparty', 'bank_operation_type',
                   'balance_after_kopecks', 'source_row', 'source_data', 'bank_transaction_id'}
        if optional.keys() - allowed:
            raise ValueError('Неизвестные поля банковской операции.')
        require_integer(amount_kopecks, 'amount_kopecks', minimum=0)
        if optional.get('balance_after_kopecks') is not None:
            require_integer(optional['balance_after_kopecks'], 'balance_after_kopecks')
        require_date(operation_date)
        if optional.get('processing_date') is not None:
            require_date(optional['processing_date'])
        if direction not in {'IN', 'OUT'}:
            raise ValueError('Направление должно быть IN или OUT.')
        values = dict(id=require_text(id, 'id'), import_id=require_text(import_id, 'import_id'),
                      bank=require_text(bank, 'bank'), account_id=require_text(account_id, 'account_id'),
                      operation_date=operation_date, description=require_text(description, 'description'),
                      amount_kopecks=amount_kopecks, direction=direction, **optional)
        if 'source_data' in values and values['source_data'] is not None:
            values['source_data'] = as_json(values['source_data'])
        return values

    def _insert_operation(self, values):
        self.connection.execute(
            f"INSERT INTO operations ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))
        from program.processing.deduplication import attach_identity
        attach_identity(self.connection, values)
        self._audit('CREATE', 'operations', values['id'], None, self.get('operations', values['id']))

    def add_operation(self, **data):
        values = self._validated_operation(**data)
        with transaction(self.connection):
            self._insert_operation(values)

    def create_project(self, identifier, name, start_date=None, end_date=None):
        for value in (start_date, end_date):
            if value is not None:
                require_date(value)
        with transaction(self.connection):
            self.connection.execute('INSERT INTO projects(id,name,start_date,end_date) VALUES (?,?,?,?)',
                                    (require_text(identifier, 'id'), require_text(name, 'name'), start_date, end_date))
            self._audit('CREATE', 'projects', identifier, None, self.get('projects', identifier))

    def create_rule(self, identifier, name):
        with transaction(self.connection):
            self.connection.execute('INSERT INTO rules(id,name) VALUES (?,?)',
                                    (require_text(identifier, 'id'), require_text(name, 'name')))
            self._audit('CREATE', 'rules', identifier, None, self.get('rules', identifier))

    def add_rule_version(self, identifier, version, conditions, action, priority, effective_from):
        require_integer(version, 'version', minimum=1)
        require_integer(priority, 'priority')
        require_date(effective_from)
        if not isinstance(conditions, dict) or not isinstance(action, dict):
            raise ValueError('Условия и действие должны быть структурированными объектами.')
        with transaction(self.connection):
            self.connection.execute(
                'INSERT INTO rule_versions(rule_id,version,conditions_json,action_json,priority,effective_from) VALUES (?,?,?,?,?,?)',
                (identifier, version, as_json(conditions), as_json(action), priority, effective_from))
            self._audit('VERSION', 'rules', identifier, None, {'version': version, 'conditions': conditions, 'action': action})

    def create_link_group(self, identifier, link_type, status, members, comment=''):
        """Persist an explicitly supplied group; never infer its financial meaning."""
        if len(members) < 2 or len({item[0] for item in members}) != len(members):
            raise ValueError('Нужны хотя бы два различных участника группы.')
        with transaction(self.connection):
            self.connection.execute('INSERT INTO operation_link_groups(id,link_type,status,comment) VALUES (?,?,?,?)',
                                    (require_text(identifier, 'id'), link_type, status, comment))
            self.connection.executemany('INSERT INTO operation_link_members VALUES (?,?,?)',
                                        [(identifier, operation, require_text(role, 'role')) for operation, role in members])
            self._audit('CREATE', 'operation_link_groups', identifier, None, {'members': members, 'type': link_type, 'status': status})

    def _decision(self, operation_type, include_in_report, category_id, subcategory_id, project_id, comment):
        if operation_type not in {'expense', 'income', 'technical', 'refund', 'undefined'}:
            raise ValueError('Неизвестный тип решения.')
        if type(include_in_report) is not bool:
            raise ValueError('include_in_report должен быть bool.')
        if operation_type in {'technical', 'undefined'} and include_in_report:
            raise ValueError('Технические и неопределённые операции не включаются в итоги.')
        if subcategory_id:
            category = self.get('categories', subcategory_id)
            if not category or not category_id or category['parent_id'] != category_id:
                raise ValueError('Подкатегория не принадлежит указанной категории.')
        return dict(operation_type=operation_type, include_in_report=int(include_in_report),
                    category_id=category_id, subcategory_id=subcategory_id, project_id=project_id, comment=comment)

    def _save_classification(self, operation_id, values):
        values = dict(operation_id=operation_id, **values)
        columns = ','.join(values)
        updates = ','.join(f'{key}=excluded.{key}' for key in values if key != 'operation_id')
        self.connection.execute(
            f"INSERT INTO classifications({columns}) VALUES ({','.join('?' for _ in values)}) "
            f'ON CONFLICT(operation_id) DO UPDATE SET {updates},updated_at=CURRENT_TIMESTAMP', tuple(values.values()))

    def set_classification(self, operation_id, *, source, operation_type, include_in_report,
                           category_id=None, subcategory_id=None, project_id=None,
                           rule_id=None, rule_version=None, related_group_id=None, comment=''):
        if source not in {'RULE', 'LINK', 'UNDEFINED'}:
            raise ValueError('Для ручного решения используйте record_manual_decision.')
        with transaction(self.connection):
            before = self.get('classifications', operation_id)
            if before and before['source'] == 'MANUAL':
                raise DatabaseError('Ручное решение защищено от автоматической перезаписи.')
            if source == 'LINK':
                row = self.connection.execute(
                    'SELECT g.status FROM operation_link_groups g JOIN operation_link_members m ON m.group_id=g.id '
                    'WHERE g.id=? AND m.operation_id=?', (related_group_id, operation_id)).fetchone()
                if row is None or row[0] not in {'AUTO_CONFIRMED', 'USER_CONFIRMED'}:
                    raise ValueError('Нужна подтверждённая группа с этой операцией.')
            if rule_version is not None:
                require_integer(rule_version, 'rule_version', minimum=1)
            values = self._decision(operation_type, include_in_report, category_id, subcategory_id, project_id, comment)
            values.update(source=source, rule_id=rule_id, rule_version=rule_version,
                          related_group_id=related_group_id, manual_override_id=None)
            self._save_classification(operation_id, values)
            self._audit('CLASSIFY', 'classifications', operation_id, before, self.get('classifications', operation_id))

    def record_manual_decision(self, operation_id, *, operation_type, include_in_report,
                               category_id=None, subcategory_id=None, project_id=None, comment):
        require_text(comment, 'comment')
        with transaction(self.connection):
            self._record_manual_decision(operation_id, operation_type=operation_type, include_in_report=include_in_report,
                category_id=category_id, subcategory_id=subcategory_id, project_id=project_id, comment=comment)

    def _record_manual_decision(self, operation_id, *, operation_type, include_in_report,
                                category_id=None, subcategory_id=None, project_id=None, comment):
        require_text(comment, 'comment')
        before = self.get('classifications', operation_id)
        previous = self.connection.execute('SELECT id FROM manual_overrides WHERE operation_id=? ORDER BY id DESC LIMIT 1',
                                           (operation_id,)).fetchone()
        values = self._decision(operation_type, include_in_report, category_id, subcategory_id, project_id, comment)
        cursor = self.connection.execute(
            f"INSERT INTO manual_overrides(operation_id,previous_id,{','.join(values)}) "
            f"VALUES ({','.join('?' for _ in range(len(values)+2))})", (operation_id, previous[0] if previous else None, *values.values()))
        values.update(source='MANUAL', manual_override_id=cursor.lastrowid,
                      related_group_id=None, rule_id=None, rule_version=None)
        self._save_classification(operation_id, values)
        self._audit('MANUAL_DECISION', 'classifications', operation_id, before, self.get('classifications', operation_id))
