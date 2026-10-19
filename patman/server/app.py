# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""The web server, which gives the page and its JSON API

The API is described in doc/serve-protocol.rst
"""

import asyncio
import hmac
import ipaddress
import os
import re
import subprocess
import sys

import aiohttp
from aiohttp import web

from u_boot_pylib import tout

from patman import database
from patman.patchwork import Patchwork
from patman.server import info
from patman.server import jobs
from patman.server import params

# Directory holding the web page and its assets
WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'web')

# File, next to the database, holding the jobs which have not finished, so
# that they can be resumed when the server restarts
JOBS_FNAME = '.patman-serve-jobs.json'

# Number of series in each page of the inbox
INBOX_PAGE = 50


# Tag given in a review which approves the patch
RE_REVIEWED_BY = re.compile(r'^Reviewed-by:', re.MULTILINE)


# pylint: disable=R0902
class Server(jobs.JobRunner):
    """Web server providing patman information as JSON

    Properties:
        cser (Cseries): Open cseries instance
        rcser (Cseries): Open cseries instance holding the reviews, which is
            cser unless reviews are kept in a separate database
        pw_url (str or None): Patchwork URL to use when a series has no
            upstream with its own URL
        sync_interval (int): Minutes between syncs, 0 to disable
        review_sync (bool): True to run 'review --sync' as part of a sync
        token (str or None): Token which clients must provide, or None
        pwork_for_testing (Patchwork or None): Patchwork object to use for
            all upstreams, for testing
        sync_cmds_for_testing (list of list of str or None): Commands to
            run for a sync, for testing
    """
    # pylint: disable=R0913,R0917
    def __init__(self, cser, pw_url=None, sync_interval=0, review_sync=False,
                 token=None, rcser=None, jobs_fname=None):
        super().__init__(cser.topdir, jobs_fname)
        self.cser = cser
        self.rcser = rcser or cser
        self.pw_url = pw_url
        self.sync_interval = sync_interval
        self.review_sync = review_sync
        self.token = token
        self.pwork_for_testing = None
        self.sync_cmds_for_testing = None
        self._pworks = {}
        self._sleep = asyncio.sleep
        self._sync_task = None

        # patman's version, found when first asked for, or '' if not known
        self._version_str = None

        # Reviewed versions which patchwork says have no cover letter
        self._no_cover = set()

    def get_pw_url(self, ups):
        """Get the patchwork URL for an upstream

        Args:
            ups (str or None): Upstream name

        Return:
            str or None: URL, or None if not known
        """
        if self.pwork_for_testing:
            return self.pwork_for_testing.url
        return (ups and self.cser.db.upstream_get_patchwork_url(ups)) or \
            self.pw_url

    def get_pwork(self, ups):
        """Get a Patchwork object for an upstream

        Args:
            ups (str or None): Upstream name

        Return:
            Patchwork: Object to use for that upstream's patchwork server

        Raises:
            ValueError: No patchwork URL is known
        """
        if self.pwork_for_testing:
            return self.pwork_for_testing
        url = self.get_pw_url(ups)
        if not url:
            raise ValueError(f"No patchwork URL for upstream '{ups}'")
        if url not in self._pworks:
            self._pworks[url] = Patchwork(url, show_progress=False)
        return self._pworks[url]

    def _review_db_args(self):
        """Get the arguments which select the review database

        Return:
            list of str: Arguments for a 'patman review' command
        """
        if self.rcser is self.cser or not self.rcser.db_dir:
            return []
        return ['--review-db-dir', self.rcser.db_dir]

    def get_sync_cmds(self):
        """Get the commands to run for a sync

        Return:
            list of list of str: Commands, each a list of arguments
        """
        if self.sync_cmds_for_testing is not None:
            return self.sync_cmds_for_testing
        fetch = self.get_fetch_cmd()
        cmds = [fetch] if fetch else []
        cmds.append(self.get_patman_cmd('series', 'gather-all', '-G'))
        if self.review_sync:
            cmds.append(self.get_patman_cmd('review', '--sync',
                                            *self._review_db_args()))
        return cmds

    def get_fetch_cmd(self):
        """Get a command which fetches the upstreams which reviews use

        Reviews apply patches to the upstream's branches, so these must be
        kept up to date. The default upstream and the upstreams of reviewed
        series are fetched, where they are git remotes.

        Return:
            list of str or None: Command, or None if there is nothing to
                fetch
        """
        wanted = {ser.upstream for ser in
                  self.rcser.db.series_get_dict(reviews_only=True).values()
                  if ser.upstream}
        default = self.cser.db.upstream_get_default()
        if default:
            wanted.add(default)
        res = subprocess.run(['git', 'remote'], cwd=self.cser.topdir,
                             capture_output=True, text=True, check=False)
        remotes = sorted(wanted & set(res.stdout.split()))
        if not remotes:
            return None
        return ['git', 'fetch', '-q', '--multiple', *remotes]

    def get_patman_cmd(self, *args):
        """Get a command which runs patman in a child process

        Args:
            args (list of str): Arguments for patman

        Return:
            list of str: Command to run
        """
        return [sys.executable, '-m', 'patman', '-v', *args]

    def _sync_job(self):
        """Get the most recent sync job

        Return:
            Job or None: Job, or None if there has not been a sync
        """
        for job in reversed(self._jobs.values()):
            if job.kind == 'sync':
                return job
        return None

    @property
    def sync_state(self):
        """dict: State of the most recent sync, with the sync settings"""
        job = self._sync_job()
        state = dict(job.state) if job else {
            'queued': False, 'running': False, 'cancelled': False,
            'started': None, 'finished': None, 'results': []}
        state['interval'] = self.sync_interval
        state['review_sync'] = self.review_sync
        state['num_series'] = info.get_gather_count(self.cser)
        return state

    def _start_sync(self):
        """Create a sync job, unless one is already waiting or running

        Return:
            Job or None: New job, or None if a sync is already active
        """
        job = self._sync_job()
        if job and job.active:
            return None
        return self._new_job('sync', 'Sync', self.get_sync_cmds())

    async def run_sync(self):
        """Run a sync now, waiting for any other job to finish first

        Return:
            bool: True if a sync was run, False if one was already active
        """
        job = self._start_sync()
        if not job:
            return False
        await self.run_job(job)
        return True

    def cancel_sync(self):
        """Stop the sync which is running or waiting, if any

        Return:
            bool: True if a sync was stopped, False if none was active
        """
        job = self._sync_job()
        return bool(job) and self.cancel_job(job)

    async def _sync_loop(self):
        """Run a sync every sync_interval minutes, starting straight away"""
        while True:
            await self.run_sync()
            await self._sleep(self.sync_interval * 60)

    async def _start_background(self, _app):
        """Resume any unfinished jobs, then start the periodic sync"""
        self.resume_jobs()
        if self.sync_interval:
            self._sync_task = asyncio.create_task(self._sync_loop())

    async def _stop_background(self, _app):
        """Stop the periodic sync, if running"""
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass

    @web.middleware
    async def _auth(self, request, handler):
        """Check that the client provided the token, if one is needed"""
        if self.token and request.path.startswith('/api/'):
            given = request.headers.get('Authorization', '')
            if given.startswith('Bearer '):
                given = given[7:]
            else:
                given = request.query.get('token', '')
            if not hmac.compare_digest(given.encode(), self.token.encode()):
                return web.json_response({'error': 'Unauthorised'},
                                         status=401)
        return await handler(request)

    @web.middleware
    async def _no_cache(self, request, handler):
        """Have the browser check the page and its files before reusing them

        The page is split into several files, so a browser which kept an old
        app.js after patman is updated would run it with the new page
        """
        resp = await handler(request)
        if request.path == '/' or request.path.startswith('/static/'):
            resp.headers['Cache-Control'] = 'no-cache'
        return resp

    @web.middleware
    async def _errors(self, request, handler):
        """Turn errors into a JSON response"""
        try:
            return await handler(request)
        except jobs.JobBusyError as exc:
            return web.json_response({'error': str(exc)}, status=409)
        except params.BadRequest as exc:
            return web.json_response({'error': str(exc)}, status=400)
        except ValueError as exc:
            return web.json_response({'error': str(exc)}, status=404)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return web.json_response(
                {'error': f'Patchwork: {type(exc).__name__}: {exc}'},
                status=502)

    async def _series_list(self, _request):
        return web.json_response(info.get_series_list(self.cser))

    async def _series_detail(self, request):
        data = info.get_series_detail(self.cser,
                                      int(request.match_info['idnum']))
        data['patchwork_url'] = self.get_pw_url(data['upstream'])
        return web.json_response(data)

    async def _series_sync(self, request):
        idnum = int(request.match_info['idnum'])
        data = await self._read_json(request)
        version = params.get_int(data, 'version')
        name, version = info.get_series_link(self.cser, idnum, version)
        cmd = self.get_patman_cmd('series', '-s', name, '-V', str(version),
                                  'gather', '-G')
        for job in self._jobs.values():
            if job.active and job.cmds == [cmd]:
                raise jobs.JobBusyError(
                    f"'{name}' v{version} is already being synced")
        job = self.add_job('gather', f"Sync '{name}' v{version}", [cmd])
        return web.json_response(job.state, status=202)

    async def _review_list(self, _request):
        return web.json_response(info.get_review_list(self.rcser))

    async def _find_covers(self, data):
        """Look up the cover letters which reviewed versions lack

        Reviews added before cover-letter IDs were recorded have none, so
        the cover letter which was reviewed cannot be shown. Read each such
        version's series from patchwork and record its cover letter, so
        this only happens once.

        Args:
            data (dict): Review detail, as from info.get_review_detail(); the
                versions' cover_id and cover_name are filled in
        """
        todo = [ver for ver in data['versions']
                if not ver['cover_id'] and ver['link'] and
                ver['svid'] not in self._no_cover and
                any(not rev['seq'] for rev in ver['reviews'])]
        if not todo:
            return
        try:
            pwork = self.get_pwork(data['upstream'])
        except ValueError:
            return
        async with aiohttp.ClientSession() as client:
            results = await asyncio.gather(
                *[pwork.get_series(client, ver['link']) for ver in todo],
                return_exceptions=True)
        for ver, result in zip(todo, results):
            if isinstance(result, Exception):
                tout.warning(f"Cannot read series {ver['link']}: {result}")
                continue
            cover = (result or {}).get('cover_letter')
            if not cover:
                self._no_cover.add(ver['svid'])
                continue
            ver['cover_id'] = cover['id']
            ver['cover_name'] = cover.get('name')
            self.rcser.db.ser_ver_set_info(database.SerVer(
                ver['svid'], None, None, None, cover['id'], 0,
                cover.get('name'), None, None, None))
        self.rcser.commit()

    async def _review_detail(self, request):
        idnum = int(request.match_info['idnum'])
        data = info.get_review_detail(self.rcser, idnum)
        # Mark the reviews which a job is acting on, so the page can wait
        for ver in data['versions']:
            pending = self.pending_jobs(idnum, ver['version'])
            for item in ver['reviews']:
                done = item['status'] in ('sent', 'replied')
                item['job'] = pending.get(item['seq'],
                                          None if done else pending.get(None))
        # 'patman review' uses the default upstream when none is given, so
        # a series reviewed that way is on its patchwork server
        data['upstream'] = (data['upstream'] or
                            self.cser.db.upstream_get_default())
        await self._find_covers(data)
        data['patchwork_url'] = self.get_pw_url(data['upstream'])
        proj = self.cser.project_get(data['upstream'])
        data['project'] = proj[2] if proj else None
        return web.json_response(data)

    async def _todo_list(self, _request):
        return web.json_response(info.get_todo_list(self.cser))

    async def _version(self, _request):
        if self._version_str is None:
            self._version_str = info.get_version() or ''
        return web.json_response({'version': self._version_str or None})

    async def _comments(self, request):
        kind = request.match_info['kind']
        item_id = int(request.match_info['item_id'])
        pwork = self.get_pwork(request.query.get('ups') or None)
        async with aiohttp.ClientSession() as client:
            if kind == 'patch':
                comments = await pwork.get_patch_comments(client, item_id)
            else:
                comments = await pwork.get_cover_comments(client, item_id)
        return web.json_response([info.comment_info(c)
                                  for c in comments or []])

    async def _mail(self, request):
        item_id = int(request.match_info['item_id'])
        pwork = self.get_pwork(request.query.get('ups') or None)
        async with aiohttp.ClientSession() as client:
            if request.match_info['kind'] == 'patch':
                data = await pwork.get_patch(client, item_id)
            else:
                data = await pwork.get_cover(client, item_id)
        return web.json_response(info.mail_info(data))

    async def _blob(self, request):
        oid, text = await info.read_blob(self.cser.topdir,
                                    request.query.getall('hash', []))
        return web.json_response({'hash': oid, 'text': text})

    async def _sync_get(self, _request):
        return web.json_response(self.sync_state)

    async def _sync_post(self, _request):
        job = self._start_sync()
        if not job:
            return web.json_response(self.sync_state, status=409)
        asyncio.create_task(self.run_job(job))

        # Let the sync start, so the response shows it running
        await asyncio.sleep(0)
        return web.json_response(self.sync_state, status=202)

    async def _sync_cancel_post(self, _request):
        if not self.cancel_sync():
            return web.json_response(self.sync_state, status=409)
        return web.json_response(self.sync_state, status=202)

    async def _upstreams(self, _request):
        out = []
        for name, (_, is_default, pw_url, *_) in \
                self.cser.db.upstream_get_dict().items():
            if pw_url:
                out.append({'name': name, 'default': bool(is_default),
                            'patchwork_url': pw_url})
        return web.json_response(out)

    async def _inbox(self, request):
        ups = request.query.get('ups') or self.cser.db.upstream_get_default()
        page = params.get_int(request.query, 'page') or 1
        proj = self.cser.project_get(ups)
        if not proj:
            raise ValueError(f"No patchwork project for upstream '{ups}'")
        pwork = self.get_pwork(ups)
        async with aiohttp.ClientSession() as client:
            series_list = await pwork.get_recent_series(client, proj[2], page,
                                                        INBOX_PAGE)
        return web.json_response({
            'ups': ups, 'page': page, 'per_page': INBOX_PAGE,
            'series': info.get_inbox(self.cser, self.rcser, series_list)})

    async def _job_list(self, _request):
        return web.json_response([job.state for job in self.get_jobs()])

    async def _job_detail(self, request):
        job = self.get_job(int(request.match_info['idnum']))
        return web.json_response(job.state)

    async def _job_cancel(self, request):
        job = self.get_job(int(request.match_info['idnum']))
        status = 202 if self.cancel_job(job) else 409
        return web.json_response(job.state, status=status)

    async def _read_json(self, request):
        """Read the JSON body of a request, which may be empty

        Return:
            dict: Request data

        Raises:
            ValueError: The body is not a JSON object
        """
        if not request.can_read_body:
            return {}
        try:
            data = await request.json()
        except ValueError as exc:
            raise params.BadRequest('Invalid JSON in request') from exc
        if not isinstance(data, dict):
            raise params.BadRequest('Request must be a JSON object')
        return data

    def _review_job(self, title, *args, target=None):
        """Start a job which runs 'patman review'

        Args:
            title (str): Description of the job
            args (list of str): Arguments for 'patman review'
            target (dict or None): Reviews which the job acts on (see Job)

        Return:
            web.Response: Response giving the job state
        """
        cmd = self.get_patman_cmd('review', *args, *self._review_db_args())
        job = self.add_job('review', title, [cmd], target)
        return web.json_response(job.state, status=202)

    async def _review_post(self, request):
        data = await self._read_json(request)
        args, title = params.parse_review_req(
            data, list(self.cser.db.upstream_get_dict()))
        return self._review_job(title, *args)

    async def _review_scan(self, request):
        data = await self._read_json(request)
        args = ['--scan'] + (['-d'] if data.get('drafts') else [])
        return self._review_job('Scan for new versions', *args)

    async def _redraft(self, request):
        idnum = int(request.match_info['idnum'])
        data = await self._read_json(request)
        return self._redraft_job(idnum, params.get_int(data, 'version'),
                                 data.get('seqs') or [])

    def _series_review_args(self, idnum, version, seqs, *action):
        """Get the 'patman review' arguments to act on a reviewed series

        Args:
            idnum (int): Series ID in the review database
            version (int or None): Version, or None for the latest
            seqs (list of int): Patches to act on (0 for the cover letter),
                or empty for all
            action (list of str): Arguments giving the action, e.g. '--send'

        Return:
            tuple:
                list of str: Arguments for 'patman review'
                str: Series and version, for the job title, e.g. "'foo' v2"
                dict: Reviews which the job acts on, as a job target

        Raises:
            ValueError: The seqs are invalid, or the version has no link
            JobBusyError: A job is already acting on one of the reviews
        """
        if not isinstance(seqs, list) or not all(
                isinstance(seq, int) and seq >= 0 for seq in seqs):
            raise params.BadRequest('Invalid seqs')
        name, desc, ups = self.rcser.db.series_get_info(idnum)
        version = version or self.rcser.db.series_get_max_version(idnum)
        link = self.rcser.db.ser_ver_get_link(idnum, version)
        if not link:
            raise ValueError(f"Series '{name}' v{version} has no link")
        self.check_free(idnum, version, seqs, self._unsent(idnum, version))
        args = ['-s', str(link), *action]
        if seqs:
            args += ['-i', ','.join(str(seq) for seq in sorted(seqs))]
        if ups:
            args += ['-U', ups]
        target = {'series': idnum, 'version': version, 'seqs': sorted(seqs)}
        return args, f"'{desc or name}' v{version}", target

    def _unsent(self, idnum, version):
        """Get the patches whose reviews have not been sent

        Args:
            idnum (int): Series ID in the review database
            version (int): Version of the series

        Return:
            list of int: Patch numbers, 0 for the cover letter
        """
        svr = self.rcser.get_ser_ver(idnum, version)
        return [rev.seq for rev in self.rcser.db.review_get_for_version(
            svr.idnum) if rev.status not in ('sent', 'replied')]

    def _redraft_job(self, idnum, version, seqs):
        """Start a job which recreates the Gmail drafts for a review

        Args:
            idnum (int): Series ID in the review database
            version (int or None): Version, or None for the latest
            seqs (list of int): Patches to redraft (0 for the cover letter),
                or empty for all

        Return:
            web.Response: Response giving the job state
        """
        args, what, target = self._series_review_args(idnum, version, seqs,
                                                      '--redraft')
        return self._review_job(f'Redraft {what}', *args, target=target)

    async def _send(self, request):
        idnum = int(request.match_info['idnum'])
        data = await self._read_json(request)
        seqs = data.get('seqs') or []
        args, what, target = self._series_review_args(
            idnum, params.get_int(data, 'version'), seqs, '--send')
        return self._review_job(f'Send {params.parts_text(seqs)} of {what}',
                                *args, target=target)

    async def _revise(self, request):
        idnum = int(request.match_info['idnum'])
        data = await self._read_json(request)
        note = data.get('note')
        if not isinstance(note, str) or not note.strip():
            raise params.BadRequest('Please say what to change')
        note = note.strip()
        part = data.get('part')
        if part:
            if not isinstance(part, str):
                raise params.BadRequest('Invalid part')
            note = f'About this part of the review:\n\n{part.strip()}' \
                   f'\n\n{note}'
        seqs = data.get('seqs') or []
        args, what, target = self._series_review_args(
            idnum, params.get_int(data, 'version'), seqs, '--revise', note)
        if data.get('drafts'):
            args.append('-d')
        return self._review_job(f'Revise {params.parts_text(seqs)} of {what}',
                                *args, target=target)

    async def _review_item(self, request):
        db = self.rcser.db
        rev = db.review_get(int(request.match_info['idnum']))
        data = await self._read_json(request)
        if rev.status in ('sent', 'replied'):
            return web.json_response(
                {'error': 'This review has already been sent'}, status=409)
        body = data.get('body', rev.body)
        if not isinstance(body, str) or not body.strip():
            raise params.BadRequest('The review cannot be empty')
        svr = db.ser_ver_get_by_id(rev.svid)
        self.check_free(svr.series_id, svr.version, [rev.seq], [rev.seq])
        # Unless told otherwise, the review approves if it gives the tag
        approved = data.get('approved')
        if approved is None:
            approved = bool(RE_REVIEWED_BY.search(body))
        db.review_set_body(rev.idnum, body, approved)
        self.rcser.commit()
        if data.get('redraft'):
            return self._redraft_job(svr.series_id, svr.version, [rev.seq])
        return web.json_response(info.review_info(db.review_get(rev.idnum)))

    async def _reply(self, request):
        db = self.rcser.db
        reply = db.reply_get(int(request.match_info['idnum']))
        data = await self._read_json(request)
        if reply.status == database.REPLY_SENT:
            return web.json_response(
                {'error': 'The response has already been sent'}, status=409)
        if 'response' in data:
            response = data['response']
            if not isinstance(response, str):
                raise params.BadRequest('Invalid response')
            db.reply_set_response(
                reply.idnum, response if response.strip() else None,
                database.REPLY_SUGGESTED if response.strip() else
                database.REPLY_NEW)
        if data.get('skip'):
            db.reply_set_status(reply.idnum, database.REPLY_SKIP,
                                reply.draft_id)
        self.rcser.commit()
        if data.get('draft'):
            return self._review_job(f'Draft response to reply {reply.idnum}',
                                    '--draft-reply', str(reply.idnum))
        return web.json_response(info.reply_info(db.reply_get(reply.idnum)))

    async def _reply_suggest(self, request):
        reply = self.rcser.db.reply_get(int(request.match_info['idnum']))
        return self._review_job(f'Suggest a response to reply {reply.idnum}',
                                '--suggest-reply', str(reply.idnum))

    async def _reply_send(self, request):
        reply = self.rcser.db.reply_get(int(request.match_info['idnum']))
        if reply.status == database.REPLY_SENT:
            return web.json_response(
                {'error': 'The response has already been sent'}, status=409)
        if not (reply.response or '').strip():
            raise params.BadRequest('There is no response to send')
        return self._review_job(f'Send the response to reply {reply.idnum}',
                                '--send-reply', str(reply.idnum))

    async def _index(self, _request):
        return web.FileResponse(os.path.join(WEB_DIR, 'index.html'))

    def make_app(self):
        """Create the aiohttp application

        Return:
            web.Application: Application, ready to run
        """
        app = web.Application(
            middlewares=[self._errors, self._auth, self._no_cache])
        app.router.add_get('/api/series', self._series_list)
        app.router.add_get(r'/api/series/{idnum:\d+}', self._series_detail)
        app.router.add_post(r'/api/series/{idnum:\d+}/sync',
                            self._series_sync)
        app.router.add_get('/api/reviews', self._review_list)
        app.router.add_get(r'/api/reviews/{idnum:\d+}', self._review_detail)
        app.router.add_get('/api/todo', self._todo_list)
        app.router.add_get('/api/version', self._version)
        app.router.add_get(r'/api/comments/{kind:patch|cover}/{item_id:\d+}',
                           self._comments)
        app.router.add_get(r'/api/{kind:patch|cover}/{item_id:\d+}',
                           self._mail)
        app.router.add_get('/api/blob', self._blob)
        app.router.add_get('/api/sync', self._sync_get)
        app.router.add_post('/api/sync', self._sync_post)
        app.router.add_post('/api/sync/cancel', self._sync_cancel_post)
        app.router.add_get('/api/upstreams', self._upstreams)
        app.router.add_get('/api/inbox', self._inbox)
        app.router.add_get('/api/jobs', self._job_list)
        app.router.add_get(r'/api/jobs/{idnum:\d+}', self._job_detail)
        app.router.add_post(r'/api/jobs/{idnum:\d+}/cancel', self._job_cancel)
        app.router.add_post('/api/review', self._review_post)
        app.router.add_post('/api/review/scan', self._review_scan)
        app.router.add_post(r'/api/reviews/{idnum:\d+}/redraft',
                            self._redraft)
        app.router.add_post(r'/api/reviews/{idnum:\d+}/revise',
                            self._revise)
        app.router.add_post(r'/api/reviews/{idnum:\d+}/send', self._send)
        app.router.add_post(r'/api/review-items/{idnum:\d+}',
                            self._review_item)
        app.router.add_post(r'/api/replies/{idnum:\d+}', self._reply)
        app.router.add_post(r'/api/replies/{idnum:\d+}/send',
                            self._reply_send)
        app.router.add_post(r'/api/replies/{idnum:\d+}/suggest',
                            self._reply_suggest)
        app.router.add_get('/', self._index)
        app.router.add_static('/static', WEB_DIR)
        app.on_startup.append(self._start_background)
        app.on_cleanup.append(self._stop_background)
        return app


def is_loopback(host):
    """Check whether a host address only accepts local connections

    Args:
        host (str): Host name or address

    Return:
        bool: True if the address is a loopback one
    """
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# pylint: disable=R0913,R0917
def serve(cser, host, port, pw_url, sync_interval, review_sync, token,
          rcser=None):
    """Run the web server until interrupted

    Args:
        cser (Cseries): Open cseries instance
        host (str): Address to listen on
        port (int): Port to listen on
        pw_url (str or None): Default patchwork URL
        sync_interval (int): Minutes between syncs, 0 to disable
        review_sync (bool): True to run 'review --sync' as part of a sync
        token (str or None): Token which clients must provide, or None
        rcser (Cseries or None): Open cseries instance holding the reviews,
            or None if they are in the main database
    """
    if not token and not is_loopback(host):
        tout.warning(f'Listening on {host} with no --token: anyone who can '
                     'reach this port can read your series and reviews')
    jobs_fname = os.path.join(os.path.dirname(cser.db.db_path), JOBS_FNAME)
    server = Server(cser, pw_url, sync_interval, review_sync, token, rcser,
                    jobs_fname)
    query = f'?token={token}' if token else ''
    tout.notice(f'Serving on http://{host}:{port}/{query}')
    if sync_interval:
        what = 'gather-all' + (' and review --sync' if review_sync else '')
        tout.notice(f'Syncing ({what}) every {sync_interval} minutes')
    web.run_app(server.make_app(), host=host, port=port, print=None)
