"""SQL persistence for financial projections; analytical policy stays in processing/."""
from .operations import Store,as_json

TABLE_KEYS={'operations':'id','classifications':'operation_id','manual_overrides':'id','rules':'id',
            'rule_versions':'rule_id,version','categories':'id','own_accounts':'id','projects':'id',
            'operation_link_groups':'id','operation_link_members':'group_id,operation_id'}


class FinancialRepository:
    def __init__(self,connection):self.connection=connection

    def state(self):
        return {table:[dict(row) for row in self.connection.execute(f'SELECT * FROM {table} ORDER BY {keys}')]
                for table,keys in TABLE_KEYS.items()}

    def accounts(self):return [dict(row) for row in self.connection.execute('SELECT * FROM own_accounts ORDER BY id')]

    def references(self):
        return ({r['id']:dict(r) for r in self.connection.execute('SELECT * FROM categories')},
                {r['id']:dict(r) for r in self.connection.execute('SELECT * FROM projects')})

    def has_unfinished_operations(self):
        return self.connection.execute("SELECT 1 FROM operations o JOIN imports i ON i.id=o.import_id WHERE i.status!='SUCCESS' LIMIT 1").fetchone() is not None

    def rule(self,identifier):
        row=self.connection.execute('SELECT * FROM rules WHERE id=?',(identifier,)).fetchone()
        return dict(row) if row else None

    def rule_version(self,identifier,version):
        row=self.connection.execute('SELECT * FROM rule_versions WHERE rule_id=? AND version=?',(identifier,version)).fetchone()
        return dict(row) if row else None

    def latest_rule_version(self,identifier):
        return self.connection.execute('SELECT coalesce(max(version),0) FROM rule_versions WHERE rule_id=?',(identifier,)).fetchone()[0]

    def active_rule_versions(self,as_of):
        return [dict(row) for row in self.connection.execute("SELECT v.* FROM rule_versions v JOIN rules r ON r.id=v.rule_id WHERE r.status='ACTIVE' AND v.effective_from<=? AND v.version=(SELECT max(x.version) FROM rule_versions x WHERE x.rule_id=v.rule_id AND x.effective_from<=?) ORDER BY v.rule_id",(as_of,as_of))]

    def data(self):
        return ([dict(row) for row in self.connection.execute('SELECT * FROM operations ORDER BY id')],
                [dict(row) for row in self.connection.execute('SELECT * FROM classifications ORDER BY operation_id')],
                [dict(row) for row in self.connection.execute('SELECT * FROM manual_overrides ORDER BY id')],
                self.groups())

    def groups(self):
        result=[]
        for row in self.connection.execute('SELECT * FROM operation_link_groups ORDER BY id'):
            group=dict(row)
            group['members']=[tuple(member) for member in self.connection.execute(
                'SELECT operation_id,role FROM operation_link_members WHERE group_id=? ORDER BY operation_id',(row['id'],))]
            result.append(group)
        return result

    def insert_group(self,group,store):
        self.connection.execute('INSERT INTO operation_link_groups(id,link_type,status,comment) VALUES (?,?,?,?)',
                                (group['id'],group['link_type'],group['status'],group['comment']))
        self.connection.executemany('INSERT INTO operation_link_members VALUES (?,?,?)',
                                    [(group['id'],identifier,role) for identifier,role in group['members']])
        store._audit('FINANCIAL_LINK','operation_link_groups',group['id'],None,group)

    def save_decision(self,identifier,value,store):
        before=store.get('classifications',identifier)
        store._save_classification(identifier,value)
        store._audit('FINANCIAL_CLASSIFY','classifications',identifier,before,store.get('classifications',identifier))

    def install_rule(self,row,store):
        changed=0;identifier=row['id'];current=store.get('rules',identifier)
        if current is None:
            self.connection.execute('INSERT INTO rules(id,name,status) VALUES (?,?,?)',(identifier,row['name'],row['status']))
            store._audit('RULE_CREATE','rules',identifier,None,store.get('rules',identifier));changed+=1
        elif current['status']!=row['status']:
            self.connection.execute('UPDATE rules SET status=? WHERE id=?',(row['status'],identifier))
            store._audit('RULE_STATUS','rules',identifier,current,store.get('rules',identifier));changed+=1
        if row['new_version']:
            self.connection.execute('INSERT INTO rule_versions(rule_id,version,conditions_json,action_json,priority,effective_from) VALUES (?,?,?,?,?,?)',
                (identifier,row['version'],as_json(row['conditions']),as_json(row['action']),row['priority'],row['effective_from']))
            store._audit('RULE_VERSION','rule_versions',f"{identifier}/{row['version']}",None,
                         {k:row[k] for k in ('conditions','action','priority','effective_from')});changed+=1
        return changed
