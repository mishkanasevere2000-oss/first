"""Queries and writes for interactive review; financial policy lives in processing."""
from uuid import uuid4
from .operations import Store, require_text
from .references import name_key,validate_current_merge


class ReviewRepository:
    def __init__(self,connection):self.connection=connection

    def references(self):
        return {table:[dict(row) for row in self.connection.execute(f'SELECT * FROM {table} ORDER BY id')]
                for table in ('categories','own_accounts','projects')}

    def rules(self):
        return [dict(row) for row in self.connection.execute(
            'SELECT r.id,r.name,r.status,v.* FROM rules r JOIN rule_versions v ON r.id=v.rule_id ORDER BY r.id,v.version')]

    def history(self,identifier):
        return [dict(row) for row in self.connection.execute(
            'SELECT * FROM manual_overrides WHERE operation_id=? ORDER BY id',(identifier,))]

    def add_category(self,name,parent=None):
        name=require_text(name,'Название категории');identifier=uuid4().hex
        record=dict(id=identifier,name=name,parent_id=parent,status='ACTIVE')
        validate_current_merge(self.connection,'categories',{identifier:record})
        if parent:
            row=self.connection.execute('SELECT status,parent_id FROM categories WHERE id=?',(parent,)).fetchone()
            if not row or row['status']!='ACTIVE' or row['parent_id'] is not None:
                raise ValueError('Для подкатегории выберите активную корневую категорию.')
        self.connection.execute('INSERT INTO categories(id,name,name_key,parent_id,status) VALUES (?,?,?,?,?)',
                                (identifier,name,name_key(name),parent,'ACTIVE'))
        Store(self.connection)._audit('CREATE','categories',identifier,None,record)
        return identifier

    def add_account(self,bank,account,owner,phone):
        identifier=uuid4().hex
        row=dict(id=identifier,bank=bank,account=account,owner_name=owner or None,phone=phone or None,status='ACTIVE')
        validate_current_merge(self.connection,'own_accounts',{identifier:row})
        self.connection.execute('INSERT INTO own_accounts(id,bank,account,owner_name,phone,status) VALUES (?,?,?,?,?,?)',tuple(row.values()))
        Store(self.connection)._audit('CREATE','own_accounts',identifier,None,row)
        return identifier

    def has_operation_account(self,bank,account):
        return self.connection.execute('SELECT 1 FROM operations WHERE bank=? AND account_id=?',(bank,account)).fetchone() is not None

    def update_group(self,group,old):
        self.connection.execute('UPDATE operation_link_groups SET status=?,comment=? WHERE id=?',
                                (group['status'],group['comment'],group['id']))
        Store(self.connection)._audit('USER_LINK','operation_link_groups',group['id'],old,group)
