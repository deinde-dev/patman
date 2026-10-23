# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2025 Canonical Ltd.
# Written by Simon Glass <simon.glass@canonical.com>
#

"""Scanning for new versions of reviewed series

The scan looks on patchwork for a newer version of each series which has
been reviewed, and reviews each one it finds in its own 'patman review'
process, optionally creating Gmail drafts for the results.
"""

from collections import namedtuple
from concurrent import futures
import os
import subprocess

import aiohttp

from u_boot_pylib import tout

import patman
from patman import interpreter
from patman import lorelist
from patman.review import common
from patman.review import drafts
from patman.review import lookup


NewVersion = namedtuple('NewVersion', 'desc,version,link,complete,active')


ScanResult = namedtuple('ScanResult', 'desc,version,link,returncode,output')


def _scan_new_versions(pwork, cser):
    """Find newer patchwork versions of already-reviewed series

    For each reviewed series, search patchwork by its cover-letter title
    for the highest version present that is newer than the latest reviewed
    one. The highest version is reported even when it is still incomplete,
    along with whether it has fully appeared, so the caller can wait for it
    rather than reviewing an older, now-superseded version.

    Args:
        pwork (Patchwork): Configured patchwork instance
        cser (Cseries): Open cseries instance

    Returns:
        list of NewVersion: one entry per series that has a newer version

    Raises:
        ValueError: The upstream's series are on lore, which cannot be
            searched for new versions yet
    """
    if isinstance(pwork, lorelist.LoreList):
        raise ValueError(f'Cannot scan {pwork.url} for new versions yet; '
                         'review each one with -s')
    series = cser.db.series_get_dict(reviews_only=True)
    found = []

    async def _scan():
        async with aiohttp.ClientSession() as client:
            for ser in series.values():
                max_ver = cser.db.series_get_max_version(ser.idnum)
                # Without a recorded reviewed version there is nothing to
                # compare against, so skip rather than treat every version
                # on patchwork as newer
                if not max_ver:
                    continue
                matches = await pwork.query_series(client, ser.desc)
                newer = [pws for pws in matches
                         if pws['name'] == ser.desc and
                         int(pws['version']) > max_ver]
                if not newer:
                    continue
                latest = max(newer, key=lambda pws: int(pws['version']))
                data = await pwork.get_series(client, latest['id'])
                received = data.get('received_total', 0)
                total = data.get('total', received)
                states = await pwork.get_series_patch_states(
                    client, latest['id'])
                active = any(state in lookup.ACTIVE_STATES for state in states)
                found.append(NewVersion(ser.desc, int(latest['version']),
                                        latest['id'], received >= total,
                                        active))

    common.run(_scan())
    return found


def _build_review_command(args, link):
    """Build the argv to review one series in a child process

    Passes through the global and review options given to --scan so each
    child behaves like a manual 'patman review -s <link>'. Global options
    (project, patchwork URL) come before the 'review' subcommand; review
    options come after it.

    Args:
        args (Namespace): Command-line arguments
        link (str): Patchwork series link/ID

    Returns:
        list of str: Command to run
    """
    cmd = [interpreter.get_python(), '-m', 'patman']
    if getattr(args, 'project', None):
        cmd += ['-p', args.project]
    if getattr(args, 'patchwork_url', None):
        cmd += ['-P', args.patchwork_url]
    if getattr(args, 'verbose', False):
        cmd.append('-v')
    if getattr(args, 'debug', False):
        cmd.append('-D')
    # The scan has already checked the patch state, so the child need not
    cmd += ['review', '-s', str(link), '--any-state']
    if getattr(args, 'upstream', None):
        cmd += ['-U', args.upstream]
    if getattr(args, 'review_db_dir', None):
        cmd += ['--review-db-dir', args.review_db_dir]
    if getattr(args, 'reviewer', None):
        cmd += ['--reviewer', args.reviewer]
    if getattr(args, 'base_branch', None):
        cmd += ['-b', args.base_branch]
    if getattr(args, 'gmail_account', None):
        cmd += ['--gmail-account', args.gmail_account]
    if getattr(args, 'signoff', None):
        cmd += ['--signoff', args.signoff]
    if getattr(args, 'spelling', None):
        cmd += ['--spelling', args.spelling]
    if getattr(args, 'context', None):
        cmd += ['-c', args.context]
    if getattr(args, 'model', None):
        cmd += ['--model', args.model]
    if getattr(args, 'create_drafts', False):
        cmd.append('--create-drafts')
    return cmd


