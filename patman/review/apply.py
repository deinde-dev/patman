# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2025 Canonical Ltd.
# Written by Simon Glass <simon.glass@canonical.com>
#

"""Applying a series to a local branch for review

A series is applied on top of the commit it says it is based on, if that
can be found, or else on the upstream branch. An agent applies the patches
from the mbox and deals with any which do not apply cleanly.
"""

import os
import re
import subprocess
import tempfile

import aiohttp

from u_boot_pylib import claude as claude_mod
from u_boot_pylib import gitutil
from u_boot_pylib import tools
from u_boot_pylib import tout

from patman.review import common
from patman.review import lookup
from patman.review import prompts


async def fetch_mbox(pwork, link):
    """Download the series mbox file from patchwork

    Args:
        pwork (Patchwork): Patchwork instance to fetch from
        link (str): Patchwork series link/ID

    Returns:
        str: Path to the downloaded mbox file

    Raises:
        ValueError: if the download fails
    """
    tout.notice(f'Downloading mbox for series {link} from {pwork.url}')
    mbox_path = os.path.join(tempfile.gettempdir(),
                             f'patman_review_{link}.mbox')
    async with aiohttp.ClientSession() as client:
        content = await pwork.get_series_mbox(client, link)
    if not content:
        raise ValueError(f'Empty mbox downloaded for series {link}')

    tools.write_file(mbox_path, content)
    tout.notice(f'Downloaded {len(content)} bytes to {mbox_path}')
    return mbox_path


async def apply_series(pwork, link, branch_name, upstream_branch,
                       repo_path):
    """Download and apply a patch series to a new local branch

    Uses the Claude agent to handle the git am process, including conflict
    resolution.

    Args:
        pwork (Patchwork): Patchwork instance to fetch from
        link (str): Patchwork series link/ID
        branch_name (str): Name for the new branch
        upstream_branch (str): Branch to base from
        repo_path (str): Path to the git repository

    Returns:
        tuple: (success, branch_name) where success is bool and
            branch_name is the name of the created branch
    """

    if not claude_mod.check_available():
        return False, None

    # Download the mbox
    mbox_path = await fetch_mbox(pwork, link)

    # Build the prompt and run the agent
    prompt = prompts.build_apply_prompt(
        mbox_path, branch_name, upstream_branch)
    options = common.agent_options(
        allowed_tools=['Bash', 'Read', 'Grep', 'Edit', 'Write'],
        cwd=repo_path, max_buffer_size=claude_mod.MAX_BUFFER_SIZE)

    tout.notice(f'Applying series to branch {branch_name}...')
    success, _ = await claude_mod.run_agent_collect(prompt, options)

    if os.path.exists(mbox_path):
        os.unlink(mbox_path)

    return success, branch_name


def apply_series_sync(pwork, link, branch_name, upstream_branch, repo_path):
    """Synchronous wrapper for apply_series()

    Args:
        pwork (Patchwork): Patchwork instance to fetch from
        link (str): Patchwork series link/ID
        branch_name (str): Name for the new branch
        upstream_branch (str): Branch to base from
        repo_path (str): Path to the git repository

    Returns:
        tuple: (success, branch_name)
    """
    return common.run(apply_series(
        pwork, link, branch_name, upstream_branch, repo_path))


def main_branch(ups, git_dir=None):
    """Get the main branch of an upstream

    U-Boot's main branch is 'main', having been 'master'. Use whichever the
    remote has, preferring 'main', since 'master' may be left behind.

    Args:
        ups (str): Upstream name, which is also the git remote
        git_dir (str or None): Directory containing the git repo, or None
            for the current directory

    Returns:
        str: Branch ref, e.g. 'us/main'
    """
    ref = f'{ups}/main'
    return ref if gitutil.ref_exists(ref, git_dir) else f'{ups}/master'


def get_upstream_branch(args, cser):
    """Determine the base branch for applying patches

    Honours --base-branch if the user supplied one. Otherwise prefers
    '<upstream>/next' when it has commits ahead of the main branch
    ('<upstream>/main', or '<upstream>/master' if there is no main),
    falling back to the main branch when next is empty (e.g. just after a
    release, when next has been merged into main and has not yet been
    reopened for the next cycle).

    Args:
        args (Namespace): Command-line arguments
        cser (Cseries): Open cseries instance

    Returns:
        str: Base branch ref, e.g. 'us/next' or 'us/main'
    """
    if args.base_branch:
        return args.base_branch
    ups = args.upstream
    if not ups:
        ups = cser.db.upstream_get_default()
    if ups:
        next_branch = f'{ups}/next'
        main = main_branch(ups)
        if gitutil.ref_exists(next_branch):
            ahead = gitutil.count_revs(None, f'{main}..{next_branch}')
            if ahead:
                return next_branch
        return main
    return 'origin/master'


# A 'base-commit:' trailer, added by 'git format-patch --base' and by patman,
# giving the commit the series was written against
RE_BASE_COMMIT = re.compile(r'^base-commit:\s*([0-9a-f]{7,40})\s*$',
                            re.MULTILINE)


# A note in the cover letter saying which branch the series is for, e.g.
# 'This series is based on master' or 'Applies on top of u-boot/next'
RE_BASED_ON = re.compile(
    r'\b(?:based|applies|apply|rebased)\s+'
    r'(?:onto|on(?:\s+top\s+of)?|to|against)'
    r'\s+(?:the\s+)?(?:(?:u-boot|upstream|origin|us)[/ ])?[\'"`]?'
    r'(master|main|next)\b', re.IGNORECASE)


