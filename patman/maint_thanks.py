# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Draft the thanks for series which a maintainer has applied

Once a series from the list is upstream, the maintainer replies to it to say
so. 'patman maint thanks' finds the series from the Message-ID: trailers of
the commits which went upstream, reads each from lore, and makes a Gmail
draft of the reply, for you to add any comments to and send. Your own
series are left out. Which series have been thanked is noted, so that none
is thanked twice.
"""

from dataclasses import dataclass, field
import json
import os
import re

import aiohttp

from u_boot_pylib import command

from patman import gmail
from patman import lore
from patman import maint
from patman.review import common

# Where the state of the thanks is kept, in the git directory
STATE_FNAME = os.path.join('patman', 'thanks.json')

# The prefix of a patch's name, e.g. '[v3,2/5] '
RE_PREFIX = re.compile(r'^\[[^]]*\]\s*')


@dataclass
class Applied:
    """A series with some patches applied

    Properties:
        root (str): Message-ID of its cover letter or first patch
        data (dict): Series, as from LoreList.get_series()
        commits (dict): Commit hash of each patch applied, by its index in
            the series, from 1
    """
    root: str
    data: dict
    commits: dict = field(default_factory=dict)


def _state_path(git_dir):
    """Get the file which holds the state of the thanks

    Args:
        git_dir (str or None): Directory of the git repo, or None for the
            current one

    Return:
        str: Path of the file
    """
    common_dir = command.output('git', 'rev-parse', '--git-common-dir',
                                cwd=git_dir).strip()
    return os.path.join(git_dir or os.getcwd(), common_dir, STATE_FNAME)


def read_state(git_dir=None):
    """Read the state of the thanks

    Args:
        git_dir (str or None): Directory of the git repo, or None for the
            current one

    Return:
        dict: State:
            last (str or None): Upstream commit up to which thanks have been
                drafted
            thanked (list of str): Message-IDs of the series thanked
    """
    try:
        with open(_state_path(git_dir), encoding='utf-8') as inf:
            state = json.load(inf)
    except (OSError, ValueError):
        state = {}
    return {'last': state.get('last'), 'thanked': state.get('thanked', [])}


def _write_state(state, git_dir):
    """Write the state of the thanks

    Args:
        state (dict): State, as from read_state()
        git_dir (str or None): Directory of the git repo, or None for the
            current one
    """
    fname = _state_path(git_dir)
    os.makedirs(os.path.dirname(fname), exist_ok=True)
    with open(fname, 'w', encoding='utf-8') as outf:
        json.dump(state, outf, indent=1)


def _msgid(cmt):
    """Get the Message-ID which a commit's trailers give, if any

    Args:
        cmt (maint.Commit): Commit

    Return:
        str or None: Message-ID, without angle brackets
    """
    for key, value in cmt.trailers:
        if key.lower() == 'message-id':
            return lore.bare_msgid(value)
    return None


async def find_applied(rules, llist, commits, thanked):
    """Find the series which some commits came from

    Args:
        rules (Rules): Rules, whose sign-off says which series are your own
        llist (LoreList): List to read the series from
        commits (list of maint.Commit): Commits, oldest first
        thanked (list of str): Message-IDs of series thanked already, which
            are left out

    Return:
        list of Applied: Each series, in the order its first patch was
            applied
    """
    own = maint.email_of(rules.signoff)
    found = {}
    async with aiohttp.ClientSession() as client:
        for cmt in commits:
            msgid = _msgid(cmt)
            if not msgid or maint.email_of(cmt.email) == own:
                continue
            root = await llist.series_of(client, msgid)
            if not root or root in thanked:
                continue
            if root not in found:
                found[root] = Applied(root, await llist.get_series(client,
                                                                   root))
            for idx, patch in enumerate(found[root].data['patches'], 1):
                if lore.bare_msgid(patch['msgid']) == msgid:
                    found[root].commits[idx] = cmt.hash
    return [app for app in found.values() if app.commits]


def first_name(person):
    """Get someone's first name, to greet them

    Args:
        person (dict): Person, with 'name' and 'email'

    Return:
        str: First name, or the start of the email address if there is no
            name
    """
    name = (person.get('name') or '').strip()
    return name.split()[0] if name else person.get('email', '').split('@')[0]


def thanks_text(app, idxs, branch, closing):
    """Write the thanks for some patches of a series

    Args:
        app (Applied): Series
        idxs (list of int): Patches to list, by their index in the series
        branch (str): Branch they were applied to, e.g. 'main'
        closing (str or None): Closing lines, e.g. 'Regards,\nSimon'

    Return:
        str: Text of the email
    """
    lines = [f"Hi {first_name(app.data['submitter'])},", '',
             f'Applied to {branch}, thanks.', '']
    total = len(app.data['patches'])
    for idx in idxs:
        title = RE_PREFIX.sub('', app.data['patches'][idx - 1]['name'])
        prefix = f'[{idx}/{total}] ' if total > 1 else ''
        lines += [f'{prefix}{title}', f'      {app.commits[idx][:12]}']
    if closing:
        lines += ['', closing]
    return '\n'.join(lines) + '\n'


async def _headers(llist, app, idxs):
    """Read the headers of the messages which the thanks reply to

    Args:
        llist (LoreList): List to read them from
        app (Applied): Series
        idxs (list of int): Messages, by their index in the series, with 0
            for the cover letter

    Return:
        dict: Headers of each message, by its index
    """
    hdrs = {}
    async with aiohttp.ClientSession() as client:
        for idx in idxs:
            if idx:
                data = await llist.get_patch(
                    client, app.data['patches'][idx - 1]['id'])
            else:
                data = await llist.get_cover(client,
                                             app.data['cover_letter']['id'])
            hdrs[idx] = data.get('headers', {})
    return hdrs


def plan_thanks(rules, llist, app, closing):
    """Work out the emails which thank a series

    Args:
        rules (Rules): Rules, whose thanks_to says whether to reply to the
            cover letter or to each patch
        llist (LoreList): List to read the messages from
        app (Applied): Series
        closing (str or None): Closing lines, e.g. 'Regards,\nSimon'

    Return:
        list of tuple: Each email:
            dict: Headers, see gmail.review_envelopes()
            str: Text
    """
    branch = rules.upstream.split('/', 1)[1]
    applied = sorted(app.commits)
    if rules.thanks_to == 'each':
        replies = [(idx, [idx]) for idx in applied]
    else:
        to = 0 if app.data.get('cover_letter') else 1
        replies = [(to, applied)]
    hdrs = common.run(_headers(llist, app, [to for to, _ in replies]))
    envs = gmail.review_envelopes(app.data, hdrs, [to for to, _ in replies])
    return [(envs[to], thanks_text(app, idxs, branch, closing))
            for to, idxs in replies]


# pylint: disable-next=R0913,R0914,R0917
def draft_thanks(rules, llist, rng, closing=None, account=None,
                 dry_run=False, quiet=False, git_dir=None):
    """Draft the thanks for the series which a range of commits came from

    Args:
        rules (Rules): Rules to use
        llist (LoreList): List to read the series from
        rng (str or None): Range of commits, e.g. 'abc123..us/main', or None
            for those which went upstream since the thanks were last drafted
        closing (str or None): Closing lines, e.g. 'Regards,\nSimon'
        account (str or None): Gmail account to use
        dry_run (bool): True to show the thanks, without drafting them
        quiet (bool): True to say nothing if there are no series to thank
        git_dir (str or None): Directory of the git repo, or None for the
            current one

    Return:
        int: 0 if the thanks were drafted, or there were none, else 1

    Raises:
        ValueError: No range is given and there is no record of the last one
    """
    state = read_state(git_dir)
    if not rng:
        if not state['last']:
            raise ValueError("No thanks have been drafted here yet: give the "
                             f"commits to thank, e.g. '{rules.upstream}~5.."
                             f"{rules.upstream}'")
        rng = f"{state['last']}..{rules.upstream}"
    start, end = rng.split('..', 1)
    commits = maint.read_commits(start, end, git_dir)
    apps = common.run(find_applied(rules, llist, commits, state['thanked']))
    emails = [(env, text) for app in apps
              for env, text in plan_thanks(rules, llist, app, closing)]
    if dry_run:
        for env, text in emails:
            print(f"To: {env['to']}\nCc: {env['cc']}\n"
                  f"Subject: {env['subject']}\n\n{text}")
        return 0
    if emails:
        drafted = gmail.create_drafts_from_envelopes(
            dict(enumerate(env for env, _ in emails)),
            dict(enumerate(text for _, text in emails)),
            account=account, sender=rules.signoff)
        if len(drafted) < len(emails):
            print(f'Drafted {len(drafted)} of {len(emails)} thanks')
            return 1
        print(f"Drafted thanks for {', '.join(app.data['name'] for app in apps)}")
    elif not quiet:
        print('No series to thank')
    state['thanked'] += [app.root for app in apps]
    state['last'] = command.output('git', 'rev-parse', end,
                                   cwd=git_dir).strip()
    _write_state(state, git_dir)
    return 0
