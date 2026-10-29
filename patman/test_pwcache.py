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
from patman import cmdline
from patman import pwcache
from patman.patchwork import Patchwork, request_kind


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


class TestPwCache(unittest.TestCase):  # pylint: disable=R0904
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

    def test_disable(self):
        """--no-cache stops this process, and those it starts, using it"""
        with mock.patch.dict(os.environ, {'PATMAN_PWCACHE': self.path}), \
                mock.patch.object(pwcache, '_cache', None):
            cache = pwcache.get()
            self.assertIsNotNone(cache)
            with mock.patch.object(cache, 'close') as close:
                pwcache.disable()
            close.assert_called_once()
            self.assertIsNone(pwcache.get())
            self.assertEqual('off', os.environ['PATMAN_PWCACHE'])
            cache.close()

        self.assertTrue(cmdline.parse_args(['--no-cache', 'status'],
                                           config_fname=False).no_cache)
        self.assertFalse(cmdline.parse_args(['status'],
                                            config_fname=False).no_cache)

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

    def test_hits_size(self):
        """A cache from before sizes were counted gains the column"""
        os.makedirs(os.path.dirname(self.path))
        con = sqlite3.connect(self.path)
        con.execute('CREATE TABLE hits (hour TEXT, server TEXT, '
                    'count INTEGER, PRIMARY KEY (hour, server))')
        con.execute("INSERT INTO hits VALUES ('2026-10-20 09', 'https://pw', 3)")
        con.execute(f'PRAGMA user_version = {pwcache.LAYOUT}')
        con.commit()
        con.close()
        cache = self.cache()
        self.assertEqual({'https://pw': [3, 0]}, cache.hits_since(''))
        cache.add_hit('https://pw', 40)
        self.assertEqual({'https://pw': [4, 40]}, cache.hits_since(''))

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
        cache.close()

        # SQLite failing, e.g. with a full disk. Close the real connection
        # first, so that it does not remove its files while tearDown() does
        cache = self.cache()
        cache.close()
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

    def test_search_series(self):
        """Series are found by their names or their patches', and kept"""
        requests = []
        query = 'project=uboot&q=video+driver&order=-date&per_page=30'
        pages = {
            f'series/?{query}': [
                {'id': 2, 'name': 'video: Add a driver', 'version': 1,
                 'date': '2026-10-02T10:00:00', 'total': 2}],
            f'patches/?{query}': [
                # A patch of a series found already, then one of another
                {'id': 21, 'date': '2026-10-02T10:00:01',
                 'series': [{'id': 2, 'name': 'video: Add a driver'}]},
                {'id': 31, 'date': '2026-10-03T10:00:00',
                 'submitter': {'name': 'Fred'},
                 'series': [{'id': 3, 'name': 'video: Tidy up', 'version': 2,
                             'web_url': 'https://pw/3'}]}],
        }

        def fake(subpath):
            requests.append(subpath)
            return pages[subpath]

        pwork = Patchwork.for_testing(fake)
        pwork.cache = self.cache()
        found = run(pwork.search_series(None, 'uboot', ' video\tdriver '))
        self.assertEqual(
            [{'id': 3, 'name': 'video: Tidy up', 'version': 2,
              'web_url': 'https://pw/3', 'date': '2026-10-03T10:00:00',
              'submitter': {'name': 'Fred'}, 'total': None},
             pages[f'series/?{query}'][0]], found)
        self.assertEqual(found, run(pwork.search_series(None, 'uboot',
                                                        'video driver')))
        self.assertEqual(2, len(requests))

        with self.assertRaises(ValueError) as exc:
            run(pwork.search_series(None, 'uboot', ' '))
        self.assertEqual('Please give some text to search for',
                         str(exc.exception))

    def test_get_series(self):
        """A series is kept once it is complete, but not before"""
        reads = []

        def fake(subpath):
            reads.append(subpath)
            return {'id': 7, 'received_all': len(reads) > 1}

        pwork = Patchwork.for_testing(fake)
        pwork.cache = self.cache()

        def get(**kwargs):
            return run(pwork.get_series(None, '7', **kwargs))

        # Still arriving: kept for a while, but sync reads it again
        self.assertFalse(get()['received_all'])
        self.assertFalse(get()['received_all'])
        self.assertEqual(1, len(reads))
        self.assertTrue(get(max_age=0)['received_all'])
        self.assertEqual(2, len(reads))

        # Complete: kept however old it is, even for sync
        self.now += 365 * 86400
        self.assertTrue(get(max_age=0)['received_all'])
        self.assertTrue(get()['received_all'])
        self.assertEqual(2, len(reads))

    def test_recent_series(self):
        """Each series in the inbox is kept under its own ID too"""
        reads = []

        def fake(subpath):
            reads.append(subpath)
            if subpath.startswith('series/?'):
                return [{'id': 7, 'received_all': True},
                        {'id': 8, 'received_all': False}]
            return {'id': int(subpath.split('/')[1]), 'received_all': True}

        pwork = Patchwork.for_testing(fake)
        pwork.cache = self.cache()
        page = run(pwork.get_recent_series(None, 'uboot', max_age=None))
        self.assertEqual([7, 8], [series['id'] for series in page])

        # Reviewing a series from the inbox needs no request
        self.assertEqual(page[0], run(pwork.get_series(None, '7')))
        self.assertEqual(page[1], run(pwork.get_series(None, '8')))
        self.assertEqual(1, len(reads))

        # A page from the cache leaves the series alone, so one which is
        # still arriving is read again once it is old, but a complete one
        # is not
        self.now += pwcache_age('SERIES_MAX_AGE') + 1
        run(pwork.get_recent_series(None, 'uboot', max_age=None))
        run(pwork.get_series(None, '7'))
        self.assertTrue(run(pwork.get_series(None, '8'))['received_all'])
        self.assertEqual(['series/8/'], reads[1:])

    def test_comments(self):
        """Comments are kept for a few minutes"""
        reads = []

        def fake(subpath):
            reads.append(subpath)
            return [{'id': len(reads)}]

        pwork = Patchwork.for_testing(fake)
        pwork.cache = self.cache()
        def get(**kwargs):
            return run(
                pwork.get_patch_comments(None, '5', **kwargs))[0]['id']

        self.assertEqual(1, get())
        self.assertEqual(1, get())
        self.now += pwcache_age('COMMENTS_MAX_AGE') + 1
        self.assertEqual(2, get())
        self.assertEqual(3, get(max_age=0))
        self.assertEqual(['patches/5/comments/'] * 3, reads)

        def get_cover():
            return run(
                pwork.get_cover_comments(None, '9'))[0]['id']

        self.assertEqual(4, get_cover())
        self.assertEqual(4, get_cover())
        self.assertEqual('covers/9/comments/', reads[-1])

    def test_mbox(self):
        """A series' mbox is kept, as bytes"""
        content = b'From x\n\xff\xfe binary-ish\n'
        gets = []

        class Response:
            """Pretend response from patchwork"""
            status = 200

            async def read(self):
                """Get the content"""
                return content

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class Client:  # pylint: disable=R0903
            """Pretend aiohttp session"""
            def get(self, url, **_kwargs):
                """Start a request"""
                gets.append(url)
                return Response()

        pwork = Patchwork('https://pw.example', show_progress=False)
        pwork.cache = self.cache()
        self.assertEqual(content,
                         run(pwork.get_series_mbox(Client(), '7')))
        self.assertEqual(content,
                         run(pwork.get_series_mbox(Client(), '7')))
        self.assertEqual(['https://pw.example/series/7/mbox/'], gets)
        self.assertEqual(1, pwork.cache_hits)

    def test_hits(self):
        """Reads which the cache answers are counted for each server"""
        pwork = Patchwork.for_testing(lambda _: {'id': 5, 'state': 'new'})
        pwork.cache = self.cache()
        for _ in range(3):
            run(pwork.get_patch(None, '5'))
        # Each read counts the size of the response
        size = len('{"id": 5, "state": "new"}')
        self.assertEqual({pwork.url: [2, 2 * size]},
                         pwork.cache.hits_since(''))

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

    def test_follow(self):
        """The cache records how far events are followed, and drops entries"""
        cache = self.cache()
        self.assertIsNone(cache.get_follow('https://pw', 'uboot'))
        cache.set_follow('https://pw', 'uboot', 1000.0, '2026-10-20T10:00', 1005)
        self.assertEqual((1000.0, '2026-10-20T10:00', 1005.0),
                         cache.get_follow('https://pw', 'uboot'))
        self.assertIsNone(cache.get_follow('https://pw', 'linux'))

        # An entry read since following began is current however old
        cache.put('https://pw', 'patches/1/', {'id': 1})
        self.now += 10 ** 6
        self.assertIsNone(cache.get('https://pw', 'patches/1/', 0))
        self.assertEqual({'id': 1}, cache.get('https://pw', 'patches/1/', 0,
                                              since=1000.0))
        self.assertIsNone(cache.get('https://pw', 'patches/1/', 0,
                                    since=1001.0))
        self.assertIsNone(cache.get('https://pw', 'patches/1/', 0,
                                    keep=lambda _: False, since=1000.0))
        cache.drop('https://pw', 'patches/1/')
        self.assertIsNone(cache.get('https://pw', 'patches/1/', None))

    def test_follow_events(self):
        """A sync uses patches from the cache while events say they are current"""
        states = {1: 'new', 2: 'new'}
        events = []
        reads = []

        def fake(subpath):
            reads.append(subpath)
            if subpath == 'series/7/':
                return {'id': 7, 'received_all': True, 'cover_letter': None,
                        'patches': [{'id': 1, 'name': 'one'},
                                    {'id': 2, 'name': 'two'}]}
            if subpath.startswith('events/'):
                if 'per_page=1&' in subpath + '&':
                    return [{'date': '2026-10-20T10:00:00'}]
                return events
            pid = int(subpath.split('/')[1])
            if subpath.endswith('/comments/'):
                return []
            return {'id': pid, 'state': states[pid],
                    'project': {'link_name': 'uboot'}}

        pwork = Patchwork.for_testing(fake)
        pwork.project_set(18, 'uboot')
        pwork.cache = self.cache()

        def sync():
            reads.clear()
            _, patches = run(pwork.series_get_state(None, '7', True, False))
            return [p.state for p in patches], [r for r in reads if
                                                not r.endswith('comments/')]

        # Following begins, so the patches are read
        self.assertEqual((['new', 'new'],
                          ['series/7/', 'events/?project=uboot&per_page=1',
                           'patches/1/', 'patches/2/']), sync())

        # Soon after, the events are not checked again, and the patches are
        # current
        self.assertEqual((['new', 'new'], []), sync())

        # Later, an event says patch 2 has changed, so only it is read
        self.now += 61
        states[2] = 'accepted'
        events[:] = [{'date': '2026-10-20T10:05:00',
                      'category': 'patch-state-changed',
                      'payload': {'patch': {'id': 2}}},
                     {'date': '2026-10-20T10:06:00',
                      'category': 'series-created', 'payload': {}}]
        self.assertEqual(
            (['new', 'accepted'],
             ['events/?project=uboot&since=2026-10-20T10%3A00%3A00&'
              'per_page=100&page=1', 'patches/2/']), sync())
        self.assertEqual('2026-10-20T10:06:00',
                         pwork.cache.get_follow(pwork.url, 'uboot')[1])

        # After too long without a check, following begins again, dropping
        # the states which may have changed meanwhile
        pwork.cache.set_states(pwork.url, {5: 'new', 6: 'superseded'})
        self.now += pwcache_age('EVENTS_MAX_GAP') + 1
        events.clear()
        reads.clear()
        self.assertIsNotNone(run(pwork.follow_events(None)))
        self.assertEqual(['events/?project=uboot&per_page=1'], reads)
        self.assertEqual({6: 'superseded'},
                         pwork.cache.get_states(pwork.url, [5, 6]))
        self.assertEqual((['new', 'accepted'], ['patches/1/', 'patches/2/']),
                         sync())

        # So it does if there are too many events to read
        self.now += 61
        events[:] = [{'date': '2026-10-20T11:00:00', 'category': 'x',
                      'payload': {}}] * pwcache_age('EVENTS_PER_PAGE')
        with mock.patch('patman.patchwork.EVENTS_MAX_PAGES', 2):
            _, read = sync()
        self.assertEqual(['events/?project=uboot&since=2026-10-20T10%3A00%3A00'
                          '&per_page=100&page=1',
                          'events/?project=uboot&since=2026-10-20T10%3A00%3A00'
                          '&per_page=100&page=2',
                          'events/?project=uboot&per_page=1',
                          'patches/1/', 'patches/2/'], read)

    def test_follow_other_project(self):
        """A patch in another project is not kept current by the events"""
        def fake(subpath):
            if subpath.startswith('events/'):
                return []
            return {'id': 5, 'state': 'new',
                    'project': {'link_name': 'linux'}}

        pwork = Patchwork.for_testing(fake)
        pwork.project_set(18, 'uboot')
        pwork.cache = self.cache()
        since = run(pwork.follow_events(None))
        run(pwork.get_patch(None, '5', max_age=0, since=since))
        before = pwork.request_count
        run(pwork.get_patch(None, '5', max_age=0, since=since))
        self.assertEqual(before + 1, pwork.request_count)

        # No events are followed with no project, or no cache
        pwork.link_name = None
        self.assertIsNone(run(pwork.follow_events(None)))
        pwork.link_name = 'uboot'
        pwork.cache = False
        self.assertIsNone(run(pwork.follow_events(None)))

    def test_contents(self):
        """The cache says what it holds, by server and kind of entry"""
        cache = self.cache()
        cache.put('https://pw', 'patches/1/', {'name': 'one'})
        self.now = 1100.0
        cache.put('https://pw', 'patches/2/', {'name': 'two'})
        cache.put('https://pw', 'patches/2/comments/', [])
        cache.put('https://other', 'patches/3/', {'name': 'three'})
        contents = cache.contents(request_kind)
        self.assertEqual({('https://pw', 'patch'),
                          ('https://pw', 'patch-comments'),
                          ('https://other', 'patch')}, set(contents))
        count, nbytes, newest = contents[('https://pw', 'patch')]
        self.assertEqual(2, count)
        self.assertGreater(nbytes, 0)
        self.assertEqual(1100.0, newest)

        self.assertEqual([], cache.follow_list())
        cache.set_follow('https://pw', 'uboot', 900.0, '', 1000.0)
        cache.set_follow('https://other', 'linux', 800.0, '', 1050.0)
        self.assertEqual([('https://other', 'linux', 800.0, 1050.0),
                          ('https://pw', 'uboot', 900.0, 1000.0)],
                         cache.follow_list())
        cache.close()

    def test_states(self):
        """The cache records the states of patches"""
        cache = self.cache()
        self.assertEqual({}, cache.get_states('https://pw', [1, 2]))
        cache.set_states('https://pw', {1: 'new', 2: 'accepted'})
        cache.set_states('https://pw', {1: 'superseded'})
        cache.set_states('https://other', {2: 'rejected'})
        self.assertEqual({1: 'superseded', 2: 'accepted'},
                         cache.get_states('https://pw', [1, 2, 3]))
        many = {pid: 'new' for pid in range(1000, 2200)}
        cache.set_states('https://pw', many)
        self.assertEqual(many, cache.get_states('https://pw', list(many)))

        # States not updated for a while are dropped
        self.now += pwcache.KEEP_DAYS * 86400 + 1
        cache.set_states('https://pw', {2: 'accepted'})
        cache.prune()
        self.assertEqual({2: 'accepted'},
                         cache.get_states('https://pw', [1, 2, 1000]))

        # States can be dropped, keeping some
        cache.set_states('https://other', {2: 'rejected'})
        cache.set_states('https://pw', {1: 'new', 3: 'superseded'})
        cache.drop_states('https://pw', ('accepted', 'superseded'))
        self.assertEqual({2: 'accepted', 3: 'superseded'},
                         cache.get_states('https://pw', [1, 2, 3]))
        cache.drop_states('https://pw')
        self.assertEqual({}, cache.get_states('https://pw', [1, 2, 3]))
        self.assertEqual({2: 'rejected'},
                         cache.get_states('https://other', [2]))
        cache.close()

    def test_fill_states(self):
        """A cache from before states were recorded gains its patches' states"""
        cache = self.cache()
        cache.put('https://pw', 'patches/1/', {'id': 1, 'state': 'accepted'})
        cache.put('https://pw', 'patches/1/comments/', [{'id': 5}])
        cache.put('https://pw', 'patches/2/', {'id': 2})
        cache.put('https://pw', 'series/3/', {'id': 3, 'state': 'x'})
        cache.con.execute('DROP TABLE states')
        cache.con.execute(
            "INSERT INTO objects VALUES ('https://pw', 'patches/4/', "
            "x'00', 0, 0)")
        cache.con.commit()
        cache.close()

        cache = self.cache()
        self.assertEqual({1: 'accepted'},
                         cache.get_states('https://pw', [1, 2, 3, 4]))
        cache.close()

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


def pwcache_age(name):
    """Get one of the cache lifetimes set in patchwork"""
    # pylint: disable=C0415
    from patman import patchwork
    return getattr(patchwork, name)
