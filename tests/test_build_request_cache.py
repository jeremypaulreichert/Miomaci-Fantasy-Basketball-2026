"""Bounded cache controls and byte-identical native request construction."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import unittest
import test_build_core
from fantasy_basketball.core.request_cache import bounded_cache


class CacheTests(unittest.TestCase):
    def test_byte_budget_and_entry_limit_evict_without_changing_results(self):
        @bounded_cache(max_entries=2,max_bytes=5,weight=len)
        def value(n):return 'x'*n
        self.assertEqual(value(3),'xxx');self.assertEqual(value(3),'xxx')
        self.assertEqual(value.cache_info().hits,1)
        value(2);self.assertEqual(value.cache_info().retained_bytes,5)
        value(4);self.assertEqual(value.cache_info().currsize,1)
        self.assertEqual(value(10),'x'*10);self.assertEqual(value.cache_info().retained_bytes,4)
        value.cache_clear();self.assertEqual(value.cache_info().currsize,0)

    def test_typed_keys_and_failures_do_not_poison_cache(self):
        @bounded_cache(max_entries=5,max_bytes=100,weight=lambda v:1)
        def value(n):
            if n=='bad':raise ValueError('fixture')
            return type(n).__name__
        self.assertEqual(value(1),'int');self.assertEqual(value(True),'bool')
        with self.assertRaises(ValueError):value('bad')
        self.assertEqual(value.cache_info().currsize,2)

    def test_clear_during_miss_prevents_stale_repopulation(self):
        entered=Event();release=Event()
        @bounded_cache(max_entries=5,max_bytes=100,weight=lambda v:1)
        def value(n):entered.set();release.wait(5);return n
        with ThreadPoolExecutor(1) as pool:
            future=pool.submit(value,1);self.assertTrue(entered.wait(5))
            value.cache_clear();release.set();self.assertEqual(future.result(),1)
        self.assertEqual(value.cache_info().currsize,0)

    def test_parallel_same_key_retention_counts_once(self):
        @bounded_cache(max_entries=5,max_bytes=100,weight=lambda v:7)
        def value(n):return n
        with ThreadPoolExecutor(8) as pool:self.assertEqual(list(pool.map(value,[1]*20)),[1]*20)
        self.assertEqual(value.cache_info().currsize,1);self.assertEqual(value.cache_info().retained_bytes,7)

    def test_request_bytes_equal_old_constructor_and_150_second_pass_hits(self):
        from test_build_research import ten_profiles,board
        from fantasy_basketball.providers.research import _cached_request
        from fantasy_basketball.core.config import canonical
        constructor=_cached_request.__wrapped__
        cached=bounded_cache(max_entries=2048,max_bytes=192*1024*1024,
            weight=lambda wire:2*len(canonical(wire).encode()))(constructor)
        profile=ten_profiles()[0];current=board(profile.provider);args=[]
        for n in range(150):
            a=(profile,current,canonical([{'kind':'fixture','text':str(n)}]),canonical({'searches':2,'reads':2,'model_calls_including_this':9}),10000,20000,None)
            args.append(a);self.assertEqual(canonical(cached(*a)),canonical(constructor(*a)))
        for a in args:cached(*a)
        self.assertEqual(cached.cache_info().hits,150);self.assertEqual(cached.cache_info().misses,150)
        self.assertLessEqual(cached.cache_info().retained_bytes,192*1024*1024)
