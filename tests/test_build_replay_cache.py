"""State equivalence, tamper detection, rollback and concurrent observers."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import sqlite3
import test_build_core as support
from fantasy_basketball.core.config import canonical,digest
from fantasy_basketball.core.journal import Journal
from fantasy_basketball.core.replay_cache import VerifiedReplayJournal


def initial():return {'values':[]}


def reduce(state,kind,data,config):
    if kind!='ADD' or type(data.get('n')) is not int:raise ValueError('bad fixture event')
    state['values'].append(data['n'])


class ReplayCacheTests(support.BuildCase):
    def journal(self,cls=VerifiedReplayJournal):
        return cls(self.directory,self.config,reduce,initial,schema='cache-fixture/1')

    def test_full_and_cached_snapshots_equal_and_returned_mutation_isolated(self):
        cached=self.journal()
        for n in range(20):
            cached.transact(lambda s:[('ADD',{'n':n})])
            self.assertEqual(cached.read(),self.journal(Journal).read())
        value=cached.read();value['values'].append(999)
        self.assertNotIn(999,cached.read()['values'])
        self.assertEqual(cached.read_with_events()[0],cached.read())

    def test_external_append_and_two_concurrent_writers_are_observed(self):
        a=self.journal();b=self.journal()
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda pair:pair[0].transact(lambda s:[('ADD',{'n':pair[1]})]),[(a,1),(b,2)]))
        self.assertEqual(sorted(a.read()['values']),[1,2]);self.assertEqual(a.read(),b.read())

    def test_rollback_does_not_cache_uncommitted_state(self):
        cached=self.journal();cached.transact(lambda s:[('ADD',{'n':1})])
        def fail(boundary):raise RuntimeError('fixture rollback')
        cached.fault=fail
        with self.assertRaises(RuntimeError):cached.transact(lambda s:[('ADD',{'n':2})])
        cached.fault=None
        self.assertEqual(cached.read(),{'values':[1]})

    def test_mutated_cached_document_hash_and_binding_are_rechecked(self):
        cached=self.journal();cached.transact(lambda s:[('ADD',{'n':1})]);cached.read()
        with closing(sqlite3.connect(cached.path)) as db, db:
            db.execute('DROP TRIGGER immutable_events_UPDATE')
            text=db.execute('select document from events').fetchone()[0];event=json.loads(text)
            event['data']['n']=2
            db.execute('update events set document=?',(canonical(event),))
        with self.assertRaises(ValueError):cached.read()
        with closing(sqlite3.connect(cached.path)) as db, db:
            db.execute('update events set document=?',(text,));db.execute('DROP TRIGGER immutable_binding_UPDATE')
            db.execute('update binding set value=?',('foreign',))
        with self.assertRaises(ValueError):cached.read()

    def test_whitespace_change_revalidates_and_invalid_new_event_rejects(self):
        cached=self.journal();cached.transact(lambda s:[('ADD',{'n':1})]);cached.read()
        with closing(sqlite3.connect(cached.path)) as db, db:
            db.execute('DROP TRIGGER immutable_events_UPDATE')
            text=db.execute('select document from events').fetchone()[0]
            db.execute('update events set document=?',(json.dumps(json.loads(text),indent=2),))
        self.assertEqual(cached.read(),{'values':[1]})
        with closing(sqlite3.connect(cached.path)) as db, db:
            event={'sequence':2,'previous':'0'*64,'config':self.config.hash,'kind':'ADD','data':{'n':2}}
            db.execute('insert into events values (?,?,?)',(2,canonical(event),digest(event)))
        with self.assertRaises(ValueError):cached.read()
