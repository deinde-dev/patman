# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Tests for the 'patman serve' web server"""

import asyncio
import os
import signal
import sys
import unittest
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from u_boot_pylib import terminal
from patman import cmdline
from patman import control
from patman import cseries
from patman.database import Pcommit
from patman.patchwork import Patchwork
from patman import server
from patman.test_common import TestCommon
from patman.workflow import Wtype

#pylint: disable=protected-access

class TestWebServer(unittest.TestCase, TestCommon):
    """Tests for the server's JSON API and periodic sync"""
    def setUp(self):
        TestCommon.setUp(self)
        self.cser = cseries.Cseries(self.tmpdir, terminal.COLOR_NEVER)
        with terminal.capture():
            self.cser.open_database()
        self.requests = []

    def tearDown(self):
        self.cser.close_database()
        TestCommon.tearDown(self)

    # pylint: disable=R0913,R0917,R0914
    def add_series(self, name, desc, versions, ups=None, source=None):
        """Add a series to the database

        Args:
            name (str): Series name
            desc (str): Series description
            versions (dict): key (int): version number
                value (list of tuple): one per patch: (subject, state,
                    patch_id, num_comments)
            ups (str or None): Upstream name
            source (str or None): 'review' for a series being reviewed

        Return:
            tuple:
                int: Series ID
                dict: key (int): version, value (int): svid
        """
        db = self.cser.db
        series_id = db.series_add(name, desc, ups)
        if source:
            db.series_set_source(series_id, source)
        svids = {}
        for version, patches in versions.items():
            svid = db.ser_ver_add(series_id, version, link=f'{version}00')
            svids[version] = svid
            db.pcommit_add_list(svid, [
                Pcommit(None, seq, subject, None, None, None, None, None)
                for seq, (subject, *_) in enumerate(patches)])
            for pcm, (_, state, patch_id, ncomments) in zip(
                    db.pcommit_get_list(svid), patches):
                db.pcommit_update(Pcommit(pcm.idnum, None, None, None, None,
                                          state, patch_id, ncomments))
        db.commit()
        return series_id, svids

    def make_server(self, **kwargs):
        """Create a server using a fake patchwork

        Return:
            Server: New server
        """
        srv = server.Server(self.cser, **kwargs)
        srv.pwork_for_testing = Patchwork.for_testing(self._fake_patchwork)
        return srv

    def _fake_patchwork(self, subpath):
        """Handle a request to the fake patchwork server"""
        self.requests.append(subpath)
        if subpath == 'patches/1234/comments/':
            return [{
                'date': '2026-10-01T10:00:00',
                'subject': 'Re: [PATCH 1/2] video: Add a driver',
                'submitter': {'name': 'Tom Rini', 'email': 'trini@k.com'},
                'content': '> some code\n\nReviewed-by: Tom Rini',
                'web_url': 'https://pw/comment/1/',
                'headers': {'ignored': 'yes'},
            }]
        if subpath == 'covers/77/comments/':
            return []
        raise ValueError(f"Unexpected request '{subpath}'")

    def call(self, srv, method, path, **kwargs):
        """Make a request to a server and return the response

        Args:
            srv (Server): Server to use
            method (str): HTTP method, e.g. 'GET'
            path (str): Path to request

        Return:
            tuple:
                int: HTTP status
                dict or list or str: decoded JSON, or text if not JSON
        """
        async def _go():
            async with TestClient(TestServer(srv.make_app())) as client:
                resp = await client.request(method, path, **kwargs)
                if resp.content_type == 'application/json':
                    return resp.status, await resp.json()
                return resp.status, await resp.text()

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_go())
        finally:
            loop.close()

    def test_series_list(self):
        """The series list shows status for the latest version only"""
        ser_id, _ = self.add_series('video', 'Video improvements', {
            1: [('video: Add a driver', 'superseded', 1000, 2)],
            2: [('video: Add a driver', 'accepted', 1234, 1),
                ('video: Tidy up', 'changes-requested', 1235, 3)],
            }, ups='us')
        self.cser.db.workflow_add(Wtype.SENT, ser_id, '2026-10-01 10:00:00')
        self.cser.db.workflow_add(Wtype.SENT, ser_id, '2026-10-05 10:00:00')
        self.add_series('rev', 'Somebody else', {1: [('x', 'new', 5, 0)]},
                        source='review')

        status, data = self.call(self.make_server(), 'GET', '/api/series')
        self.assertEqual(200, status)
        self.assertEqual(1, len(data))
        item = data[0]
        self.assertEqual('video', item['name'])
        self.assertEqual(2, item['version'])
        self.assertEqual(2, item['count'])
        self.assertEqual({'accepted': 1, 'changes-requested': 1},
                         item['states'])
        self.assertEqual(4, item['num_comments'])
        self.assertEqual('2026-10-05 10:00:00', item['sent'])
        self.assertEqual('us', item['upstream'])

    def test_series_detail(self):
        """Series detail shows each version's patches, newest first"""
        ser_id, _ = self.add_series('video', 'Video improvements', {
            1: [('video: Add a driver', 'superseded', 1000, 2)],
            2: [('video: Add a driver', 'accepted', 1234, 1),
                ('video: Tidy up', None, None, None)],
            })
        status, data = self.call(self.make_server(), 'GET',
                                 f'/api/series/{ser_id}')
        self.assertEqual(200, status)
        self.assertEqual('https://my-fake-url', data['patchwork_url'])
        self.assertEqual([2, 1], [v['version'] for v in data['versions']])
        latest = data['versions'][0]
        self.assertEqual('200', latest['link'])
        self.assertEqual(
            [{'seq': 1, 'subject': 'video: Add a driver', 'state': 'accepted',
              'patch_id': 1234, 'num_comments': 1},
             {'seq': 2, 'subject': 'video: Tidy up', 'state': None,
              'patch_id': None, 'num_comments': 0}],
            latest['patches'])
        self.assertEqual({'accepted': 1, 'unknown': 1}, latest['states'])

        status, data = self.call(self.make_server(), 'GET', '/api/series/999')
        self.assertEqual(404, status)
        self.assertIn('No series found', data['error'])

    def test_reviews(self):
        """Reviews written with 'patman review' can be listed and read"""
        ser_id, svids = self.add_series(
            'rev', 'Somebody else', {1: [('a', 'new', 5, 0)],
                                     2: [('a', 'new', 6, 0)]},
            source='review')
        db = self.cser.db
        db.review_add(svids[1], 1, 'old review', False, '2026-09-01 10:00:00')
        db.review_add(svids[2], 0, 'Cover comments', False,
                      '2026-10-01 10:00:00')
        rid = db.review_add(svids[2], 1, '> code\nLooks good', True,
                            '2026-10-01 10:00:00')
        db.review_set_draft_id(rid, 'draft1')
        db.ser_ver_set_notes(svids[2], 'Some notes')
        db.commit()
        self.add_series('video', 'Mine', {1: [('x', 'new', 7, 0)]})

        srv = self.make_server()
        status, data = self.call(srv, 'GET', '/api/reviews')
        self.assertEqual(200, status)
        self.assertEqual(1, len(data))
        self.assertEqual(
            {'id': ser_id, 'name': 'rev', 'desc': 'Somebody else',
             'upstream': None, 'version': 2, 'link': '200', 'num_reviews': 2,
             'approved': 1, 'statuses': {'new': 1, 'draft': 1},
             'timestamp': '2026-10-01 10:00:00'}, data[0])

        status, data = self.call(srv, 'GET', f'/api/reviews/{ser_id}')
        self.assertEqual(200, status)
        latest = data['versions'][0]
        self.assertEqual(2, latest['version'])
        self.assertEqual('Some notes', latest['notes'])
        self.assertEqual([0, 1], [r['seq'] for r in latest['reviews']])
        self.assertEqual(
            {'seq': 1, 'body': '> code\nLooks good', 'approved': True,
             'timestamp': '2026-10-01 10:00:00', 'status': 'draft'},
            latest['reviews'][1])
        self.assertEqual(['old review'],
                         [r['body'] for r in data['versions'][1]['reviews']])

    def test_review_db(self):
        """Reviews can be read from a separate database"""
        rdir = os.path.join(self.tmpdir, 'rev')
        os.mkdir(rdir)
        rcser = cseries.Cseries(self.tmpdir, terminal.COLOR_NEVER,
                                db_dir=rdir)
        with terminal.capture():
            rcser.open_database()
        try:
            rdb = rcser.db
            series_id = rdb.series_add('rev', 'In the review db', None)
            rdb.series_set_source(series_id, 'review')
            svid = rdb.ser_ver_add(series_id, 1, link='123')
            rdb.review_add(svid, 1, 'Looks good', True, '2026-10-01 10:00:00')
            rdb.commit()
            self.add_series('mine', 'In the main db', {1: [('x', 'new', 7, 0)]},
                            source='review')

            srv = server.Server(self.cser, review_sync=True, rcser=rcser)
            _, data = self.call(srv, 'GET', '/api/reviews')
            self.assertEqual(['In the review db'], [r['desc'] for r in data])
            _, data = self.call(srv, 'GET', f'/api/reviews/{series_id}')
            self.assertEqual('Looks good',
                             data['versions'][0]['reviews'][0]['body'])
            self.assertEqual(['--review-db-dir', rdir],
                             srv.get_sync_cmds()[1][-2:])
        finally:
            rcser.close_database()

    def test_todo(self):
        """Todos are listed with whether they are due yet"""
        ser_id, _ = self.add_series('video', 'Mine', {1: []})
        self.cser.db.workflow_add(Wtype.TODO, ser_id, '2020-01-01 00:00:00')
        ser2, _ = self.add_series('usb', 'Later', {1: []})
        self.cser.db.workflow_add(Wtype.TODO, ser2, '2099-01-01 00:00:00')
        self.cser.db.commit()

        status, data = self.call(self.make_server(), 'GET', '/api/todo')
        self.assertEqual(200, status)
        self.assertEqual([('video', True), ('usb', False)],
                         [(t['name'], t['due']) for t in data])
        self.assertEqual(ser_id, data[0]['series_id'])

    def test_comments(self):
        """Comments are read from patchwork and trimmed down"""
        srv = self.make_server()
        status, data = self.call(srv, 'GET', '/api/comments/patch/1234')
        self.assertEqual(200, status)
        self.assertEqual(
            [{'date': '2026-10-01T10:00:00',
              'name': 'Tom Rini', 'email': 'trini@k.com',
              'subject': 'Re: [PATCH 1/2] video: Add a driver',
              'content': '> some code\n\nReviewed-by: Tom Rini',
              'web_url': 'https://pw/comment/1/'}], data)

        status, data = self.call(srv, 'GET', '/api/comments/cover/77')
        self.assertEqual((200, []), (status, data))
        self.assertEqual(['patches/1234/comments/', 'covers/77/comments/'],
                         self.requests)

        status, _ = self.call(srv, 'GET', '/api/comments/fred/77')
        self.assertEqual(404, status)

    def test_pwork_per_upstream(self):
        """Each upstream uses its own patchwork URL"""
        self.cser.db.upstream_add('us', 'https://us.git',
                                  patchwork_url='https://pw.us')
        srv = server.Server(self.cser, pw_url='https://pw.default')
        self.assertEqual('https://pw.us', srv.get_pwork('us').url)
        self.assertIs(srv.get_pwork('us'), srv.get_pwork('us'))
        self.assertEqual('https://pw.default', srv.get_pwork(None).url)
        self.assertEqual('https://pw.default', srv.get_pwork('other').url)

        srv = server.Server(self.cser)
        with self.assertRaises(ValueError) as exc:
            srv.get_pwork('other')
        self.assertIn("No patchwork URL for upstream 'other'",
                      str(exc.exception))

    def test_token(self):
        """A token is needed for the API when one is set"""
        srv = self.make_server(token='secret')
        status, data = self.call(srv, 'GET', '/api/todo')
        self.assertEqual((401, {'error': 'Unauthorised'}), (status, data))
        status, _ = self.call(srv, 'GET', '/api/todo',
                              headers={'Authorization': 'Bearer wrong'})
        self.assertEqual(401, status)
        status, _ = self.call(srv, 'GET', '/api/todo',
                              headers={'Authorization': 'Bearer secret'})
        self.assertEqual(200, status)
        status, _ = self.call(srv, 'GET', '/api/todo?token=secret')
        self.assertEqual(200, status)

        # The page itself does not need the token, since it asks for it
        status, text = self.call(srv, 'GET', '/')
        self.assertEqual(200, status)
        self.assertIn('<title>patman</title>', text)
        status, _ = self.call(srv, 'GET', '/static/manifest.json')
        self.assertEqual(200, status)

    def test_sync_cmds(self):
        """The sync never gathers tags into branches"""
        srv = server.Server(self.cser)
        patman = [sys.executable, '-m', 'patman', '-v']
        self.assertEqual([patman + ['series', 'gather-all', '-G']],
                         srv.get_sync_cmds())
        self.assertEqual('patman series gather-all -G',
                         srv._cmd_label(srv.get_sync_cmds()[0]))
        self.assertEqual('sh -c true', srv._cmd_label(['sh', '-c', 'true']))
        srv = server.Server(self.cser, review_sync=True)
        self.assertEqual([patman + ['series', 'gather-all', '-G'],
                          patman + ['review', '--sync']],
                         srv.get_sync_cmds())

    def test_sync(self):
        """A sync runs each command and records its output"""
        srv = self.make_server()
        srv.sync_cmds_for_testing = [
            ['sh', '-c', 'echo one; pwd'],
            ['sh', '-c', 'echo bad >&2; exit 3'],
            ['/no/such/program'],
        ]
        status, data = self.call(srv, 'GET', '/api/sync')
        self.assertEqual(200, status)
        self.assertFalse(data['running'])
        self.assertIsNone(data['started'])

        loop = asyncio.new_event_loop()
        try:
            self.assertTrue(loop.run_until_complete(srv.run_sync()))
        finally:
            loop.close()
        state = srv.sync_state
        self.assertFalse(state['running'])
        self.assertTrue(state['started'])
        self.assertTrue(state['finished'])
        res = state['results']
        self.assertEqual(3, len(res))
        self.assertEqual(
            {'cmd': 'sh -c echo one; pwd', 'returncode': 0,
             'output': f'one\n{self.tmpdir}\n'}, res[0])
        self.assertEqual((3, 'bad\n'), (res[1]['returncode'], res[1]['output']))
        self.assertEqual(-1, res[2]['returncode'])
        self.assertIn('No such file', res[2]['output'])

    def test_sync_post(self):
        """POST /api/sync starts a sync, but only one at a time"""
        srv = self.make_server()
        srv.sync_cmds_for_testing = [['sh', '-c', 'echo hi']]

        async def _go():
            async with TestClient(TestServer(srv.make_app())) as client:
                resp = await client.post('/api/sync')
                first = resp.status, (await resp.json())['running']
                resp = await client.post('/api/sync')
                second = resp.status
                while srv.sync_state['running']:
                    await asyncio.sleep(0.01)
                return first, second

        loop = asyncio.new_event_loop()
        try:
            first, second = loop.run_until_complete(_go())
        finally:
            loop.close()
        self.assertEqual((202, True), first)
        self.assertEqual(409, second)
        self.assertEqual('hi\n', srv.sync_state['results'][0]['output'])

    def test_sync_progress(self):
        """Output is visible while a command is still running"""
        srv = self.make_server()
        srv.sync_cmds_for_testing = [['sh', '-c', 'echo first; sleep 10']]

        async def _go():
            task = asyncio.create_task(srv.run_sync())
            while (not srv.sync_state['results'] or
                   not srv.sync_state['results'][0]['output']):
                await asyncio.sleep(0.01)
            seen = dict(srv.sync_state['results'][0])
            srv.cancel_sync()
            await task
            return seen

        loop = asyncio.new_event_loop()
        try:
            seen = loop.run_until_complete(_go())
        finally:
            loop.close()
        self.assertEqual('first\n', seen['output'])
        self.assertIsNone(seen['returncode'])

    def test_sync_cancel(self):
        """POST /api/sync/cancel stops the sync and skips later commands"""
        srv = self.make_server()
        srv.sync_cmds_for_testing = [['sh', '-c', 'echo start; sleep 10'],
                                     ['sh', '-c', 'echo never']]

        async def _go():
            async with TestClient(TestServer(srv.make_app())) as client:
                resp = await client.post('/api/sync/cancel')
                idle = resp.status
                await client.post('/api/sync')
                while not srv.sync_state['results'][0]['output']:
                    await asyncio.sleep(0.01)
                resp = await client.post('/api/sync/cancel')
                busy = resp.status
                while srv.sync_state['running']:
                    await asyncio.sleep(0.01)
                return idle, busy

        loop = asyncio.new_event_loop()
        try:
            idle, busy = loop.run_until_complete(_go())
        finally:
            loop.close()
        self.assertEqual((409, 202), (idle, busy))
        state = srv.sync_state
        self.assertTrue(state['cancelled'])
        self.assertEqual(1, len(state['results']))
        self.assertEqual('start\n', state['results'][0]['output'])
        self.assertEqual(-signal.SIGTERM, state['results'][0]['returncode'])

    def test_parse_review_req(self):
        """Review requests are turned into 'patman review' arguments"""
        ups = ['us', 'ci']
        parse = server.parse_review_req
        self.assertEqual((['-s', '123'], 'Review series 123'),
                         parse({'target': ' 123 '}, ups))
        self.assertEqual(
            (['-s', '456', '-i', '1,3-5', '-U', 'us', '-d', '-f'],
             'Review series 456'),
            parse({'target': 'https://pw/project/uboot/list/?series=456',
                   'index': '1, 3-5', 'ups': 'us', 'drafts': True,
                   'force': True}, ups))
        self.assertEqual((['-s', '457'], 'Review series 457'),
                         parse({'target': 'https://pw/series/457/'}, ups))
        self.assertEqual(
            (['-p', '2000', '-w'], 'Review series of patch 2000'),
            parse({'target': 'https://pw/project/uboot/patch/2000/',
                   'whole': True}, ups))
        self.assertEqual((['-p', '2001'], 'Review patch 2001'),
                         parse({'patch': 2001}, ups))
        self.assertEqual((['-S', 'video: Fix', '-V', '3'],
                          "Review 'video: Fix'"),
                         parse({'title': 'video: Fix', 'version': '3'}, ups))

        for data, msg in (
                ({}, 'Please give a series'),
                ({'target': 'fred'}, 'Cannot find a series'),
                ({'target': '1', 'title': 'x'}, 'only one of'),
                ({'target': '1', 'index': '1;rm'}, 'Invalid patch selection'),
                ({'target': '1', 'ups': 'zz'}, "Unknown upstream 'zz'"),
                ({'patch': 'x'}, "Invalid patch 'x'"),
                ({'patch': True}, "Invalid patch 'True'")):
            with self.assertRaises(ValueError) as exc:
                parse(data, ups)
            self.assertIn(msg, str(exc.exception))

    def run_jobs(self, srv, calls):
        """Make requests to a server, then wait for its jobs to finish

        Args:
            srv (Server): Server to use
            calls (list of tuple): (method, path, json) for each request

        Return:
            list of tuple: (status, data) for each request
        """
        async def _go():
            out = []
            async with TestClient(TestServer(srv.make_app())) as client:
                for method, path, data in calls:
                    resp = await client.request(method, path, json=data)
                    out.append((resp.status, await resp.json()))
                while any(job.active for job in srv.get_jobs()):
                    await asyncio.sleep(0.01)
            return out

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_go())
        finally:
            loop.close()

    def test_review_jobs(self):
        """Reviews and redrafts run as jobs, one at a time"""
        series_id, _ = self.add_series('rev', 'Somebody else',
                                       {1: [('a', 'new', 5, 0)]}, ups='us',
                                       source='review')
        srv = self.make_server()
        srv.get_patman_cmd = lambda *args: ['echo', *args]
        res = self.run_jobs(srv, [
            ('POST', '/api/review', {'target': '123', 'drafts': True}),
            ('POST', '/api/review/scan', {}),
            ('POST', f'/api/reviews/{series_id}/redraft', {'seqs': [2, 0]}),
            ('POST', '/api/review', {'title': ''}),
        ])
        self.assertEqual([202, 202, 202, 404], [status for status, _ in res])
        self.assertEqual('Review series 123', res[0][1]['title'])
        self.assertTrue(res[0][1]['queued'] or res[0][1]['running'])
        self.assertIn('Please give a series', res[3][1]['error'])

        _, jobs = self.call(srv, 'GET', '/api/jobs')
        self.assertEqual([3, 2, 1], [job['id'] for job in jobs])
        self.assertEqual(
            ['review -s 123 -d', 'review --scan',
             'review -s 100 --redraft -i 0,2 -U us'],
            [job['results'][0]['output'].strip() for job in reversed(jobs)])
        self.assertEqual("Redraft 'Somebody else' v1", jobs[0]['title'])

        status, job = self.call(srv, 'GET', '/api/jobs/2')
        self.assertEqual((200, 'review'), (status, job['kind']))
        status, _ = self.call(srv, 'GET', '/api/jobs/99')
        self.assertEqual(404, status)
        status, _ = self.call(srv, 'POST', '/api/jobs/1/cancel')
        self.assertEqual(409, status)

    def test_cancel_queued_job(self):
        """A job which is waiting for another can be cancelled"""
        srv = self.make_server()
        cmds = iter([['sh', '-c', 'echo busy; sleep 10'], ['echo', 'never']])
        srv.get_patman_cmd = lambda *args: next(cmds)

        async def _go():
            async with TestClient(TestServer(srv.make_app())) as client:
                await client.post('/api/review', json={'target': '1'})
                await client.post('/api/review', json={'target': '2'})
                first, second = srv.get_job(1), srv.get_job(2)
                while not first.state['results'] or \
                        not first.state['results'][0]['output']:
                    await asyncio.sleep(0.01)
                queued = second.state['queued']
                await client.post('/api/jobs/2/cancel')
                await client.post('/api/jobs/1/cancel')
                while first.active or second.active:
                    await asyncio.sleep(0.01)
                return queued

        loop = asyncio.new_event_loop()
        try:
            self.assertTrue(loop.run_until_complete(_go()))
        finally:
            loop.close()
        self.assertTrue(srv.get_job(1).state['cancelled'])
        second = srv.get_job(2).state
        self.assertTrue(second['cancelled'])
        self.assertEqual([], second['results'])

    def test_inbox(self):
        """The inbox lists recent series, marking those already reviewed"""
        rev_id, _ = self.add_series('rev', 'video: Add a driver',
                                    {2: [('a', 'new', 6, 0)]},
                                    source='review')
        self.add_series('mine', 'My series', {1: [('x', 'new', 7, 0)]})
        db = self.cser.db
        db.upstream_add('us', 'https://us', 'https://pw.us')
        db.upstream_set_default('us')
        db.patchwork_update('U-Boot', 6, 'uboot', 'us')
        db.commit()

        def series(idnum, name, version):
            return {'id': idnum, 'name': name, 'version': version,
                    'date': '2026-10-01T10:00:00', 'total': 1,
                    'received_all': True, 'web_url': f'https://pw/{idnum}',
                    'submitter': {'name': 'Fred', 'email': 'f@b.com'},
                    'cover_letter': None,
                    'patches': [{'id': idnum * 10, 'name': name}]}

        pages = {
            'series/?project=uboot&order=-date&page=1&per_page=50': [
                series(300, '[v3] video: Add a driver', 3),
                series(200, 'video: Add a driver', 2),
                series(100, 'My series', 1),
                series(400, 'net: Something new', 1)],
        }
        srv = self.make_server()
        srv.pwork_for_testing.fake_request = pages.get
        status, data = self.call(srv, 'GET', '/api/inbox')
        self.assertEqual(200, status)
        self.assertEqual(('us', 1), (data['ups'], data['page']))
        items = {item['id']: item for item in data['series']}
        self.assertEqual([300, 200, 100, 400], list(items))

        # A new version of a reviewed series is linked to the review
        self.assertEqual((None, rev_id, False),
                         (items[300]['reviewed'], items[300]['earlier'],
                          items[300]['mine']))
        self.assertEqual((rev_id, None), (items[200]['reviewed'],
                                          items[200]['earlier']))
        self.assertTrue(items[100]['mine'])
        self.assertEqual((None, None, False),
                         (items[400]['reviewed'], items[400]['earlier'],
                          items[400]['mine']))
        self.assertEqual('Fred', items[400]['submitter'])
        self.assertEqual([{'id': 4000, 'name': 'net: Something new'}],
                         items[400]['patches'])

        status, data = self.call(srv, 'GET', '/api/inbox?ups=xx')
        self.assertEqual(404, status)
        self.assertIn("No patchwork project for upstream 'xx'", data['error'])

    def test_upstreams(self):
        """Upstreams with a patchwork server are listed"""
        with terminal.capture():
            self.cser.upstream_add('us', 'https://us',
                                   patchwork_url='https://pw.us')
            self.cser.upstream_add('ci', 'https://ci')
            self.cser.upstream_set_default('us')
        _, data = self.call(self.make_server(), 'GET', '/api/upstreams')
        self.assertEqual([{'name': 'us', 'default': True,
                           'patchwork_url': 'https://pw.us'}], data)

    def test_periodic_sync(self):
        """The periodic sync starts with the server and runs repeatedly"""
        srv = self.make_server(sync_interval=1)
        srv.sync_cmds_for_testing = [['true']]
        sleeps = []

        async def fake_sleep(secs):
            sleeps.append(secs)
            if len(sleeps) == 2:
                raise asyncio.CancelledError()

        srv._sleep = fake_sleep

        async def _go():
            async with TestClient(TestServer(srv.make_app())):
                try:
                    await srv._sync_task
                except asyncio.CancelledError:
                    pass

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_go())
        finally:
            loop.close()
        self.assertEqual([60, 60], sleeps)
        self.assertEqual(0, srv.sync_state['results'][0]['returncode'])

    def test_is_loopback(self):
        """Loopback addresses are detected"""
        self.assertTrue(server.is_loopback('127.0.0.1'))
        self.assertTrue(server.is_loopback('::1'))
        self.assertTrue(server.is_loopback('localhost'))
        self.assertFalse(server.is_loopback('0.0.0.0'))
        self.assertFalse(server.is_loopback('100.64.1.2'))
        self.assertFalse(server.is_loopback('myhost'))

    def test_serve_cmdline(self):
        """The serve command passes its options through to the server"""
        # Use a separate database, since setUp() already has one open
        dbdir = os.path.join(self.tmpdir, 'serve')
        os.mkdir(dbdir)
        args = cmdline.parse_args(
            ['serve', '--host', '0.0.0.0', '--port', '1234', '-s', '-r',
             '-t', 'tok'], config_fname=False)
        with mock.patch.object(server.web, 'run_app') as run_app, \
                terminal.capture() as (out, err):
            control.do_serve(args, dbdir)
        run_app.assert_called_once()
        self.assertEqual(('0.0.0.0', 1234),
                         (run_app.call_args.kwargs['host'],
                          run_app.call_args.kwargs['port']))
        self.assertIn('Serving on http://0.0.0.0:1234/?token=tok',
                      out.getvalue() + err.getvalue())
        self.assertIn('Syncing (gather-all and review --sync) every 60 '
                      'minutes', out.getvalue() + err.getvalue())

        args = cmdline.parse_args(['serve', '--host', '0.0.0.0'],
                                  config_fname=False)
        with mock.patch.object(server.web, 'run_app'), \
                terminal.capture() as (out, err):
            control.do_serve(args, dbdir)
        self.assertIn('no --token', out.getvalue() + err.getvalue())


if __name__ == '__main__':
    unittest.main()
