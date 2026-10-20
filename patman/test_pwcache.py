# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Tests for the cache of responses from patchwork"""

import asyncio
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from u_boot_pylib import terminal
from patman import pwcache
from patman.patchwork import Patchwork


def run(coro):
    """Run a coroutine in a loop of its own

    asyncio.run() leaves the thread with no event loop, which breaks later
    tests in the same process that expect one

    Args:
        coro (coroutine): Coroutine to run

    Returns:
        Its result
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestPwCache(unittest.TestCase):
    """Test the patchwork cache"""
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='patman.')
        self.path = os.path.join(self.tmpdir, 'cache', 'patchwork.db')
        self.now = 1000.0

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def cache(self):
        """Open the cache, with a clock which the test controls"""
        return pwcache.Cache(self.path, clock=lambda: self.now)

    def test_get_put(self):
        """Responses are kept until they are too old for the caller"""
        cache = self.cache()
        self.assertIsNone(cache.get('https://pw', 'patches/1/', 60))
        cache.put('https://pw', 'patches/1/', {'id': 1, 'state': 'new'})
        self.assertEqual({'id': 1, 'state': 'new'},
                         cache.get('https://pw', 'patches/1/', 60))

        # Each server has its own entries
        self.assertIsNone(cache.get('https://other', 'patches/1/', 60))

        # A response older than the caller accepts is not used
        self.now += 61
        self.assertIsNone(cache.get('https://pw', 'patches/1/', 60))
        self.assertEqual({'id': 1, 'state': 'new'},
                         cache.get('https://pw', 'patches/1/', None))

        # It is kept when the cache is opened again
        cache.close()
        self.assertEqual({'id': 1, 'state': 'new'},
                         self.cache().get('https://pw', 'patches/1/', None))

    def test_prune(self):
        """Entries which are not used for a while are dropped"""
        cache = self.cache()
        cache.put('https://pw', 'patches/1/', {'id': 1})
        cache.put('https://pw', 'patches/2/', {'id': 2})
        self.now += 10 * 86400
        cache.get('https://pw', 'patches/2/', None)
        self.assertEqual(1, cache.prune(keep_days=5))
        self.assertIsNone(cache.get('https://pw', 'patches/1/', None))
        self.assertEqual({'id': 2}, cache.get('https://pw', 'patches/2/', None))

    def test_layout(self):
        """A cache with an old layout is thrown away"""
        cache = self.cache()
        cache.put('https://pw', 'patches/1/', {'id': 1})
        cache.close()
        with mock.patch.object(pwcache, 'LAYOUT', pwcache.LAYOUT + 1):
            self.assertIsNone(
                self.cache().get('https://pw', 'patches/1/', None))

    def test_broken(self):
        """A cache which fails acts as an empty one, after saying so once"""
        cache = self.cache()
        cache.put('https://pw', 'patches/1/', {'id': 1})

        # An entry which cannot be decoded
        with cache.con:
            cache.con.execute("UPDATE objects SET data = x'00'")
        with terminal.capture() as (_, err):
            self.assertIsNone(cache.get('https://pw', 'patches/1/', None))
        self.assertIn('Cannot use the patchwork cache', err.getvalue())
        self.assertTrue(cache.broken)

        # SQLite failing, e.g. with a full disk
        cache = self.cache()
        cache.con = mock.MagicMock()
        cache.con.execute.side_effect = sqlite3.OperationalError('disk full')
        with terminal.capture() as (_, err):
            cache.put('https://pw', 'patches/2/', {'id': 2})
            self.assertIsNone(cache.get('https://pw', 'patches/2/', None))
            cache.add_hit('https://pw')
            self.assertEqual({}, cache.hits_since(''))
            self.assertEqual(0, cache.prune())
        self.assertEqual(1, err.getvalue().count('disk full'))
        self.assertEqual(1, cache.con.execute.call_count)

    def test_open_fails(self):
        """A cache which cannot be opened is not used, after saying so"""
        with mock.patch.object(pwcache, '_cache', None), \
                mock.patch.dict(os.environ, {'PATMAN_PWCACHE': self.path}), \
                mock.patch.object(pwcache, 'Cache', side_effect=
                                  sqlite3.OperationalError('read-only')), \
                terminal.capture() as (_, err):
            self.assertIsNone(pwcache.get())
            self.assertIsNone(pwcache.get())
        self.assertEqual(1, err.getvalue().count('Cannot open the patchwork '
                                                 'cache'))

    def test_default_path(self):
        """The cache goes in the user's cache directory, unless set"""
        with mock.patch.dict(os.environ, {'HOME': '/home/fred'}, clear=True):
            self.assertEqual('/home/fred/.cache/patman/patchwork.db',
                             pwcache.default_path())
        with mock.patch.dict(os.environ, {'XDG_CACHE_HOME': '/c'}, clear=True):
            self.assertEqual('/c/patman/patchwork.db', pwcache.default_path())
        with mock.patch.dict(os.environ, {'PATMAN_PWCACHE': '/x/pw.db'}):
            self.assertEqual('/x/pw.db', pwcache.default_path())
        with mock.patch.dict(os.environ, {'PATMAN_PWCACHE': 'off'}):
            self.assertIsNone(pwcache.default_path())

    def test_get_patch(self):
        """A patch is read from patchwork once, then from the cache"""
        requests = []

        def fake(subpath):
            requests.append(subpath)
            return {'id': 5, 'state': f'state{len(requests)}'}

        pwork = Patchwork.for_testing(fake)
        pwork.cache = self.cache()

        def get(**kwargs):
            return run(pwork.get_patch(None, '5', **kwargs))['state']

        self.assertEqual('state1', get())
        self.assertEqual('state1', get())
        self.assertEqual((['patches/5/'], 1), (requests, pwork.cache_hits))

        # Asking for the current state reads patchwork, and keeps the result
        self.assertEqual('state2', get(max_age=0))
        self.assertEqual('state2', get())
        self.assertEqual(2, len(requests))

        # Once the kept copy is too old, patchwork is read again
        self.now += 2 * 60 * 60
        self.assertEqual('state3', get())
        self.assertEqual(3, len(requests))

        # With no cache, every call reads patchwork
        pwork.cache = False
        self.assertEqual('state4', get())
        self.assertEqual('state5', get())

    def test_hits(self):
        """Reads which the cache answers are counted for each server"""
        pwork = Patchwork.for_testing(lambda _: {'id': 5, 'state': 'new'})
        pwork.cache = self.cache()
        for _ in range(3):
            run(pwork.get_patch(None, '5'))
        self.assertEqual({pwork.url: 2}, pwork.cache.hits_since(''))

    def test_get_patch_broken(self):
        """Patches are still read when the cache fails"""
        pwork = Patchwork.for_testing(lambda _: {'id': 5, 'state': 'new'})
        pwork.cache = self.cache()
        pwork.cache.con = mock.MagicMock()
        pwork.cache.con.execute.side_effect = sqlite3.OperationalError('full')
        with terminal.capture():
            for _ in range(2):
                self.assertEqual('new',
                                 run(pwork.get_patch(None, '5'))['state'])
        self.assertEqual((2, 0), (pwork.request_count, pwork.cache_hits))

    def test_prune_daily(self):
        """Old entries are dropped when the cache is opened, once a day"""
        cache = self.cache()
        cache.put('https://pw', 'patches/1/', {'id': 1})
        cache.close()

        # Opening it again soon after does not prune, even with old entries
        self.now += pwcache.KEEP_DAYS * 86400 + 1
        with mock.patch.object(pwcache, 'PRUNE_INTERVAL', 10 ** 9):
            cache = self.cache()
        self.assertEqual({'id': 1}, cache.get('https://pw', 'patches/1/', None))
        cache.close()

        # A day after the last prune, it does
        self.now += pwcache.KEEP_DAYS * 86400 + 1
        self.assertIsNone(self.cache().get('https://pw', 'patches/1/', None))
