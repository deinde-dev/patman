# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Apply a series from the mailing list, as the project's rules say

'patman maint apply <link>' reads a series from its thread on lore, adds the
review tags given in reply to it, then the trailers which the rules say each
patch ends with, e.g. its Message-ID and the maintainer's sign-off, and
applies it to the push branch, e.g. 'main-push', which it creates from the
upstream branch if needed.

The patches are applied in a temporary worktree, so that the tree you are
working in is not touched. If the push branch is checked out somewhere, they
are applied there instead, once it is clean. Then the push branch is checked
(see maint.do_check()).
"""

from collections import defaultdict
import os
import re
import shutil
import tempfile

import aiohttp

from u_boot_pylib import command
from u_boot_pylib import gitutil
from u_boot_pylib import tout

from patman import cseries
from patman import lore
from patman import lorelist
from patman import maint
from patman import status
from patman.review import common

# Review tags which are added from the replies, in the order they are added
TAGS = ('Reviewed-by', 'Acked-by', 'Tested-by')

# Title of a series, without its prefix, e.g. 'dm: Tidy up'
RE_PREFIX = re.compile(r'^(\[[^]]*\]\s*)+')


def parse_patches(spec, count):
    """Parse a list of patch numbers, e.g. '1-3,5'

    Args:
        spec (str or None): Patch numbers, counting from 1, or None for all
        count (int): Number of patches in the series

    Return:
        list of int: Indexes of the patches, counting from 0, in order

    Raises:
        ValueError: The list is not valid
    """
    if not spec:
        return list(range(count))
    out = set()
    for part in spec.split(','):
        first, _, last = part.strip().partition('-')
        try:
            start, end = int(first), int(last or first)
        except ValueError as exc:
            raise ValueError(f"Cannot parse '{spec}': use e.g. '1-3,5'") \
                from exc
        if not 1 <= start <= end <= count:
            raise ValueError(f"Patch '{part}' is not in the series, which "
                             f'has {count} patch{"es" if count != 1 else ""}')
        out.update(range(start - 1, end))
    return sorted(out)


def _norm_title(name):
    """Normalise the title of a series, to find other versions of it

    Args:
        name (str): Name, e.g. '[v2,0/3] dm: Tidy up'

    Return:
        str: Title, e.g. 'dm: tidy up'
    """
    return ' '.join(RE_PREFIX.sub('', name or '').split()).lower()


def _tags_of(content, comments):
    """Get the review tags given in replies, which a patch does not have

    Args:
        content (str): Patch, or '' for a cover letter
        comments (list of dict): Replies to it

    Return:
        dict: key (str): Tag, e.g. 'Reviewed-by'
            value (set of str): People who gave it
    """
    base, _ = status.process_reviews(content, [], {})
    new, _ = status.process_reviews(content, comments, base)
    return {tag: people for tag, people in new.items() if tag in TAGS}


async def read_series(llist, link):
    """Read a series from lore, with the tags given in reply to it

    Args:
        llist (LoreList): List to read from
        link (str): Message-ID of the cover letter or first patch, or a link
            to it on lore

    Return:
        tuple:
            dict: Series, as from LoreList.get_series()
            list of dict: Tags for each patch, as from _tags_of(), with
                those given in reply to the cover letter
            list of dict: Other versions of the series which are newer,
                as from LoreList.query_series()
    """
    async with aiohttp.ClientSession() as client:
        data = await llist.get_series(client, link, max_age=0)
        cover_tags = {}
        if data['cover_letter']:
            cover_tags = _tags_of('', await llist.get_cover_comments(
                client, data['cover_letter']['id']))
        tags = []
        for pat in data['patches']:
            patch = await llist.get_patch(client, pat['id'])
            found = defaultdict(set, _tags_of(
                patch['content'],
                await llist.get_patch_comments(client, pat['id'])))
            for tag, people in cover_tags.items():
                found[tag] |= people
            tags.append(dict(found))
        title = _norm_title(data['name'])
        newer = [ser for ser in await llist.query_series(client, title)
                 if _norm_title(ser['name']) == title and
                 (ser.get('version') or 1) > (data['version'] or 1)]
        mbox = await llist.get_mbox(client, [pat['msgid']
                                             for pat in data['patches']])
    return data, tags, newer, mbox


def _split_mbox(mbox):
    """Split an mbox into its messages

    Args:
        mbox (bytes): mbox, as from LoreList.get_mbox()

    Return:
        list of bytes: Each message, with its 'From ' line
    """
    sep = b'From mboxrd@z Thu Jan  1 00:00:00 1970\n'
    return [sep + part for part in mbox.split(sep) if part.strip()]


def end_trailers(rules, msgid, own):
    """Get the trailers which a patch from the list ends with

    Args:
        rules (Rules): Rules to use
        msgid (str): Message-ID of the patch, without angle brackets
        own (bool): True if it is your own patch

    Return:
        list of str: Trailers, e.g. ['Message-ID: <x@y>', 'Signed-off-by: Me']
    """
    out = []
    for kind in rules.trailers_end:
        if kind == 'message-id':
            out.append(f'Message-ID: <{msgid}>')
        elif kind == 'link':
            out.append(f'Link: https://lore.kernel.org/r/{msgid}')
        elif not own or rules.signoff_own:
            out.append(f'Signed-off-by: {rules.signoff}')
    return out


def tag_trailers(tags):
    """Get the trailers for the review tags of a patch, in order

    Args:
        tags (dict): Tags, as from _tags_of()

    Return:
        list of str: Trailers, e.g. ['Reviewed-by: Fred <fred@x.org>']
    """
    return [f'{tag}: {who}' for tag in TAGS for who in sorted(tags.get(tag, ()))]


def _git(*args, cwd, **kwargs):
    """Run git

    Args:
        args (list of str): Arguments
        cwd (str): Directory to run in
        kwargs: Other arguments for command.run_one()

    Return:
        CommandResult: Result
    """
    return command.run_one('git', *args, cwd=cwd, capture=True,
                           capture_stderr=True, **kwargs)


def _worktree_of(branch, git_dir):
    """Find the worktree which has a branch checked out

    Args:
        branch (str): Branch, e.g. 'main-push'
        git_dir (str or None): Directory of the git repo, or None for the
            current one

    Return:
        str or None: Directory of the worktree, or None if none has it
    """
    out = command.output('git', 'worktree', 'list', '--porcelain', cwd=git_dir)
    path = None
    for line in out.splitlines():
        if line.startswith('worktree '):
            path = line.split(' ', 1)[1]
        elif line == f'branch refs/heads/{branch}':
            return path
    return None


def _rewrite(commits, trailers, cwd):
    """Add trailers to commits, keeping their trees, authors and order

    Args:
        commits (list of str): Hashes, oldest first
        trailers (list of list of str): Trailers to add to each
        cwd (str): Directory of the worktree

    Return:
        str: Hash of the new last commit
    """
    parent = _git('rev-parse', f'{commits[0]}~1', cwd=cwd).stdout.strip()
    for chash, extra in zip(commits, trailers):
        msg = _git('log', '-1', '--format=%B', chash, cwd=cwd).stdout
        args = [arg for trailer in extra for arg in ('--trailer', trailer)]
        msg = command.run_one('git', 'interpret-trailers', '--if-exists',
                              'add', *args, cwd=cwd, capture=True,
                              stdin_data=msg).stdout
        name, email, date = _git('log', '-1', '--format=%an%x00%ae%x00%ad',
                                 '--date=raw', chash,
                                 cwd=cwd).stdout.strip().split('\0')
        env = dict(os.environ, GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email,
                   GIT_AUTHOR_DATE=date)
        parent = command.run_one(
            'git', 'commit-tree', f'{chash}^{{tree}}', '-p', parent,
            cwd=cwd, capture=True, stdin_data=msg, env=env).stdout.strip()
    return parent


def _am(mbox, cwd):
    """Apply patches with 'git am', trying a three-way merge if needed

    Args:
        mbox (bytes): Patches
        cwd (str): Directory of the worktree

    Return:
        str or None: None if they applied, else what went wrong
    """
    with tempfile.NamedTemporaryFile(suffix='.mbox') as outf:
        outf.write(mbox)
        outf.flush()
        for extra in ([], ['-3']):
            result = _git('am', '--quiet', *extra, outf.name, cwd=cwd,
                          raise_on_error=False)
            if not result.return_code:
                return None
            _git('am', '--abort', cwd=cwd, raise_on_error=False)
    return (result.stdout + result.stderr).strip()


def series_name(data):
    """Get the name of a series to show, with its version

    Args:
        data (dict): Series, as from LoreList.get_series()

    Return:
        str: Name, e.g. 'dtc: Tidy (v2)'
    """
    return f"{data['name']} (v{data['version'] or 1})"


# pylint: disable-next=R0913,R0917
def check_series(rules, data, newer, sel, force, git_dir, redo=False):
    """Check that a series can be applied

    Args:
        rules (Rules): Rules to use
        data (dict): Series, as from LoreList.get_series()
        newer (list of dict): Newer versions of it, from read_series()
        sel (list of int): Indexes of the patches to apply
        force (bool): True to apply a series which is not complete, or of
            which there is a newer version
        git_dir (str or None): Directory of the git repo, or None for the
            current one
        redo (bool): True if the push branch is to start again from
            upstream, so that what is on it does not count

    Return:
        bool: True if the patches are all applied already, so there is
            nothing to apply, which is then said

    Raises:
        ValueError: The series is not complete, there is a newer version,
            or some of its patches are applied already
    """
    name = series_name(data)
    if not data['received_all'] and not force:
        raise ValueError(f"Only {data['received_total']} of the "
                         f"{data['total']} patches of '{name}' are on the "
                         'list: use -f to apply them anyway')
    if newer and not force:
        raise ValueError(
            f"'{name}' is not the latest version: v{newer[0]['version']} is "
            f"{newer[0]['id']}; use -f to apply it anyway")
    refs = [rules.upstream]
    if not redo and gitutil.ref_exists(f'refs/heads/{rules.push_branch}',
                                       git_dir):
        refs.append(rules.push_branch)
    for ref in refs:
        have = lorelist.applied_msgids(ref, git_dir)
        done = [idx + 1 for idx in sel
                if lore.bare_msgid(data['patches'][idx]['msgid']) in have]
        if len(done) == len(sel):
            what = f"'{name}' is" if len(sel) == len(data['patches']) else \
                f"Those patches of '{name}' are"
            print(f'{what} on {ref} already: nothing to apply')
            return True
        if done:
            others = ','.join(str(idx + 1) for idx in sel
                              if idx + 1 not in done)
            raise ValueError(
                f"{ref} has patch{'es' if len(done) > 1 else ''} "
                f"{', '.join(map(str, done))} of '{name}' already: use "
                f'-P {others} to apply the others')
    return False


def _count(sel, data):
    """Say how many of the patches of a series are chosen

    Args:
        sel (list of int): Indexes of the patches chosen
        data (dict): Series, as from LoreList.get_series()

    Return:
        str: e.g. '2 of 3 patches'
    """
    total = len(data['patches'])
    return f"{len(sel)} of {total} patch{'es' if total > 1 else ''}"


def plan(rules, data, tags, sel):
    """Show each patch to be applied with the tags it gets, and work out
    the trailers to add

    Args:
        rules (Rules): Rules to use
        data (dict): Series, as from LoreList.get_series()
        tags (list of dict): Tags for each patch, from read_series()
        sel (list of int): Indexes of the patches to apply

    Return:
        list of list of str: Trailers to add to each patch applied
    """
    own = (maint.email_of(data['submitter']['email']) ==
           maint.email_of(rules.signoff))
    trailers = []
    for idx in sel:
        patch = data['patches'][idx]
        added = tag_trailers(tags[idx])
        trailers.append(added + end_trailers(
            rules, lore.bare_msgid(patch['msgid']), own))
        print(f"  {idx + 1:2} {patch['name']}")
        for trailer in added:
            print(f'       + {trailer}')
    return trailers


def _set_branch(branch, target, git_dir):
    """Move a branch to a commit, as 'git branch -f' does

    Where the branch is checked out, its worktree is reset to the commit
    instead, which git requires

    Args:
        branch (str): Branch to move, e.g. 'main-push'
        target (str): Commit to move it to
        git_dir (str or None): Directory of the git repo, or None for the
            current one

    Raises:
        ValueError: The worktree which has the branch is not clean
    """
    path = _worktree_of(branch, git_dir)
    if path:
        if _git('status', '--porcelain', '--untracked-files=no',
                cwd=path).stdout.strip():
            raise ValueError(f'{branch} is checked out in {path}, which has '
                             'changes: commit or stash them first')
        _git('reset', '--quiet', '--hard', target, cwd=path)
    else:
        command.run_one('git', 'branch', '--quiet', '--force', '--no-track',
                        branch, target, cwd=git_dir)


def _restart(rules, dry_run, git_dir):
    """Start the push branch again from upstream, dropping what is on it

    Args:
        rules (Rules): Rules to use
        dry_run (bool): True to say what would be dropped, without dropping
            it
        git_dir (str or None): Directory of the git repo, or None for the
            current one

    Return:
        str or None: The commit the push branch was at, so that it can be
            put back, or None if there is no push branch yet
    """
    branch = rules.push_branch
    if not gitutil.ref_exists(f'refs/heads/{branch}', git_dir):
        return None
    old = command.output('git', 'rev-parse', branch, cwd=git_dir).strip()
    count = int(command.output('git', 'rev-list', '--count',
                               f'{rules.upstream}..{branch}', cwd=git_dir))
    verb = 'Would start' if dry_run else 'Starting'
    msg = f'{verb} {branch} again from {rules.upstream}'
    if count:
        msg += f", dropping {count} commit{'s' if count > 1 else ''} " \
            f'(was {old[:12]})'
    print(msg)
    if not dry_run:
        _set_branch(branch, rules.upstream, git_dir)
    return old


def find_link(series, test_db=None, git_dir=None):
    """Get the link of the series which 'maint apply' is to apply

    A Message-ID, or a link to lore, is used as it is. Otherwise the series
    is one of your own, named or else the one on the current branch, and the
    latest version which has been sent is used, by the link which patman
    recorded when it sent it.

    Args:
        series (str or None): Message-ID, link or name of the series, or
            None for the current branch's
        test_db (str or None): Directory of the test database, or None to
            use the normal one
        git_dir (str or None): Directory of the git repo, or None for the
            current one

    Return:
        str: Message-ID or link of the series

    Raises:
        ValueError: The series is not known, or has not been sent
    """
    if series and ('@' in series or '/' in series):
        return series
    name = series or maint.current_branch(git_dir)
    if not name:
        raise ValueError('No branch is checked out: give the Message-ID of '
                         'the series to apply')
    cser = cseries.Cseries(test_db)
    cser.open_database()
    try:
        idnum = cser.db.series_find_by_name(name)
        svers = cser.db.ser_ver_get_for_series(idnum) if idnum else []
    finally:
        cser.close_database()
    if not idnum:
        raise ValueError(f"There is no series '{name}': give the Message-ID "
                         'of the series to apply')
    sent = [svr for svr in svers if svr.link]
    if not sent:
        raise ValueError(f"Series '{name}' has not been sent: give the "
                         'Message-ID of the series to apply')
    latest = max(sent, key=lambda svr: svr.version)
    print(f"Series '{name}' v{latest.version}: {latest.link}")
    return latest.link


# pylint: disable-next=R0913,R0914,R0917
def apply_series(rules, llist, link, patches=None, force=False,
                 dry_run=False, git_dir=None, redo=False):
    """Apply a series from the list to the push branch, then check it

    Args:
        rules (Rules): Rules to use
        llist (LoreList): List to read the series from
        link (str): Message-ID of the cover letter or first patch, or a link
        patches (str or None): Patches to apply, e.g. '1-3,5', or None for
            all
        force (bool): True to apply a series which is not complete, or of
            which there is a newer version
        dry_run (bool): True to say what would be applied, without applying
            it
        git_dir (str or None): Directory of the git repo, or None for the
            current one
        redo (bool): True to start the push branch again from upstream
            first, dropping what is on it, e.g. to apply a series again

    Return:
        int: 0 if it was applied and all is well, else 1
    """
    data, tags, newer, mbox = common.run(read_series(llist, link))
    sel = parse_patches(patches, len(data['patches']))
    if check_series(rules, data, newer, sel, force, git_dir, redo):
        return 0
    trailers = plan(rules, data, tags, sel)
    old = _restart(rules, dry_run, git_dir) if redo else None
    if dry_run:
        print(f'Would apply {_count(sel, data)} to {rules.push_branch}')
        return 0

    rules.branch = rules.push_branch
    if not gitutil.ref_exists(f'refs/heads/{rules.branch}', git_dir):
        command.run_one('git', 'branch', '--no-track', rules.branch,
                        rules.upstream, cwd=git_dir)
        tout.notice(f'Created {rules.branch} from {rules.upstream}')
    msgs = _split_mbox(mbox)
    problem = _apply_in_tree(rules.branch, b''.join(msgs[idx] for idx in sel),
                             trailers, git_dir)
    if problem:
        if old:
            _set_branch(rules.branch, old, git_dir)
        print(f"Cannot apply '{series_name(data)}' to {rules.branch}, even "
              'with a three-way merge, so nothing is applied; perhaps ask for '
              f'a rebase:\n{problem}')
        return 1
    print()
    ret = maint.do_check(rules, git_dir=git_dir)
    print(f'{_count(sel, data)} applied to {rules.branch}')
    return ret


def _apply_in_tree(branch, mbox, trailers, git_dir):
    """Apply patches to a branch in a worktree, adding trailers to each

    The worktree which has the branch checked out is used, if one does;
    else a temporary one is made

    Args:
        branch (str): Branch to apply to
        mbox (bytes): Patches
        trailers (list of list of str): Trailers to add to each patch
        git_dir (str or None): Directory of the git repo, or None for the
            current one

    Return:
        str or None: None if they applied, else what went wrong

    Raises:
        ValueError: The worktree which has the branch is not clean
    """
    path = _worktree_of(branch, git_dir)
    tmpdir = None
    if path:
        if _git('status', '--porcelain', '--untracked-files=no',
                cwd=path).stdout.strip():
            raise ValueError(f'{branch} is checked out in {path}, which has '
                             'changes: commit or stash them first')
    else:
        tmpdir = tempfile.mkdtemp(prefix='patman-apply.')
        path = os.path.join(tmpdir, 'tree')
        command.run_one('git', 'worktree', 'add', '--quiet', path, branch,
                        cwd=git_dir, capture=True, capture_stderr=True)
    try:
        before = _git('rev-parse', 'HEAD', cwd=path).stdout.strip()
        problem = _am(mbox, path)
        if problem:
            return problem
        commits = _git('rev-list', '--reverse', f'{before}..HEAD',
                       cwd=path).stdout.split()
        last = _rewrite(commits, trailers, path)
        _git('update-ref', f'refs/heads/{branch}', last, commits[-1],
             cwd=path)
        return None
    finally:
        if tmpdir:
            command.run_one('git', 'worktree', 'remove', '--force', path,
                            cwd=git_dir, capture=True, capture_stderr=True)
            shutil.rmtree(tmpdir, ignore_errors=True)
