"""Experimental verified-prefix replay cache, not installed in native entry.

Every read still checks the binding and every stored document's byte signature.
Only a previously verified identical prefix can reuse a private state copy.
Fresh instances and observational event reads perform complete reducer replay.
"""
from copy import deepcopy
import hashlib
import json
from threading import RLock

from .config import digest
from .journal import Journal


class VerifiedReplayJournal(Journal):
    def __init__(self,*args,**kwargs):
        self._prefix_lock=RLock();self._verified=None
        super().__init__(*args,**kwargs)

    def _replay(self,db,events=None):
        if events is not None:return super()._replay(db,events)
        with self._prefix_lock:
            self._binding(db)
            rows=db.execute('SELECT sequence, document, hash FROM events ORDER BY sequence').fetchall()
            signature=tuple((n,hashlib.sha256(text.encode()).hexdigest(),checksum) for n,text,checksum in rows)
            cached=self._verified
            if cached is None or len(rows)<cached[2] or signature[:cached[2]]!=cached[3]:
                state,previous,count=super()._replay(db)
            else:
                state=deepcopy(cached[0]);previous=cached[1];count=cached[2]
                for sequence,document,checksum in rows[count:]:
                    count+=1;event=json.loads(document)
                    if (sequence!=count or event['sequence']!=count or event['previous']!=previous
                            or event['config']!=self.config.hash or digest(event)!=checksum):
                        raise ValueError('journal integrity failure')
                    self.reducer(state,event['kind'],event['data'],self.config);previous=checksum
            # Never share returned mutable state with the cache. In particular,
            # transact() may mutate it before a commit that later rolls back.
            self._verified=(deepcopy(state),previous,count,signature)
            return state,previous,count
