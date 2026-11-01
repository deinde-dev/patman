# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""What your git repo says about a series, for the rules which suggest one

Some rules need the history of the tree: whether a Fixes: tag names one of
your commits, whether a series changes lines you wrote, and whether its
submitter has commits in the tree already. This finds those out with git,
in the repo which patman is run in.
"""

import re
import subprocess

# A line of 'git blame --porcelain' which starts the lines from a commit:
# the commit, then its line numbers
RE_BLAME_HEAD = re.compile(r'^([0-9a-f]{40}) \d+ \d+')


def _git(git_dir, *args):
    """Run a git command, giving its output or None if it fails

    Args:
        git_dir (str): Top of the repo
        args (str): Arguments for git

    Returns:
        str or None: Output, or None if git fails, e.g. as a commit is not
            in the repo. Text which is not valid UTF-8, such as an old
            commit's author, is replaced, not an error
    """
    res = subprocess.run(['git', *args], cwd=git_dir, capture_output=True,
                         text=True, errors='replace', check=False)
    return res.stdout if res.returncode == 0 else None


def ref_exists(git_dir, ref):
    """Check whether a commit is in the repo

    Args:
        git_dir (str): Top of the repo
        ref (str): Commit or branch

    Returns:
        bool: True if it is there
    """
    return _git(git_dir, 'rev-parse', '--verify', '-q',
                f'{ref}^{{commit}}') is not None


def own_commits(git_dir, commits, emails):
    """Find which of some commits are yours

    Args:
        git_dir (str): Top of the repo
        commits (list of str): Commits, perhaps abbreviated
        emails (set of str): Your addresses, in lower case

    Returns:
        list of str: Those of the commits which you wrote, as given; commits
            which are not in the repo are left out
    """
    mine = []
    for cid in commits:
        out = _git(git_dir, 'log', '-1', '--no-walk', '--format=%ae', cid)
        if out and out.strip().lower() in emails:
            mine.append(cid)
    return mine


def _ranges(lines):
    """Join line numbers into ranges, for 'git blame -L'

    Args:
        lines (list of int): Line numbers

    Returns:
        list of str: Ranges, e.g. ['3,5', '9,9']

    >>> _ranges([9, 3, 4, 5])
    ['3,5', '9,9']
    """
    out = []
    for num in sorted(set(lines)):
        if out and out[-1][1] == num - 1:
            out[-1][1] = num
        else:
            out.append([num, num])
    return [f'{start},{end}' for start, end in out]


def blame_mine(git_dir, ref, removed, emails):
    """Count the lines you wrote, among those which a series changes

    Args:
        git_dir (str): Top of the repo
        ref (str): Commit to blame, which the series is based on
        removed (dict): Lines which the series removes or changes:
            key (str): File
            value (list of int): Line numbers
        emails (set of str): Your addresses, in lower case

    Returns:
        dict: key (str): File with lines you wrote
            value (int): Number of them
    """
    found = {}
    for fname, lines in removed.items():
        args = ['blame', '--porcelain']
        for rng in _ranges(lines):
            args += ['-L', rng]
        out = _git(git_dir, *args, ref, '--', fname)
        mails = {}
        cur = None
        for line in (out or '').splitlines():
            match = RE_BLAME_HEAD.match(line)
            if match:
                cur = match.group(1)
            elif line.startswith('author-mail '):
                mails[cur] = line.split(' ', 1)[1].strip('<>').lower()
            elif line.startswith('\t') and mails.get(cur) in emails:
                found[fname] = found.get(fname, 0) + 1
    return found


def authors(git_dir, ref):
    """Get the address of everyone with a commit in the tree

    Args:
        git_dir (str): Top of the repo
        ref (str): Commit whose history to read

    Returns:
        set of str: Addresses, in lower case
    """
    out = _git(git_dir, 'log', '--format=%ae', ref)
    return {line.strip().lower() for line in (out or '').splitlines()
            if line.strip()}