def find_series_base(text):
    """Find which commit or branch a series says it is based on

    Args:
        text (str): Cover letter, or the last patch if there is none

    Returns:
        tuple:
            str or None: Base commit from a 'base-commit:' trailer
            str or None: 'main' or 'next', if the text says the series
                is based on one of them ('master' counts as 'main')
    """
    match = RE_BASE_COMMIT.search(text or '')
    commit = match.group(1) if match else None
    match = RE_BASED_ON.search(text or '')
    branch = None
    if match:
        branch = 'next' if match.group(1).lower() == 'next' else 'main'
    return commit, branch


def _base_text(ctx):
    """Get the text which may say what a series is based on

    'git format-patch --base' puts the trailer at the end of the cover
    letter, or of the last patch if there is no cover letter. The cover
    letter is kept in ctx.cover_content for the review.

    Args:
        ctx (ReviewContext): Review context (uses pwork and series_data)

    Returns:
        str: Text to search
    """
    ctx.cover_content = lookup.fetch_cover_content(ctx.pwork, ctx.series_data)
    if ctx.cover_content:
        return ctx.cover_content
    patches = ctx.series_data.get('patches') or []
    if not patches:
        return ''

    data = common.pw_call(ctx.pwork.get_patch, str(patches[-1]['id']))
    return f"{data.get('content') or ''}\n{data.get('diff') or ''}"


def _is_ancestor(commit, ref, git_dir):
    """Check whether a commit is in the history of a ref"""
    return subprocess.run(
        ['git', 'merge-base', '--is-ancestor', commit, ref], cwd=git_dir,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False).returncode == 0


def _commit_exists(commit, git_dir):
    """Check whether a commit is in the repository"""
    return gitutil.ref_exists(f'{commit}^{{commit}}', git_dir)


def series_base(ctx, ups, default):
    """Work out the base to apply a series to, from what the series says

    During the merge window, series are often written against main rather
    than next, and do not apply to next. If the series gives a base commit,
    use whichever of next and main holds it, preferring the default; if
    neither does, use the commit itself. The upstream is fetched if the
    commit is not known yet. Failing that, use the branch which the cover
    letter says the series is based on, if any.

    Args:
        ctx (ReviewContext): Review context (uses pwork, series_data and
            main_repo)
        ups (str or None): Upstream name, which is also the git remote
        default (str): Base which would otherwise be used, e.g. 'us/next'

    Returns:
        str or None: Base to use, or None to use the default
    """
    commit, branch = find_series_base(_base_text(ctx))
    if not ups:
        return commit if commit and _commit_exists(
            commit, ctx.main_repo) else None
    if commit and not _commit_exists(commit, ctx.main_repo):
        tout.notice(f"Fetching '{ups}' to find base-commit {commit}")
        subprocess.run(['git', 'fetch', '-q', ups], cwd=ctx.main_repo,
                       check=False)
    main = main_branch(ups, ctx.main_repo)
    branches = [default] + [ref for ref in (f'{ups}/next', main)
                            if ref != default]
    if commit:
        if _commit_exists(commit, ctx.main_repo):
            for ref in branches:
                if gitutil.ref_exists(ref, ctx.main_repo) and \
                        _is_ancestor(commit, ref, ctx.main_repo):
                    tout.notice(f'The series is based on {commit[:12]}, '
                                f'which is in {ref}')
                    return ref
            tout.notice(f'The series is based on {commit[:12]}, which is '
                        f'not in {ups}/next or {main}; using it')
            return commit
        tout.warning(f'Cannot find base-commit {commit}; using {default}')
    if branch:
        ref = main if branch == 'main' else f'{ups}/{branch}'
        if gitutil.ref_exists(ref, ctx.main_repo):
            tout.notice(f'The cover letter says the series is based on '
                        f'{branch}; using {ref}')
            return ref
    return None


def apply_and_check(ctx, link):
    """Download, apply patches and verify they applied

    Runs in the per-review worktree at ctx.repo_path; the branch
    ctx.branch_name has already been created and reset to upstream by
    worktree.ensure_worktree(), so the agent only needs to 'git am'.

    Args:
        ctx (ReviewContext): Review context (uses pwork, cser, series_id,
            version, upstream_branch, branch_name, repo_path)
        link (str): Patchwork series link/ID

    Returns:
        True if the patches applied cleanly, None if there was nothing to
        apply because every patch is already upstream (a clean skip), or
        False if applying failed
    """
    success, _ = apply_series_sync(ctx.pwork, link, ctx.branch_name,
        ctx.upstream_branch, ctx.repo_path)

    if not success:
        # The agent hit a real problem, or was interrupted (which leaves
        # the branch missing); either way there is nothing to review
        tout.error('Failed to apply patches to branch')
        ctx.cser.db.ser_ver_remove(ctx.series_id, ctx.version)
        ctx.cser.commit()
        return False

    applied = gitutil.count_revs(
        ctx.repo_path, f'{ctx.upstream_branch}..{ctx.branch_name}')
    if not applied:
        # The agent ran to completion but applied nothing: every patch is
        # already present upstream (a fresh checkout of an obsolete
        # series). That is a valid outcome, not a failure -- there is
        # simply nothing to review
        tout.notice(f"Nothing to review for '{ctx.branch_name}': every "
                    'patch is already applied upstream')
        ctx.cser.db.ser_ver_remove(ctx.series_id, ctx.version)
        ctx.cser.commit()
        return None
    if applied != ctx.patch_count:
        # Common with kernel-import series: the agent legitimately skips
        # patches that are already applied upstream. Warn and proceed
        # rather than discarding the apply
        tout.warning(f'Only {applied} of {ctx.patch_count} patches applied '
                     f'to {ctx.branch_name}; reviewing what is there')
    tout.notice(f'Patches applied to branch: {ctx.branch_name}')
    return True
