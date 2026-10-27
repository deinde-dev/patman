# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
# pylint: disable=C0302
"""The web server, which gives the page and its JSON API

The API is described in doc/serve-protocol.rst
"""

import asyncio
import hmac
import ipaddress
import os
import re
import subprocess
import time
from datetime import datetime, timedelta

import aiohttp
from aiohttp import web

from u_boot_pylib import gitutil
from u_boot_pylib import tout

from patman import database
from patman import interpreter
from patman import lorelist
from patman import patchwork
from patman import pwcache
from patman import settings
from patman import times
from patman import traffic
from patman.patchwork import Patchwork
from patman.review import apply
from patman.review import checkout
from patman.review import given
from patman.review import lookup
from patman.review import stats
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

# Most weeks which the stats count, a year
MAX_STATS_WEEKS = 52

# Seconds for which the inbox read from patchwork is kept, so that showing it
# again does not read it again
INBOX_KEEP = 10 * 60

# Seconds after which the inbox fetches the upstream again, so that it can
# show which patches have been applied since
FETCH_MAX_AGE = 60 * 60

# Seconds for which the inbox waits for a fetch to finish; after that it
# shows the branches as they are, and the fetch carries on
FETCH_WAIT = 20


# Arguments of 'patman review' which act on stored reviews rather than review
# a series
REVIEW_ACTIONS = {'--redraft', '--send', '--revise', '--respond-comment',
                  '--delete'}

# Tag given in a review which approves the patch
RE_REVIEWED_BY = re.compile(r'^Reviewed-by:', re.MULTILINE)

# ID of a patch or cover letter in a request: a patchwork ID, or one read from
# lore, e.g. 'lore:<msgid>'
ITEM_ID = r'\d+|lore:[^/]+'


