# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2025 Canonical Ltd.
# Written by Simon Glass <simon.glass@canonical.com>
#

"""Common code for the review modules

This holds the review context, the options for the Claude agents and helpers
for running coroutines and calling patchwork, which the other review modules
share.
"""

import asyncio
import os
import re
import shutil
import uuid

import aiohttp

from u_boot_pylib import gitutil
from u_boot_pylib import tools
from u_boot_pylib import tout

try:
    from claude_agent_sdk import ClaudeAgentOptions
except ImportError:
    ClaudeAgentOptions = None


# Claude model to use for the current review run, or None to let the SDK
# use whatever model the user's Claude default resolves to. Set once at the
# start of do_review() from --model / the 'model' setting, then read by
# agent_options() so every agent in the run uses the same model
_AGENT_MODEL = None

# Where Claude Code's installer puts it, which is often not on the PATH of a
# service, such as the one running 'patman serve', or of a cron job
LOCAL_BIN = '~/.local/bin'

# Set once it is reported that only the copy bundled with the SDK is present
_BUNDLED_WARNED = False


def run(coro):
    """Run a coroutine on the event loop and wait for it to finish

    Args:
        coro (coroutine): Coroutine to run

    Returns:
        The coroutine's result
    """
    return asyncio.get_event_loop().run_until_complete(coro)


def pw_call(method, *args):
    """Call a Patchwork method which needs a client session, and wait for it

    Args:
        method (coroutine function): Patchwork method, which takes the client
            session as its first argument
        args: Further arguments for the method

    Returns:
        The method's result
    """
    async def _call():
        async with aiohttp.ClientSession() as client:
            return await method(client, *args)
    return run(_call())


def set_agent_model(model):
    """Set the Claude model for the agents in this review run

    Args:
        model (str or None): Model to use, or None for the SDK default
    """
    global _AGENT_MODEL  # pylint: disable=W0603
    _AGENT_MODEL = model


def find_claude():
    """Find the installed Claude Code, looking in ~/.local/bin if need be

    If Claude Code is not on the PATH but is in ~/.local/bin, that directory
    is added to the end of the PATH, for this process and anything it runs.
    This lets u_boot_pylib, which picks the binary to run and reports it,
    find the same one, so that the old copy bundled with claude-agent-sdk is
    not used instead.

    Returns:
        str or None: Path to Claude Code, or None if it is not installed
    """
    global _BUNDLED_WARNED  # pylint: disable=W0603
    cli = shutil.which('claude')
    if cli:
        return cli
    local = os.path.expanduser(LOCAL_BIN)
    cli = shutil.which('claude', path=local)
    if cli:
        os.environ['PATH'] = os.pathsep.join(
            filter(None, [os.environ.get('PATH'), local]))
        return cli
    if not _BUNDLED_WARNED:
        _BUNDLED_WARNED = True
        tout.warning(f'Claude Code is not on the PATH or in {LOCAL_BIN}, so '
                     'the copy bundled with claude-agent-sdk is used, which '
                     "may be old; 'claude update' does not update it")
    return None


def agent_options(**kwargs):
    """Create ClaudeAgentOptions with the selected review model applied

    A model chosen with --model (or the 'model' setting) overrides the
    user's global Claude default for the whole review run, so a review
    always uses the intended model even if the user's default is something
    else. With no model selected the SDK default is used unchanged.

    The SDK prefers the Claude Code CLI bundled with it, which may be too
    old to use the current models. Use the installed 'claude' instead, if
    there is one, so that updating Claude Code is enough to keep reviews
    working. See find_claude() for where it is looked for.

    Args:
        kwargs: Fields to pass to ClaudeAgentOptions

    Returns:
        ClaudeAgentOptions: Configured options
    """
    if _AGENT_MODEL:
        kwargs.setdefault('model', _AGENT_MODEL)
    cli = find_claude()
    if cli:
        kwargs.setdefault('cli_path', cli)
    return ClaudeAgentOptions(**kwargs)


def session_options(ctx, seq, **kwargs):
    """Create agent options for reviewing a patch in a new, recorded session

    The session ID is kept in ctx.session_ids so that it can be stored with
    the review and the session resumed later, e.g. to revise the review.

    Args:
        ctx (ReviewContext): Review context
        seq (int): Patch number, or 0 for the cover letter
        kwargs: Fields to pass to ClaudeAgentOptions

    Returns:
        ClaudeAgentOptions: Configured options
    """
    ctx.session_ids[seq] = str(uuid.uuid4())
    return agent_options(session_id=ctx.session_ids[seq], **kwargs)