def child_env():
    """Build the environment for a 'python -m patman' child process

    The child re-invokes patman with 'python -m patman'. When
    patman is run from a source checkout rather than an installed package,
    a fresh interpreter cannot import it, so put the directory that holds
    the patman package on PYTHONPATH. This is where the parent found it:
    the directory above the patman package. For an installed patman the
    directory is already importable, so adding it changes nothing.

    Returns:
        dict: Environment for the child process
    """
    env = os.environ.copy()
    pkg_parent = os.path.dirname(os.path.dirname(
        os.path.abspath(patman.__file__)))
    existing = env.get('PYTHONPATH', '')
    env['PYTHONPATH'] = (pkg_parent + os.pathsep + existing if existing
                         else pkg_parent)
    return env


def _review_one_subprocess(args, desc, version, link):
    """Review a single series in a child process

    Runs 'patman review -s <link>' so the review has its own database
    connection, event loop and worktree, capturing its combined output to
    print as one block once it finishes.

    Args:
        args (Namespace): Command-line arguments
        desc (str): Series description (cover-letter title)
        version (int): Series version being reviewed
        link (str): Patchwork series link/ID

    Returns:
        ScanResult: outcome of the review
    """
    cmd = _build_review_command(args, link)
    proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, check=False,
                          env=child_env())
    return ScanResult(desc, version, link, proc.returncode, proc.stdout)


def _print_scan_result(result, done, total):
    """Print the buffered output of a finished review as a labelled block

    Args:
        result (ScanResult): Result to print
        done (int): Number of reviews finished so far, including this one
        total (int): Total number of reviews being run
    """
    status = 'ok' if not result.returncode else f'failed ({result.returncode})'
    tout.notice('')
    tout.notice(f"===== [{done}/{total}] v{result.version} of "
                f"'{result.desc}' (link {result.link}): {status} =====")
    if result.output:
        tout.notice(result.output.rstrip())


def _review_stats(cser, link):
    """Count patches, comments and approvals for a reviewed series version

    Args:
        cser (Cseries): Open cseries instance
        link (str or int): Patchwork link of the reviewed version

    Returns:
        tuple or None: (num_patches, num_commented, num_approved), or None
            if the version is not in the database. A patch is 'commented'
            when its stored review requested changes and 'approved' when it
            was approved; the cover letter (seq 0) is not counted, and a
            patch the review had nothing to say about counts in neither
    """
    found = cser.db.series_find_by_link(link)
    if not found:
        return None
    _, _, _, svid = found
    num_patches = len(cser.db.pcommit_get_list(svid))
    approved = commented = 0
    for rev in cser.db.review_get_for_version(svid):
        if not rev.seq:
            continue
        if rev.approved:
            approved += 1
        else:
            commented += 1
    return num_patches, commented, approved


def _draft_undrafted(args, pwork, cser, dry_run=False):
    """Draft reviewed series whose reviews have no Gmail draft yet

    Finds each reviewed series whose latest version has stored reviews
    that are not yet in Gmail and creates the drafts from them, without
    re-running the review. This catches up a series reviewed by '--scan'
    without -d: run '--scan -d' again and the drafts are created.

    Args:
        args (Namespace): Command-line arguments
        pwork (Patchwork): Configured patchwork instance
        cser (Cseries): Open cseries instance
        dry_run (bool): True to report what would be drafted, not draft it

    Returns:
        int: Number of series drafted (or that would be drafted)
    """
    review_ids = {ser.idnum for ser in
                  cser.db.series_get_dict(reviews_only=True).values()}
    count = 0
    for svid, series_id, version in cser.db.series_get_all_max_versions():
        if series_id not in review_ids:
            continue
        reviews = cser.db.review_get_for_version(svid)
        if not any(drafts.needs_draft(rev, False) for rev in reviews):
            continue
        link = cser.db.ser_ver_get_link(series_id, version)
        if not link:
            continue
        count += 1
        if dry_run:
            tout.notice(f'Would create Gmail drafts for link {link} '
                        f'(v{version})')
            continue
        series_data = lookup.fetch_series(pwork, link)[0]
        drafts.draft_stored_reviews(args, reviews, series_data, pwork, cser)
    return count


