"""All SQL persistence for import attempts, source files and their observations."""
import json

from .connection import transaction
from .operations import Store, as_json
from program.processing.deduplication import match_occurrences


class ImportLedger:
    def __init__(self, connection, store=None):
        self.connection = connection
        self.store = store or Store(connection)

    def start(self, identifier, filename):
        with transaction(self.connection):
            self.connection.execute("INSERT INTO imports(id,filename,bank,status,archive_status) VALUES (?,?,?,'PENDING','NOT_REQUIRED')",
                                    (identifier,filename,'UNKNOWN'))

    def fail(self, identifier, sha256, error):
        with transaction(self.connection):
            self.connection.execute("UPDATE imports SET status='FAILED',sha256=?,error=?,archive_status='NOT_REQUIRED',"
                                    "new_count=0,repeated_count=0 WHERE id=?",(sha256,str(error),identifier))
            self.store._audit('IMPORT_FAILED','imports',identifier,None,{'error':str(error)})

    def source(self, sha256):
        row=self.connection.execute('SELECT * FROM source_files WHERE sha256=?',(sha256,)).fetchone()
        return dict(row) if row else None

    def attempt(self, identifier):
        return self.store.get('imports',identifier)

    def commit(self, identifier, sha256, bank, parser_name, parser_version, metadata, records, pending, known):
        with transaction(self.connection):
            if known:
                count=known['source_count']
                observations=self.connection.execute('SELECT * FROM import_observations WHERE import_id=? ORDER BY source_index',
                                                      (known['first_import_id'],)).fetchall()
                if len(observations)!=count:
                    raise ValueError('Нарушена история исходного файла.')
                for row in observations:
                    self.connection.execute('INSERT INTO import_observations VALUES (?,?,?,?,?,0)',
                        (identifier,row['source_index'],row['operation_id'],row['source_row'],row['observed_json']))
                new_count=0
                if pending:
                    self.connection.execute("UPDATE source_files SET pending_path=?,archive_state='PENDING',archive_error=NULL WHERE sha256=?",
                                            (pending,sha256))
            else:
                count=len(records)
                matches=match_occurrences(self.connection,records)
                new_count=0
                for index,(record,matched_id) in enumerate(zip(records,matches)):
                    is_new=matched_id is None
                    if is_new:
                        self.store._insert_operation(record)
                        matched_id=record['id']
                        new_count+=1
                    observed={key:value for key,value in record.items() if key not in {'id','import_id'}}
                    if isinstance(observed.get('source_data'),str):
                        observed['source_data']=json.loads(observed['source_data'])
                    self.connection.execute('INSERT INTO import_observations VALUES (?,?,?,?,?,?)',
                        (identifier,index,matched_id,record.get('source_row'),as_json(observed),int(is_new)))
                bank_folder = {'sber_credit_pdf': 'sberbank', 'sber_debit_pdf': 'sberbank', 'ozon_pdf': 'ozonbank', 'vtb_card_pdf': 'vtb', 'rshb_card_pdf': 'rshb', 'tbank_card_pdf': 'tbank'}[parser_name]
                relative=f"{metadata['period_start'][:4]}/{bank_folder}/{sha256}.pdf"
                self.connection.execute('INSERT INTO source_files VALUES (?,?,?,?,?,?,?,?)',
                    (sha256,identifier,count,relative,pending,'PENDING',None,as_json(metadata)))
            self.connection.execute("UPDATE imports SET bank=?,sha256=?,parser_name=?,parser_version=?,status='SUCCESS',"
                                    "source_count=?,new_count=?,repeated_count=?,archive_status='PENDING' WHERE id=?",
                                    (bank,sha256,parser_name,parser_version,count,new_count,count-new_count,identifier))
            self.store._audit('BANK_IMPORT','imports',identifier,None,
                             {'new':new_count,'repeated':count-new_count,'source':count,'sha256':sha256})
        return new_count,count

    def mark_archived(self, sha256):
        # Called while archive finalization holds the transaction/write lock.
        self.connection.execute("UPDATE source_files SET archive_state='ARCHIVED',archive_error=NULL WHERE sha256=?",(sha256,))
        self.connection.execute("UPDATE imports SET archive_status='ARCHIVED' WHERE sha256=? AND status='SUCCESS'",(sha256,))

    def mark_archive_error(self, sha256, error):
        with transaction(self.connection):
            self.connection.execute("UPDATE source_files SET archive_state='ERROR',archive_error=? WHERE sha256=?",(str(error),sha256))
            self.connection.execute("UPDATE imports SET archive_status='ERROR' WHERE sha256=? AND status='SUCCESS'",(sha256,))

    def interrupted(self):
        with transaction(self.connection):
            rows=self.connection.execute("SELECT id FROM imports WHERE status='PENDING' AND archive_status!='LEGACY'").fetchall()
            for row in rows:
                self.connection.execute("UPDATE imports SET status='FAILED',error='Прерванный импорт: финансовая транзакция не завершена',"
                                        "archive_status='NOT_REQUIRED' WHERE id=?",(row['id'],))
                self.store._audit('IMPORT_INTERRUPTED','imports',row['id'],None,{'status':'FAILED'})

    def pending_archives(self):
        return [row[0] for row in self.connection.execute("SELECT sha256 FROM source_files WHERE archive_state!='ARCHIVED'")]