class ReviewContext:  # pylint: disable=R0902
    """Common context for review operations

    Attributes:
        reviewer_name (str): Reviewer's name
        reviewer_email (str): Reviewer's email
        series_data (dict): Series data from patchwork
        main_repo (str): Top-level of the user's main checkout (where
            the .git dir lives and where shared refs are resolved)
        repo_path (str): Path to the per-review worktree (used as cwd
            for apply/build/agent)
        signoff (str or None): Sign-off text for reviews with comments
        spelling (str): Spelling convention
        em_dash (str): Text to use in place of each em dash which the agent
            writes, or an em dash to keep them
        comments_path (str or None): Path to existing patchwork comments
        pwork (Patchwork or None): Patchwork instance
        cser (Cseries or None): Open cseries instance
        series_id (int or None): Series database ID
        svid (int or None): ser_ver database ID
        version (int or None): Series version number
        branch_name (str or None): Branch with applied patches
        upstream_branch (str or None): Upstream branch ref
        patch_count (int or None): Number of patches
        patch_selection (set of int or None): Patch indices to review
            (None means all)
        context (str or None): Extra user-supplied context to pass to
            the review agent (e.g. 'this is RFC, ignore whitespace')
    """

    def __init__(self, pwork, cser, series_data):
        self.pwork = pwork
        self.cser = cser
        self.series_data = series_data
        self.reviewer_name = None
        self.reviewer_email = None
        self.main_repo = None
        self.repo_path = None
        self.signoff = None
        self.spelling = 'British'
        self.em_dash = '-'
        self.comments_path = None
        self.series_id = None
        self.svid = None
        self.version = None
        self.branch_name = None
        self.upstream_branch = None
        self.patch_count = None
        self.patch_selection = None
        self.cover_content = None
        # Agent session ID used to review each patch (0 for the cover letter)
        self.session_ids = {}
        # Review of each patch (0 for the cover letter) in an earlier
        # version, and that version
        self.previous_reviews = {}
        self.previous_versions = {}
        # Context which the agent was given for each patch (0 for the cover
        # letter), as from prompts.context_parts()
        self.contexts = {}
        self.diffstat = None
        self.context = None
        self.coverity_text = None

    @property
    def reviewer_tag(self):
        """Get 'Name <email>' string for the reviewer"""
        return f'{self.reviewer_name} <{self.reviewer_email}>'

    @property
    def author_name(self):
        """Get the series submitter's name"""
        return self.series_data.get('submitter', {}).get('name', '')

    @property
    def author_email(self):
        """Get the series submitter's email"""
        return self.series_data.get('submitter', {}).get('email', '')

    @property
    def date(self):
        """Get the series date string"""
        return self.series_data.get('date', '')

    def drop_version(self):
        """Remove the version being reviewed, since it is not to be reviewed

        The series goes too if it has no other versions, so that it does
        not look reviewed, e.g. as an earlier version in the inbox
        """
        self.cser.db.ser_ver_remove(self.series_id, self.version)
        self.cser.db.series_remove_if_empty(self.series_id)
        self.cser.commit()


def read_context(spec):
    """Resolve a --context argument value to literal text

    Args:
        spec (str): Either a literal string, or '@path' to read from
            a file (path is expanded for '~')

    Return:
        str: The context text to pass to the agent
    """
    if spec.startswith('@'):
        path = os.path.expanduser(spec[1:])
        return tools.read_file(path, binary=False).rstrip()
    return spec


def parse_patch_selection(spec):
    """Parse a patch selection string into a set of patch numbers

    Supports comma-separated numbers and ranges, e.g. '1,3,5' or '2-7'
    or '1,3-5,8'.

    Args:
        spec (str or None): Selection string, or None for all patches

    Returns:
        set of int or None: Selected patch numbers, or None for all
    """
    if not spec:
        return None
    result = set()
    for part in spec.split(','):
        if '-' in part:
            start, end = part.split('-', 1)
            result.update(range(int(start), int(end) + 1))
        else:
            result.add(int(part))
    return result


def parse_reviewer(args):
    """Extract reviewer name and email from args

    Uses --reviewer if provided, otherwise falls back to git config.

    Args:
        args (Namespace): Command-line arguments

    Returns:
        tuple: (reviewer_name, reviewer_email)

    Raises:
        ValueError: if the reviewer identity cannot be determined
    """

    if args.reviewer:
        match = re.match(r'(.+?)\s*<(.+?)>', args.reviewer)
        if not match:
            raise ValueError(
                f"Invalid reviewer format '{args.reviewer}';"
                " use 'Name <email>'")
        return match.group(1).strip(), match.group(2).strip()
    name = gitutil.get_default_user_name()
    email = gitutil.get_default_user_email()
    if not name or not email:
        raise ValueError(
            'Cannot determine reviewer identity; set git user.name'
            ' and user.email, or use --reviewer')
    return name, email
