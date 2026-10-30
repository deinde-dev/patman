# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Test the queue which a maintainer is about to push, and run CI on it

'patman maint test' runs the project's test commands on the queue, in a
worktree of its own, so that the tree you are working in is not touched.

'patman maint ci' checks the queue, pushes it to the branches which run CI,
then waits for GitHub Actions and the GitLab pipeline to finish with the
commit which was pushed. Any run for that commit counts, wherever it was
started from.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
import shutil
import subprocess
import tempfile
import time
from urllib.parse import quote
import urllib.request

from u_boot_pylib import command
from u_boot_pylib import gitutil

from patman import maint

# Lines of a failing test's output to show
TAIL_LINES = 20

# Seconds between asking about the CI runs, how long to wait for a run to
# start, and how long to wait for one to finish
CI_POLL = 30
CI_START = 5 * 60
CI_TIMEOUT = 60 * 60

# How each state of a GitHub run or GitLab pipeline is taken
PENDING = 'pending'
SUCCESS = 'success'
FAILURE = 'failure'
MISSING = 'missing'
FINAL = (SUCCESS, FAILURE)

GITLAB_STATES = {
    'success': SUCCESS,
    'failed': FAILURE,
    'canceled': FAILURE,
    'skipped': FAILURE,
}

GITLAB_API = 'https://gitlab.com/api/v4'


@dataclass
class CiResult:
    """The state of CI on a commit, in one place

    Properties:
        system (str): Where CI runs, e.g. 'github'
        repo (str): Repo, e.g. 'dgibson/dtc'
        state (str): PENDING, SUCCESS, FAILURE or MISSING
        url (str or None): Page of the run which gives the state, if any
        error (str or None): Why the state could not be read, if it could
            not, in which case it is PENDING
    """
    system: str
    repo: str
    state: str
    url: str = None
    error: str = None


def _branch_commit(rules, git_dir):
    """Get the commit which the branch to test is at

    Args:
        rules (Rules): Rules, whose branch is the one to test
        git_dir (str or None): Directory of the git repo, or None for the
            current one

    Return:
        str: Commit hash

    Raises:
        ValueError: There is no branch to test
    """
    if not rules.branch:
        raise ValueError('No branch is checked out: use -b to say which')
    if not gitutil.ref_exists(f'refs/heads/{rules.branch}', git_dir):
        raise ValueError(f"There is no branch '{rules.branch}'")
    return command.output('git', 'rev-parse', rules.branch,
                          cwd=git_dir).strip()


@contextmanager
def _tree(commit, git_dir):
    """Check out a commit in a temporary worktree

    Args:
        commit (str): Commit to check out
        git_dir (str or None): Directory of the git repo, or None for the
            current one

    Yields:
        str: Directory of the worktree
    """
    tmpdir = tempfile.mkdtemp(prefix='patman-test.')
    path = os.path.join(tmpdir, 'tree')
    command.run_one('git', 'worktree', 'add', '--quiet', '--detach', path,
                    commit, cwd=git_dir, capture=True, capture_stderr=True)
    try:
        yield path
    finally:
        command.run_one('git', 'worktree', 'remove', '--force', path,
                        cwd=git_dir, capture=True, capture_stderr=True)
        shutil.rmtree(tmpdir, ignore_errors=True)


def do_test(rules, git_dir=None):
    """Run the project's test commands on the queue

    The commands run one after the other, in a temporary worktree at the
    branch's last commit, and stop at the first which fails.

    Args:
        rules (Rules): Rules, giving the branch and the test commands
        git_dir (str or None): Directory of the git repo, or None for the
            current one

    Return:
        int: 0 if every command passed, else 1

    Raises:
        ValueError: There are no test commands, or no branch to test
    """
    if not rules.test:
        raise ValueError("No tests to run: set 'test' in the [maint] section "
                         "of the settings, e.g. 'test: make check'")
    commit = _branch_commit(rules, git_dir)
    print(f'Testing {rules.branch} at {commit[:12]}')
    with _tree(commit, git_dir) as path:
        for cmd in rules.test:
            result = subprocess.run(['sh', '-c', cmd], cwd=path,
                                    capture_output=True, text=True,
                                    check=False)
            if result.returncode:
                print(f'  FAIL {cmd}')
                out = (result.stdout + result.stderr).rstrip('\n')
                for line in out.splitlines()[-TAIL_LINES:]:
                    print(f'       {line}')
                return 1
            print(f'  ok   {cmd}')
    return 0


def _combine(system, repo, runs):
    """Work out the state of CI from the state of each run

    Args:
        system (str): Where CI runs, e.g. 'github'
        repo (str): Repo, e.g. 'dgibson/dtc'
        runs (list of tuple): Each run:
            str: PENDING, SUCCESS or FAILURE
            str: Page of the run

    Return:
        CiResult: A failure if any run failed, else pending if any is still
            going, else success; MISSING if there are no runs
    """
    for state in (FAILURE, PENDING, SUCCESS):
        for got, url in runs:
            if got == state:
                return CiResult(system, repo, state, url)
    return CiResult(system, repo, MISSING)


def github_state(repo, sha):
    """Get the state of GitHub Actions on a commit

    Args:
        repo (str): Repo, e.g. 'dgibson/dtc'
        sha (str): Full hash of the commit

    Return:
        CiResult: State
    """
    out = command.output(
        'gh', 'api', f'repos/{repo}/actions/runs?head_sha={sha}', '-q',
        r'.workflow_runs[] | [.status, .conclusion // "", .html_url] | @tsv')
    runs = []
    for line in out.splitlines():
        status, conclusion, url = line.split('\t')
        if status != 'completed':
            state = PENDING
        elif conclusion in ('success', 'skipped', 'neutral'):
            state = SUCCESS
        else:
            state = FAILURE
        runs.append((state, url))
    return _combine('github', repo, runs)