# pylint: disable-next=R0902
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
        own_emails (set of str or None): Our own email addresses, in lower
            case, or None if not yet found
    """
    # pylint: disable-next=R0913,R0917
    def __init__(self, cser, pw_url=None, sync_interval=0, review_sync=False,
                 token=None, rcser=None, jobs_fname=None, max_parallel=None):
        """Set up the server

        Args:
            cser (Cseries): Open cseries instance
            pw_url (str or None): Patchwork URL to use when a series has no
                upstream with its own URL
            sync_interval (int): Minutes between syncs, 0 to disable
            review_sync (bool): True to run 'review --sync' as part of a sync
            token (str or None): Token which clients must provide, or None
            rcser (Cseries or None): Open cseries instance holding the reviews,
                or None if they are in the main database
            jobs_fname (str or None): File which holds the jobs to run again if
                the server restarts, or None to not keep them
            max_parallel (int or None): Maximum number of reviews to run at
                once, or None for the default
        """
        super().__init__(cser.topdir, jobs_fname, max_parallel)
        self.cser = cser
        self.rcser = rcser or cser
        self.pw_url = pw_url
        self.sync_interval = sync_interval
        self.review_sync = review_sync
        self.token = token
        self.pwork_for_testing = None
        self.sync_cmds_for_testing = None
        self.own_emails = None
        self._pworks = {}
        self._sleep = asyncio.sleep
        self._clock = time.time
        self._sync_task = None

        # Fetches of each upstream started by the inbox: key (str): upstream
        # name; value: dict with 'started' (time the last fetch began, or
        # None to fetch at once), 'task' (asyncio.Task running it) and 'done'
        # (time the last one which worked finished, or None)
        self._fetches = {}

        # patman's version, found when first asked for, or '' if not known
        self._version_str = None

        # Reviewed versions which patchwork says have no cover letter
        self._no_cover = set()

    def get_pw_url(self, ups):
        """Get the patchwork URL for an upstream

        Args:
            ups (str or None): Upstream name

        Return:
            str or None: URL, or None if not known, or if the upstream's
                series are on lore instead
        """
        if self.pwork_for_testing:
            return self.pwork_for_testing.url
        if self.get_lore_url(ups):
            return None
        return (ups and self.cser.db.upstream_get_patchwork_url(ups)) or \
            self.pw_url

    def get_lore_url(self, ups):
        """Get the list on lore for an upstream with no patchwork server

        Args:
            ups (str or None): Upstream name

        Return:
            str or None: URL of the list on lore, or None if the upstream
                has none, or has a patchwork server
        """
        if not ups or self.cser.db.upstream_get_patchwork_url(ups):
            return None
        return self.cser.db.upstream_get_lore_url(ups)

    def get_own_emails(self):
        """Get our own email addresses, finding them the first time

        Return:
            set of str: Our own email addresses, in lower case
        """
        if self.own_emails is None:
            # The address which 'patman review' sends reviews from
            email = gitutil.get_default_user_email()
            self.own_emails = {email.lower()} if email else set()
        return self.own_emails

    def get_pwork(self, ups):
        """Get a Patchwork object for an upstream

        Args:
            ups (str or None): Upstream name

        Return:
            Patchwork or LoreList: Object to use for that upstream's
                patchwork server, or for its list on lore if it has no
                patchwork server

        Raises:
            ValueError: No patchwork URL is known
        """
        if self.pwork_for_testing:
            return self.pwork_for_testing
        lore_url = self.get_lore_url(ups)
        if lore_url:
            if lore_url not in self._pworks:
                self._pworks[lore_url] = lorelist.LoreList(lore_url)
            return self._pworks[lore_url]
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
        return [interpreter.get_python(), '-m', 'patman', '-v', *args]

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
        """dict: State of the most recent sync, with the sync settings and
        the recent traffic to each patchwork server"""
        job = self._sync_job()
        state = dict(job.state) if job else {
            'queued': False, 'running': False, 'cancelled': False,
            'started': None, 'finished': None, 'results': []}
        state['interval'] = self.sync_interval
        state['review_sync'] = self.review_sync
        state['num_series'] = info.get_gather_count(self.cser)
        state['traffic'] = traffic.get_summary(self.cser.db,
                                               cache=pwcache.get())
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
        """Resume any unfinished jobs, then start the periodic sync

        Args:
            _app (web.Application): Application which is starting (not used)
        """
        self.resume_jobs()
        if self.sync_interval:
            self._sync_task = asyncio.create_task(self._sync_loop())

    async def _stop_background(self, _app):
        """Stop the periodic sync, if running

        Args:
            _app (web.Application): Application which is stopping (not used)
        """
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass

    @web.middleware
    async def _auth(self, request, handler):
        """Check that the client provided the token, if one is needed

        The token can be given in an 'Authorization: Bearer' header or in a
        'token' query parameter. Only the API needs it, so that the page can
        load and ask for it.

        Args:
            request (web.Request): Request to check
            handler (callable): Next handler, to call if the request is allowed

        Return:
            web.Response: Response from the handler, or status 401 if the token
                is missing or wrong
        """
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
    async def _utc_offset(self, request, handler):
        """Say how far the server's time zone is from UTC

        Times from the database are local to the server and do not give a
        time zone, but the browser may be in a different one. Each API
        response says the server's offset, so that the page can work out
        when they were.

        Args:
            request (web.Request): Request being handled
            handler (callable): Next handler

        Return:
            web.Response: Response from the handler, with an
                'X-Patman-UTC-Offset' header giving the offset in minutes,
                e.g. -360 for UTC-6
        """
        resp = await handler(request)
        if request.path.startswith('/api/'):
            resp.headers['X-Patman-UTC-Offset'] = str(times.utc_offset())
        return resp

    @web.middleware
    async def _save_traffic(self, request, handler):
        """Count the requests sent to patchwork while handling a request

        The server runs until it is stopped, so it writes the counts out as
        it goes, rather than when it exits

        Args:
            request (web.Request): Request being handled
            handler (callable): Next handler

        Return:
            web.Response: Response from the handler
        """
        try:
            return await handler(request)
        finally:
            traffic.save(self.cser.db)

    @web.middleware
    async def _no_cache(self, request, handler):
        """Have the browser check the page and its files before reusing them

        The page is split into several files, so a browser which kept an old
        app.js after patman is updated would run it with the new page

        Args:
            request (web.Request): Request being handled
            handler (callable): Next handler

        Return:
            web.Response: Response from the handler, with a Cache-Control
                header for the page and its static files
        """
        resp = await handler(request)
        if request.path == '/' or request.path.startswith('/static/'):
            resp.headers['Cache-Control'] = 'no-cache'
        return resp

    @web.middleware
    async def _errors(self, request, handler):
        """Turn errors into a JSON response

        Args:
            request (web.Request): Request being handled
            handler (callable): Next handler

        Return:
            web.Response: Response from the handler, or a JSON object with an
                'error' message and a status of 409 if a job is busy, 400 for
                a bad request, 404 if something does not exist, or 502 if
                patchwork cannot be read
        """
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
        """Handle 'GET /api/series': list the series

        Args:
            _request (web.Request): Request (not used)

        Return:
            web.Response: List of series, with the status of each one's latest
                version
        """
        return web.json_response(info.get_series_list(self.cser))

    async def _series_detail(self, request):
        """Handle 'GET /api/series/<id>': show a series

        Args:
            request (web.Request): Request, with the series ID as 'idnum'

        Return:
            web.Response: Series with all its versions and their patches, along
                with the URL of its patchwork server

        Raises:
            ValueError: The series does not exist
        """
        data = info.get_series_detail(self.cser,
                                      _idnum(request))
        data['patchwork_url'] = self.get_pw_url(data['upstream'])
        return web.json_response(data)

    async def _series_sync(self, request):
        """Handle 'POST /api/series/<id>/sync': sync one version of a series

        This runs 'patman series gather -G' for the version as a job. The body
        may give a 'version', which defaults to the latest.

        Args:
            request (web.Request): Request, with the series ID as 'idnum'

        Return:
            web.Response: Job state, with status 202

        Raises:
            BadRequest: The version is invalid
            ValueError: The series or version does not exist, or the version
                has no patchwork link
            JobBusyError: The version is already being synced
        """
        idnum = _idnum(request)
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
        """Handle 'GET /api/reviews': list the reviewed series

        Args:
            _request (web.Request): Request (not used)

        Return:
            web.Response: List of reviewed series, most recent review first
        """
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
                str(ver['link']).isdigit() and
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
        """Handle 'GET /api/reviews/<id>': show the reviews of a series

        Each review is marked with the job acting on it, if any. Cover letters
        which are not yet known are looked up on patchwork.

        Args:
            request (web.Request): Request, with the series ID as 'idnum'

        Return:
            web.Response: Reviewed series with its versions and their reviews,
                along with its upstream, patchwork URL and project

        Raises:
            ValueError: The series does not exist
        """
        idnum = _idnum(request)
        data = info.get_review_detail(self.rcser, idnum)
        # Mark the reviews which a job is acting on, so the page can wait
        for ver in data['versions']:
            pending = self.pending_jobs(idnum, ver['version'])
            for item in ver['reviews']:
                done = item['status'] in database.REVIEW_DONE
                item['job'] = pending.get(item['seq'],
                                          None if done else pending.get(None))
        # 'patman review' uses the default upstream when none is given, so
        # a series reviewed that way is on its patchwork server
        data['upstream'] = (data['upstream'] or
                            self.cser.db.upstream_get_default())
        await self._set_review_branches(data['versions'], data['upstream'],
                                        'link')
        await self._find_covers(data)
        data['patchwork_url'] = self.get_pw_url(data['upstream'])
        proj = self.cser.project_get(data['upstream'])
        data['project'] = proj[2] if proj else None
        return web.json_response(data)

    async def _review_context(self, request):
        """Handle 'GET /api/reviews/<id>/context': show the agent's context

        The 'version' query parameter gives the version of the series. Each
        review has the parts of the context which the agent was given, as
        recorded with it, or else as found in the transcript of its session.

        Args:
            request (web.Request): Request, with the series ID as 'idnum'

        Return:
            web.Response: Object with the 'version' and its 'reviews', each
                with its 'id', 'seq', 'source' ('stored', 'transcript' or None
                if the context is not known) and 'parts', each with a 'title'
                and 'text'

        Raises:
            BadRequest: The version is missing or invalid
            ValueError: The series has no such version
        """
        idnum = _idnum(request)
        version = params.get_int(request.query, 'version')
        if not version:
            raise params.BadRequest('Please give the version')
        db = self.rcser.db
        svinfo = db.ser_ver_get_for_series(idnum, version)
        out = []
        for rev in db.review_get_for_version(svinfo.idnum):
            source, parts = given.get_context(db, rev.idnum)
            out.append({'id': rev.idnum, 'seq': rev.seq, 'source': source,
                        'parts': [{'title': title, 'text': text}
                                  for title, text in parts]})
        return web.json_response({'version': version, 'reviews': out})

    async def _todo_list(self, _request):
        """Handle 'GET /api/todo': list the todo items

        Args:
            _request (web.Request): Request (not used)

        Return:
            web.Response: List of todos, earliest first
        """
        return web.json_response(info.get_todo_list(self.cser))

    async def _stats(self, request):
        """Handle 'GET /api/stats': count the reviews, emails and replies

        The 'weeks' query parameter gives how many weeks to count, including
        this one, at most MAX_STATS_WEEKS.

        Args:
            request (web.Request): Request

        Return:
            web.Response: Object with the 'totals' and the 'weeks', as from
                stats.get_stats()

        Raises:
            BadRequest: The number of weeks is invalid
        """
        weeks = params.get_int(request.query, 'weeks') or stats.WEEKS
        return web.json_response(
            stats.get_stats(self.rcser.db, min(weeks, MAX_STATS_WEEKS)))

    async def _version(self, _request):
        """Handle 'GET /api/version': give patman's version and the project

        The version is worked out on the first request and kept.

        Args:
            _request (web.Request): Request (not used)

        Return:
            web.Response: Object with the 'version', which is None if it is
                not known, the 'project' which the server is for, from
                the 'project' review setting, e.g. 'dtc', and the
                'source_url' for browsing a line of its source, or None (see
                settings.source_url())
        """
        if self._version_str is None:
            self._version_str = info.get_version() or ''
        return web.json_response({'version': self._version_str or None,
                                  'project': settings.review_project(),
                                  'source_url': settings.source_url()})

    async def _comments(self, request):
        """Handle 'GET /api/comments/<kind>/<id>': read comments from patchwork

        The 'ups' query parameter selects the upstream whose patchwork server
        is used, defaulting as for the inbox. Comments read in the last few
        minutes are taken from the cache, unless the 'refresh' query parameter
        is 1.

        Each comment says whether we sent it, from our git user.email
        address, which is how a review we sent appears on patchwork

        Args:
            request (web.Request): Request, with 'kind' ('patch' or 'cover')
                and the patchwork ID as 'item_id', or 'lore:<msgid>' for one
                on lore

        Return:
            web.Response: List of comments on the patch or cover letter

        Raises:
            ValueError: No patchwork URL is known for the upstream
        """
        kind = request.match_info['kind']
        item_id = _item_id(request)
        pwork = self._mail_pwork(request, item_id)
        max_age = (0 if request.query.get('refresh') == '1'
                   else patchwork.COMMENTS_MAX_AGE)
        async with aiohttp.ClientSession() as client:
            if kind == 'patch':
                comments = await pwork.get_patch_comments(client, item_id,
                                                          max_age)
            else:
                comments = await pwork.get_cover_comments(client, item_id,
                                                          max_age)
        return web.json_response([info.comment_info(c, self.get_own_emails())
                                  for c in comments or []])

    def _mail_pwork(self, request, item_id):
        """Get what to read a patch or cover letter, or its comments, with

        Args:
            request (web.Request): Request, with the upstream as the 'ups'
                query parameter, or none to use the default one
            item_id (int or str): Patchwork ID, or 'lore:<msgid>'

        Return:
            Patchwork or LoreList: Object for the upstream

        Raises:
            ValueError: The item is on lore, but the upstream's series are
                not
        """
        ups = request.query.get('ups')
        if not ups:
            try:
                ups = self._default_upstream()
            except ValueError:
                ups = None
        pwork = self.get_pwork(ups)
        if isinstance(item_id, str) and not isinstance(pwork,
                                                       lorelist.LoreList):
            raise ValueError(f"'{item_id}' is on lore, but upstream '{ups}' "
                             "has no list there; give one with 'ups'")
        return pwork

    async def _mail(self, request):
        """Handle 'GET /api/patch/<id>' or 'GET /api/cover/<id>'

        This reads a patch or cover letter from patchwork. The 'ups' query
        parameter selects the upstream whose patchwork server is used,
        defaulting as for the inbox. A patch read in the last hour is taken
        from the cache, unless the 'refresh' query parameter is 1. The
        response says when it was read.

        Args:
            request (web.Request): Request, with 'kind' ('patch' or 'cover')
                and the patchwork ID as 'item_id', or 'lore:<msgid>' for one
                on lore

        Return:
            web.Response: Information about the patch or cover letter

        Raises:
            ValueError: No patchwork URL is known for the upstream
        """
        item_id = _item_id(request)
        pwork = self._mail_pwork(request, item_id)
        refresh = request.query.get('refresh') == '1'
        read = None
        async with aiohttp.ClientSession() as client:
            if request.match_info['kind'] == 'patch':
                data = await pwork.get_patch(
                    client, item_id,
                    max_age=0 if refresh else patchwork.PATCH_MAX_AGE)
                read = pwork.read_time(pwork.patch_path(item_id))
            else:
                data = await pwork.get_cover(client, item_id)
        out = info.mail_info(data)
        out['fetched'] = times.stamp_str(read) or times.now_str()
        return web.json_response(out)

    async def _blob(self, request):
        """Handle 'GET /api/blob': read a file from the git repo

        Each 'hash' query parameter gives a blob to try, and the first which is
        in the repo is used.

        Args:
            request (web.Request): Request

        Return:
            web.Response: Object with the 'hash' found and the 'text' of the
                file

        Raises:
            ValueError: A hash is invalid, none of the blobs is in the repo or
                the file is too large or not text
        """
        oid, text = await info.read_blob(self.cser.topdir,
                                         request.query.getall('hash', []))
        return web.json_response({'hash': oid, 'text': text})

    async def _sync_get(self, _request):
        """Handle 'GET /api/sync': give the state of the sync

        Args:
            _request (web.Request): Request (not used)

        Return:
            web.Response: Sync state, see sync_state
        """
        return web.json_response(self.sync_state)

    async def _sync_post(self, _request):
        """Handle 'POST /api/sync': start a sync of all series

        Args:
            _request (web.Request): Request (not used)

        Return:
            web.Response: Sync state, with status 202, or 409 if a sync is
                already waiting or running
        """
        job = self._start_sync()
        if not job:
            return web.json_response(self.sync_state, status=409)
        self.start_job(job)

        # Let the sync start, so the response shows it running
        await asyncio.sleep(0)
        return web.json_response(self.sync_state, status=202)

    async def _sync_cancel_post(self, _request):
        """Handle 'POST /api/sync/cancel': stop the sync

        Args:
            _request (web.Request): Request (not used)

        Return:
            web.Response: Sync state, with status 202, or 409 if no sync is
                waiting or running
        """
        if not self.cancel_sync():
            return web.json_response(self.sync_state, status=409)
        return web.json_response(self.sync_state, status=202)

    async def _upstreams(self, _request):
        """Handle 'GET /api/upstreams': list the upstreams with patchwork

        Args:
            _request (web.Request): Request (not used)

        Return:
            web.Response: List of upstreams which have a patchwork URL, or a
                list on lore, each with its 'name', whether it is the
                'default', its 'patchwork_url' and its 'lore_url', which is
                None if it has a patchwork URL
        """
        out = [{'name': name, 'default': bool(is_default),
                'patchwork_url': pw_url or None,
                'lore_url': None if pw_url else lore_url}
               for name, (_, is_default, pw_url, *_, lore_url) in
               self._served_upstreams().items()]
        return web.json_response(out)

    async def _inbox(self, request):
        """Handle 'GET /api/inbox': list recent series on patchwork

        The 'ups' query parameter selects the upstream, defaulting to the
        default one, and 'page' selects the page of results. For a later
        page, 'cached=1' uses the copy in the cache however old it is, so
        that the page can show again the pages it showed before.

        What is read from patchwork is kept in the cache (see pwcache) for
        INBOX_KEEP seconds, so that showing the inbox again, from this page
        or another, or after a restart, does not read it again; 'refresh=1'
        reads it anyway. A later page is only used from the cache if it is no
        older than the first, so that it follows on from it.

        Args:
            request (web.Request): Request

        For an upstream whose list is on lore, the series are read from lore
        (see LoreList.get_recent_series()), which has no patch states.

        The first page starts with the series added to the inbox (see
        _read_added()), each marked as 'added'. For an upstream with no feed
        (see 'patman upstream add --no-feed'), these are all it lists.

        Return:
            web.Response: Object with the upstream, page, page size, whether
                there may be 'more' pages, whether it has a 'feed', and a list
                of series

        Raises:
            BadRequest: The page is invalid
            ValueError: The upstream has no patchwork project
        """
        ups = request.query.get('ups') or self._default_upstream()
        page = params.get_int(request.query, 'page') or 1
        pwork = self.get_pwork(ups)
        project = self._inbox_project(ups, pwork)
        feed = not self.cser.db.upstream_get_no_feed(ups)
        first = pwork.recent_series_path(project, 1, INBOX_PAGE)
        if request.query.get('refresh') == '1':
            max_age = 0
            # Fetch the upstream too, to see what has been applied
            if ups in self._fetches:
                self._fetches[ups]['started'] = None
        elif page == 1:
            max_age = INBOX_KEEP
        elif request.query.get('cached') == '1':
            # The page shows again what it showed before, however old
            max_age = None
        else:
            max_age = pwork.read_age(first) or 0
        # Fetch the upstream while reading patchwork, since neither needs the
        # other
        fetching = asyncio.ensure_future(self._fetch_upstream(ups))
        try:
            series_list, more = await self._read_inbox_page(
                pwork, ups, project, page, max_age, feed)
        except BaseException:
            # The fetch carries on; this only stops waiting for it
            fetching.cancel()
            raise
        read = pwork.read_time(first) if feed else None
        # This waits for the fetch, so comes before the time of the fetch
        series = await self._inbox_items(series_list, ups, pwork, fetching)
        return web.json_response({
            'ups': ups, 'page': page, 'per_page': INBOX_PAGE, 'feed': feed,
            'more': more,
            'fetched': times.stamp_str(read) or times.now_str(),
            'upstream_fetched': self._fetched_str(ups),
            'series': series})

    # pylint: disable-next=R0913,R0917
    async def _read_inbox_page(self, pwork, ups, project, page, max_age,
                               feed):
        """Read a page of the inbox: the series added to it, then the feed

        Args:
            pwork (Patchwork or LoreList): Object to read the series with
            ups (str): Upstream name
            project (str): Patchwork project's link name, or the list's name
            page (int): Page to read, from 1; only the first has the series
                added to the inbox
            max_age (float or None): Oldest copy of the feed to use from the
                cache, in seconds, 0 to read it, and the added series, again,
                or None for any
            feed (bool): True to read the feed, False if the inbox has only
                the added series

        Return:
            tuple:
                list of dict: Series, as from get_recent_series()
                bool: True if there may be more pages
        """
        added = (await self._read_added(pwork, ups, max_age == 0)
                 if page == 1 else [])
        found = (await self._read_inbox(pwork, project, page, max_age)
                 if feed else [])
        # Each page of lore's feed has a set number of messages, not series
        more = feed and (bool(found) if isinstance(pwork, lorelist.LoreList)
                         else len(found) >= INBOX_PAGE)
        # A series which is added comes first, so is left out of the feed
        ids = {str(ser['id']) for ser in added}
        return added + [ser for ser in found if str(ser['id']) not in ids], more

    async def _inbox_items(self, series_list, ups, pwork, fetching):
        """Get the information about the series in the inbox, to show them

        Args:
            series_list (list of dict): Series, as from _read_inbox_page()
            ups (str): Upstream name
            pwork (Patchwork or LoreList): Object the series were read with
            fetching (asyncio.Future): Completes when the upstream is fetched
                (see _upstream_commits())

        Return:
            list of dict: Series, as from info.get_inbox(), each with the
                'job' reviewing it, if any, whether it is 'added' to the
                inbox and its review branch (see _set_review_branches())
        """
        commits = await self._upstream_commits(series_list, ups, fetching)
        states = pwork.get_states([pat['id'] for ser in series_list
                                   for pat in ser.get('patches') or []])
        series = info.get_inbox(
            self.cser, self.rcser, series_list,
            info.get_patch_status(series_list, commits, states),
            self.get_own_emails())
        links = self._inbox_links(ups)
        for item in series:
            item['job'] = self._reviewing(item['id'])
            item['added'] = str(item['id']) in links
        await self._set_review_branches(series, ups, 'id')
        return series

    async def _read_added(self, pwork, ups, refresh):
        """Read the series added to the inbox of an upstream

        Args:
            pwork (Patchwork or LoreList): Object to read the series with
            ups (str): Upstream name
            refresh (bool): True to read each series again, rather than use
                a recent copy

        Return:
            list of dict: Series, as from get_series(), latest added first.
                One which cannot be read has only its 'id' and 'name'
        """
        added = self.cser.db.inbox_get(ups)
        if not added:
            return []
        kwargs = {'max_age': 0} if refresh else {}
        async with aiohttp.ClientSession() as client:
            results = await asyncio.gather(
                *[pwork.get_series(client, link, **kwargs)
                  for link, *_ in added], return_exceptions=True)
        out = []
        for (link, name, _), result in zip(added, results):
            if isinstance(result, Exception):
                tout.warning(f"Cannot read series {link}: {result}")
                result = {'id': int(link) if link.isdigit() else link,
                          'name': name, 'patches': [], 'received_all': True}
            out.append(result)
        return out

    async def _search(self, request):
        """Handle 'GET /api/search': search for series to add to the inbox

        The 'ups' query parameter selects the upstream, as for the inbox, and
        'q' gives the text to search for: on patchwork, in the names of the
        series and their patches (see Patchwork.search_series()), or on
        lore, in the subjects of the patches and cover letters (see
        LoreList.search_series())

        Args:
            request (web.Request): Request

        Return:
            web.Response: Object with the upstream, the text, 'where' it was
                searched for, e.g. 'uboot on patchwork.ozlabs.org', and a
                list of series, newest first, each saying whether it is
                'added' to the inbox

        Raises:
            BadRequest: There is no text to search for
            ValueError: The upstream has no patchwork project
        """
        ups = request.query.get('ups') or self._default_upstream()
        text = ' '.join((request.query.get('q') or '').split())
        if not text:
            raise params.BadRequest('Please give some text to search for')
        pwork = self.get_pwork(ups)
        project = self._inbox_project(ups, pwork)
        async with aiohttp.ClientSession() as client:
            found = await pwork.search_series(client, project, text)
        links = self._inbox_links(ups)
        series = []
        for data in found:
            submitter = data.get('submitter') or {}
            series.append({
                'id': data['id'], 'name': data.get('name') or '',
                'version': data.get('version'), 'date': data.get('date'),
                'submitter': submitter.get('name') or submitter.get('email'),
                'total': data.get('total'), 'web_url': data.get('web_url'),
                'added': str(data['id']) in links})
        site = pwork.url.split('://', 1)[-1]
        where = (site if isinstance(pwork, lorelist.LoreList)
                 else f'{project} on {site}')
        return web.json_response({'ups': ups, 'q': text, 'where': where,
                                  'series': series})

    async def _inbox_add(self, request):
        """Handle 'POST /api/inbox/add': add a series to an upstream's inbox

        Args:
            request (web.Request): Request, whose body is checked by
                params.parse_inbox_req()

        Return:
            web.Response: Object with the upstream and the series 'id'

        Raises:
            BadRequest: The request is invalid
        """
        ups, link, name = params.parse_inbox_req(
            await self._read_json(request), self.cser.db.upstream_get_dict())
        lore = isinstance(self.get_pwork(ups), lorelist.LoreList)
        if lore != ('@' in link):
            raise params.BadRequest(
                f"Series '{link}' is not on {ups}'s "
                f"{'list' if lore else 'patchwork server'}")
        self.cser.db.inbox_add(ups, link, name, times.now_str())
        self.cser.commit()
        return web.json_response({'ups': ups, 'id': link})

    async def _inbox_remove(self, request):
        """Handle 'POST /api/inbox/remove': remove a series from an inbox

        Args:
            request (web.Request): Request, whose body is checked by
                params.parse_inbox_req()

        Return:
            web.Response: Object with the upstream and the series 'id'

        Raises:
            BadRequest: The request is invalid
            ValueError: The series is not in the inbox
        """
        ups, link, _ = params.parse_inbox_req(
            await self._read_json(request), self.cser.db.upstream_get_dict())
        if not self.cser.db.inbox_remove(ups, link):
            raise ValueError(f"Series '{link}' is not in {ups}'s inbox")
        self.cser.commit()
        return web.json_response({'ups': ups, 'id': link})

    def _served_upstreams(self):
        """Get the upstreams which have a patchwork server or a list on lore

        Return:
            dict: Upstreams, as from Database.upstream_get_dict()
        """
        return {name: info for name, info in
                self.cser.db.upstream_get_dict().items()
                if info[2] or info[-1]}

    def _inbox_links(self, ups):
        """Get the links of the series added to the inbox of an upstream

        Args:
            ups (str): Upstream name

        Return:
            set of str: Links, e.g. patchwork series IDs or Message-IDs
        """
        return {link for link, *_ in self.cser.db.inbox_get(ups)}

    def _default_upstream(self):
        """Get the upstream to use for a request which does not give one

        Return:
            str: The default upstream, or the only one with a patchwork
                server or a list on lore, if there is no default

        Raises:
            ValueError: There is no default upstream, and not just one
        """
        ups = self.cser.db.upstream_get_default()
        if ups:
            return ups
        names = list(self._served_upstreams())
        if len(names) != 1:
            raise ValueError("There is no default upstream, so give one with "
                             "'ups'")
        return names[0]

    def _inbox_project(self, ups, pwork):
        """Get the project whose series the inbox lists

        Args:
            ups (str): Upstream name
            pwork (Patchwork or LoreList): Object to read the series with

        Return:
            str: Patchwork project's link name, or the name of the list on
                lore

        Raises:
            ValueError: The upstream has no patchwork project
        """
        if isinstance(pwork, lorelist.LoreList):
            return pwork.link_name
        proj = self.cser.project_get(ups)
        if not proj:
            raise ValueError(f"No patchwork project for upstream '{ups}'")
        return proj[2]

    @staticmethod
    async def _read_inbox(pwork, project, page, max_age):
        """Read a page of the inbox from patchwork, with the latest events

        Args:
            pwork (Patchwork): Patchwork for the upstream
            project (str): Patchwork project's link name
            page (int): Page to read, from 1
            max_age (float or None): Oldest copy in seconds to use from the
                cache, or None for any

        Return:
            list of dict: Series from patchwork
        """
        async with aiohttp.ClientSession() as client:
            series_list = await pwork.get_recent_series(
                client, project, page, INBOX_PAGE, max_age=max_age)
            # Learn which patches have changed state since the last check
            await pwork.follow_events(client, project)
        return series_list

    async def _upstream_commits(self, series_list, ups, fetching):
        """Get the commits upstream which may be patches in the inbox

        These are the commits on the upstream's main and next branches since
        the oldest series, so that the inbox can show which patches have
        been applied. They are read once the upstream is fetched, if the
        inbox has not done so for FETCH_MAX_AGE (see _fetch_upstream()).

        Args:
            series_list (list of dict): Series from patchwork
            ups (str): Upstream name, which is also the git remote
            fetching (asyncio.Future): Completes when the fetch is done, or
                the inbox stops waiting for it

        Return:
            dict: Commits by subject (see info.get_upstream_commits())
        """
        dates = [ser['date'] for ser in series_list if ser.get('date')]
        if not dates:
            fetching.cancel()
            return {}
        # Allow for the time zone, since patchwork gives the date in UTC
        since = (datetime.fromisoformat(min(dates)[:10]) -
                 timedelta(days=1)).strftime('%Y-%m-%d')
        await fetching
        refs = [apply.main_branch(ups, self.cser.topdir), f'{ups}/next']
        return await info.get_upstream_commits(self.cser.topdir, refs, since)

    async def _git(self, *args):
        """Run git in the server's repo

        Args:
            args (list of str): Arguments for git

        Return:
            tuple:
                int: Return code
                str: What git wrote to stderr
                str: What git wrote to stdout
        """
        proc = await asyncio.create_subprocess_exec(
            'git', *args, cwd=self.cser.topdir, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        return (proc.returncode, err.decode('utf-8', 'replace').strip(),
                out.decode('utf-8', 'replace'))

    async def _set_review_branches(self, items, ups, key):
        """Note the review branch of each series, from the server's repo

        This lets the page say which branch 'patman review --checkout'
        checks out, or which it makes if there is none yet

        Args:
            items (list of dict): Series or versions, each of which gains a
                'review_branch', or None if it has none, and the
                'review_name' of the branch which a review makes
            ups (str or None): Upstream name
            key (str): Key in each item which holds the patchwork link
        """
        code, _, out = await self._git(
            'for-each-ref', '--format=%(refname:short)', 'refs/heads/*-review')
        branches = set() if code else set(out.split())
        for item in items:
            item['review_branch'] = checkout.find_branch_in(item[key], ups,
                                                            branches)
            item['review_name'] = (lookup.make_review_name(item[key], ups)
                                   if item[key] else None)

    async def _run_fetch(self, ups):
        """Fetch an upstream, noting when it is done

        Args:
            ups (str): Upstream name, which is also the git remote
        """
        code, err, _ = await self._git('fetch', '-q', ups)
        if code:
            tout.warning(f"Cannot fetch upstream '{ups}': {err}")
        else:
            self._fetches[ups]['done'] = self._clock()

    async def _fetch_upstream(self, ups):
        """Fetch an upstream, if the inbox has not done so for a while

        This keeps its branches current, so that the inbox can show which
        patches have been applied, without a sync. It sends nothing to
        patchwork. The inbox reads patchwork meanwhile, and waits for the
        fetch for up to FETCH_WAIT seconds in all, then shows what it has
        while the fetch carries on.
        'Refresh' sets 'started' to None, so that it fetches at once.

        Args:
            ups (str): Upstream name, which is only fetched if it is a git
                remote
        """
        fetch = self._fetches.get(ups)
        now = self._clock()
        if not fetch or (fetch['task'].done() and (
                fetch['started'] is None or
                now - fetch['started'] >= FETCH_MAX_AGE)):
            code, _, remotes = await self._git('remote')
            if code or ups not in remotes.split():
                return
            fetch = self._fetches.setdefault(ups, {'done': None})
            fetch['started'] = now
            fetch['task'] = asyncio.ensure_future(self._run_fetch(ups))
        if not fetch['task'].done():
            try:
                await asyncio.wait_for(asyncio.shield(fetch['task']),
                                       FETCH_WAIT)
            except asyncio.TimeoutError:
                pass

    def _fetched_str(self, ups):
        """Get when the inbox last fetched an upstream

        Args:
            ups (str): Upstream name

        Return:
            str or None: Time it finished, as in the database, or None if it
                has not fetched it since the server started
        """
        done = (self._fetches.get(ups) or {}).get('done')
        return times.stamp_str(done)

    def _job_state(self, job):
        """Get the state of a job, with the reviewed series it acts on

        Args:
            job (Job): Job

        Return:
            dict: The job's state, with 'review' (int or None), the ID of the
                series in the review database, and 'name' (str or None), its
                name, once known
        """
        state = dict(job.state)
        review = None
        if job.kind == 'review':
            target = job.state['target']
            if target:
                review = target['series']
            else:
                for cmd in job.cmds:
                    link = cmd_series(cmd)
                    if link:
                        found = self.rcser.db.series_find_by_link(link)
                        review = found[0] if found else None
        state['review'] = review
        if review:
            name, desc, _ = self.rcser.db.series_get_info(review)
            state['name'] = desc or name
        return state

    async def _job_list(self, _request):
        """Handle 'GET /api/jobs': list the jobs

        Args:
            _request (web.Request): Request (not used)

        Return:
            web.Response: List of job states
        """
        return web.json_response([self._job_state(job)
                                  for job in self.get_jobs()])

    async def _job_detail(self, request):
        """Handle 'GET /api/jobs/<id>': show a job

        Args:
            request (web.Request): Request, with the job number as 'idnum'

        Return:
            web.Response: Job state

        Raises:
            ValueError: The job does not exist
        """
        job = self.get_job(_idnum(request))
        return web.json_response(self._job_state(job))

    async def _job_cancel(self, request):
        """Handle 'POST /api/jobs/<id>/cancel': stop a job

        Args:
            request (web.Request): Request, with the job number as 'idnum'

        Return:
            web.Response: Job state, with status 202, or 409 if the job has
                finished

        Raises:
            ValueError: The job does not exist
        """
        job = self.get_job(_idnum(request))
        status = 202 if self.cancel_job(job) else 409
        return web.json_response(job.state, status=status)

    async def _read_json(self, request):
        """Read the JSON body of a request, which may be empty

        Args:
            request (web.Request): Request to read

        Return:
            dict: Request data

        Raises:
            BadRequest: The body is not a JSON object
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

    def _review_job(self, title, *args, target=None, parallel=False,
                    name=None):
        """Start a job which runs 'patman review'

        Args:
            title (str): Description of the job
            args (list of str): Arguments for 'patman review'
            target (dict or None): Reviews which the job acts on (see Job)
            parallel (bool): True to run alongside other jobs (see Job)
            name (str or None): Name of the series, if known

        Return:
            web.Response: Response giving the job state
        """
        cmd = self.get_patman_cmd('review', *args, *self._review_db_args())
        job = self.add_job('review', title, [cmd], target, parallel)
        job.state['name'] = name or None
        return web.json_response(job.state, status=202)

    def _reviewing(self, link):
        """Find the job reviewing a series, if one is waiting or running

        Other jobs which run 'patman review' on the series, e.g. to redraft
        or send its reviews, are not counted

        Args:
            link (int or str): Patchwork series ID

        Return:
            int or None: Job number, or None if the series is not being
                reviewed
        """
        for job in self._jobs.values():
            if not job.active or job.kind != 'review':
                continue
            for cmd in job.cmds:
                if (cmd_series(cmd) == str(link) and
                        not REVIEW_ACTIONS & set(cmd)):
                    return job.idnum
        return None

    async def _review_post(self, request):
        """Handle 'POST /api/review': review a series

        This runs 'patman review' as a job, with the options given in the body
        (see params.parse_review_req()).

        Args:
            request (web.Request): Request

        Return:
            web.Response: Job state, with status 202

        Raises:
            BadRequest: The request is invalid
            JobBusyError: The series is already being reviewed
        """
        data = await self._read_json(request)
        args, title = params.parse_review_req(
            data, list(self.cser.db.upstream_get_dict()))
        link = cmd_series(args)
        if link:
            busy = self._reviewing(link)
            if busy:
                raise jobs.JobBusyError(
                    f'Series {link} is already being reviewed, by job {busy}')
        # Each review has its own worktree, so several can run at once
        # The page may give the series' name, which is shown until the
        # review has recorded the series
        return self._review_job(title, *args, parallel=True,
                                name=params.get_str(data, 'name'))

    async def _review_scan(self, request):
        """Handle 'POST /api/review/scan': look for new versions to review

        This runs 'patman review --scan' as a job. If the body gives 'drafts',
        Gmail drafts are created for the new reviews.

        Args:
            request (web.Request): Request

        Return:
            web.Response: Job state, with status 202
        """
        data = await self._read_json(request)
        args = ['--scan'] + (['-d'] if data.get('drafts') else [])
        return self._review_job('Scan for new versions', *args)

    # pylint: disable-next=R0913,R0917
    def _respond_job(self, idnum, version, seq, comment_id, note=None):
        """Start a job which suggests a response to a comment on patchwork

        Args:
            idnum (int): Series ID in the review database
            version (int or None): Version, or None for the latest
            seq (int): Patch which the comment is about, 0 for the cover
                letter
            comment_id (int): ID of the comment on patchwork
            note (str or None): What the reviewer would like to say, for the
                agent

        Return:
            web.Response: Response giving the job state

        Raises:
            ValueError: The series or version does not exist, or has no link
        """
        label, version, args = self._version_args(
            idnum, version, '--respond-comment', str(comment_id), '-i',
            str(seq))
        if note:
            args += ['-c', note]
        what = f'patch {seq}' if seq else 'the cover letter'
        return self._review_job(
            f'Respond to a comment on {what} of {label}', *args)

    async def _respond(self, request):
        """Handle 'POST /api/reviews/<id>/respond': respond to a comment

        This has the agent suggest a response to a comment on patchwork
        about the cover letter or a patch, as a job. The body gives the
        patchwork 'comment' ID and the 'seq' of the patch it is about (0 for
        the cover letter), and may give a 'version', which defaults to the
        latest, and a 'note' saying what the reviewer would like to say.

        Args:
            request (web.Request): Request, with the series ID as 'idnum'

        Return:
            web.Response: Job state, with status 202

        Raises:
            BadRequest: The request is invalid
            ValueError: The series or version does not exist, or has no link
        """
        idnum = _idnum(request)
        data = await self._read_json(request)
        seq = params.get_seq(data)
        comment = params.get_int(data, 'comment')
        if not comment:
            raise params.BadRequest('Please give the comment to respond to')
        return self._respond_job(idnum, params.get_int(data, 'version'), seq,
                                 comment, params.get_str(data, 'note'))

    async def _redraft(self, request):
        """Handle 'POST /api/reviews/<id>/redraft': recreate review drafts

        The body may give a 'version', which defaults to the latest, and
        'seqs', the patches to redraft (0 for the cover letter), which default
        to all of them.

        Args:
            request (web.Request): Request, with the series ID as 'idnum'

        Return:
            web.Response: Job state, with status 202

        Raises:
            BadRequest: The version or seqs are invalid
            ValueError: The series or version does not exist, or has no link
            JobBusyError: A job is already acting on one of the reviews
        """
        idnum = _idnum(request)
        data = await self._read_json(request)
        return self._redraft_job(idnum, params.get_int(data, 'version'),
                                 params.get_seqs(data))

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
            ValueError: The version has no link
            JobBusyError: A job is already acting on one of the reviews
        """
        if seqs:
            action = [*action, '-i',
                      ','.join(str(seq) for seq in sorted(seqs))]
        label, version, args = self._version_args(idnum, version, *action)
        self.check_free(idnum, version, seqs, self._unsent(idnum, version))
        target = {'series': idnum, 'version': version, 'seqs': sorted(seqs)}
        return args, label, target

    def _version_args(self, idnum, version, *action):
        """Get the 'patman review' arguments which act on a reviewed version

        Args:
            idnum (int): Series ID in the review database
            version (int or None): Version, or None for the latest
            action (list of str): Arguments giving the action, which go
                after the series and before its upstream

        Return:
            tuple:
                str: Series and version, for a job title, e.g. "'foo' v2"
                int: Version
                list of str: Arguments giving the series, the action and the
                    upstream

        Raises:
            ValueError: The series or version does not exist, or has no link
        """
        name, desc, ups = self.rcser.db.series_get_info(idnum)
        version = version or self.rcser.db.series_get_max_version(idnum)
        link = self.rcser.db.ser_ver_get_link(idnum, version)
        if not link:
            raise ValueError(f"Series '{name}' v{version} has no link")
        args = ['-s', str(link), *action] + (['-U', ups] if ups else [])
        return f"'{desc or name}' v{version}", version, args

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
            svr.idnum) if rev.status not in database.REVIEW_DONE]

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
        """Handle 'POST /api/reviews/<id>/send': send review drafts

        The body may give a 'version', which defaults to the latest, and
        'seqs', the patches whose reviews to send (0 for the cover letter),
        which default to all of them.

        Args:
            request (web.Request): Request, with the series ID as 'idnum'

        Return:
            web.Response: Job state, with status 202

        Raises:
            BadRequest: The version or seqs are invalid
            ValueError: The series or version does not exist, or has no link
            JobBusyError: A job is already acting on one of the reviews
        """
        idnum = _idnum(request)
        data = await self._read_json(request)
        seqs = params.get_seqs(data)
        args, what, target = self._series_review_args(
            idnum, params.get_int(data, 'version'), seqs, '--send')
        return self._review_job(f'Send {params.parts_text(seqs)} of {what}',
                                *args, target=target)

    async def _hide(self, request):
        """Handle 'POST /api/reviews/<id>/hide': hide a reviewed series

        This hides the series from the list of reviews, or shows it again.
        The body gives 'hidden' (bool).

        Args:
            request (web.Request): Request, with the series ID as 'idnum'

        Return:
            web.Response: Object with the series 'id' and whether it is
                'hidden'

        Raises:
            BadRequest: 'hidden' is missing or not true or false
            ValueError: The series does not exist
        """
        idnum = _idnum(request)
        data = await self._read_json(request)
        hidden = params.get_bool(data, 'hidden',
                                 "Please say whether it is 'hidden'")
        self.rcser.db.series_get_info(idnum)
        self.rcser.db.series_set_hidden(idnum, hidden)
        self.rcser.commit()
        return web.json_response({'id': idnum, 'hidden': hidden})

    async def _delete(self, request):
        """Handle 'POST /api/reviews/<id>/delete': delete reviews

        This deletes the reviews of some patches, which must not have been
        sent, along with their Gmail drafts, as a job. The body gives 'seqs',
        the patches (0 for the cover letter), and may give a 'version',
        which defaults to the latest.

        Args:
            request (web.Request): Request, with the series ID as 'idnum'

        Return:
            web.Response: Job state, with status 202

        Raises:
            BadRequest: The version or seqs are invalid, or no seqs are given
            ValueError: The series or version does not exist, or has no link
            JobBusyError: A job is already acting on one of the reviews
        """
        idnum = _idnum(request)
        data = await self._read_json(request)
        seqs = params.get_seqs(data)
        if not seqs:
            raise params.BadRequest('Please say which reviews to delete')
        args, what, target = self._series_review_args(
            idnum, params.get_int(data, 'version'), seqs, '--delete')
        return self._review_job(
            f'Delete {params.parts_text(seqs)} of {what}', *args,
            target=target)

    async def _revise(self, request):
        """Handle 'POST /api/reviews/<id>/revise': revise reviews

        This has the agent revise the reviews, as a job. The body gives a
        'note' saying what to change, and may give the 'part' of the review
        which it is about, a 'version' (default the latest), 'seqs' as for
        _send() and 'drafts' to update the Gmail drafts too.

        Args:
            request (web.Request): Request, with the series ID as 'idnum'

        Return:
            web.Response: Job state, with status 202

        Raises:
            BadRequest: The note, part, version or seqs are invalid
            ValueError: The series or version does not exist, or has no link
            JobBusyError: A job is already acting on one of the reviews
        """
        idnum = _idnum(request)
        data = await self._read_json(request)
        note = params.get_str(data, 'note', 'Please say what to change')
        part = params.get_str(data, 'part')
        if part:
            note = f'About this part of the review:\n\n{part}\n\n{note}'
        seqs = params.get_seqs(data)
        args, what, target = self._series_review_args(
            idnum, params.get_int(data, 'version'), seqs, '--revise', note)
        if data.get('drafts'):
            args.append('-d')
        return self._review_job(f'Revise {params.parts_text(seqs)} of {what}',
                                *args, target=target)

    async def _review_item(self, request):
        """Handle 'POST /api/review-items/<id>': edit a review

        The body may give a new 'body' and whether the review is 'approved',
        which otherwise depends on whether the body gives a Reviewed-by tag. If
        it gives 'redraft', the Gmail draft is recreated afterwards.

        Args:
            request (web.Request): Request, with the review ID as 'idnum'

        Return:
            web.Response: Updated review, or the state of the redraft job with
                status 202, or status 409 if the review has been sent

        Raises:
            BadRequest: The body is empty or invalid
            ValueError: The review does not exist
            JobBusyError: A job is acting on the review
        """
        db = self.rcser.db
        rev = db.review_get(_idnum(request))
        data = await self._read_json(request)
        if rev.status in database.REVIEW_DONE:
            return _already_sent('This review')
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
        """Handle 'POST /api/replies/<id>': update the response to a reply

        The body may give the 'response', 'skip' to skip the reply, and 'draft'
        to create a Gmail draft of the response afterwards.

        Args:
            request (web.Request): Request, with the reply ID as 'idnum'

        Return:
            web.Response: Updated reply, or the state of the draft job with
                status 202, or status 409 if the response has been sent

        Raises:
            BadRequest: The response is invalid
            ValueError: The reply does not exist
        """
        db = self.rcser.db
        reply = db.reply_get(_idnum(request))
        data = await self._read_json(request)
        if reply.status == database.REPLY_SENT:
            return _already_sent('The response')
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
        """Handle 'POST /api/replies/<id>/suggest': suggest a response

        This has the agent suggest a response to a reply, as a job.

        Args:
            request (web.Request): Request, with the reply ID as 'idnum'

        Return:
            web.Response: Job state, with status 202

        Raises:
            ValueError: The reply does not exist
        """
        reply = self.rcser.db.reply_get(_idnum(request))
        if reply.comment_id:
            # A comment on patchwork is read again, to respond to it
            svr = self.rcser.db.ser_ver_get_by_id(reply.svid)
            return self._respond_job(svr.series_id, svr.version, reply.seq,
                                     reply.comment_id)
        return self._review_job(f'Suggest a response to reply {reply.idnum}',
                                '--suggest-reply', str(reply.idnum))

    async def _reply_send(self, request):
        """Handle 'POST /api/replies/<id>/send': send the response to a reply

        Args:
            request (web.Request): Request, with the reply ID as 'idnum'

        Return:
            web.Response: Job state, with status 202, or status 409 if the
                response has been sent

        Raises:
            BadRequest: There is no response to send
            ValueError: The reply does not exist
        """
        reply = self.rcser.db.reply_get(_idnum(request))
        if reply.status == database.REPLY_SENT:
            return _already_sent('The response')
        if not (reply.response or '').strip():
            raise params.BadRequest('There is no response to send')
        return self._review_job(f'Send the response to reply {reply.idnum}',
                                '--send-reply', str(reply.idnum))

    async def _index(self, _request):
        """Handle 'GET /': send the page

        Args:
            _request (web.Request): Request (not used)

        Return:
            web.FileResponse: The page's HTML
        """
        return web.FileResponse(os.path.join(WEB_DIR, 'index.html'))

    def make_app(self):
        """Create the aiohttp application

        Return:
            web.Application: Application, ready to run
        """
        app = web.Application(
            middlewares=[self._errors, self._auth, self._no_cache,
                         self._utc_offset, self._save_traffic])
        app.router.add_get('/api/series', self._series_list)
        app.router.add_get(r'/api/series/{idnum:\d+}', self._series_detail)
        app.router.add_post(r'/api/series/{idnum:\d+}/sync',
                            self._series_sync)
        app.router.add_get('/api/reviews', self._review_list)
        app.router.add_get(r'/api/reviews/{idnum:\d+}', self._review_detail)
        app.router.add_get(r'/api/reviews/{idnum:\d+}/context',
                           self._review_context)
        app.router.add_get('/api/todo', self._todo_list)
        app.router.add_get('/api/stats', self._stats)
        app.router.add_get('/api/version', self._version)
        item = r'{kind:patch|cover}/{item_id:' + ITEM_ID + '}'
        app.router.add_get('/api/comments/' + item, self._comments)
        app.router.add_get('/api/' + item, self._mail)
        app.router.add_get('/api/blob', self._blob)
        app.router.add_get('/api/sync', self._sync_get)
        app.router.add_post('/api/sync', self._sync_post)
        app.router.add_post('/api/sync/cancel', self._sync_cancel_post)
        app.router.add_get('/api/upstreams', self._upstreams)
        app.router.add_get('/api/inbox', self._inbox)
        app.router.add_post('/api/inbox/add', self._inbox_add)
        app.router.add_post('/api/inbox/remove', self._inbox_remove)
        app.router.add_get('/api/search', self._search)
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
        app.router.add_post(r'/api/reviews/{idnum:\d+}/delete',
                            self._delete)
        app.router.add_post(r'/api/reviews/{idnum:\d+}/hide', self._hide)
        app.router.add_post(r'/api/reviews/{idnum:\d+}/respond',
                            self._respond)
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


def _item_id(request):
    """Get the ID of the patch or cover letter which a request is for

    Args:
        request (web.Request): Request, with the ID as 'item_id'

    Return:
        int or str: Patchwork ID, or 'lore:<msgid>' for one on lore
    """
    item_id = request.match_info['item_id']
    return int(item_id) if item_id.isdigit() else item_id


def _idnum(request):
    """Get the ID of the series, job, review or reply which a request is for

    Args:
        request (web.Request): Request, with the ID as 'idnum'

    Return:
        int: ID
    """
    return int(request.match_info['idnum'])


def _already_sent(what):
    """Refuse to change something which has been sent

    Args:
        what (str): What it is, e.g. 'This review'

    Return:
        web.Response: Error, with status 409
    """
    return web.json_response({'error': f'{what} has already been sent'},
                             status=409)


def cmd_series(cmd):
    """Get the series which a 'patman review' command acts on

    Args:
        cmd (list of str): Command, or its arguments

    Return:
        str or None: Series link given with '-s', or None if there is none
    """
    if '-s' in cmd[:-1]:
        return cmd[cmd.index('-s') + 1]
    return None


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


# pylint: disable-next=R0913,R0917
def serve(cser, host, port, pw_url, sync_interval, review_sync, token,
          rcser=None, max_reviews=None):
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
        max_reviews (int or None): Maximum number of reviews to run at
            once, or None for the default

    Raises:
        ValueError: max_reviews is less than 1
    """
    if max_reviews is not None and max_reviews < 1:
        raise ValueError('--max-reviews must be at least 1')
    if not token and not is_loopback(host):
        tout.warning(f'Listening on {host} with no --token: anyone who can '
                     'reach this port can read your series and reviews')
    jobs_fname = os.path.join(os.path.dirname(cser.db.db_path), JOBS_FNAME)
    server = Server(cser, pw_url, sync_interval, review_sync, token, rcser,
                    jobs_fname, max_reviews)
    query = f'?token={token}' if token else ''
    tout.notice(f'Serving on http://{host}:{port}/{query}')
    if sync_interval:
        what = 'gather-all' + (' and review --sync' if review_sync else '')
        tout.notice(f'Syncing ({what}) every {sync_interval} minutes')
    web.run_app(server.make_app(), host=host, port=port, print=None)
