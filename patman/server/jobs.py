# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Run the jobs which the server starts, one at a time

A job runs patman commands in a child process, e.g. to sync with patchwork
or to send a review. Jobs which have not finished are saved, so that they
can be resumed when the server restarts.
"""

import asyncio
from collections import OrderedDict
from datetime import datetime
import json
import os
import signal
import sys

from u_boot_pylib import tout

from patman import review as review_mod

# Maximum amount of output to keep from each command run by a job
MAX_JOB_OUTPUT = 20000

# Maximum number of jobs to remember
MAX_JOBS = 20

# Maximum number of times to resume a job, in case it is the job which is
# causing the server to stop
MAX_RESUMES = 3


class JobBusyError(Exception):
    """A job is already acting on a review which a request would change"""


class Job:
    """A background task which runs patman commands one after the other

    Properties:
        idnum (int): Job number, starting at 1
        kind (str): Kind of job, e.g. 'sync' or 'review'
        cmds (list of list of str): Commands to run
        cancel (bool): True if the job has been asked to stop
        proc (asyncio.subprocess.Process or None): Command which is running
        state (dict): State of the job, as provided by the API
    """
    # pylint: disable=R0913,R0917
    def __init__(self, idnum, kind, title, cmds, resumed=0, target=None):
        """Set up a new job

        Args:
            idnum (int): Job number
            kind (str): Kind of job
            title (str): Description of the job, for display
            cmds (list of list of str): Commands to run
            resumed (int): Number of times the job has been resumed after
                the server stopped while it was waiting or running
            target (dict or None): Reviews which the job acts on, with the
                review series ID ('series'), its version ('version') and the
                patches ('seqs', empty for all unsent reviews), or None
        """
        self.idnum = idnum
        self.kind = kind
        self.cmds = cmds
        self.cancel = False
        self.proc = None
        self.state = {
            'id': idnum,
            'kind': kind,
            'title': title,
            'queued': True,
            'running': False,
            'cancelled': False,
            'started': None,
            'finished': None,
            'results': [],
            'resumed': resumed,
            'target': target,
        }

    @property
    def active(self):
        """True if the job is waiting to run, or running"""
        return not self.state['finished']

    @property
    def interrupted(self):
        """True if a command was stopped part-way, other than by cancelling

        This happens when the server stops while the job is running, since
        the command is stopped too
        """
        return not self.cancel and any(
            res['returncode'] is None or res['returncode'] < 0
            for res in self.state['results'])

    @property
    def unfinished(self):
        """True if the job should be run again when the server restarts"""
        return self.active or self.interrupted


class JobRunner:
    """Keeps a list of jobs and runs them in the order they are added

    Properties:
        topdir (str): Top directory of the git tree, where commands run
        jobs_fname (str or None): File holding the jobs which have not
            finished, or None to not keep them
    """
    def __init__(self, topdir, jobs_fname=None):
        self.topdir = topdir
        self.jobs_fname = jobs_fname

        # Jobs run one at a time, in the order they are added, since they
        # may all write to the database
        self._job_lock = asyncio.Lock()
        self._jobs = OrderedDict()
        self._last_job = 0

    def add_job(self, kind, title, cmds, target=None):
        """Add a job and start it once the jobs before it have finished

        Args:
            kind (str): Kind of job
            title (str): Description of the job, for display
            cmds (list of list of str): Commands to run
            target (dict or None): Reviews which the job acts on (see Job)

        Return:
            Job: New job
        """
        job = self._new_job(kind, title, cmds, target=target)
        asyncio.create_task(self.run_job(job))
        return job

    # pylint: disable=R0913,R0917
    def _new_job(self, kind, title, cmds, resumed=0, target=None):
        """Create a job and remember it, dropping old jobs if needed"""
        self._last_job += 1
        job = Job(self._last_job, kind, title, cmds, resumed, target)
        self._jobs[job.idnum] = job
        for old in list(self._jobs.values()):
            if len(self._jobs) <= MAX_JOBS:
                break
            if not old.active:
                del self._jobs[old.idnum]
        self.save_jobs()
        return job

    def save_jobs(self):
        """Write out the jobs which should run again if the server restarts

        Syncs are not included, since a sync runs when the server starts
        anyway. The file is replaced in one step, so that it is complete
        even if the server stops while writing it.
        """
        if not self.jobs_fname:
            return
        jobs = [{'kind': job.kind, 'title': job.state['title'],
                 'cmds': job.cmds, 'resumed': job.state['resumed'],
                 'target': job.state['target']}
                for job in self._jobs.values()
                if job.kind != 'sync' and job.unfinished]
        tmp = f'{self.jobs_fname}.tmp'
        with open(tmp, 'w', encoding='utf-8') as outf:
            json.dump(jobs, outf, indent=1)
        os.replace(tmp, self.jobs_fname)

    def resume_jobs(self):
        """Start the jobs which had not finished when the server stopped

        Return:
            list of Job: Jobs which were started
        """
        if not self.jobs_fname or not os.path.exists(self.jobs_fname):
            return []
        try:
            with open(self.jobs_fname, encoding='utf-8') as inf:
                saved = json.load(inf)
        except (OSError, ValueError) as exc:
            tout.warning(f"Cannot read jobs from '{self.jobs_fname}': {exc}")
            return []
        jobs = []
        for item in saved:
            title = item['title']
            resumed = item.get('resumed', 0) + 1
            if resumed > MAX_RESUMES:
                tout.warning(f"Not resuming job '{title}' again: it has "
                             f'been resumed {MAX_RESUMES} times')
                continue
            tout.notice(f"Resuming job '{title}'")
            job = self._new_job(item['kind'], title, item['cmds'], resumed,
                                item.get('target'))
            asyncio.create_task(self.run_job(job))
            jobs.append(job)
        self.save_jobs()
        return jobs

    def get_job(self, idnum):
        """Get a job by its number

        Args:
            idnum (int): Job number

        Return:
            Job: Job

        Raises:
            ValueError: The job does not exist
        """
        job = self._jobs.get(idnum)
        if not job:
            raise ValueError(f'No job {idnum}')
        return job

    def get_jobs(self):
        """Get the jobs, newest first

        Return:
            list of Job: Jobs
        """
        return list(reversed(self._jobs.values()))

    async def run_job(self, job):
        """Run a job's commands one after the other, once it is its turn

        Output from each command is recorded in the job's state as it
        arrives. If the job is cancelled, the command which is running is
        stopped and the rest are not run.

        Args:
            job (Job): Job to run
        """
        state = job.state
        async with self._job_lock:
            state['queued'] = False
            state['running'] = True
            state['started'] = _now_str()
            try:
                for cmd in job.cmds:
                    if job.cancel:
                        break
                    result = {
                        'cmd': self._cmd_label(cmd),
                        'returncode': None,
                        'output': '',
                    }
                    state['results'].append(result)
                    await self._run_cmd(job, cmd, result)
            finally:
                state['running'] = False
                state['cancelled'] = job.cancel
                state['finished'] = _now_str()
                self.save_jobs()

    @staticmethod
    def _cmd_label(cmd):
        """Get a short description of a command, for display

        Args:
            cmd (list of str): Command

        Return:
            str: Description, e.g. 'patman series gather-all -G'
        """
        patman = [sys.executable, '-m', 'patman']
        if cmd[:3] == patman:
            cmd = ['patman'] + [arg for arg in cmd[3:4] if arg != '-v'] + \
                cmd[4:]
        return ' '.join(cmd)

    def cancel_job(self, job):
        """Stop a job, whether it is running or waiting to run

        Args:
            job (Job): Job to stop

        Return:
            bool: True if the job was stopped, False if it had finished
        """
        if not job.active:
            return False
        job.cancel = True
        if job.proc and job.proc.returncode is None:
            # Stop any child processes too, since they hold the output open
            tout.info(f'Job {job.idnum}: cancelling')
            try:
                os.killpg(job.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        return True

    async def _run_cmd(self, job, cmd, result):
        """Run a single command in the git tree

        The output is added to the result as each line arrives, so that a
        client can show the progress of the job.

        Args:
            job (Job): Job which is running the command
            cmd (list of str): Command to run
            result (dict): Information about the command, updated with its
                output and return code
        """
        tout.info(f"Job {job.idnum}: running {result['cmd']}")
        env = review_mod.scan.child_env()
        env['PYTHONUNBUFFERED'] = '1'
        # Fail rather than wait for a password which nobody can type
        env['GIT_TERMINAL_PROMPT'] = '0'
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self.topdir, env=env, start_new_session=True)
        except OSError as exc:
            result['returncode'] = -1
            result['output'] = str(exc)
            return
        job.proc = proc
        try:
            async for line in proc.stdout:
                output = result['output'] + line.decode('utf-8',
                                                        errors='replace')
                result['output'] = output[-MAX_JOB_OUTPUT:]
            result['returncode'] = await proc.wait()
        finally:
            job.proc = None
        if result['returncode']:
            tout.warning(f"Job {job.idnum}: '{result['cmd']}' failed "
                         f"(code {result['returncode']})")

    def pending_jobs(self, idnum, version):
        """Find the jobs which are waiting or running for a review series

        Args:
            idnum (int): Series ID in the review database
            version (int): Version of the series

        Return:
            dict: Map of patch number to the ID of the job acting on its
                review, with None for a job acting on all unsent reviews
        """
        out = {}
        for job in self._jobs.values():
            target = job.state['target']
            if (job.active and target and target['series'] == idnum and
                    target['version'] == version):
                for seq in target['seqs'] or [None]:
                    out.setdefault(seq, job.idnum)
        return out

    def check_free(self, idnum, version, seqs, unsent):
        """Check that no job is acting on some reviews

        Args:
            idnum (int): Series ID in the review database
            version (int): Version of the series
            seqs (list of int): Patches whose reviews are to be changed, or
                empty for all unsent reviews
            unsent (list of int): Patches whose reviews have not been sent

        Raises:
            ValueError: A job is waiting or running for one of the reviews
        """
        pending = self.pending_jobs(idnum, version)
        busy = None in pending or any(seq in pending
                                      for seq in (seqs or unsent))
        if not busy and not seqs:
            busy = bool(pending)
        if busy:
            raise JobBusyError('A job is already acting on this review; '
                               'wait for it to finish')


def _now_str():
    """Get the current time as a string

    Return:
        str: Time in the form 'YYYY-MM-DD HH:MM:SS'
    """
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')