def do_scan(args, pwork, cser):
    """Scan patchwork for new versions of already-reviewed series

    For every reviewed series, look for a version on patchwork higher than
    the latest one reviewed. Only the highest version is considered: if it
    has not fully appeared on patchwork yet, the series is left to wait
    rather than reviewing an older, superseded version. Complete versions
    are reviewed, each in its own child process, up to --jobs at a time.

    Args:
        args (Namespace): Command-line arguments
        pwork (Patchwork): Configured patchwork instance
        cser (Cseries): Open cseries instance

    Returns:
        int: 0 on success, 1 if any review failed
    """
    found = _scan_new_versions(pwork, cser)
    make_drafts = getattr(args, 'create_drafts', False)

    # With -d we also catch up drafts for series reviewed earlier without
    # it, so an empty scan is not the end of the story
    if not found and not make_drafts:
        tout.notice('No new versions found')
        return 0

    any_state = getattr(args, 'any_state', False)
    to_review = []
    waiting = 0
    skipped = 0
    for new in found:
        if not new.active and not any_state:
            skipped += 1
            tout.notice(f"Skipping v{new.version} of '{new.desc}' "
                        '(not active on patchwork)')
        elif new.complete:
            tout.notice(f"New version v{new.version} of '{new.desc}'")
            to_review.append(new)
        else:
            waiting += 1
            tout.notice(f"Waiting for v{new.version} of '{new.desc}' "
                        'to fully appear')

    total = len(to_review)
    if getattr(args, 'dry_run', False):
        for new in to_review:
            tout.notice(f"Would review v{new.version} of '{new.desc}'")
        drafted = (_draft_undrafted(args, pwork, cser, dry_run=True)
                   if make_drafts else 0)
        draft_str = f', {drafted} to draft' if make_drafts else ''
        tout.notice(f'Dry run: {len(found)} new, {total} to review, '
                    f'{waiting} waiting, {skipped} skipped{draft_str}')
        return 0

    failed = 0
    if total:
        jobs = max(1, getattr(args, 'jobs', 1))
        tout.notice(f'Launching {total} review(s), {min(jobs, total)} '
                    'at a time')
        done = 0
        ok_links = set()
        with futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            pending = [pool.submit(_review_one_subprocess, args, new.desc,
                                   new.version, new.link)
                       for new in to_review]
            for future in futures.as_completed(pending):
                result = future.result()
                done += 1
                _print_scan_result(result, done, total)
                if result.returncode:
                    failed += 1
                else:
                    ok_links.add(result.link)

        # Long-form summary of what each review found, in the order the
        # versions were listed above, before the one-line totals
        summarised = [new for new in to_review if new.link in ok_links]
        if summarised:
            tout.notice('')
            tout.notice('Review summary:')
            for new in summarised:
                stats = _review_stats(cser, new.link)
                if stats is None:
                    continue
                n_patches, commented, approved = stats
                tout.notice(
                    f'  {new.link}: {n_patches} patches, {commented} with '
                    f'comments, {approved} approved - {new.desc}')

    # Catch up drafts for series reviewed earlier without -d (the new
    # versions just reviewed are drafted by their child run already)
    drafted = _draft_undrafted(args, pwork, cser) if make_drafts else 0

    draft_str = f', {drafted} drafted' if make_drafts else ''
    tout.notice(f'Scanned: {len(found)} new, {total - failed} reviewed, '
                f'{waiting} waiting, {skipped} skipped, {failed} failed'
                f'{draft_str}')
    return 1 if failed else 0
