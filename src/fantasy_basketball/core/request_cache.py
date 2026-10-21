"""Bounded memoization candidate. Not installed in the native request path yet.

The byte budget measures weighted serialized retention, not resident process RAM.
Cached functions must be deterministic; callers retain responsibility for copies.
"""
from collections import OrderedDict,namedtuple
from functools import wraps
from threading import RLock

CacheInfo=namedtuple('CacheInfo','hits misses maxsize currsize retained_bytes max_bytes')


def bounded_cache(*,max_entries,max_bytes,weight):
    if type(max_entries) is not int or max_entries<1 or type(max_bytes) is not int or max_bytes<1 or not callable(weight):
        raise ValueError('positive explicit cache bounds required')
    def decorate(function):
        entries=OrderedDict();lock=RLock();hits=misses=retained=generation=0
        @wraps(function)
        def wrapped(*args,**kwargs):
            nonlocal hits,misses,retained
            key=(args,tuple(sorted(kwargs.items())),tuple(type(v) for v in args),tuple((k,type(v)) for k,v in sorted(kwargs.items())))
            with lock:
                if key in entries:
                    hits+=1;entries.move_to_end(key);return entries[key][0]
                misses+=1;observed=generation
            value=function(*args,**kwargs);size=weight(value)
            if type(size) is not int or size<0:raise ValueError('cache weight must be a nonnegative integer')
            with lock:
                if generation!=observed or size>max_bytes:return value
                if key in entries:return entries[key][0]
                while entries and (len(entries)>=max_entries or retained+size>max_bytes):
                    _,(_,removed)=entries.popitem(last=False);retained-=removed
                entries[key]=(value,size);retained+=size
            return value
        def clear():
            nonlocal hits,misses,retained,generation
            with lock:entries.clear();hits=misses=retained=0;generation+=1
        def info():
            with lock:return CacheInfo(hits,misses,max_entries,len(entries),retained,max_bytes)
        wrapped.cache_clear=clear;wrapped.cache_info=info
        return wrapped
    return decorate