def gitlab_state(repo, sha):
    """Get the state of the GitLab pipelines on a commit

    Args:
        repo (str): Repo, e.g. 'dgibson/dtc'
        sha (str): Full hash of the commit

    Return:
        CiResult: State
    """
    url = (f'{GITLAB_API}/projects/{quote(repo, safe="")}/pipelines'
           f'?sha={sha}')
    with urllib.request.urlopen(url, timeout=30) as resp:
        pipelines = json.load(resp)
    return _combine('gitlab', repo, [
        (GITLAB_STATES.get(pipe['status'], PENDING), pipe['web_url'])
        for pipe in pipelines])


def _ci_places(rules):
    """Get where CI runs for the project

    Args:
        rules (Rules): Rules, whose repos give the places

    Return:
        list of tuple: Each place:
            str: System, e.g. 'github'
            str: Repo, e.g. 'dgibson/dtc'
    """
    places = []
    for repo in rules.repos:
        host, path = repo.split('/', 1)
        places.append((host.removesuffix('.com'), path))
    return places


STATE_FUNCS = {'github': github_state, 'gitlab': gitlab_state}


def ci_states(rules, sha):
    """Get the state of CI on a commit, in each place it runs

    A place whose state cannot be read is taken as still running, so that
    a passing network problem does not end a wait, and is never taken as
    passing.

    Args:
        rules (Rules): Rules, whose repos give the places
        sha (str): Full hash of the commit

    Return:
        list of CiResult: State in each place
    """
    results = []
    for system, repo in _ci_places(rules):
        try:
            results.append(STATE_FUNCS[system](repo, sha))
        except (OSError, ValueError, command.CommandExc) as exc:
            results.append(CiResult(system, repo, PENDING,
                                    error=str(exc).strip()))
    return results


def wait_ci(rules, sha, sleep=time.sleep, clock=time.monotonic):
    """Wait for CI to finish on a commit, in each place it runs

    A place with no run for the commit after CI_START is given up on, as is
    one still running after CI_TIMEOUT.

    Args:
        rules (Rules): Rules, whose repos give the places
        sha (str): Full hash of the commit
        sleep (function): Function to wait a number of seconds
        clock (function): Function to get the time in seconds

    Return:
        list of CiResult: State in each place
    """
    start = clock()
    while True:
        results = ci_states(rules, sha)
        waited = clock() - start
        if all(res.state in FINAL or
               (res.state == MISSING and waited >= CI_START)
               for res in results) or waited >= CI_TIMEOUT:
            return results
        sleep(CI_POLL)


def show_ci(results):
    """Show the state of CI in each place, one line for each

    Args:
        results (list of CiResult): State in each place

    Return:
        bool: True if CI passed everywhere
    """
    for res in results:
        state = {SUCCESS: 'ok', MISSING: 'no run', PENDING: 'running'}.get(
            res.state, 'FAIL')
        print(f"  {state:7} {res.system} {res.url or res.repo}"
              f"{f': {res.error}' if res.error else ''}")
    return all(res.state == SUCCESS for res in results)


def do_ci(rules, dry_run=False, wait=True, git_dir=None,
          sleep=time.sleep):
    """Check the queue, push it to run CI and wait for CI to finish

    Args:
        rules (Rules): Rules, giving the branch and where to push it
        dry_run (bool): True to say where the branch would be pushed,
            without pushing it
        wait (bool): True to wait for CI to finish
        git_dir (str or None): Directory of the git repo, or None for the
            current one
        sleep (function): Function to wait a number of seconds

    Return:
        int: 0 if the branch was pushed and CI passed (or was not waited
            for), else 1

    Raises:
        ValueError: There is nowhere to push, or no branch to push
    """
    if not rules.ci_push:
        raise ValueError("Nowhere to run CI: set 'ci_push' in the [maint] "
                         "section of the settings, e.g. "
                         "'ci_push: david +{branch}:ci'")
    sha = _branch_commit(rules, git_dir)
    if maint.do_check(rules, git_dir=git_dir):
        print('Not pushed, since the check failed')
        return 1
    if not command.output('git', 'rev-list', '-1',
                          f'{rules.upstream}..{rules.branch}',
                          cwd=git_dir).strip():
        return 0
    targets = [(remote, spec.replace('{branch}', rules.branch))
               for remote, spec in rules.ci_push]
    where = ', '.join(f'{remote} {spec}' for remote, spec in targets)
    if dry_run:
        print(f'Would push {sha[:12]} to {where}')
        return 0
    if not _push(targets, git_dir):
        return 1
    print(f'Pushed {sha[:12]} to {where}')
    if wait and not _ci_places(rules):
        print("Not waiting for CI: set 'github' or 'gitlab' in the [maint] "
              'section to say where it runs')
    if not wait or not _ci_places(rules):
        return 0
    return 0 if show_ci(wait_ci(rules, sha, sleep)) else 1


def _push(targets, git_dir):
    """Push to each place in turn, stopping at the first which fails

    Args:
        targets (list of tuple): Each place:
            str: Remote, e.g. 'david'
            str: Refspec, e.g. '+main-push:ci'
        git_dir (str or None): Directory of the git repo, or None for the
            current one

    Return:
        bool: True if each push worked; if not, the problem is shown
    """
    for remote, spec in targets:
        result = subprocess.run(['git', 'push', '--quiet', remote, spec],
                                cwd=git_dir, capture_output=True, text=True,
                                check=False)
        if result.returncode:
            print(f'Cannot push to {remote}: {result.stderr.strip()}')
            return False
    return True
