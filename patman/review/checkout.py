# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#

"""Checking out a series from patchwork, to try it

'patman review --checkout' puts a series on a branch in the current tree.
A series which has been reviewed already has a branch, so that is checked
out. Otherwise the series is applied with 'git am', onto the base which a
review would use, to a branch of the same name, so that a later review
finds it. Nothing is recorded in the database and no agent is used, so a
series which does not apply cleanly is left alone.
"""

import os
import subprocess

from u_boot_pylib import gitutil
from u_boot_pylib import tout

from patman.review import apply
from patman.review import common
from patman.review import lookup


def _git(repo, *args):
    """Run git in a repo

    Args:
        repo (str): Top level of the repo
        args (list of str): Arguments for git

    Return:
        subprocess.CompletedProcess: Result, with the output as text
    """
    return subprocess.run(['git', *args], cwd=repo, capture_output=True,
                          text=True, check=False)


def branch_names(link, ups):
    """Get the names which a review branch of a series may have

    A review from the review database has no upstream, so its branch
    starts with 'pw'

    Args:
        link (str): Patchwork series link/ID
        ups (str or None): Upstream name

    Return:
        list of str: Names, most likely first
    """
    return list(dict.fromkeys([lookup.make_review_name(link, ups),
                               lookup.make_review_name(link)]))


def find_branch_in(link, ups, branches):
    """Find the review branch of a series among a set of branches

    Args:
        link (str or int): Patchwork series link/ID
        ups (str or None): Upstream name
        branches (set of str): Branch names

    Return:
        str or None: Branch name, or None if there is none
    """
    return next((name for name in branch_names(link, ups)
                 if name in branches), None)


def find_branch(link, ups, repo=None):
    """Find the review branch of a series, if there is one

    Args:
        link (str): Patchwork series link/ID
        ups (str or None): Upstream name
        repo (str or None): Top level of the repo, or None for the current
            directory

    Return:
        str or None: Branch name, or None if there is none
    """
    for name in branch_names(link, ups):
        if gitutil.ref_exists(f'refs/heads/{name}', repo):
            return name
    return None


def _apply(args, pwork, cser, link, repo):
    """Apply a series to a new branch with 'git am', and check it out

    Args:
        args (Namespace): Command-line arguments
        pwork (Patchwork): Configured patchwork instance
        cser (Cseries): Open cseries instance
        link (str): Patchwork series link/ID
        repo (str): Top level of the repo

    Return:
        str: Branch name

    Raises:
        ValueError: The series is incomplete or does not apply
    """
    series_data, _, _, patch_count = lookup.fetch_series(pwork, link)
    ctx = common.ReviewContext(pwork, cser, series_data)
    ctx.main_repo = repo
    base = apply.get_upstream_branch(args, cser)
    if not args.base_branch:
        base = apply.series_base(
            ctx, args.upstream or cser.db.upstream_get_default(),
            base) or base

    mbox = common.run(apply.fetch_mbox(pwork, link, series_data))
    name = lookup.make_review_name(link, pwork.upstream)
    head = _git(repo, 'symbolic-ref', '-q', '--short', 'HEAD').stdout.strip()
    if not head:
        head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    try:
        res = _git(repo, 'checkout', '-q', '-b', name, base)
        if res.returncode:
            raise ValueError(f"Cannot create branch '{name}' from {base}: "
                             f'{res.stderr.strip()}')
        res = _git(repo, 'am', '-3', '-q', mbox)
        if res.returncode:
            # Put the tree back as it was
            _git(repo, 'am', '--abort')
            _git(repo, 'checkout', '-q', head)
            _git(repo, 'branch', '-q', '-D', name)
            raise ValueError(
                f'The series does not apply to {base}:\n'
                f'{(res.stdout + res.stderr).strip()}\n'
                "Try 'patman review --apply-only', which uses the agent to "
                'deal with conflicts, or choose another base with -b')
    finally:
        os.unlink(mbox)
    what = '1 patch' if patch_count == 1 else f'{patch_count} patches'
    tout.notice(f"Checked out '{name}', with {what} on {base}")
    return name


def do_checkout(args, pwork, cser, link):
    """Check out a series in the current tree, to try it

    Args:
        args (Namespace): Command-line arguments
        pwork (Patchwork): Configured patchwork instance
        cser (Cseries): Open cseries instance
        link (str): Patchwork series link/ID

    Return:
        int: 0 on success

    Raises:
        ValueError: The tree has changes, or the series cannot be checked
            out
    """
    repo = gitutil.get_top_level()
    if _git(repo, 'status', '--porcelain', '--untracked-files=no').stdout:
        raise ValueError('The tree has uncommitted changes; commit or stash '
                         'them first')
    name = find_branch(link, pwork.upstream, repo)
    if not name:
        _apply(args, pwork, cser, link, repo)
        return 0
    res = _git(repo, 'checkout', '-q', name)
    if res.returncode:
        raise ValueError(f"Cannot check out '{name}': {res.stderr.strip()}")
    tout.notice(f"Checked out '{name}', from the review of the series")
    return 0
