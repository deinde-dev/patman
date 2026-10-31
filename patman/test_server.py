# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Tests for the 'patman serve' web server"""

import asyncio
from datetime import datetime, timedelta
import json
import os
import signal
import socket
import subprocess
import unittest
from unittest import mock

import aiohttp
import aiohttp.abc
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import yarl

from u_boot_pylib import terminal
from u_boot_pylib import tools
from patman import cmdline
from patman import control
from patman import cseries
from patman import database
from patman import interpreter
from patman import lorelist
from patman.database import Pcommit
from patman import patchwork
from patman.patchwork import Patchwork
from patman import pwcache
from patman import server
from patman import settings
from patman import times
from patman import traffic
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
        srv.own_emails = set()
        return srv

    def _fake_patchwork(self, subpath):
        """Handle a request to the fake patchwork server"""
        self.requests.append(subpath)
        if subpath == 'patches/1234/comments/':
            return [{
                'id': 1,
                'date': '2026-10-01T10:00:00',
                'subject': 'Re: [PATCH 1/2] video: Add a driver',
                'submitter': {'name': 'Tom Rini', 'email': 'trini@k.com'},
                'content': '> some code\n\nReviewed-by: Tom Rini',
                'web_url': 'https://pw/comment/1/',
                'headers': {'ignored': 'yes'},
            }]
        if subpath == 'patches/1234/':
            return {
                'id': 1234, 'name': '[1/2] video: Add a driver',
                'date': '2026-10-01T09:00:00', 'state': 'new',
                'submitter': {'name': 'Fred', 'email': 'f@b.com'},
                'content': 'Add a driver\n\nSigned-off-by: Fred',
                'diff': '--- a/x.c\n+++ b/x.c\n@@ -1 +1 @@\n-old\n+new\n',
                'web_url': 'https://pw/patch/1234/', 'headers': {'big': 1},
                'series': [{'id': 99, 'name': 'video', 'version': 2,
                            'url': 'https://pw/api/series/99/'}],
            }
        if subpath == 'covers/77/':
            return {'id': 77, 'name': '[0/2] video', 'content': 'Cover text',
                    'date': '2026-10-01T09:00:00', 'submitter': {}}
        if subpath == 'covers/77/comments/':
            return []
        if subpath == 'series/200/':
            return {'id': 200, 'cover_letter': {'id': 77, 'name': '[0/2] rev'}}
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

    def test_series_sync(self):
        """One version of a series can be synced from patchwork as a job"""
        ser_id, _ = self.add_series('video', 'Video improvements', {
            1: [('video: Add a driver', 'superseded', 1000, 2)],
            2: [('video: Add a driver', 'accepted', 1234, 1)],
            3: [('video: Add a driver', None, None, None)],
            })
        self.cser.db.ser_ver_set_link(ser_id, 3, None)
        self.cser.db.commit()
        srv = self.make_server()

        # Take a while, so that a second sync of the same version is refused
        srv.get_patman_cmd = lambda *args: [
            'sh', '-c', 'sleep 0.2; echo "$@"', 'sh', *args]
        path = f'/api/series/{ser_id}/sync'
        res = self.run_jobs(srv, [
            ('POST', path, {'version': 2}),
            ('POST', path, {'version': 2}),
            ('POST', path, {'version': 1}),
            ('POST', path, {}),
            ('POST', path, {'version': 4}),
            ('POST', '/api/series/999/sync', {}),
        ])
        self.assertEqual([202, 409, 202, 404, 404, 404],
                         [status for status, _ in res])
        self.assertEqual("Sync 'video' v2", res[0][1]['title'])
        self.assertIn("'video' v2 is already being synced", res[1][1]['error'])
        self.assertIn("'video' v3 has no patchwork link", res[3][1]['error'])
        self.assertIn("has no version 4", res[4][1]['error'])
        self.assertIn('No series found', res[5][1]['error'])

        _, jobs = self.call(srv, 'GET', '/api/jobs')
        self.assertEqual(
            ['series -s video -V 2 gather -G', 'series -s video -V 1 gather -G'],
            [job['results'][0]['output'].strip() for job in reversed(jobs)])
        self.assertEqual({'gather'}, {job['kind'] for job in jobs})

        # A full sync reads only the latest version, which has no link here
        _, state = self.call(srv, 'GET', '/api/sync')
        self.assertEqual(0, state['num_series'])
        self.cser.db.ser_ver_set_link(ser_id, 3, '300')
        self.cser.db.commit()
        _, state = self.call(srv, 'GET', '/api/sync')
        self.assertEqual(1, state['num_series'])

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
             'subjects': ['a'],
             'upstream': None, 'version': 2, 'link': '200', 'num_reviews': 2,
             'approved': 1, 'statuses': {'new': 1, 'draft': 1},
             'timestamp': '2026-10-01 10:00:00', 'hidden': False,
             'replies_waiting': 0},
            data[0])

        status, data = self.call(srv, 'GET', f'/api/reviews/{ser_id}')
        self.assertEqual(200, status)
        latest = data['versions'][0]
        self.assertEqual(2, latest['version'])
        self.assertEqual('Some notes', latest['notes'])
        self.assertEqual([0, 1], [r['seq'] for r in latest['reviews']])
        self.assertEqual(
            {'id': rid, 'seq': 1, 'body': '> code\nLooks good',
             'approved': True, 'timestamp': '2026-10-01 10:00:00',
             'status': 'draft', 'msgid': None, 'own_draft': False,
             'replies': [], 'job': None},
            latest['reviews'][1])

        # Outside a git repo there are no review branches; once there is one,
        # the page can say that 'patman review --checkout' uses it
        self.assertIsNone(latest['review_branch'])
        for args in (['init', '-q'],
                     ['commit', '-q', '--allow-empty', '-m', 'first'],
                     ['branch', 'pw-200-review']):
            subprocess.run(['git', '-c', 'user.name=T', '-c', 'user.email=t@e',
                            *args], cwd=self.tmpdir, check=True)
        _, data = self.call(srv, 'GET', f'/api/reviews/{ser_id}')
        self.assertEqual(['pw-200-review', None],
                         [ver['review_branch'] for ver in data['versions']])

        # A review with its headers gives the message it replies to, which
        # locates the patch on the list
        db.review_set_envelope(rid, {'in_reply_to': '<p1@x>'})
        db.commit()
        _, data = self.call(srv, 'GET', f'/api/reviews/{ser_id}')
        self.assertEqual('<p1@x>', data['versions'][0]['reviews'][1]['msgid'])
        self.assertEqual(['old review'],
                         [r['body'] for r in data['versions'][1]['reviews']])
        self.assertEqual([(1, 'a', 6)],
                         [(p['seq'], p['subject'], p['patch_id'])
                          for p in latest['patches']])

        # The cover letter of the version with a cover-letter review is
        # looked up once and recorded
        self.assertEqual(['series/200/'], self.requests)
        self.assertEqual((77, '[0/2] rev'),
                         (latest['cover_id'], latest['cover_name']))
        self.assertEqual(77, db.ser_ver_get_by_id(svids[2]).cover_id)
        self.assertIsNone(data['versions'][1]['cover_id'])
        _, data = self.call(srv, 'GET', f'/api/reviews/{ser_id}')
        self.assertEqual(77, data['versions'][0]['cover_id'])
        self.assertEqual(['series/200/'], self.requests)

    def test_review_default_upstream(self):
        """A review with no upstream uses the default upstream's patchwork"""
        self.cser.db.upstream_add('us', 'https://us.git',
                                  patchwork_url='https://pw.us')
        self.cser.db.upstream_set_default('us')
        ser_id, _ = self.add_series('rev', 'Somebody else',
                                    {1: [('a', 'new', 5, 0)]}, source='review')
        srv = server.Server(self.cser, pw_url='https://pw.default')
        _, data = self.call(srv, 'GET', f'/api/reviews/{ser_id}')
        self.assertEqual(('us', 'https://pw.us'),
                         (data['upstream'], data['patchwork_url']))

    def test_review_no_cover(self):
        """A version without a cover letter is only looked up once"""
        ser_id, svids = self.add_series(
            'rev', 'Somebody else', {3: [('a', 'new', 5, 0)]},
            source='review')
        self.cser.db.review_add(svids[3], 0, 'Cover comments', False,
                                '2026-10-01 10:00:00')
        self.cser.db.commit()
        srv = self.make_server()
        srv.pwork_for_testing = Patchwork.for_testing(
            lambda subpath: self.requests.append(subpath) or {'id': 300})
        for _ in range(2):
            _, data = self.call(srv, 'GET', f'/api/reviews/{ser_id}')
            self.assertIsNone(data['versions'][0]['cover_id'])
        self.assertEqual(['series/300/'], self.requests)

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

    def test_stats(self):
        """The server counts the reviews, emails and replies"""
        _, svids = self.add_series(
            'rev', 'Somebody else', {1: [('a', 'new', 5, 0)]}, ups='us',
            source='review')
        db = self.cser.db
        rid = db.review_add(svids[1], 1, 'Looks good', True,
                            '2026-10-01 10:00:00')
        db.review_set_sent(rid, 'Looks good')
        db.commit()
        srv = self.make_server()

        status, data = self.call(srv, 'GET', '/api/stats')
        self.assertEqual(200, status)
        totals = data['totals']
        self.assertEqual((1, 1, 1, 0), (totals['series'], totals['patches'],
                                        totals['emails'], totals['untimed']))
        self.assertEqual(8, len(data['weeks']))
        self.assertEqual(1, data['weeks'][-1]['sent'])

        # The number of weeks can be chosen, up to a year
        _, data = self.call(srv, 'GET', '/api/stats?weeks=3')
        self.assertEqual(3, len(data['weeks']))
        _, data = self.call(srv, 'GET', '/api/stats?weeks=1000')
        self.assertEqual(52, len(data['weeks']))
        status, data = self.call(srv, 'GET', '/api/stats?weeks=x')
        self.assertEqual(400, status)
        self.assertIn('weeks', data['error'])

    def test_review_context(self):
        """The server gives the context which the agent was given"""
        series_id, svids = self.add_series(
            'rev', 'Somebody else', {1: [('a', 'new', 5, 0)]}, ups='us',
            source='review')
        db = self.cser.db
        sid = '0b8c2f7e-1d3a-4c5b-9e6f-7a8b9c0d1e2f'
        rid = db.review_add(svids[1], 0, 'Cover', False, '2026-10-01')
        db.review_set_session(rid, sid)
        rid = db.review_add(svids[1], 1, 'Looks good', True, '2026-10-01')
        db.review_set_context(rid, [('Series', '  >>> 1/1 abc a'),
                                    ('Earlier review', 'Hi Fred')])
        db.review_add(svids[1], 2, 'Gone', False, '2026-10-01')
        db.commit()

        # An older review's context is in the transcript of its session
        base = os.path.join(self.tmpdir, 'claude')
        os.makedirs(os.path.join(base, 'projects', 'wt'))
        prompt = ('Review this\n\nUSER CONTEXT (extra notes from the '
                  'reviewer for this run):\nThis is an RFC\n\nTASK:\nDo it\n')
        with open(os.path.join(base, 'projects', 'wt', f'{sid}.jsonl'), 'w',
                  encoding='utf-8') as outf:
            for entry in ({'type': 'queue-operation'},
                          {'type': 'user', 'message': {'content': prompt}}):
                print(json.dumps(entry), file=outf)

        srv = self.make_server()
        path = f'/api/reviews/{series_id}/context'
        with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': base}):
            status, data = self.call(srv, 'GET', f'{path}?version=1')
        self.assertEqual(200, status)
        self.assertEqual(1, data['version'])
        self.assertEqual(
            [(0, 'transcript', [('Your notes', 'This is an RFC')]),
             (1, 'stored', [('Series', '  >>> 1/1 abc a'),
                            ('Earlier review', 'Hi Fred')]),
             (2, None, [])],
            [(item['seq'], item['source'],
              [(part['title'], part['text']) for part in item['parts']])
             for item in data['reviews']])

        # The version must be one which the series has
        status, data = self.call(srv, 'GET', path)
        self.assertEqual(400, status)
        self.assertIn('version', data['error'])
        status, data = self.call(srv, 'GET', f'{path}?version=9')
        self.assertIn('version 9', data['error'])

    def test_version(self):
        """The server gives the version of patman which it runs"""
        with mock.patch.object(server.info, 'get_version',
                               return_value='1.2.3 (v1.2.3-4-gabcdef0)'):
            status, data = self.call(self.make_server(), 'GET', '/api/version')
        self.assertEqual(200, status)
        self.assertEqual({'version': '1.2.3 (v1.2.3-4-gabcdef0)',
                          'project': 'U-Boot',
                          'source_url': 'https://elixir.bootlin.com/u-boot/'
                                        'latest/source/{path}#L{line}'}, data)

        # The project comes from the review settings
        url = 'https://github.com/dgibson/dtc/blob/main/{path}#L{line}'
        with mock.patch.object(server.info, 'get_version', return_value=None), \
                mock.patch.dict(settings.review, {'project': 'dtc'}):
            _, data = self.call(self.make_server(), 'GET', '/api/version')
        self.assertEqual({'version': None, 'project': 'dtc',
                          'source_url': url}, data)

        # Another project has no source URL unless one is given
        with mock.patch.dict(settings.review, {'project': 'gcc'}):
            _, data = self.call(self.make_server(), 'GET', '/api/version')
        self.assertIsNone(data['source_url'])
        mine = 'https://example.com/gcc/{path}?line={line}'
        with mock.patch.dict(settings.review, {'project': 'gcc',
                                               'source_url': mine}):
            _, data = self.call(self.make_server(), 'GET', '/api/version')
        self.assertEqual(mine, data['source_url'])

        # Linux is known too, in any case
        with mock.patch.dict(settings.review, {'project': 'Linux'}):
            self.assertEqual('https://elixir.bootlin.com/linux/latest/source/'
                             '{path}#L{line}', settings.source_url())

    def test_utc_offset(self):
        """API responses say how far the server's time zone is from UTC"""
        srv = self.make_server()

        async def _go():
            async with TestClient(TestServer(srv.make_app())) as client:
                api = await client.get('/api/version')
                page = await client.get('/')
                return (api.headers.get('X-Patman-UTC-Offset'),
                        page.headers.get('X-Patman-UTC-Offset'))

        loop = asyncio.new_event_loop()
        try:
            api, page = loop.run_until_complete(_go())
        finally:
            loop.close()
        self.assertEqual(str(times.utc_offset()), api)
        self.assertIsNone(page)

        with mock.patch.object(times, 'utc_offset', return_value=330):
            loop = asyncio.new_event_loop()
            try:
                api, _ = loop.run_until_complete(_go())
            finally:
                loop.close()
        self.assertEqual('330', api)

    def test_get_version(self):
        """The version comes from a checkout, with its commit, or the package"""
        top = os.path.join(self.tmpdir, 'checkout')
        os.mkdir(top)
        tools.write_file(os.path.join(top, 'pyproject.toml'),
                         '[project]\nname = "patch-manager"\n'
                         'version = "1.2.3"\n', binary=False)
        self.assertEqual('1.2.3', server.info.get_version(top))

        # In a git checkout at the release, the version alone; after it, the
        # commit too
        def git(*args):
            subprocess.run(['git', *args], cwd=top, check=True,
                           capture_output=True)
        git('init', '-q')
        git('add', '.')
        git('commit', '-q', '-m', 'Release')
        git('tag', 'v1.2.3')
        self.assertEqual('1.2.3', server.info.get_version(top))
        git('commit', '-q', '--allow-empty', '-m', 'More')
        self.assertRegex(server.info.get_version(top),
                         r'^1\.2\.3 \(v1\.2\.3-1-g[0-9a-f]+\)$')

        # Another project's pyproject.toml is not patman's
        other = os.path.join(self.tmpdir, 'other')
        os.mkdir(other)
        tools.write_file(os.path.join(other, 'pyproject.toml'),
                         '[project]\nname = "other"\nversion = "9.9"\n',
                         binary=False)
        with mock.patch.object(server.info.metadata, 'version',
                               return_value='0.4.0'):
            self.assertEqual('0.4.0', server.info.get_version(other))
        with mock.patch.object(server.info.metadata, 'version',
                               side_effect=server.info.metadata.PackageNotFoundError):
            self.assertIsNone(server.info.get_version(other))

    def test_comments(self):
        """Comments are read from patchwork and trimmed down"""
        srv = self.make_server()

        # Our own address is found the first time it is needed
        srv.own_emails = None
        with mock.patch.object(server.app.gitutil, 'get_default_user_email',
                               return_value='me@k.com') as get_email:
            status, data = self.call(srv, 'GET', '/api/comments/patch/1234')
        self.assertEqual(200, status)
        self.assertEqual(
            [{'id': 1, 'date': '2026-10-01T10:00:00',
              'name': 'Tom Rini', 'email': 'trini@k.com',
              'subject': 'Re: [PATCH 1/2] video: Add a driver',
              'content': '> some code\n\nReviewed-by: Tom Rini',
              'web_url': 'https://pw/comment/1/', 'own': False}], data)
        get_email.assert_called_once()
        self.assertEqual({'me@k.com'}, srv.own_emails)

        # A comment from our own address, such as a review we sent, says so
        srv.own_emails = {'trini@k.com'}
        _, data = self.call(srv, 'GET', '/api/comments/patch/1234')
        self.assertTrue(data[0]['own'])

        status, data = self.call(srv, 'GET', '/api/comments/cover/77')
        self.assertEqual((200, []), (status, data))
        self.assertEqual(['patches/1234/comments/', 'patches/1234/comments/',
                          'covers/77/comments/'], self.requests)

        status, _ = self.call(srv, 'GET', '/api/comments/fred/77')
        self.assertEqual(404, status)

    def test_patch(self):
        """A patch or cover letter can be read from patchwork"""
        srv = self.make_server()
        status, data = self.call(srv, 'GET', '/api/patch/1234')
        self.assertEqual(200, status)
        # With no cache, the patch is read from patchwork now
        self.assertTrue(data.pop('fetched'))
        self.assertEqual(
            {'id': 1234, 'name': '[1/2] video: Add a driver',
             'date': '2026-10-01T09:00:00', 'submitter': 'Fred',
             'state': 'new', 'content': 'Add a driver\n\nSigned-off-by: Fred',
             'diff': '--- a/x.c\n+++ b/x.c\n@@ -1 +1 @@\n-old\n+new\n',
             'web_url': 'https://pw/patch/1234/',
             'series': [{'id': 99, 'name': 'video', 'version': 2}]}, data)

        status, data = self.call(srv, 'GET', '/api/cover/77')
        self.assertEqual(200, status)
        self.assertEqual(('Cover text', '', None),
                         (data['content'], data['diff'], data['submitter']))

    def test_patch_refresh(self):
        """A patch and its comments come from the cache, unless refreshed"""
        srv = self.make_server()
        now = [1000.0]
        srv.pwork_for_testing.cache = pwcache.Cache(
            os.path.join(self.tmpdir, 'pwcache.db'), clock=lambda: now[0])
        self.requests.clear()
        for _ in range(2):
            _, data = self.call(srv, 'GET', '/api/patch/1234')
            self.call(srv, 'GET', '/api/comments/patch/1234')
        self.assertEqual(['patches/1234/', 'patches/1234/comments/'],
                         self.requests)
        self.assertEqual(times.to_str(datetime.fromtimestamp(1000)),
                         data['fetched'])

        # A refresh reads both from patchwork, and says when
        now[0] += 60
        _, data = self.call(srv, 'GET', '/api/patch/1234?refresh=1')
        self.call(srv, 'GET', '/api/comments/patch/1234?refresh=1')
        self.assertEqual(['patches/1234/', 'patches/1234/comments/'] * 2,
                         self.requests)
        self.assertEqual(times.to_str(datetime.fromtimestamp(1060)),
                         data['fetched'])

    def test_blob(self):
        """A file can be read from the git repo by its blob hash"""
        def add_blob(data):
            return subprocess.run(
                ['git', 'hash-object', '-w', '--stdin'], cwd=self.tmpdir,
                input=data, capture_output=True, check=True).stdout.decode(
                    ).strip()

        subprocess.run(['git', 'init', '-q'], cwd=self.tmpdir, check=True)
        oid = add_blob(b'line 1\nline 2\n')
        binary = add_blob(b'a\0b')
        srv = self.make_server()

        # The first blob which the repo holds is used
        status, data = self.call(srv, 'GET', '/api/blob',
                                 params=[('hash', '1234567'),
                                         ('hash', oid[:10])])
        self.assertEqual((200, {'hash': oid[:10], 'text': 'line 1\nline 2\n'}),
                         (status, data))

        for params, code, msg in (
                ([('hash', '1234567')], 404, 'The file is not in the git repo'),
                ([], 404, 'The file is not in the git repo'),
                ([('hash', '--help')], 400, "Invalid blob hash '--help'"),
                ([('hash', binary)], 404, 'File is not text')):
            status, data = self.call(srv, 'GET', '/api/blob', params=params)
            self.assertEqual((code, {'error': msg}), (status, data))

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

        # So do its script and style sheet
        self.assertIn('static/app.js', text)
        self.assertIn('static/app.css', text)
        status, text = self.call(srv, 'GET', '/static/app.js')
        self.assertEqual(200, status)
        self.assertIn('function route()', text)
        status, _ = self.call(srv, 'GET', '/static/app.css')
        self.assertEqual(200, status)

    def test_no_cache(self):
        """The page and its files are checked before a browser reuses them"""
        srv = server.Server(self.cser)

        async def _go():
            async with TestClient(TestServer(srv.make_app())) as client:
                out = {}
                for path in ('/', '/static/app.js', '/api/series'):
                    # Read the body, else the server cannot finish sending
                    # one larger than the client's buffer, and the test
                    # server waits for it when closing
                    async with client.get(path) as resp:
                        await resp.read()
                        out[path] = resp.headers.get('Cache-Control')
                return out

        loop = asyncio.new_event_loop()
        try:
            got = loop.run_until_complete(_go())
        finally:
            loop.close()
        self.assertEqual('no-cache', got['/'])
        self.assertEqual('no-cache', got['/static/app.js'])
        self.assertIsNone(got['/api/series'])

    def get_raw(self, srv, paths, headers=None):
        """Make GET requests to a server and return the responses as sent

        Args:
            srv (Server): Server to use
            paths (list of str or yarl.URL): Paths to request
            headers (dict or None): Headers to send with each request

        Return:
            dict: key (str or yarl.URL): path
                value (tuple):
                    int: HTTP status
                    CIMultiDictProxy: headers of the response
                    bytes: body of the response, decompressed
        """
        async def _go():
            async with TestClient(TestServer(srv.make_app())) as client:
                out = {}
                for path in paths:
                    resp = await client.get(path, headers=headers)
                    out[path] = resp.status, resp.headers, await resp.read()
                return out

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_go())
        finally:
            loop.close()

    def test_compress(self):
        """Larger responses are compressed for a browser which accepts it"""
        for seq in range(30):
            self.add_series(f'video{seq}', f'Video improvements {seq}',
                            {1: [('video: Add a driver', 'new', seq, 0)]})
        srv = server.Server(self.cser)
        paths = ['/api/series', '/api/version', '/', '/static/app.js',
                 '/static/icon-512.png']
        got = self.get_raw(srv, paths, {'Accept-Encoding': 'gzip, deflate'})
        plain = self.get_raw(srv, paths, {'Accept-Encoding': 'identity'})

        # The list of series, the page and its script are compressed with
        # gzip, which the client undoes
        for path in ('/api/series', '/', '/static/app.js'):
            status, headers, body = got[path]
            self.assertEqual(200, status)
            self.assertEqual('gzip', headers.get('Content-Encoding'), path)
            self.assertEqual('Accept-Encoding', headers.get('Vary'))
            self.assertEqual(plain[path][2], body)
        self.assertEqual(30, len(json.loads(got['/api/series'][2])))
        self.assertIn(b'function route()', got['/static/app.js'][2])

        # The version is too small to be worth it and the icon is
        # compressed already
        for path in ('/api/version', '/static/icon-512.png'):
            self.assertIsNone(got[path][1].get('Content-Encoding'), path)
        self.assertEqual('image/png',
                         got['/static/icon-512.png'][1]['Content-Type'])

        # Nothing is compressed for a browser which does not accept it, but
        # the responses say that it could be
        for path in paths:
            self.assertIsNone(plain[path][1].get('Content-Encoding'), path)
        self.assertEqual('Accept-Encoding',
                         plain['/api/series'][1].get('Vary'))

    def test_static_etag(self):
        """The page's files are not sent again if the browser has them"""
        srv = server.Server(self.cser)
        got = self.get_raw(srv, ['/', '/static/app.js'])
        status, headers, _ = got['/static/app.js']
        self.assertEqual(200, status)
        # Python gives text/javascript or, before 3.12, application/javascript
        self.assertRegex(headers['Content-Type'],
                         r'^(text|application)/javascript; charset=utf-8$')
        etag = headers['ETag']
        self.assertNotEqual(etag, got['/'][1]['ETag'])

        # The file has not changed, so the browser keeps its copy and there
        # is no body, compressed or not
        again = self.get_raw(srv, ['/static/app.js'],
                             {'If-None-Match': etag,
                              'Accept-Encoding': 'gzip'})
        status, headers, body = again['/static/app.js']
        self.assertEqual(304, status)
        self.assertEqual(etag, headers['ETag'])
        self.assertIsNone(headers.get('Content-Encoding'))
        self.assertEqual(b'', body)

        # Another ETag is out of date
        status, _, body = self.get_raw(
            srv, ['/static/app.js'],
            {'If-None-Match': '"old"'})['/static/app.js']
        self.assertEqual(200, status)
        self.assertIn(b'function route()', body)

        # A file which is not there, or not in the page's directory. Send the
        # paths as they are, since the client would otherwise remove the '..'
        got = self.get_raw(srv, [
            '/static/missing.js',
            yarl.URL('/static/%2e%2e', encoded=True),
            yarl.URL('/static/..%2fserver%2fapp.py', encoded=True),
            yarl.URL('/static/%2e%2e%2fserver%2fapp.py', encoded=True)])
        for path, (status, _, _) in got.items():
            self.assertEqual(404, status, path)

    def test_sync_cmds(self):
        """The sync never gathers tags into branches"""
        srv = server.Server(self.cser)
        patman = [interpreter.get_python(), '-m', 'patman', '-v']
        self.assertEqual([patman + ['series', 'gather-all', '-G']],
                         srv.get_sync_cmds())
        self.assertEqual('patman series gather-all -G',
                         srv._cmd_label(srv.get_sync_cmds()[0]))
        self.assertEqual('sh -c true', srv._cmd_label(['sh', '-c', 'true']))
        srv = server.Server(self.cser, review_sync=True)
        self.assertEqual([patman + ['series', 'gather-all', '-G'],
                          patman + ['review', '--sync']],
                         srv.get_sync_cmds())

    def test_sync_fetch(self):
        """The sync fetches the upstreams which reviews use"""
        db = self.cser.db
        db.upstream_add('us', 'https://us', 'https://pw.us')
        db.upstream_set_default('us')
        db.commit()
        self.add_series('rev', 'Review', {1: []}, ups='other',
                        source='review')
        self.add_series('rev2', 'Review', {1: []}, ups='ci', source='review')
        self.add_series('mine', 'Mine', {1: []}, ups='mine')
        srv = server.Server(self.cser)

        # Without the remotes, there is nothing to fetch
        self.assertIsNone(srv.get_fetch_cmd())

        subprocess.run(['git', 'init', '-q'], cwd=self.tmpdir, check=True)
        for name in ['us', 'other', 'mine']:
            subprocess.run(['git', 'remote', 'add', name, f'https://{name}'],
                           cwd=self.tmpdir, check=True)
        self.assertEqual(['git', 'fetch', '-q', '--multiple', 'other', 'us'],
                         srv.get_sync_cmds()[0])

    def test_sync_traffic(self):
        """The sync state shows the traffic to each patchwork server"""
        srv = self.make_server()
        traffic.take()
        traffic.add('https://pw', requests=2, nbytes=500)

        # The server writes out the counts after handling a request
        self.call(srv, 'GET', '/api/version')
        self.assertEqual({}, traffic.take())
        _, state = self.call(srv, 'GET', '/api/sync')
        none = {'hour': [0, 0], 'today': [0, 0], 'week': [0, 0]}
        self.assertEqual([{'url': 'https://pw', 'hour': [2, 500],
                           'today': [2, 500], 'week': [2, 500],
                           'cached': none, 'kinds': {}}],
                         state['traffic'])

        # Older counts only appear in the longer periods
        now = datetime.now()
        self.cser.db.traffic_add(traffic.hour_key(now - timedelta(days=2)),
                                 {'https://pw': (10, 1000)})
        self.assertEqual(
            [{'url': 'https://pw', 'hour': [2, 500], 'today': [2, 500],
              'week': [12, 1500], 'cached': none, 'kinds': {}}],
            traffic.get_summary(self.cser.db, now))

        # Reads which the cache answers are counted too, including for a
        # server which is not otherwise read
        cache = pwcache.Cache(os.path.join(self.tmpdir, 'pwcache.db'))
        cache.add_hit('https://pw', 100)
        cache.add_hit('https://pw', 50)
        cache.add_hit('https://other', 7)
        self.assertEqual(
            [{'url': 'https://other', 'hour': [0, 0], 'today': [0, 0],
              'week': [0, 0],
              'cached': {'hour': [1, 7], 'today': [1, 7], 'week': [1, 7]},
              'kinds': {}},
             {'url': 'https://pw', 'hour': [2, 500], 'today': [2, 500],
              'week': [12, 1500],
              'cached': {'hour': [2, 150], 'today': [2, 150],
                         'week': [2, 150]}, 'kinds': {}}],
            traffic.get_summary(self.cser.db, now, cache))

        # The requests of each kind are listed, leaving out those counted
        # before kinds were recorded
        self.cser.db.traffic_add(traffic.hour_key(now), {
            ('https://pw', 'patch'): (2, 150),
            ('https://pw', 'patch-comments'): (1, 20)})
        summary = traffic.get_summary(self.cser.db, now)
        self.assertEqual(([5, 670], [15, 1670]),
                         (summary[0]['today'], summary[0]['week']))
        self.assertEqual(
            {'patch': {'today': [2, 150], 'week': [2, 150]},
             'patch-comments': {'today': [1, 20], 'week': [1, 20]}},
            summary[0]['kinds'])

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

    def test_params(self):
        """Fields of a request are checked"""
        prm = server.params

        def error(func, *args):
            with self.assertRaises(prm.BadRequest) as exc:
                func(*args)
            return str(exc.exception)

        self.assertEqual(('fix it', None, None),
                         (prm.get_str({'note': ' fix it '}, 'note'),
                          prm.get_str({'note': ' '}, 'note'),
                          prm.get_str({}, 'note')))
        self.assertEqual('Invalid note',
                         error(prm.get_str, {'note': 3}, 'note'))
        self.assertEqual('Say what', error(prm.get_str, {'note': ''}, 'note',
                                           'Say what'))

        self.assertFalse(prm.get_bool({'hidden': False}, 'hidden', 'Oops'))
        for val in (None, 0, 'yes'):
            self.assertEqual('Oops', error(prm.get_bool, {'hidden': val},
                                           'hidden', 'Oops'))

        self.assertEqual((0, 3), (prm.get_seq({'seq': 0}),
                                  prm.get_seq({'seq': 3})))
        for val in (None, -1, True, '1'):
            self.assertEqual(f"Invalid seq '{val}'",
                             error(prm.get_seq, {'seq': val}))

        self.assertEqual(([], [0, 2]), (prm.get_seqs({}),
                                        prm.get_seqs({'seqs': [0, 2]})))
        for val in ('1', [1, -1], [True], [None]):
            self.assertEqual('Invalid seqs',
                             error(prm.get_seqs, {'seqs': val}))

    def test_parse_review_req(self):
        """Review requests are turned into 'patman review' arguments"""
        ups = ['us', 'ci']
        parse = server.params.parse_review_req
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

        # A series on lore is known by its Message-ID
        for target in ('<c@x>', 'https://lore.kernel.org/dtc/c@x/'):
            self.assertEqual((['-s', 'c@x'], 'Review series c@x'),
                             parse({'target': target}, ups))
        self.assertEqual(
            (['-p', '2000', '-w'], 'Review series of patch 2000'),
            parse({'target': 'https://pw/project/uboot/patch/2000/',
                   'whole': True}, ups))
        self.assertEqual((['-p', '2001'], 'Review patch 2001'),
                         parse({'patch': 2001}, ups))
        self.assertEqual((['-S', 'video: Fix', '-V', '3'],
                          "Review 'video: Fix'"),
                         parse({'title': 'video: Fix', 'version': '3'}, ups))

        # Notes for the agent are passed on, unless empty
        self.assertEqual(['-s', '1', '-i', '0', '-c', 'An RFC; ignore style'],
                         parse({'target': '1', 'index': '0',
                                'context': ' An RFC; ignore style\n'}, ups)[0])
        self.assertEqual(['-s', '1'], parse({'target': '1', 'context': ' '},
                                            ups)[0])

        for data, msg in (
                ({}, 'Please give a series'),
                ({'target': 'fred'}, 'Cannot find a series'),
                ({'target': '1', 'title': 'x'}, 'only one of'),
                ({'target': '1', 'index': '1;rm'}, 'Invalid patch selection'),
                ({'target': '1', 'ups': 'zz'}, "Unknown upstream 'zz'"),
                ({'patch': 'x'}, "Invalid patch 'x'"),
                ({'patch': True}, "Invalid patch 'True'"),
                ({'target': '1', 'context': 3}, 'Invalid context')):
            with self.assertRaises(ValueError) as exc:
                parse(data, ups)
            self.assertIn(msg, str(exc.exception))

    def run_jobs(self, srv, calls, each=False):
        """Make requests to a server, then wait for its jobs to finish

        Args:
            srv (Server): Server to use
            calls (list of tuple): (method, path, json) for each request
            each (bool): True to wait for the jobs after each request, so
                that each one is free to act on the reviews

        Return:
            list of tuple: (status, data) for each request
        """
        async def _wait():
            while any(job.active for job in srv.get_jobs()):
                await asyncio.sleep(0.01)

        async def _go():
            out = []
            async with TestClient(TestServer(srv.make_app())) as client:
                for method, path, data in calls:
                    resp = await client.request(method, path, json=data)
                    out.append((resp.status, await resp.json()))
                    if each:
                        await _wait()
                await _wait()
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
        self.assertEqual([202, 202, 202, 400], [status for status, _ in res])
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

    def test_hide_review(self):
        """A reviewed series can be hidden from the list, and shown again"""
        series_id, _ = self.add_series('rev', 'Somebody else',
                                       {1: [('a', 'new', 5, 0)]},
                                       source='review')
        srv = self.make_server()
        path = f'/api/reviews/{series_id}/hide'
        status, data = self.call(srv, 'POST', path, json={'hidden': True})
        self.assertEqual((200, {'id': series_id, 'hidden': True}),
                         (status, data))
        _, data = self.call(srv, 'GET', '/api/reviews')
        self.assertTrue(data[0]['hidden'])
        _, data = self.call(srv, 'GET', f'/api/reviews/{series_id}')
        self.assertTrue(data['hidden'])

        self.call(srv, 'POST', path, json={'hidden': False})
        _, data = self.call(srv, 'GET', '/api/reviews')
        self.assertFalse(data[0]['hidden'])

        status, data = self.call(srv, 'POST', path, json={'hidden': 'yes'})
        self.assertEqual(400, status)
        self.assertIn("whether it is 'hidden'", data['error'])
        status, _ = self.call(srv, 'POST', '/api/reviews/999/hide',
                              json={'hidden': True})
        self.assertEqual(404, status)

    def test_delete_review(self):
        """Reviews can be deleted from the page, as a job"""
        series_id, svids = self.add_series(
            'rev', 'Somebody else', {1: [('a', 'new', 5, 0)]}, ups='us',
            source='review')
        self.cser.db.review_add(svids[1], 1, 'Nit', False,
                                '2026-10-01 10:00:00')
        self.cser.db.commit()
        srv = self.make_server()
        srv.get_patman_cmd = lambda *args: ['echo', *args]
        path = f'/api/reviews/{series_id}/delete'
        res = self.run_jobs(srv, [('POST', path, {'seqs': [1]}),
                                  ('POST', path, {})])
        self.assertEqual([202, 400], [status for status, _ in res])
        self.assertEqual("Delete patch 1 of 'Somebody else' v1",
                         res[0][1]['title'])
        self.assertIn('say which reviews to delete', res[1][1]['error'])
        _, jobs = self.call(srv, 'GET', '/api/jobs')
        self.assertEqual('review -s 100 --delete -i 1 -U us',
                         jobs[0]['results'][0]['output'].strip())

    def test_job_series(self):
        """Jobs give the reviewed series which they act on, and its name"""
        series_id, _ = self.add_series('rev', 'Somebody else',
                                       {1: [('a', 'new', 5, 0)]}, ups='us',
                                       source='review')
        srv = self.make_server()
        srv.get_patman_cmd = lambda *args: ['echo', *args]
        res = self.run_jobs(srv, [
            ('POST', '/api/review', {'target': '100'}),
            ('POST', '/api/review', {'target': '999', 'name': ' New one '}),
            ('POST', '/api/review', {'target': '998'}),
            ('POST', f'/api/reviews/{series_id}/redraft', {}),
            ('POST', '/api/review/scan', {}),
        ], each=True)
        self.assertEqual([202] * 5, [status for status, _ in res])
        _, jobs = self.call(srv, 'GET', '/api/jobs')
        self.assertEqual(
            [(series_id, 'Somebody else'), (None, 'New one'), (None, None),
             (series_id, 'Somebody else'), (None, None)],
            [(job['review'], job['name']) for job in reversed(jobs)])
        _, job = self.call(srv, 'GET', '/api/jobs/1')
        self.assertEqual((series_id, 'Somebody else'),
                         (job['review'], job['name']))

    def test_send_reviews(self):
        """The drafts of one review, or all of them, can be sent"""
        series_id, _ = self.add_series('rev', 'Somebody else',
                                       {1: [('a', 'new', 5, 0)]}, ups='us',
                                       source='review')
        srv = self.make_server()
        srv.get_patman_cmd = lambda *args: ['echo', *args]
        path = f'/api/reviews/{series_id}/send'
        res = self.run_jobs(srv, [
            ('POST', path, {'seqs': [0]}),
            ('POST', path, {}),
            ('POST', path, {'seqs': 'all'}),
        ], each=True)
        self.assertEqual([202, 202, 400], [status for status, _ in res])
        self.assertEqual(["Send the cover letter of 'Somebody else' v1",
                          "Send reviews of 'Somebody else' v1"],
                         [res[0][1]['title'], res[1][1]['title']])
        self.assertEqual(
            ['review -s 100 --send -i 0 -U us', 'review -s 100 --send -U us'],
            [job.state['results'][0]['output'].strip()
             for job in reversed(srv.get_jobs())])

    def test_revise_review(self):
        """The agent can be asked to change a review, part of one, or all"""
        series_id, _ = self.add_series('rev', 'Somebody else',
                                       {1: [('a', 'new', 5, 0)]}, ups='us',
                                       source='review')
        srv = self.make_server()
        srv.get_patman_cmd = lambda *args: ['echo', *args]
        path = f'/api/reviews/{series_id}/revise'
        res = self.run_jobs(srv, [
            ('POST', path, {'seqs': [1], 'note': ' Drop this ',
                            'part': '> code\nNit', 'drafts': True}),
            ('POST', path, {'note': 'Approve them all'}),
            ('POST', path, {'note': ' '}),
            ('POST', path, {'note': 'x', 'seqs': ['1']}),
        ], each=True)
        self.assertEqual([202, 202, 400, 400], [status for status, _ in res])
        self.assertEqual("Revise patch 1 of 'Somebody else' v1",
                         res[0][1]['title'])
        self.assertEqual("Revise reviews of 'Somebody else' v1",
                         res[1][1]['title'])
        self.assertIn('Please say what to change', res[2][1]['error'])
        self.assertIn('Invalid seqs', res[3][1]['error'])
        self.assertEqual(
            ['review -s 100 --revise About this part of the review:\n\n'
             '> code\nNit\n\nDrop this -i 1 -U us -d',
             'review -s 100 --revise Approve them all -U us'],
            [job.state['results'][0]['output'].strip()
             for job in reversed(srv.get_jobs())])

    def test_review_lock(self):
        """A review cannot be changed while a job is acting on it"""
        series_id, svids = self.add_series(
            'rev', 'Somebody else', {1: [('a', 'new', 5, 0)]}, ups='us',
            source='review')
        db = self.cser.db
        cover = db.review_add(svids[1], 0, 'Cover', False,
                              '2026-10-01 10:00:00')
        rid = db.review_add(svids[1], 1, 'Looks good', True,
                            '2026-10-01 10:00:00')
        db.commit()
        srv = self.make_server()
        flag = os.path.join(self.tmpdir, 'flag')
        srv.get_patman_cmd = lambda *args: [
            'sh', '-c', f'while [ ! -e {flag} ]; do sleep 0.05; done']
        path = f'/api/reviews/{series_id}'

        async def _go():
            out = {}
            async with TestClient(TestServer(srv.make_app())) as client:
                async def call(method, url, data=None):
                    resp = await client.request(method, url, json=data)
                    return resp.status, await resp.json()

                # Revise patch 1, which locks just its review
                status, job = await call('POST', f'{path}/revise',
                                         {'seqs': [1], 'note': 'Fix it'})
                out['started'] = status, job['target']
                _, data = await call('GET', path)
                out['marked'] = [rev['job'] for rev in
                                 data['versions'][0]['reviews']]
                out['again'] = (await call('POST', f'{path}/redraft',
                                           {'seqs': [1]}))[0]
                out['all'] = (await call('POST', f'{path}/send', {}))[0]
                out['edit'] = await call('POST', f'/api/review-items/{rid}',
                                         {'body': 'Changed'})
                out['other'] = (await call('POST',
                                           f'/api/review-items/{cover}',
                                           {'body': 'New cover'}))[0]

                # Once the job finishes, the review can be changed again
                tools.write_file(flag, b'')
                while any(job.active for job in srv.get_jobs()):
                    await asyncio.sleep(0.01)
                _, data = await call('GET', path)
                out['after'] = [rev['job'] for rev in
                                data['versions'][0]['reviews']]
                out['free'] = (await call('POST', f'/api/review-items/{rid}',
                                          {'body': 'Changed'}))[0]
            return out

        loop = asyncio.new_event_loop()
        try:
            out = loop.run_until_complete(_go())
        finally:
            loop.close()
        self.assertEqual((202, {'series': series_id, 'version': 1,
                                'seqs': [1]}), out['started'])
        self.assertEqual([None, 1], out['marked'])
        self.assertEqual(409, out['again'])
        self.assertEqual(409, out['all'])
        self.assertEqual(409, out['edit'][0])
        self.assertIn('already acting on this review', out['edit'][1]['error'])
        self.assertEqual(200, out['other'])
        self.assertEqual([None, None], out['after'])
        self.assertEqual(200, out['free'])
        self.assertEqual('Changed', db.review_get(rid).body)

    def test_edit_review(self):
        """A review which has not been sent can be changed and redrafted"""
        series_id, svids = self.add_series(
            'rev', 'Somebody else', {1: [('a', 'new', 5, 0)]}, ups='us',
            source='review')
        db = self.cser.db
        rid = db.review_add(svids[1], 1, 'Looks good', True,
                            '2026-10-01 10:00:00')
        db.review_set_draft_id(rid, 'draft1')
        sent = db.review_add(svids[1], 0, 'Cover', False,
                             '2026-10-01 10:00:00')
        db.review_set_sent(sent, 'Cover')
        db.commit()

        srv = self.make_server()
        srv.get_patman_cmd = lambda *args: ['echo', *args]
        res = self.run_jobs(srv, [
            ('POST', f'/api/review-items/{rid}',
             {'body': 'Please fix', 'approved': False}),
            ('POST', f'/api/review-items/{rid}',
             {'body': 'Please fix this', 'redraft': True}),
            ('POST', f'/api/review-items/{rid}',
             {'body': 'Fine\n\nReviewed-by: Me <me@b.com>'}),
            ('POST', f'/api/review-items/{sent}', {'body': 'x'}),
            ('POST', f'/api/review-items/{rid}', {'body': ' '}),
            ('POST', '/api/review-items/999', {'body': 'x'}),
        ], each=True)
        self.assertEqual([200, 202, 200, 409, 400, 404],
                         [status for status, _ in res])
        self.assertTrue(res[2][1]['approved'])
        res = res[:2] + res[3:]
        self.assertEqual(('Please fix', False),
                         (res[0][1]['body'], res[0][1]['approved']))

        # Its Gmail draft is now out of date
        self.assertEqual('edited', res[0][1]['status'])
        self.assertIn('already been sent', res[2][1]['error'])
        self.assertIn('cannot be empty', res[3][1]['error'])
        rev = db.review_get(rid)
        self.assertEqual(1, rev.approved)
        self.assertEqual(['review -s 100 --redraft -i 1 -U us'],
                         [job.state['results'][0]['output'].strip()
                          for job in srv.get_jobs()])
        self.assertEqual(series_id, db.ser_ver_get_by_id(svids[1]).series_id)

    def test_respond_comment(self):
        """The agent can be asked to respond to a comment on patchwork"""
        series_id, svids = self.add_series(
            'rev', 'Somebody else', {1: [('a', 'new', 5, 0)]}, ups='us',
            source='review')
        db = self.cser.db
        rid = db.reply_add_comment(
            svids[1], 0, 77, '<c1@x>', '<cover@x>', 'Alex <a@x>', 'f@x', '',
            'Re: [PATCH 0/1] a', 'today', 'Why?', '2026-10-02 10:00:00')
        db.commit()

        srv = self.make_server()
        srv.get_patman_cmd = lambda *args: ['echo', *args]
        _, data = self.call(srv, 'GET', '/api/reviews')
        self.assertEqual(1, data[0]['replies_waiting'])
        _, data = self.call(srv, 'GET', f'/api/reviews/{series_id}')
        self.assertEqual(
            [(rid, 0, 77, 'Alex <a@x>', 'new')],
            [(c['id'], c['seq'], c['comment_id'], c['sender'], c['status'])
             for c in data['versions'][0]['comments']])

        path = f'/api/reviews/{series_id}/respond'
        res = self.run_jobs(srv, [
            ('POST', path, {'seq': 0, 'comment': 77, 'note': ' Say X '}),
            ('POST', path, {'seq': 1, 'comment': 78, 'version': 1}),
            ('POST', f'/api/replies/{rid}/suggest', {}),
            ('POST', path, {'seq': -1, 'comment': 77}),
            ('POST', path, {'seq': True, 'comment': 77}),
            ('POST', path, {'seq': 0}),
            ('POST', path, {'seq': 0, 'comment': 77, 'note': 3}),
            ('POST', '/api/reviews/999/respond', {'seq': 0, 'comment': 77}),
        ])
        self.assertEqual([202, 202, 202, 400, 400, 400, 400, 404],
                         [status for status, _ in res])
        self.assertEqual("Respond to a comment on the cover letter of "
                         "'Somebody else' v1", res[0][1]['title'])
        self.assertEqual("Respond to a comment on patch 1 of 'Somebody else' "
                         'v1', res[1][1]['title'])
        self.assertIn('Please give the comment', res[5][1]['error'])

        # Asking for a new suggestion reads the comment again
        _, jobs = self.call(srv, 'GET', '/api/jobs')
        self.assertEqual(
            ['review -s 100 --respond-comment 77 -i 0 -U us -c Say X',
             'review -s 100 --respond-comment 78 -i 1 -U us',
             'review -s 100 --respond-comment 77 -i 0 -U us'],
            [job['results'][0]['output'].strip() for job in reversed(jobs)])

    def test_replies(self):
        """Replies to reviews can be seen, and responses changed and drafted"""
        series_id, svids = self.add_series(
            'rev', 'Somebody else', {1: [('a', 'new', 5, 0)]}, ups='us',
            source='review')
        db = self.cser.db
        rid = db.review_add(svids[1], 1, 'Please fix', False,
                            '2026-10-01 10:00:00')
        db.review_set_sent(rid, 'Please fix', 'm1', 't1')
        first = db.reply_add(rid, 'm2', '<a2@x>', '<r1@me>', 'Fred <f@x>',
                             'me@x', '', 'Re: [PATCH] a', 'today', 'Why?',
                             '2026-10-02 10:00:00')
        second = db.reply_add(rid, 'm3', '<a3@x>', '<r1@me>', 'Tom <t@x>',
                              'me@x', '', 'Re: [PATCH] a', 'today', 'OK',
                              '2026-10-02 11:00:00')
        db.reply_set_response(second, 'Thanks', database.REPLY_SENT)
        db.commit()

        srv = self.make_server()
        srv.get_patman_cmd = lambda *args: ['echo', *args]
        _, data = self.call(srv, 'GET', '/api/reviews')
        self.assertEqual(1, data[0]['replies_waiting'])
        _, data = self.call(srv, 'GET', f'/api/reviews/{series_id}')
        replies = data['versions'][0]['reviews'][0]['replies']
        self.assertEqual(
            {'id': first, 'sender': 'Fred <f@x>', 'subject': 'Re: [PATCH] a',
             'date': 'today', 'body': 'Why?', 'response': None,
             'status': 'new', 'timestamp': '2026-10-02 10:00:00',
             'seq': None, 'comment_id': None},
            replies[0])
        self.assertEqual(('sent', 'Thanks'),
                         (replies[1]['status'], replies[1]['response']))

        res = self.run_jobs(srv, [
            ('POST', f'/api/replies/{first}', {'response': 'Because'}),
            ('POST', f'/api/replies/{first}',
             {'response': 'Because of this', 'draft': True}),
            ('POST', f'/api/replies/{first}/suggest', None),
            ('POST', f'/api/replies/{first}/send', None),
            ('POST', f'/api/replies/{second}', {'response': 'x'}),
            ('POST', f'/api/replies/{second}/send', None),
            ('POST', '/api/replies/999', {'skip': True}),
        ])
        self.assertEqual([200, 202, 202, 202, 409, 409, 404],
                         [status for status, _ in res])
        self.assertEqual(('suggested', 'Because'),
                         (res[0][1]['status'], res[0][1]['response']))
        self.assertEqual(
            [f'review --draft-reply {first}', f'review --suggest-reply {first}',
             f'review --send-reply {first}'],
            [job.state['results'][0]['output'].strip()
             for job in reversed(srv.get_jobs())])
        self.assertEqual('Because of this', db.reply_get(first).response)

        _, data = self.call(srv, 'POST', f'/api/replies/{first}',
                            json={'skip': True})
        self.assertEqual('skip', data['status'])
        _, data = self.call(srv, 'GET', '/api/reviews')
        self.assertEqual(0, data[0]['replies_waiting'])

    def test_parallel_reviews(self):
        """Reviews of series run alongside each other, a few at once"""
        flag = os.path.join(self.tmpdir, 'flag')

        async def _go():
            async with TestClient(TestServer(srv.make_app())) as client:
                for target in ('1', '2', '3'):
                    await client.post('/api/review', json={'target': target})
                await client.post('/api/review/scan', json={})
                first, second, third, scan = (srv.get_job(num)
                                              for num in (1, 2, 3, 4))

                # Two reviews run at once, along with the scan
                while not (first.state['running'] and
                           second.state['running'] and
                           scan.state['running']):
                    await asyncio.sleep(0.01)
                waiting = third.state['queued']
                tools.write_file(flag, b'')
                while any(job.active for job in srv.get_jobs()):
                    await asyncio.sleep(0.01)
                return waiting

        srv = self.make_server(max_parallel=2)
        srv.get_patman_cmd = lambda *args: [
            'sh', '-c', f'echo started; while [ ! -e {flag} ]; do sleep 0.01; '
                        'done']
        loop = asyncio.new_event_loop()
        try:
            self.assertTrue(loop.run_until_complete(_go()))
        finally:
            loop.close()
        self.assertEqual([True] * 4, [job.state['results'][0]['returncode'] == 0
                                      for job in srv.get_jobs()])

    def test_cancel_queued_job(self):
        """A job which is waiting for another can be cancelled"""
        srv = self.make_server()
        cmds = iter([['sh', '-c', 'echo busy; sleep 10'], ['echo', 'never']])
        srv.get_patman_cmd = lambda *args: next(cmds)

        async def _go():
            async with TestClient(TestServer(srv.make_app())) as client:
                # Scans run one at a time, unlike reviews
                await client.post('/api/review/scan', json={})
                await client.post('/api/review/scan', json={})
                first, second = srv.get_job(1), srv.get_job(2)
                while not first.state['results'] or \
                        not first.state['results'][0]['output']:
                    await asyncio.sleep(0.01)
                queued = second.state['queued']
                # The tasks running the jobs are kept until they finish
                running = len(srv._tasks)
                await client.post('/api/jobs/2/cancel')
                await client.post('/api/jobs/1/cancel')
                while first.active or second.active:
                    await asyncio.sleep(0.01)
                for _ in range(100):
                    if not srv._tasks:
                        break
                    await asyncio.sleep(0.01)
                return queued, running, len(srv._tasks)

        loop = asyncio.new_event_loop()
        try:
            self.assertEqual((True, 2, 0), loop.run_until_complete(_go()))
        finally:
            loop.close()
        self.assertTrue(srv.get_job(1).state['cancelled'])
        second = srv.get_job(2).state
        self.assertTrue(second['cancelled'])
        self.assertEqual([], second['results'])

    def test_resume_jobs(self):
        """Jobs are kept when the server restarts, and unfinished ones resumed"""
        fname = os.path.join(self.tmpdir, 'jobs.json')
        flag = os.path.join(self.tmpdir, 'flag')
        srv = self.make_server(jobs_fname=fname)
        cmds = iter([
            ['sh', '-c', f'echo busy; [ -e {flag} ] || sleep 10'],
            ['sh', '-c', 'echo cancel; sleep 10'],
            ['echo', 'done']])
        srv.get_patman_cmd = lambda *args: next(cmds)

        def read_jobs():
            with open(fname, encoding='utf-8') as inf:
                return json.load(inf)

        async def wait_output(job):
            while not job.state['results'] or \
                    not job.state['results'][0]['output']:
                await asyncio.sleep(0.01)

        async def _go():
            async with TestClient(TestServer(srv.make_app())) as client:
                for target in '123':
                    await client.post('/api/review', json={'target': target})
                await wait_output(srv.get_job(1))
                saved = [item['state']['title']
                         for item in read_jobs()['jobs']]

                # Stop the first job as if the server were stopping, then
                # cancel the second
                os.killpg(srv.get_job(1).proc.pid,
                          signal.SIGTERM)
                await wait_output(srv.get_job(2))
                await client.post('/api/jobs/2/cancel')
                while any(job.active for job in srv.get_jobs()):
                    await asyncio.sleep(0.01)
                return saved

        loop = asyncio.new_event_loop()
        try:
            with terminal.capture():
                saved = loop.run_until_complete(_go())
        finally:
            loop.close()
        self.assertEqual(['Review series 1', 'Review series 2',
                          'Review series 3'], saved)
        data = read_jobs()
        self.assertEqual(3, data['last'])
        # The first was stopped part-way, the second cancelled and the third
        # finished
        first, second, third = (item['state'] for item in data['jobs'])
        self.assertEqual((1, False), (first['id'], first['cancelled']))
        self.assertLess(first['results'][0]['returncode'], 0)
        self.assertEqual((2, True), (second['id'], second['cancelled']))
        self.assertEqual((3, 0), (third['id'],
                                  third['results'][0]['returncode']))
        self.assertEqual(
            [['sh', '-c', f'echo busy; [ -e {flag} ] || sleep 10']],
            data['jobs'][0]['cmds'])

        # A new server keeps the jobs, running the stopped one again with
        # its number, and carries on the numbering
        with open(flag, 'w', encoding='utf-8'):
            pass
        srv = self.make_server(jobs_fname=fname)
        srv.get_patman_cmd = lambda *args: ['echo', 'new']
        with terminal.capture() as (out, _):
            res = self.run_jobs(srv, [('POST', '/api/review',
                                       {'target': '4'})])
        self.assertIn("Resuming job 'Review series 1'", out.getvalue())
        self.assertNotIn('series 2', out.getvalue())
        self.assertEqual(4, res[0][1]['id'])
        self.assertEqual([4, 3, 2, 1], [job.idnum for job in srv.get_jobs()])
        job = srv.get_job(1).state
        self.assertEqual((1, 0, 'busy\n'),
                         (job['resumed'], job['results'][0]['returncode'],
                          job['results'][0]['output']))
        self.assertTrue(srv.get_job(2).state['cancelled'])
        self.assertEqual('done\n', srv.get_job(3).state['results'][0]['output'])
        self.assertEqual(4, read_jobs()['last'])

        # A job which has gone is described
        status, data = self.call(srv, 'GET', '/api/jobs/99')
        self.assertEqual((404, 'No job 99'), (status, data['error']))

        # A file from before the jobs were kept still has its jobs resumed
        with open(fname, 'w', encoding='utf-8') as outf:
            json.dump([{'kind': 'review', 'title': 'Old', 'cmds': [['true']],
                        'resumed': 0}], outf)
        srv = self.make_server(jobs_fname=fname)
        with terminal.capture() as (out, _):
            self.run_jobs(srv, [])
        self.assertIn("Resuming job 'Old'", out.getvalue())
        self.assertEqual([(1, 'Old', 1)],
                         [(job.idnum, job.state['title'], job.state['resumed'])
                          for job in srv.get_jobs()])

        # A job is not resumed too often, and a sync is not resumed at all,
        # though both are kept
        unfinished = {'queued': False, 'running': True, 'cancelled': False,
                      'started': '2026-10-19 10:00:00', 'finished': None,
                      'results': [], 'target': None}
        with open(fname, 'w', encoding='utf-8') as outf:
            json.dump({'last': 7, 'jobs': [
                {'cmds': [['false']], 'parallel': False,
                 'state': dict(unfinished, id=6, kind='review', title='Bad',
                               resumed=server.jobs.MAX_RESUMES)},
                {'cmds': [['false']], 'parallel': False,
                 'state': dict(unfinished, id=7, kind='sync', title='Sync',
                               resumed=0)}]}, outf)
        srv = self.make_server(jobs_fname=fname)
        with terminal.capture() as (out, err):
            self.run_jobs(srv, [])
        self.assertIn("Not resuming job 'Bad' again", err.getvalue())
        self.assertNotIn('Resuming', out.getvalue())
        self.assertEqual([(7, False), (6, False)],
                         [(job.idnum, job.active) for job in srv.get_jobs()])

    def test_inbox(self):
        """The inbox lists recent series, marking those already reviewed"""
        rev_id, svids = self.add_series(
            'rev', 'video: Add a driver',
            {2: [('a', 'new', 6, 0), ('b', 'new', 8, 0)]}, source='review')
        self.add_series('mine', 'My series', {1: [('x', 'new', 7, 0)]})
        db = self.cser.db

        # Someone has replied to the review of v2's first patch; the cover
        # letter is approved but not drafted, and the second patch has no
        # review
        rid = db.review_add(svids[2], 1, 'Please fix', False,
                            '2026-10-01 10:00:00')
        db.review_add(svids[2], 0, 'Looks good', True, '2026-10-01 09:00:00')
        db.review_set_sent(rid, 'Please fix', 'm1', 't1')
        db.reply_add(rid, 'm2', '<a2@x>', '<r1@me>', 'Fred <f@x>', 'me@x', '',
                     'Re: [PATCH] a', 'today', 'Why?', '2026-10-02 10:00:00')
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
        self.assertEqual(('us', 1, False),
                         (data['ups'], data['page'], data['more']))
        items = {item['id']: item for item in data['series']}
        self.assertEqual([300, 200, 100, 400], list(items))

        # A new version of a reviewed series is linked to the review, with
        # the version reviewed, its parts and the replies waiting
        part = {'approved': False, 'status': None, 'replies': 0}
        self.assertEqual((None, {
            'id': rev_id, 'version': 2, 'link': '200',
            'date': '2026-10-01 10:00:00', 'replies': 1, 'parts': [
                {**part, 'seq': 0, 'name': 'Cover letter', 'mail_id': None,
                 'approved': True, 'status': 'new'},
                {**part, 'seq': 1, 'name': 'a', 'mail_id': 6, 'status': 'sent',
                 'replies': 1},
                {**part, 'seq': 2, 'name': 'b', 'mail_id': 8}]}, False),
                         (items[300]['reviewed'], items[300]['earlier'],
                          items[300]['mine']))
        self.assertEqual((rev_id, None), (items[200]['reviewed'],
                                          items[200]['earlier']))

        # The reply is waiting for your response
        self.assertEqual({'state': 'me',
                          'why': '1 reply waiting for your response'},
                         items[200]['next'])
        self.assertEqual([None, None, None],
                         [items[idnum]['next'] for idnum in (300, 100, 400)])
        self.assertTrue(items[100]['mine'])
        self.assertEqual((None, None, False),
                         (items[400]['reviewed'], items[400]['earlier'],
                          items[400]['mine']))
        self.assertEqual('Fred', items[400]['submitter'])
        self.assertEqual([{'id': 4000, 'name': 'net: Something new',
                           'commit': None, 'branch': None, 'state': None}],
                         items[400]['patches'])

        # A series sent from one of our own addresses is ours too
        srv.own_emails = {'f@b.com'}
        _, data = self.call(srv, 'GET', '/api/inbox')
        self.assertEqual({300: True, 200: True, 100: True, 400: True},
                         {item['id']: item['mine'] for item in data['series']})
        srv.own_emails = set()

        status, data = self.call(srv, 'GET', '/api/inbox?ups=xx')
        self.assertEqual(404, status)
        self.assertIn("No patchwork project for upstream 'xx'", data['error'])

        # A series being reviewed shows its job, but not a series whose
        # reviews are being redrafted, nor one whose review has finished
        reviewing = srv._new_job('review', 'Review series 400',
                                 [['patman', 'review', '-s', '400', '-d']])
        srv._new_job('review', 'Redraft', [['patman', 'review', '-s', '300',
                                            '--redraft']])
        done = srv._new_job('review', 'Review series 100',
                            [['patman', 'review', '-s', '100']])
        done.state['finished'] = '2026-10-01 10:00:00'
        _, data = self.call(srv, 'GET', '/api/inbox')
        self.assertEqual({300: None, 200: None, 100: None,
                          400: reviewing.idnum},
                         {item['id']: item['job'] for item in data['series']})

        # A second review of it is refused
        status, data = self.call(srv, 'POST', '/api/review',
                                 json={'target': '400'})
        self.assertEqual(409, status)
        self.assertEqual(
            f'Series 400 is already being reviewed, by job {reviewing.idnum}',
            data['error'])

    def test_next_move(self):
        """Who has the next move on a series you reviewed"""
        now = datetime(2026, 11, 1)

        def part(seq, status, approved=False, replies=0):
            return {'seq': seq, 'status': status, 'approved': approved,
                    'replies': replies}

        def check(parts, sent='2026-10-30 10:00:00', newer=False):
            res = server.info.next_move(parts, sent, newer, now)
            return res and (res['state'], res['why'])

        # A reply waiting, or a review not sent, is your move, even with a
        # newer version
        self.assertEqual(('me', '2 replies waiting for your response'),
                         check([part(1, 'sent', replies=1),
                                part(2, 'replied', replies=1)], newer=True))
        self.assertEqual(('me', '1 review not sent'),
                         check([part(0, 'new', True), part(1, 'sent')]))
        self.assertEqual(('me', '2 reviews not sent'),
                         check([part(1, 'draft'), part(2, 'edited')]))

        # Every patch with your Reviewed-by is ready; the cover letter does
        # not need one
        self.assertEqual(('ready',
                          'Your reviews give every patch a Reviewed-by tag'),
                         check([part(0, 'sent'), part(1, 'sent', True),
                                part(2, 'replied', True)]))

        # Changes asked for wait on the submitter, until they are stale
        why = ('You asked for changes to 1 patch and there is no new version '
               'yet')
        self.assertEqual(('submitter', why),
                         check([part(1, 'sent'), part(2, 'sent', True)]))
        self.assertEqual(('submitter', why),
                         check([part(1, 'sent')], sent=None))
        self.assertEqual(('stale', f'{why}, 28 days after your review'),
                         check([part(1, 'sent')], sent='2026-10-04 00:00:00'))
        self.assertEqual(('submitter', why.replace('1 patch', '2 patches')),
                         check([part(1, 'sent'), part(2, 'replied')]))

        # A newer version has the next move, and a patch you did not review
        # is not ready, nor waiting for changes
        self.assertIsNone(check([part(1, 'sent')], newer=True))
        self.assertIsNone(check([part(1, 'sent', True), part(2, None)]))
        self.assertIsNone(check([part(1, 'deleted')]))

    def test_inbox_not_reviewed(self):
        """A version applied but not reviewed is not an earlier review"""
        db = self.cser.db
        rev_id, svids = self.add_series(
            'rev', 'video: Add a driver',
            {1: [('a', 'new', 6, 0)], 2: [('a', 'new', 7, 0)]},
            source='review')
        db.review_add(svids[1], 1, 'Please fix', False, '2026-10-01 10:00:00')

        # A series left with no versions by a review with nothing to review
        db.series_set_source(db.series_add('empty', 'net: Add a driver'),
                             'review')
        db.upstream_add('us', 'https://us', 'https://pw.us')
        db.upstream_set_default('us')
        db.patchwork_update('U-Boot', 6, 'uboot', 'us')
        db.commit()

        def series(idnum, name, version):
            return {'id': idnum, 'name': name, 'version': version,
                    'total': 1, 'patches': [{'id': idnum * 10, 'name': name}]}

        pages = {
            'series/?project=uboot&order=-date&page=1&per_page=50': [
                series(300, '[v3] video: Add a driver', 3),
                series(200, '[v2] video: Add a driver', 2),
                series(500, 'net: Add a driver', 1)],
        }
        srv = self.make_server()
        srv.pwork_for_testing.fake_request = pages.get
        _, data = self.call(srv, 'GET', '/api/inbox')
        items = {item['id']: item for item in data['series']}

        # v2 is only applied, so it can be reviewed, and v1 is the review
        # to show for it and for v3
        self.assertEqual((None, 1), (items[200]['reviewed'],
                                     items[200]['earlier']['version']))
        self.assertEqual((rev_id, 1), (items[300]['earlier']['id'],
                                       items[300]['earlier']['version']))
        self.assertEqual((None, None), (items[500]['reviewed'],
                                        items[500]['earlier']))

    def test_inbox_b4(self):
        """An earlier version sent by b4 is found, though its title differs"""
        db = self.cser.db
        rev_id, svids = self.add_series(
            'rev', 'spi: Probe a device', {1: [('a', 'new', 6, 0)]},
            source='review')
        rev = db.review_add(svids[1], 1, 'Please fix', False,
                            '2026-10-01 10:00:00')
        db.review_set_envelope(rev, {
            'in_reply_to': '<20260930-spi-probe-v1-1-abc@iav.lv>'})
        db.commit()
        self.setup_inbox_ups()

        def series(idnum, name, msgid):
            return {'id': idnum, 'name': name, 'version': 2, 'total': 1,
                    'patches': [{'id': idnum * 10, 'name': name,
                                 'msgid': msgid}]}

        pages = {
            'series/?project=uboot&order=-date&page=1&per_page=50': [
                series(300, '[v2] cmd: sf: Probe a flash',
                       '<20261027-spi-probe-v2-1-def@iav.lv>'),
                series(400, '[v2] cmd: sf: Probe a flash',
                       '<20261027-other-v2-1-def@iav.lv>')],
        }
        srv = self.make_server()
        srv.pwork_for_testing.fake_request = pages.get
        _, data = self.call(srv, 'GET', '/api/inbox')
        items = {item['id']: item for item in data['series']}
        self.assertEqual((rev_id, 1), (items[300]['earlier']['id'],
                                       items[300]['earlier']['version']))
        self.assertIsNone(items[400]['earlier'])

    def setup_inbox_ups(self):
        """Add an upstream with a patchwork project, for the inbox"""
        db = self.cser.db
        db.upstream_add('us', 'https://us', 'https://pw.us')
        db.upstream_set_default('us')
        db.patchwork_update('U-Boot', 6, 'uboot', 'us')
        db.commit()

    def test_inbox_added(self):
        """Series added to the inbox come first, or are all it lists"""
        self.setup_inbox_ups()

        def series(idnum, name):
            return {'id': idnum, 'name': name, 'version': 1, 'total': 1,
                    'date': '2026-10-01T10:00:00', 'received_all': True,
                    'patches': [{'id': idnum * 10, 'name': name}]}

        feed = 'series/?project=uboot&order=-date&page=1&per_page=50'
        pages = {
            feed: [series(300, 'video: New'), series(200, 'video: Added')],
            'series/200/': series(200, 'video: Added'),
            'series/150/': series(150, 'net: Old'),
        }
        requests = []

        def fake(subpath):
            requests.append(subpath)
            if subpath not in pages:
                raise ValueError(f"Unexpected request '{subpath}'")
            return pages[subpath]

        srv = self.make_server()
        srv.pwork_for_testing.fake_request = fake

        def add(link, name=None, ups='us'):
            return self.call(srv, 'POST', '/api/inbox/add',
                             json={'ups': ups, 'id': link, 'name': name})

        self.assertEqual((200, {'ups': 'us', 'id': '150'}),
                         add(150, 'net: Old'))
        self.assertEqual(200, add('999', 'Gone')[0])
        self.assertEqual(200, add('200')[0])
        for args, error in (
                (('x@y',), "Series 'x@y' is not on us's patchwork server"),
                (('abc',), "Invalid series 'abc'"),
                (('1', None, 'other'), "Unknown upstream 'other'")):
            self.assertEqual((400, error), (add(*args)[0],
                                            add(*args)[1]['error']))

        # The latest added comes first, and is not listed again in the feed.
        # One which cannot be read keeps its name
        with terminal.capture():
            _, data = self.call(srv, 'GET', '/api/inbox')
        self.assertEqual((True, False), (data['feed'], data['more']))
        self.assertEqual(
            [(200, 'video: Added', True), (999, 'Gone', True),
             (150, 'net: Old', True), (300, 'video: New', False)],
            [(ser['id'], ser['name'], ser['added']) for ser in data['series']])

        # With no feed, only the added series are listed
        self.cser.db.upstream_set('us', no_feed=True)
        self.cser.db.commit()
        requests.clear()
        with terminal.capture():
            _, data = self.call(srv, 'GET', '/api/inbox')
        self.assertEqual((False, False), (data['feed'], data['more']))
        self.assertEqual([200, 999, 150],
                         [ser['id'] for ser in data['series']])
        self.assertNotIn(feed, requests)
        _, data = self.call(srv, 'GET', '/api/inbox?page=2')
        self.assertEqual([], data['series'])

        # A series can be removed, once
        def remove(link):
            return self.call(srv, 'POST', '/api/inbox/remove',
                             json={'ups': 'us', 'id': link})
        self.assertEqual((200, {'ups': 'us', 'id': '999'}), remove('999'))
        self.assertEqual((404, "Series '999' is not in us's inbox"),
                         (remove('999')[0], remove('999')[1]['error']))
        self.assertEqual(['200', '150'],
                         [link for link, *_ in self.cser.db.inbox_get('us')])

    def test_search(self):
        """Series can be searched for, to add them to the inbox"""
        self.setup_inbox_ups()
        self.cser.db.inbox_add('us', '200', 'video: Added',
                               '2026-10-01 10:00:00')
        self.cser.db.commit()
        query = 'project=uboot&q=video&order=-date&per_page=30'
        pages = {
            f'series/?{query}': [
                {'id': 200, 'name': 'video: Added', 'version': 2, 'total': 3,
                 'date': '2026-10-02T10:00:00', 'web_url': 'https://pw/200',
                 'submitter': {'name': 'Fred', 'email': 'f@x'}}],
            f'patches/?{query}': [
                {'id': 1001, 'date': '2026-10-01T10:00:00',
                 'submitter': {'email': 'ann@x'},
                 'series': [{'id': 100, 'name': 'video: Old', 'version': 1}]}],
        }
        srv = self.make_server()
        srv.pwork_for_testing.fake_request = pages.get
        status, data = self.call(srv, 'GET', '/api/search?q=+video+')
        self.assertEqual(200, status)
        self.assertEqual(
            {'ups': 'us', 'q': 'video', 'where': 'uboot on my-fake-url',
             'series': [
                {'id': 200, 'name': 'video: Added', 'version': 2,
                 'date': '2026-10-02T10:00:00', 'submitter': 'Fred',
                 'total': 3, 'web_url': 'https://pw/200', 'added': True},
                {'id': 100, 'name': 'video: Old', 'version': 1,
                 'date': '2026-10-01T10:00:00', 'submitter': 'ann@x',
                 'total': None, 'web_url': None, 'added': False}]},
            data)

        status, data = self.call(srv, 'GET', '/api/search?q=+')
        self.assertEqual((400, 'Please give some text to search for'),
                         (status, data['error']))

    def test_search_lore(self):
        """A list on lore is searched there, and its series added"""
        with terminal.capture():
            self.cser.upstream_add('linux', 'https://linux',
                                   lore_url='https://lore/all', no_feed=True)
        srv = self.make_server()
        srv.pwork_for_testing = None
        found = [{'id': 'c@x', 'name': '[v2] fit: Add images', 'version': 2,
                  'total': 7, 'date': '2026-10-20T15:36:52',
                  'submitter': {'name': 'Doug', 'email': 'd@x'},
                  'web_url': 'https://lore/all/c@x/'}]
        with mock.patch.object(lorelist.LoreList, 'search_series',
                               mock.AsyncMock(return_value=found)) as search:
            _, data = self.call(srv, 'GET', '/api/search?ups=linux&q=fit')
        self.assertEqual(('all', 'fit'), search.call_args.args[1:])
        self.assertEqual('lore/all', data['where'])
        self.assertEqual([('c@x', 'Doug', False)],
                         [(ser['id'], ser['submitter'], ser['added'])
                          for ser in data['series']])

        # A series is added by its Message-ID, or its URL on lore
        status, data = self.call(srv, 'POST', '/api/inbox/add', json={
            'ups': 'linux', 'id': 'https://lore/all/c@x/T/#u',
            'name': found[0]['name']})
        self.assertEqual((200, 'c@x'), (status, data['id']))
        status, data = self.call(srv, 'POST', '/api/inbox/add',
                                 json={'ups': 'linux', 'id': '123'})
        self.assertEqual((400, "Series '123' is not on linux's list"),
                         (status, data['error']))

        # The inbox reads it from its thread
        with mock.patch.object(lorelist.LoreList, 'get_series',
                               mock.AsyncMock(return_value={
                                   **found[0], 'patches': []})) as get:
            _, data = self.call(srv, 'GET', '/api/inbox?ups=linux')
        self.assertEqual('c@x', get.call_args.args[1])
        self.assertEqual([('c@x', True)], [(ser['id'], ser['added'])
                                           for ser in data['series']])
        self.assertFalse(data['feed'])

    def test_inbox_applied(self):
        """The inbox says which patches are applied upstream"""
        def git(*args, date='2026-10-05T10:00:00'):
            env = dict(os.environ, GIT_COMMITTER_DATE=date,
                       GIT_AUTHOR_DATE=date)
            return subprocess.run(
                ['git', '-c', 'user.name=T', '-c', 'user.email=t@e', *args],
                cwd=self.tmpdir, env=env, capture_output=True, text=True,
                check=True).stdout.strip()

        git('init', '-q')
        git('commit', '-q', '--allow-empty', '-m', 'video: Old change',
            date='2026-09-01T10:00:00')
        git('commit', '-q', '--allow-empty', '-m', 'video: Add a  driver')
        main = git('rev-parse', 'HEAD')
        git('update-ref', 'refs/remotes/us/main', main)
        git('commit', '-q', '--allow-empty', '-m', 'video: Add a test')
        git('commit', '-q', '--allow-empty', '-m', 'Merge something')
        nxt = git('rev-parse', 'HEAD')
        git('update-ref', 'refs/remotes/us/next', nxt)
        test_commit = git('rev-parse', 'HEAD~1')

        db = self.cser.db
        db.upstream_add('us', 'https://us', 'https://pw.us')
        db.upstream_set_default('us')
        db.patchwork_update('U-Boot', 6, 'uboot', 'us')
        db.commit()
        names = ['[v2,1/3] Video: add a driver', '[v2,2/3] video: Add a test',
                 '[v2,3/3] video: Old change']
        pages = {
            'series/?project=uboot&order=-date&page=1&per_page=50': [{
                'id': 300, 'name': 'video: Add a driver', 'version': 2,
                'date': '2026-10-01T10:00:00', 'total': 3,
                'received_all': True, 'cover_letter': None,
                'patches': [{'id': 30 + seq, 'name': name}
                            for seq, name in enumerate(names)]}],
        }
        srv = self.make_server()
        srv.pwork_for_testing.fake_request = pages.get
        _, data = self.call(srv, 'GET', '/api/inbox')

        # Case and spacing do not matter, a commit on both branches is given
        # with the main one, and commits from before the series are not
        # looked at
        self.assertEqual(
            [(main, 'us/main'), (test_commit, 'us/next'), (None, None)],
            [(pat['commit'], pat['branch'])
             for pat in data['series'][0]['patches']])

    def test_inbox_states(self):
        """The inbox gives the state of each patch, as last seen"""
        db = self.cser.db
        db.upstream_add('us', 'https://us', 'https://pw.us')
        db.upstream_set_default('us')
        db.patchwork_update('U-Boot', 6, 'uboot', 'us')
        db.commit()

        events = []
        reads = []
        def read(subpath):
            reads.append(subpath)
            if subpath.startswith('events/'):
                if 'per_page=1&' in subpath + '&':
                    return [{'date': '2026-10-20T10:00:00'}]
                return events
            return [{'id': 7, 'name': 'video: Add a driver', 'version': 1,
                     'patches': [{'id': pid, 'name': f'patch {pid}'}
                                 for pid in (1, 2, 3)]}]

        srv = self.make_server()
        pwork = srv.pwork_for_testing
        pwork.fake_request = read
        now = [1000.0]
        pwork.cache = pwcache.Cache(os.path.join(self.tmpdir, 'pwcache.db'),
                                    clock=lambda: now[0])
        def states(args=''):
            _, data = self.call(srv, 'GET', f'/api/inbox{args}')
            return [pat['state'] for pat in data['series'][0]['patches']]

        # Following the events begins, dropping a state seen before then,
        # since it may have changed
        pwork._to_cache('patches/2/', {'id': 2, 'state': 'under-review'})
        self.assertEqual([None, None, None], states())
        self.assertIn('events/?project=uboot&per_page=1', reads)

        # A patch which has been read gives its state; patch 1 was put in the
        # cache without its state, so it is not known
        pwork.cache.put(pwork.url, 'patches/1/', {'id': 1, 'state': 'new'})
        pwork._to_cache('patches/2/', {'id': 2, 'state': 'under-review'})
        self.assertEqual([None, 'under-review', None], states())

        # An event gives the new state of a patch, the latest winning
        now[0] += server.app.INBOX_KEEP + 1
        events[:] = [
            {'date': '2026-10-20T10:07:00', 'category': 'patch-state-changed',
             'payload': {'patch': {'id': 3}, 'current_state': 'accepted'}},
            {'date': '2026-10-20T10:05:00', 'category': 'patch-state-changed',
             'payload': {'patch': {'id': 3}, 'current_state': 'superseded'}},
            {'date': '2026-10-20T10:06:00', 'category': 'patch-delegated',
             'payload': {'patch': {'id': 2}, 'delegate': None}}]
        self.assertEqual([None, 'under-review', 'accepted'], states())

        # The events are not read again within a minute
        reads.clear()
        self.assertEqual([None, 'under-review', 'accepted'], states('?page=2'))
        self.assertEqual(['series/?project=uboot&order=-date&page=2'
                          '&per_page=50'], reads)

    def test_inbox_fetch(self):
        """The inbox fetches the upstream, to see what has been applied"""
        upstream = os.path.join(self.tmpdir, 'upstream')

        def git(*args, cwd=self.tmpdir):
            return subprocess.run(
                ['git', '-c', 'user.name=T', '-c', 'user.email=t@e', *args],
                cwd=cwd, capture_output=True, text=True, check=True).stdout

        os.mkdir(upstream)
        git('init', '-q', '-b', 'main', cwd=upstream)
        git('commit', '-q', '--allow-empty', '-m', 'video: Add a driver',
            cwd=upstream)
        git('init', '-q')
        git('remote', 'add', 'us', upstream)

        db = self.cser.db
        db.upstream_add('us', 'https://us', 'https://pw.us')
        db.upstream_set_default('us')
        db.patchwork_update('U-Boot', 6, 'uboot', 'us')
        db.commit()
        pages = {
            'series/?project=uboot&order=-date&page=1&per_page=50': [{
                'id': 300, 'name': 'video', 'version': 1,
                'date': '2026-10-01T10:00:00', 'total': 2,
                'received_all': True, 'cover_letter': None,
                'patches': [{'id': 1, 'name': '[1/2] video: Add a driver'},
                            {'id': 2, 'name': '[2/2] video: Add a test'}]}],
        }
        srv = self.make_server()
        srv.pwork_for_testing.fake_request = pages.get
        now = [1000.0]
        srv._clock = lambda: now[0]

        def at(when):
            return times.to_str(datetime.fromtimestamp(when))

        def applied(args=''):
            _, data = self.call(srv, 'GET', f'/api/inbox{args}')
            return ([pat['branch'] for pat in data['series'][0]['patches']],
                    data['upstream_fetched'])

        # The first time, the upstream is fetched, while patchwork is read
        pwork = srv.pwork_for_testing
        get_recent_series = pwork.get_recent_series
        fetching = []

        async def reading(*args, **kwargs):
            for _ in range(100):
                if 'us' in srv._fetches:
                    break
                await asyncio.sleep(.01)
            fetching.append('us' in srv._fetches)
            return await get_recent_series(*args, **kwargs)

        with mock.patch.object(pwork, 'get_recent_series', reading):
            self.assertEqual((['us/main', None], at(1000)), applied())
        self.assertEqual([True], fetching)

        # A series with a review branch says so, so that the page can say
        # what 'patman review --checkout' does
        git('branch', 'us-300-review', 'us/main')
        _, data = self.call(srv, 'GET', '/api/inbox')
        self.assertEqual('us-300-review', data['series'][0]['review_branch'])

        # Not again within the hour, so a new commit is not seen
        git('commit', '-q', '--allow-empty', '-m', 'video: Add a test',
            cwd=upstream)
        now[0] += server.app.FETCH_MAX_AGE - 1
        self.assertEqual((['us/main', None], at(1000)), applied())

        # 'Refresh' fetches it at once
        self.assertEqual((['us/main', 'us/main'], at(now[0])),
                         applied('?refresh=1'))

        # An upstream which is not a git remote is not fetched; removing the
        # remote drops its branches too
        srv._fetches.clear()
        git('remote', 'remove', 'us')
        self.assertEqual(([None, None], None), applied())

        # A fetch which fails says so, and the inbox is still shown
        git('remote', 'add', 'us', os.path.join(self.tmpdir, 'missing'))
        with terminal.capture() as (_, err):
            self.assertEqual(([None, None], None), applied())
        self.assertIn("Cannot fetch upstream 'us'", err.getvalue())

    def test_inbox_keep(self):
        """The inbox is kept for a while, rather than read each time"""
        db = self.cser.db
        db.upstream_add('us', 'https://us', 'https://pw.us')
        db.upstream_set_default('us')
        db.patchwork_update('U-Boot', 6, 'uboot', 'us')
        db.commit()

        reads = []
        def read(subpath):
            # The events are covered by test_inbox_states()
            if subpath.startswith('events/'):
                return []
            reads.append(subpath)
            return [{'id': len(reads), 'name': 'video: Add a driver',
                     'version': 1, 'patches': []}]

        srv = self.make_server()
        srv.pwork_for_testing.fake_request = read
        now = [1000.0]
        srv.pwork_for_testing.cache = pwcache.Cache(
            os.path.join(self.tmpdir, 'pwcache.db'), clock=lambda: now[0])
        query = 'series/?project=uboot&order=-date&page={}&per_page=50'

        def at(when):
            return times.to_str(datetime.fromtimestamp(when))

        def inbox(args=''):
            status, data = self.call(srv, 'GET', f'/api/inbox{args}')
            self.assertEqual(200, status)
            return data['series'][0]['id'], data['fetched']

        self.assertEqual((1, at(1000)), inbox())

        # Showing it again, or its next page twice, reads only that page
        now[0] += server.app.INBOX_KEEP
        self.assertEqual((1, at(1000)), inbox())
        self.assertEqual(2, inbox('?page=2')[0])
        self.assertEqual(2, inbox('?page=2')[0])
        self.assertEqual([query.format(1), query.format(2)], reads)

        # Once it is too old, it is read again, and so is the next page,
        # which is now older than the first, unless the page is showing
        # again what it showed before
        now[0] += 1
        self.assertEqual((3, at(now[0])), inbox())
        self.assertEqual(2, inbox('?page=2&cached=1')[0])
        self.assertEqual(4, inbox('?page=2')[0])

        # A refresh reads it at once
        self.assertEqual(5, inbox('?refresh=1')[0])
        self.assertEqual(5, inbox()[0])

        # It is kept when the server starts again
        srv = self.make_server()
        srv.pwork_for_testing.fake_request = read
        srv.pwork_for_testing.cache = pwcache.Cache(
            os.path.join(self.tmpdir, 'pwcache.db'), clock=lambda: now[0])
        self.assertEqual(5, inbox()[0])
        self.assertEqual(5, len(reads))

    def test_request_kind(self):
        """Each request to patchwork is counted by its kind"""
        for subpath, kind in (
                ('patches/5/comments/', 'patch-comments'),
                ('patches/5/', 'patch'),
                ('covers/9/comments/', 'cover-comments'),
                ('covers/9/', 'cover'),
                ('series/7/mbox/', 'mbox'),
                ('series/7/', 'series'),
                ('patches/?series=7', 'patch-states'),
                ('series/?project=1&q=video', 'series-search'),
                ('series/?project=uboot&order=-date&page=1&per_page=50',
                 'recent-series'),
                ('patches/?project=1&q=video&order=-date&per_page=20',
                 'patch-search'),
                ('patches/?project=uboot&order=-date&per_page=50&page=1',
                 'patch-list'),
                ('projects/?page=1&per_page=100', 'projects'),
                ('events/?project=uboot', 'events'),
                ('users/3/', 'users')):
            self.assertEqual(kind, patchwork.request_kind(subpath), subpath)

    def test_patchwork_retry(self):
        """Patchwork requests are retried when the server is slow or busy"""
        calls = []
        real_sleep = asyncio.sleep

        async def handler(request):
            calls.append(request.path)
            if len(calls) == 1:
                await real_sleep(1)
            if len(calls) == 2:
                return web.Response(status=502)
            return web.json_response({'ok': len(calls)})

        app = web.Application()
        app.router.add_get('/api/1.2/{tail:.*}', handler)

        urls = []

        async def _go():
            async with TestServer(app) as tserver:
                urls.append(str(tserver.make_url('')).rstrip('/'))
                pwork = Patchwork(urls[0], show_progress=False)
                async with aiohttp.ClientSession() as client:
                    return await pwork._request(client, 'patches/1/')

        traffic.take()
        loop = asyncio.new_event_loop()
        try:
            with mock.patch.object(patchwork, 'REQUEST_TIMEOUT', 0.2), \
                    mock.patch.object(asyncio, 'sleep',
                                      side_effect=self._no_sleep(asyncio.sleep)):
                self.assertEqual({'ok': 3}, loop.run_until_complete(_go()))
        finally:
            loop.close()
        self.assertEqual(3, len(calls))

        # Each try is counted, along with what came back
        self.assertEqual({(urls[0], 'patch'): (3, len('{"ok": 3}'))},
                         traffic.take())

    def test_patchwork_connect_retry(self):
        """A failed connection is retried, but a name which fails is not"""
        sleeps = []

        async def fake_sleep(secs):
            sleeps.append(secs)

        class FailResolver(aiohttp.abc.AbstractResolver):
            """Resolver which fails as the given resolver does"""
            def __init__(self, exc):
                self.exc = exc

            async def resolve(self, host, port=0, family=socket.AF_INET):
                raise self.exc

            async def close(self):
                pass

        async def _go(url, exc):
            pwork = Patchwork(url, show_progress=False)
            conn = aiohttp.TCPConnector(
                resolver=FailResolver(exc) if exc else None)
            async with aiohttp.ClientSession(connector=conn) as client:
                return await pwork._request(client, 'patches/1/')

        bad = 'http://no-such-host.invalid'
        cases = [('http://127.0.0.1:1', None, [1, 2, 3]),

                 # The default resolver, using getaddrinfo()
                 (bad, socket.gaierror(socket.EAI_NONAME, 'Name not known'),
                  [])]

        # aiohttp's resolver for aiodns raises a plain OSError, which only
        # newer versions of aiohttp report as a DNS error
        if patchwork.DNS_ERROR:
            cases.append((bad, OSError(None, 'Domain name not found'), []))
        for url, exc, expect in cases:
            sleeps.clear()
            loop = asyncio.new_event_loop()
            try:
                with mock.patch.object(patchwork.asyncio, 'sleep', fake_sleep):
                    with self.assertRaises(aiohttp.ClientConnectorError):
                        loop.run_until_complete(_go(url, exc))
            finally:
                loop.close()
            self.assertEqual(expect, sleeps)

    def test_patchwork_timeout_message(self):
        """A server which never responds gives an error saying which it is"""
        real_sleep = asyncio.sleep

        async def handler(_request):
            await real_sleep(1)
            return web.json_response({})

        app = web.Application()
        app.router.add_get('/api/1.2/{tail:.*}', handler)

        async def _go():
            async with TestServer(app) as tserver:
                url = str(tserver.make_url('')).rstrip('/')
                pwork = Patchwork(url, show_progress=False)
                async with aiohttp.ClientSession() as client:
                    with self.assertRaises(asyncio.TimeoutError) as exc:
                        await pwork._request(client, 'patches/1/')
                return url, str(exc.exception)

        loop = asyncio.new_event_loop()
        try:
            with mock.patch.object(patchwork, 'REQUEST_TIMEOUT', 0.05), \
                    mock.patch.object(asyncio, 'sleep',
                                      side_effect=self._no_sleep(asyncio.sleep)):
                url, msg = loop.run_until_complete(_go())
        finally:
            loop.close()
        self.assertEqual(f"Patchwork '{url}' did not respond within 0.05 "
                         f'seconds, after {patchwork.RETRIES + 1} tries', msg)

    def test_patchwork_timeout_setting(self):
        """The patchwork_timeout setting sets the request timeout"""
        args = cmdline.parse_args(['--patchwork-timeout', '7', 'upstream',
                                   'ls'], config_fname=False)
        with mock.patch.object(patchwork, 'REQUEST_TIMEOUT', 30), \
                mock.patch.object(control, 'upstream'):
            control.do_patman(args)
            self.assertEqual(7, patchwork.REQUEST_TIMEOUT)

    def test_patchwork_proxy(self):
        """Patchwork requests go through the proxy given by the environment"""
        hosts = []

        async def handler(request):
            hosts.append(request.host)
            return web.json_response({'id': 1})

        # Act as the proxy: it receives the request for the real server
        app = web.Application()
        app.router.add_get('/api/1.2/{tail:.*}', handler)

        async def _go():
            async with TestServer(app) as tserver:
                proxy = str(tserver.make_url('')).rstrip('/')
                pwork = Patchwork('http://patchwork.invalid',
                                  show_progress=False)
                with mock.patch.dict(os.environ, {'HTTP_PROXY': proxy,
                                                  'NO_PROXY': ''}):
                    async with aiohttp.ClientSession() as client:
                        return await pwork._request(client, 'patches/1/')

        loop = asyncio.new_event_loop()
        try:
            data = loop.run_until_complete(_go())
        finally:
            loop.close()
        self.assertEqual({'id': 1}, data)
        self.assertEqual(['patchwork.invalid'], hosts)

    def test_patchwork_no_proxy(self):
        """A proxy is used only when set, and not for hosts in NO_PROXY"""
        url = 'https://patchwork.example.org/api/1.2/'
        with mock.patch.dict(os.environ, clear=True):
            self.assertIsNone(patchwork.get_proxy(url))
        with mock.patch.dict(os.environ, {'HTTPS_PROXY': 'http://gate:8888'},
                             clear=True):
            self.assertEqual('http://gate:8888', patchwork.get_proxy(url))
            os.environ['NO_PROXY'] = 'example.org'
            self.assertIsNone(patchwork.get_proxy(url))

    @staticmethod
    def _no_sleep(real_sleep):
        """Make retry delays short, without affecting other sleeps"""
        async def fake(secs, *args):
            await real_sleep(min(secs, 0.01) if secs >= 1 else secs, *args)
        return fake

    def test_upstreams(self):
        """Upstreams with a patchwork server or a list on lore are listed"""
        with terminal.capture():
            self.cser.upstream_add('us', 'https://us',
                                   patchwork_url='https://pw.us')
            self.cser.upstream_add('ci', 'https://ci')
            self.cser.upstream_add('dtc', 'https://dtc',
                                   lore_url='https://lore/dtc')
            self.cser.upstream_add('both', 'https://both',
                                   patchwork_url='https://pw.both',
                                   lore_url='https://lore/both')
            self.cser.upstream_set_default('us')
        _, data = self.call(self.make_server(), 'GET', '/api/upstreams')
        self.assertEqual(
            [{'name': 'us', 'default': True, 'patchwork_url': 'https://pw.us',
              'lore_url': None},
             {'name': 'dtc', 'default': False, 'patchwork_url': None,
              'lore_url': 'https://lore/dtc'},
             {'name': 'both', 'default': False,
              'patchwork_url': 'https://pw.both', 'lore_url': None}], data)

    def test_lore_upstream(self):
        """An upstream whose list is on lore is read from there"""
        with terminal.capture():
            self.cser.upstream_add('dtc', 'https://dtc',
                                   lore_url='https://lore/dtc')
        srv = self.make_server()
        srv.pwork_for_testing = None
        llist = srv.get_pwork('dtc')
        self.assertIsInstance(llist, lorelist.LoreList)
        self.assertIs(llist, srv.get_pwork('dtc'))
        self.assertIsNone(srv.get_pw_url('dtc'))

        # The inbox needs no patchwork project
        series = [{'id': 'c@x', 'name': 'dm: Tidy up', 'version': 1,
                   'date': '2026-10-01T10:00:00', 'total': 1,
                   'received_all': True, 'web_url': 'https://lore/dtc/c@x/',
                   'submitter': {'name': 'Ann', 'email': 'ann@x'},
                   'cover_letter': {'id': 'lore:c@x', 'name': 'dm: Tidy up'},
                   'patches': [{'id': 'lore:p1@x', 'name': 'dm: First'}]}]
        with mock.patch.object(lorelist.LoreList, 'get_recent_series',
                               mock.AsyncMock(return_value=series)) as recent:
            status, data = self.call(srv, 'GET', '/api/inbox?ups=dtc')
        self.assertEqual(200, status)
        self.assertEqual('dtc', recent.call_args.args[1])
        self.assertTrue(data['more'])
        [item] = data['series']
        self.assertEqual(
            ('c@x', 'lore:c@x', None, 'dtc-c-review', False,
             [{'id': 'lore:p1@x', 'name': 'dm: First', 'commit': None,
               'branch': None, 'state': None}]),
            (item['id'], item['cover_id'], item['review_branch'],
             item['review_name'], item['mine'], item['patches']))

        # An empty page is the last. With no default upstream, the only one
        # is used
        with mock.patch.object(lorelist.LoreList, 'get_recent_series',
                               mock.AsyncMock(return_value=[])):
            _, data = self.call(srv, 'GET', '/api/inbox?page=2')
        self.assertEqual(('dtc', False), (data['ups'], data['more']))

        # So it is for a patch, too
        with mock.patch.object(lorelist.LoreList, 'get_patch',
                               mock.AsyncMock(return_value={'id': 'lore:p@x'})):
            status, data = self.call(srv, 'GET', '/api/patch/lore:p@x')
        self.assertEqual((200, 'lore:p@x'), (status, data['id']))

        # With more than one, the upstream must be given
        with terminal.capture():
            self.cser.upstream_add('us', 'https://us',
                                   patchwork_url='https://pw.us')
        status, data = self.call(srv, 'GET', '/api/inbox')
        self.assertEqual(
            (404, "There is no default upstream, so give one with 'ups'"),
            (status, data['error']))

        # A patch and its comments are read from lore by their Message-ID
        patch = {'id': 'lore:p1@x', 'name': 'dm: First', 'state': 'new',
                 'submitter': {'name': 'Ann'}, 'content': 'First'}
        comment = {'id': None, 'msgid': '<r@x>', 'date': '2026-10-02',
                   'submitter': {'name': 'Bob', 'email': 'bob@x'},
                   'subject': 'Re: [PATCH] dm: First', 'content': 'OK',
                   'web_url': 'https://lore/dtc/r@x/'}
        with mock.patch.object(lorelist.LoreList, 'get_patch',
                               mock.AsyncMock(return_value=patch)) as get, \
                mock.patch.object(lorelist.LoreList, 'get_patch_comments',
                                  mock.AsyncMock(return_value=[comment])):
            status, data = self.call(srv, 'GET',
                                     '/api/patch/lore:p1@x?ups=dtc')
            self.assertEqual(200, status)
            self.assertEqual(('lore:p1@x', 'First'),
                             (data['id'], data['content']))
            self.assertEqual('lore:p1@x', get.call_args.args[1])
            status, data = self.call(srv, 'GET',
                                     '/api/comments/patch/lore:p1@x?ups=dtc')
            self.assertEqual(200, status)
            self.assertEqual(['OK'], [cmt['content'] for cmt in data])

        # A patch on lore is not looked for on patchwork
        with terminal.capture():
            self.cser.upstream_set_default('us')
        status, data = self.call(srv, 'GET', '/api/comments/patch/lore:p1@x')
        self.assertEqual(
            (404, "'lore:p1@x' is on lore, but upstream 'us' has no list "
             "there; give one with 'ups'"), (status, data['error']))

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
        self.assertTrue(server.app.is_loopback('127.0.0.1'))
        self.assertTrue(server.app.is_loopback('::1'))
        self.assertTrue(server.app.is_loopback('localhost'))
        self.assertFalse(server.app.is_loopback('0.0.0.0'))
        self.assertFalse(server.app.is_loopback('100.64.1.2'))
        self.assertFalse(server.app.is_loopback('myhost'))

    def test_cmd_series(self):
        """The series which a review command acts on is found"""
        self.assertEqual(['123', None, None], [
            server.app.cmd_series(['patman', 'review', '-s', '123', '-d']),
            server.app.cmd_series(['patman', 'review', '--scan']),
            server.app.cmd_series(['patman', 'review', '-s'])])

    def test_serve_cmdline(self):
        """The serve command passes its options through to the server"""
        # Use a separate database, since setUp() already has one open
        dbdir = os.path.join(self.tmpdir, 'serve')
        os.mkdir(dbdir)
        args = cmdline.parse_args(
            ['serve', '--host', '0.0.0.0', '--port', '1234', '-s', '-r',
             '-t', 'tok'], config_fname=False)
        with mock.patch.object(server.app.web, 'run_app') as run_app, \
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
        self.assertEqual(5, args.max_reviews)
        with mock.patch.object(server.app.web, 'run_app'), \
                mock.patch.object(server.app, 'Server',
                                  wraps=server.app.Server) as make, \
                terminal.capture() as (out, err):
            control.do_serve(args, dbdir)
        self.assertIn('no --token', out.getvalue() + err.getvalue())
        self.assertEqual(5, make.call_args.args[7])

        args = cmdline.parse_args(['serve', '--max-reviews', '0'],
                                  config_fname=False)
        with self.assertRaises(ValueError) as exc:
            control.do_serve(args, dbdir)
        self.assertIn('--max-reviews must be at least 1', str(exc.exception))


if __name__ == '__main__':
    unittest.main()
