# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Serve series status and reviews over HTTP for viewing on a phone

'patman serve' runs a small web server in the git tree. It provides a JSON
API over the patman database plus a mobile-friendly page which uses it.
Comments on patches and cover letters are not stored in the database, so
these are read live from patchwork when asked for.

The server can optionally keep the database up to date by running
'patman series gather-all -G' (and optionally 'patman review --sync')
periodically in a child process. The -G flag matters: without it,
gather-all writes review tags into the local branches, which must never
happen behind the user's back.

API (all responses are JSON):
    GET  /api/series                  Own series with latest-version status
    GET  /api/series/<id>             All versions of a series with patches
    GET  /api/comments/<kind>/<id>    Comments on a 'patch' or 'cover', read
                                      from patchwork; ?ups=<upstream>
    GET  /api/reviews                 Series reviewed with 'patman review'
    GET  /api/reviews/<id>            Reviews for each version of one
    GET  /api/todo                    Todo entries from the workflow table
    GET  /api/sync                    State of the periodic sync, including
                                      the output so far
    POST /api/sync                    Start a sync now
    POST /api/sync/cancel             Stop the sync which is running
"""

import asyncio
from collections import Counter
from datetime import datetime
import hmac
import ipaddress
import os
import signal
import sys

import aiohttp
from aiohttp import web

from u_boot_pylib import tout

from patman import review as review_mod
from patman.patchwork import Patchwork
from patman.workflow import Wtype

# Directory holding the web page and its assets
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')

# Maximum amount of output to keep from each sync command
MAX_SYNC_OUTPUT = 20000

def _state_counts(pcommits):
    """Count the patchwork states of a list of patches

    Args:
        pcommits (list of Pcommit): Patches to count

    Return:
        dict: key (str): state, or 'unknown' if not set
            value (int): number of patches in that state
    """
    return dict(Counter(pcm.state or 'unknown' for pcm in pcommits))


def _patch_info(pcm):
    """Convert a pcommit record to a JSON-friendly dict

    Args:
        pcm (Pcommit): Record to convert

    Return:
        dict: Patch information
    """
    return {
        'seq': pcm.seq + 1,
        'subject': pcm.subject,
        'state': pcm.state,
        'patch_id': pcm.patch_id,
        'num_comments': pcm.num_comments or 0,
    }


def _version_info(svr):
    """Convert a ser_ver record to a JSON-friendly dict

    Args:
        svr (SerVer): Record to convert

    Return:
        dict: Version information, without the patches
    """
    return {
        'svid': svr.idnum,
        'version': svr.version,
        'link': svr.link,
        'desc': svr.desc,
        'cover_id': svr.cover_id,
        'cover_name': svr.name,
        'cover_num_comments': svr.cover_num_comments or 0,
        'archive_tag': svr.archive_tag,
    }


def _review_info(rev):
    """Convert a review record to a JSON-friendly dict

    Args:
        rev (Review): Record to convert

    Return:
        dict: Review information
    """
    return {
        'seq': rev.seq,
        'body': rev.body,
        'approved': bool(rev.approved),
        'timestamp': rev.timestamp,
        'status': rev.status,
    }


def _comment_info(comment):
    """Pick out the useful fields from a patchwork comment

    Args:
        comment (dict): Comment as returned by the patchwork API

    Return:
        dict: Comment information
    """
    submitter = comment.get('submitter') or {}
    return {
        'date': comment.get('date'),
        'name': submitter.get('name'),
        'email': submitter.get('email'),
        'subject': comment.get('subject'),
        'content': comment.get('content'),
        'web_url': comment.get('web_url'),
    }


def _latest_versions(cser):
    """Get the ser_ver record for the latest version of each series

    Args:
        cser (Cseries): Open cseries instance

    Return:
        dict: key (int): series ID
            value (SerVer): latest version of that series
    """
    svdict = cser.get_ser_ver_dict()
    return {series_id: svdict[svid]
            for svid, series_id, _ in cser.db.series_get_all_max_versions()}


def _last_sent(cser):
    """Get the time that each series was last sent

    Args:
        cser (Cseries): Open cseries instance

    Return:
        dict: key (int): series ID
            value (str): timestamp, 'YYYY-MM-DD HH:MM:SS'
    """
    # Entries come back in time order, so later ones win
    return {series_id: timestamp for series_id, _, _, timestamp in
            cser.db.workflow_get_by_type(Wtype.SENT)}


def get_series_list(cser):
    """Get a summary of the user's own (non-archived) series

    Args:
        cser (Cseries): Open cseries instance

    Return:
        list of dict: One item for each series, sorted by name
    """
    latest = _latest_versions(cser)
    sent = _last_sent(cser)
    out = []
    for ser in cser.db.series_get_dict().values():
        svr = latest.get(ser.idnum)
        pcommits = cser.db.pcommit_get_list(svr.idnum) if svr else []
        out.append({
            'id': ser.idnum,
            'name': ser.name,
            'desc': ser.desc,
            'upstream': ser.upstream,
            'version': svr.version if svr else None,
            'link': svr.link if svr else None,
            'count': len(pcommits),
            'states': _state_counts(pcommits),
            'num_comments': sum(pcm.num_comments or 0 for pcm in pcommits),
            'cover_num_comments': (svr.cover_num_comments or 0) if svr else 0,
            'sent': sent.get(ser.idnum),
        })
    return sorted(out, key=lambda item: item['name'])


def get_series_detail(cser, idnum):
    """Get all versions of a series, with their patches

    Args:
        cser (Cseries): Open cseries instance
        idnum (int): Series ID

    Return:
        dict: Series information, with 'versions' newest first

    Raises:
        ValueError: The series does not exist
    """
    name, desc, ups = cser.db.series_get_info(idnum)
    versions = []
    for svr in cser.db.ser_ver_get_for_series(idnum):
        info = _version_info(svr)
        pcommits = cser.db.pcommit_get_list(svr.idnum)
        info['patches'] = [_patch_info(pcm)
                           for pcm in sorted(pcommits, key=lambda p: p.seq)]
        info['states'] = _state_counts(pcommits)
        versions.append(info)
    versions.sort(key=lambda item: item['version'], reverse=True)
    return {'id': idnum, 'name': name, 'desc': desc, 'upstream': ups,
            'versions': versions}


def get_review_list(cser):
    """Get a summary of the series reviewed with 'patman review'

    Args:
        cser (Cseries): Open cseries instance

    Return:
        list of dict: One item for each series, most recent review first
    """
    latest = _latest_versions(cser)
    out = []
    for ser in cser.db.series_get_dict(reviews_only=True).values():
        svr = latest.get(ser.idnum)
        reviews = cser.db.review_get_for_version(svr.idnum) if svr else []
        out.append({
            'id': ser.idnum,
            'name': ser.name,
            'desc': ser.desc,
            'upstream': ser.upstream,
            'version': svr.version if svr else None,
            'link': svr.link if svr else None,
            'num_reviews': len(reviews),
            'approved': sum(1 for rev in reviews if rev.approved),
            'statuses': dict(Counter(rev.status or 'new' for rev in reviews)),
            'timestamp': max((rev.timestamp for rev in reviews
                              if rev.timestamp), default=None),
        })
    return sorted(out, key=lambda item: item['timestamp'] or '',
                  reverse=True)


def get_review_detail(cser, idnum):
    """Get the reviews for each version of a reviewed series

    Args:
        cser (Cseries): Open cseries instance
        idnum (int): Series ID

    Return:
        dict: Series information, with 'versions' newest first

    Raises:
        ValueError: The series does not exist
    """
    name, desc, ups = cser.db.series_get_info(idnum)
    versions = []
    for svr in cser.db.ser_ver_get_for_series(idnum):
        info = _version_info(svr)
        info['notes'] = svr.notes
        info['reviews'] = [_review_info(rev) for rev in
                           cser.db.review_get_for_version(svr.idnum)]
        versions.append(info)
    versions.sort(key=lambda item: item['version'], reverse=True)
    return {'id': idnum, 'name': name, 'desc': desc, 'upstream': ups,
            'versions': versions}


def get_todo_list(cser):
    """Get the scheduled todos, whether due or not

    Args:
        cser (Cseries): Open cseries instance

    Return:
        list of dict: One item per todo, earliest first
    """
    now = cser.get_now()
    out = []
    for series_id, name, desc, timestamp in cser.db.workflow_get_by_type(
            Wtype.TODO):
        when = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
        out.append({'series_id': series_id, 'name': name, 'desc': desc,
                    'timestamp': timestamp, 'due': when <= now})
    return out


# pylint: disable=R0902
class Server:
    """Web server providing patman information as JSON

    Properties:
        cser (Cseries): Open cseries instance
        pw_url (str or None): Patchwork URL to use when a series has no
            upstream with its own URL
        sync_interval (int): Minutes between syncs, 0 to disable
        review_sync (bool): True to run 'review --sync' as part of a sync
        token (str or None): Token which clients must provide, or None
        pwork_for_testing (Patchwork or None): Patchwork object to use for
            all upstreams, for testing
        sync_cmds_for_testing (list of list of str or None): Commands to
            run for a sync, for testing
        sync_state (dict): State of the most recent sync
    """
    # pylint: disable=R0913,R0917
    def __init__(self, cser, pw_url=None, sync_interval=0, review_sync=False,
                 token=None):
        self.cser = cser
        self.pw_url = pw_url
        self.sync_interval = sync_interval
        self.review_sync = review_sync
        self.token = token
        self.pwork_for_testing = None
        self.sync_cmds_for_testing = None
        self._pworks = {}
        self._sleep = asyncio.sleep
        self._sync_lock = asyncio.Lock()
        self._sync_task = None
        self._sync_proc = None
        self._sync_cancel = False
        self.sync_state = {
            'running': False,
            'cancelled': False,
            'interval': sync_interval,
            'review_sync': review_sync,
            'started': None,
            'finished': None,
            'results': [],
        }

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

    def get_sync_cmds(self):
        """Get the commands to run for a sync

        Return:
            list of list of str: Commands, each a list of arguments
        """
        if self.sync_cmds_for_testing is not None:
            return self.sync_cmds_for_testing
        patman = [sys.executable, '-m', 'patman', '-v']
        cmds = [patman + ['series', 'gather-all', '-G']]
        if self.review_sync:
            cmds.append(patman + ['review', '--sync'])
        return cmds

    async def run_sync(self):
        """Run the sync commands one after the other

        Output from each command is recorded in sync_state as it arrives.
        If a sync is already running, this does nothing. If the sync is
        cancelled, the command which is running is stopped and the rest are
        not run.

        Return:
            bool: True if a sync was run, False if one was already running
        """
        if self._sync_lock.locked():
            return False
        async with self._sync_lock:
            state = self.sync_state
            self._sync_cancel = False
            state['running'] = True
            state['cancelled'] = False
            state['started'] = _now_str()
            state['finished'] = None
            state['results'] = []
            try:
                for cmd in self.get_sync_cmds():
                    if self._sync_cancel:
                        break
                    result = {
                        'cmd': ' '.join(cmd[1:] if cmd[0] == sys.executable
                                        else cmd),
                        'returncode': None,
                        'output': '',
                    }
                    state['results'].append(result)
                    await self._run_cmd(cmd, result)
            finally:
                state['running'] = False
                state['cancelled'] = self._sync_cancel
                state['finished'] = _now_str()
        return True

    def cancel_sync(self):
        """Stop the sync which is running, if any

        Return:
            bool: True if a sync was running, False if not
        """
        if not self.sync_state['running']:
            return False
        self._sync_cancel = True
        if self._sync_proc and self._sync_proc.returncode is None:
            # Stop any child processes too, since they hold the output open
            tout.info('Sync: cancelling')
            try:
                os.killpg(self._sync_proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        return True

    async def _run_cmd(self, cmd, result):
        """Run a single sync command in the git tree

        The output is added to the result as each line arrives, so that a
        client can show the progress of the sync.

        Args:
            cmd (list of str): Command to run
            result (dict): Information about the command, updated with its
                output and return code
        """
        tout.info(f"Sync: running {' '.join(cmd[1:])}")
        env = review_mod._child_env()  # pylint: disable=W0212
        env['PYTHONUNBUFFERED'] = '1'
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self.cser.topdir, env=env, start_new_session=True)
        except OSError as exc:
            result['returncode'] = -1
            result['output'] = str(exc)
            return
        self._sync_proc = proc
        try:
            async for line in proc.stdout:
                output = result['output'] + line.decode('utf-8',
                                                        errors='replace')
                result['output'] = output[-MAX_SYNC_OUTPUT:]
            result['returncode'] = await proc.wait()
        finally:
            self._sync_proc = None
        if result['returncode']:
            tout.warning(f"Sync: '{result['cmd']}' failed "
                         f"(code {result['returncode']})")

    async def _sync_loop(self):
        """Run a sync every sync_interval minutes, starting straight away"""
        while True:
            await self.run_sync()
            await self._sleep(self.sync_interval * 60)

    async def _start_background(self, _app):
        """Start the periodic sync, if enabled"""
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
    async def _errors(self, request, handler):
        """Turn errors into a JSON response"""
        try:
            return await handler(request)
        except ValueError as exc:
            return web.json_response({'error': str(exc)}, status=404)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return web.json_response(
                {'error': f'Patchwork: {type(exc).__name__}: {exc}'},
                status=502)

    async def _series_list(self, _request):
        return web.json_response(get_series_list(self.cser))

    async def _series_detail(self, request):
        data = get_series_detail(self.cser, int(request.match_info['idnum']))
        data['patchwork_url'] = self.get_pw_url(data['upstream'])
        return web.json_response(data)

    async def _review_list(self, _request):
        return web.json_response(get_review_list(self.cser))

    async def _review_detail(self, request):
        data = get_review_detail(self.cser, int(request.match_info['idnum']))
        data['patchwork_url'] = self.get_pw_url(data['upstream'])
        return web.json_response(data)

    async def _todo_list(self, _request):
        return web.json_response(get_todo_list(self.cser))

    async def _comments(self, request):
        kind = request.match_info['kind']
        item_id = int(request.match_info['item_id'])
        pwork = self.get_pwork(request.query.get('ups') or None)
        async with aiohttp.ClientSession() as client:
            if kind == 'patch':
                comments = await pwork.get_patch_comments(client, item_id)
            else:
                comments = await pwork.get_cover_comments(client, item_id)
        return web.json_response([_comment_info(c) for c in comments or []])

    async def _sync_get(self, _request):
        return web.json_response(self.sync_state)

    async def _sync_post(self, _request):
        if self.sync_state['running']:
            return web.json_response(self.sync_state, status=409)
        asyncio.create_task(self.run_sync())

        # Let the sync start, so the response shows it running
        await asyncio.sleep(0)
        return web.json_response(self.sync_state, status=202)

    async def _sync_cancel_post(self, _request):
        if not self.cancel_sync():
            return web.json_response(self.sync_state, status=409)
        return web.json_response(self.sync_state, status=202)

    async def _index(self, _request):
        return web.FileResponse(os.path.join(WEB_DIR, 'index.html'))

    def make_app(self):
        """Create the aiohttp application

        Return:
            web.Application: Application, ready to run
        """
        app = web.Application(middlewares=[self._errors, self._auth])
        app.router.add_get('/api/series', self._series_list)
        app.router.add_get(r'/api/series/{idnum:\d+}', self._series_detail)
        app.router.add_get('/api/reviews', self._review_list)
        app.router.add_get(r'/api/reviews/{idnum:\d+}', self._review_detail)
        app.router.add_get('/api/todo', self._todo_list)
        app.router.add_get(r'/api/comments/{kind:patch|cover}/{item_id:\d+}',
                           self._comments)
        app.router.add_get('/api/sync', self._sync_get)
        app.router.add_post('/api/sync', self._sync_post)
        app.router.add_post('/api/sync/cancel', self._sync_cancel_post)
        app.router.add_get('/', self._index)
        app.router.add_static('/static', WEB_DIR)
        app.on_startup.append(self._start_background)
        app.on_cleanup.append(self._stop_background)
        return app


def _now_str():
    """Get the current time as a string

    Return:
        str: Time in the form 'YYYY-MM-DD HH:MM:SS'
    """
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


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
def serve(cser, host, port, pw_url, sync_interval, review_sync, token):
    """Run the web server until interrupted

    Args:
        cser (Cseries): Open cseries instance
        host (str): Address to listen on
        port (int): Port to listen on
        pw_url (str or None): Default patchwork URL
        sync_interval (int): Minutes between syncs, 0 to disable
        review_sync (bool): True to run 'review --sync' as part of a sync
        token (str or None): Token which clients must provide, or None
    """
    if not token and not is_loopback(host):
        tout.warning(f'Listening on {host} with no --token: anyone who can '
                     'reach this port can read your series and reviews')
    server = Server(cser, pw_url, sync_interval, review_sync, token)
    query = f'?token={token}' if token else ''
    tout.notice(f'Serving on http://{host}:{port}/{query}')
    if sync_interval:
        jobs = 'gather-all' + (' and review --sync' if review_sync else '')
        tout.notice(f'Syncing ({jobs}) every {sync_interval} minutes')
    web.run_app(server.make_app(), host=host, port=port, print=None)
