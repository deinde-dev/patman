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
import time

import aiohttp

from u_boot_pylib import command

from patman import gmail
from patman import lore
from patman import maint
from patman import maint_apply
from patman.review import common

# Where the state of the thanks is kept, in the git directory
STATE_FNAME = os.path.join('patman', 'thanks.json')

# The prefix of a patch's name, e.g. '[v3,2/5] '
RE_PREFIX = re.compile(r'^\[[^]]*\]\s*')

# A line of a diffstat, e.g. ' dtc.c | 3 ++-' or ' 1 file changed, ...'
RE_DIFFSTAT = re.compile(r'^ (\S.* \| +(\d+|Bin)|\d+ files? changed)')

# How many lines of the message to quote, as a review does
QUOTE_LINES = 20

# The longest diffstat to quote, as a review does
DIFFSTAT_LINES = 30


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
            drafts (dict): Drafts of the thanks for each series thanked,
                by its Message-ID, see drafts_of()
            done (list of str): Message-IDs of the series finished with, see
                'patman maint done'
    """
    try:
        with open(_state_path(git_dir), encoding='utf-8') as inf:
            state = json.load(inf)
    except (OSError, ValueError):
        state = {}
    return {'last': state.get('last'), 'thanked': state.get('thanked', []),
            'drafts': state.get('drafts', {}), 'done': state.get('done', [])}


def write_state(state, git_dir):
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


def quote(msg):
    """Quote a patch or cover letter, as a review does

    The subject and message are quoted, as far as the first 20 lines, then
    a patch's diffstat, unless it is long

    Args:
        msg (dict): Patch or cover letter, as from LoreList.get_patch() or
            get_cover()

    Return:
        list of str: Lines of the quote, starting with who wrote it
    """
    who = msg['submitter']
    lines = [f"On {msg['date']}, {who['name']} <{who['email']}> wrote:"
             if msg.get('date') else f"{who['name']} <{who['email']}> wrote:"]
    text, _, rest = msg.get('content', '').partition('\n---\n')
    body = [RE_PREFIX.sub('', msg.get('name', '')), '']
    body += text.strip('\n').splitlines()
    lines += [f'> {line}'.rstrip() for line in body[:QUOTE_LINES]]
    if len(body) > QUOTE_LINES:
        lines.append('> [...]')
    stat = [line for line in rest.splitlines() if RE_DIFFSTAT.match(line)]
    if stat and len(stat) <= DIFFSTAT_LINES:
        lines += ['>'] + [f'> {line}' for line in stat]
    return lines


# pylint: disable-next=R0913,R0917
def thanks_text(app, idxs, branch, closing, own=False, msg=None):
    """Write the thanks for some patches of a series

    Args:
        app (Applied): Series
        idxs (list of int): Patches to list, by their index in the series
        branch (str): Branch they were applied to, e.g. 'main'
        closing (str or None): Closing lines, e.g. 'Regards,\nSimon'
        own (bool): True if the series is your own, so that there is no one
            to greet or thank
        msg (dict or None): Patch or cover letter replied to, to quote, as
            from LoreList.get_patch() or get_cover()

    Return:
        str: Text of the email
    """
    lines = [] if own else [f"Hi {first_name(app.data['submitter'])},", '']
    if msg:
        lines += quote(msg) + ['']
    lines += [f'Applied to {branch}.' if own else
              f'Applied to {branch}, thanks.', '']
    total = len(app.data['patches'])
    for idx in idxs:
        title = RE_PREFIX.sub('', app.data['patches'][idx - 1]['name'])
        prefix = f'[{idx}/{total}] ' if total > 1 else ''
        lines += [f'{prefix}{title}', f'      {app.commits[idx][:12]}']
    if closing:
        lines += ['', closing]
    return '\n'.join(lines) + '\n'


async def _messages(llist, app, idxs):
    """Read the messages which the thanks are about, with their replies

    Args:
        llist (LoreList): List to read them from
        app (Applied): Series
        idxs (list of int): Messages, by their index in the series, with 0
            for the cover letter

    Return:
        tuple:
            dict: Each message, as from LoreList.get_patch() or
                get_cover(), by its index
            dict: Latest reply to each message, directly or not, as from
                LoreList.get_patch_comments(), or None if it has none, by
                its index
    """
    msgs = {}
    latest = {}
    async with aiohttp.ClientSession() as client:
        for idx in idxs:
            if idx:
                item_id = app.data['patches'][idx - 1]['id']
                msgs[idx] = await llist.get_patch(client, item_id)
                replies = await llist.get_patch_comments(client, item_id)
            else:
                item_id = app.data['cover_letter']['id']
                msgs[idx] = await llist.get_cover(client, item_id)
                replies = await llist.get_cover_comments(client, item_id)
            latest[idx] = max(replies, key=lambda rep: rep.get('date') or '',
                              default=None)
    return msgs, latest


def follow_reply(env, reply):
    """Make the thanks reply to the latest reply to the message, if any

    Then the thanks come at the end of the discussion. They still go to the
    submitter, with everyone in that reply copied in.

    Args:
        env (dict): Headers to reply to the message, see
            gmail.review_envelopes()
        reply (dict or None): Latest reply, as from
            LoreList.get_patch_comments(), or None if there is none

    Return:
        dict: Headers for the thanks
    """
    if not reply:
        return env
    hdrs = reply.get('headers', {})
    refs = hdrs.get('References') or env['references'] or ''
    return dict(env, in_reply_to=reply['msgid'],
                references=f"{refs} {reply['msgid']}".strip(),
                cc=gmail.format_addrs([env['cc'], hdrs.get('From', ''),
                                       hdrs.get('To', ''),
                                       hdrs.get('Cc', '')]))


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
    msgs, latest = common.run(_messages(llist, app,
                                        [to for to, _ in replies]))
    envs = gmail.review_envelopes(
        app.data, {idx: msg.get('headers', {}) for idx, msg in msgs.items()},
        [to for to, _ in replies])
    envs = {idx: follow_reply(env, latest[idx]) for idx, env in envs.items()}
    own = (maint.email_of(app.data['submitter'].get('email', '')) ==
           maint.email_of(rules.signoff))
    return [(envs[to], thanks_text(app, idxs, branch, closing, own, msgs[to]))
            for to, idxs in replies]


# pylint: disable-next=R0913,R0917
def thank(rules, llist, what=None, closing=None, account=None, dry_run=False,
          redraft=False, test_db=None, git_dir=None):
    """Draft the thanks for a series, or for a range of commits

    Args:
        rules (Rules): Rules to use
        llist (LoreList): List to read the series from
        what (str or None): Range of commits, e.g. 'abc123..us/main'; or a
            series, by its Message-ID, link or name; or None for the current
            branch's series, if patman sent it, else the commits which went
            upstream since the thanks were last drafted
        closing (str or None): Closing lines, e.g. 'Regards,\nSimon'
        account (str or None): Gmail account to use
        dry_run (bool): True to show the thanks, without drafting them
        redraft (bool): True to draft the thanks again for series thanked
            already, deleting the drafts made before
        test_db (str or None): Directory of the test database, or None to
            use the normal one
        git_dir (str or None): Directory of the git repo, or None for the
            current one

    Return:
        int: 0 if the thanks were drafted, or there were none, else 1
    """
    link = None
    if not what or '..' not in what:
        link = maint_apply.find_link(what, test_db, git_dir, need=bool(what),
                                     verb='thank')
    if link:
        return thank_series(rules, llist, link, closing, account, dry_run,
                            redraft, git_dir)
    return draft_thanks(rules, llist, what, closing, account, dry_run,
                        redraft=redraft, git_dir=git_dir)


def find_upstream(rules, llist, link, git_dir=None):
    """Read a series and find which of its patches are upstream

    Args:
        rules (Rules): Rules to use
        llist (LoreList): List to read the series from
        link (str): Message-ID of the cover letter or first patch, or a link
        git_dir (str or None): Directory of the git repo, or None for the
            current one

    Return:
        tuple:
            Applied: Series, with the commit of each patch which is upstream
            list of maint.Commit: Those commits, oldest first
    """
    async def read():
        async with aiohttp.ClientSession() as client:
            return await llist.get_series(client, link)

    data = common.run(read())
    app = Applied(data['id'], data)
    idxs = {lore.bare_msgid(pat['msgid']): idx
            for idx, pat in enumerate(data['patches'], 1)}
    cmts = [cmt for cmt in maint.find_commits(rules.upstream, list(idxs),
                                              git_dir)
            if _msgid(cmt) in idxs]
    for cmt in cmts:
        app.commits[idxs[_msgid(cmt)]] = cmt.hash
    return app, cmts


# pylint: disable-next=R0913,R0917
def thank_series(rules, llist, link, closing=None, account=None,
                 dry_run=False, redraft=False, git_dir=None):
    """Draft the thanks for a series, for its patches which are upstream

    The series is thanked even if it is your own, since it is asked for.

    Args:
        rules (Rules): Rules to use
        llist (LoreList): List to read the series from
        link (str): Message-ID of the cover letter or first patch, or a link
        closing (str or None): Closing lines, e.g. 'Regards,\nSimon'
        account (str or None): Gmail account to use
        dry_run (bool): True to show the thanks, without drafting them
        redraft (bool): True to draft the thanks again if they were drafted
            before, deleting the drafts made then
        git_dir (str or None): Directory of the git repo, or None for the
            current one

    Return:
        int: 0 if the thanks were drafted, else 1
    """
    app, _ = find_upstream(rules, llist, link, git_dir)
    name = maint_apply.series_name(app.data)
    if not app.commits:
        print(f"'{name}' is not in {rules.upstream} yet")
        return 1
    state = read_state(git_dir)
    if app.root in state['thanked'] and not (dry_run or redraft):
        print(f"Thanks for '{name}' were drafted already: use --redraft to "
              'draft them again')
        return 0
    return 0 if _draft(rules, llist, [app], closing, account, dry_run,
                       state, git_dir) else 1


# pylint: disable-next=R0913,R0917
def _draft(rules, llist, apps, closing, account, dry_run, state, git_dir,
           quiet=False):
    """Draft the thanks for some series, or show them in a dry run

    Once they are all drafted, the series are noted as thanked, with their
    drafts, and any drafts made for them before are deleted, so that there
    is only one set

    Args:
        rules (Rules): Rules to use
        llist (LoreList): List to read the series from
        apps (list of Applied): Series to thank
        closing (str or None): Closing lines, e.g. 'Regards,\nSimon'
        account (str or None): Gmail account to use
        dry_run (bool): True to show the thanks, without drafting them
        state (dict): State of the thanks, as from read_state(), which is
            updated and written
        git_dir (str or None): Directory of the git repo, or None for the
            current one
        quiet (bool): True to say nothing if there are no series to thank

    Return:
        bool: True if each was drafted, or shown
    """
    roots = []
    emails = []
    for app in apps:
        for env, text in plan_thanks(rules, llist, app, closing):
            roots.append(app.root)
            emails.append((env, text))
    if dry_run:
        for env, text in emails:
            print(f"To: {env['to']}\nCc: {env['cc']}\n"
                  f"Subject: {env['subject']}\n"
                  f"In-Reply-To: {env['in_reply_to']}\n\n{text}")
        return True
    if emails:
        drafted = gmail.create_drafts_from_envelopes(
            dict(enumerate(env for env, _ in emails)),
            dict(enumerate(text for _, text in emails)),
            account=account, sender=rules.signoff)
        if len(drafted) < len(emails):
            print(f'Drafted {len(drafted)} of {len(emails)} thanks')
            return False
        print(f"Drafted thanks for {', '.join(app.data['name'] for app in apps)}")
        _replace_drafts(state, roots, drafted, account)
    elif not quiet:
        print('No series to thank')
    state['thanked'] += [app.root for app in apps
                         if app.root not in state['thanked']]
    write_state(state, git_dir)
    return True


def _replace_drafts(state, roots, drafted, account):
    """Note the drafts of the thanks, deleting any made before

    Drafts which were sent or deleted already are left alone.

    Args:
        state (dict): State of the thanks, as from read_state(), whose drafts
            are updated
        roots (list of str): Message-ID of the series which each email
            thanks, in order
        drafted (dict): Gmail draft ID of each email, by its index in roots
        account (str or None): Gmail account to use
    """
    old = [draft['id'] for root in set(roots)
           for draft in drafts_of(state, root)]
    if old:
        service = gmail.get_service(account)
        for draft_id in old:
            gmail.delete_draft(service, draft_id)
    now = int(time.time())
    for root in set(roots):
        state['drafts'][root] = [{'id': drafted[num], 'time': now}
                                 for num, rt in enumerate(roots)
                                 if rt == root]


def drafts_of(state, root):
    """Get the drafts of the thanks for a series

    Args:
        state (dict): State of the thanks, as from read_state()
        root (str): Message-ID of the series

    Return:
        list of dict: Each draft, in the order of the emails:
            id (str): Gmail draft ID
            time (int or None): When it was made, in seconds since the
                epoch, if known
    """
    return [draft if isinstance(draft, dict) else {'id': draft, 'time': None}
            for draft in state['drafts'].get(root, [])]


# pylint: disable-next=R0913,R0914,R0917
def draft_thanks(rules, llist, rng, closing=None, account=None,
                 dry_run=False, quiet=False, git_dir=None, redraft=False):
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
        redraft (bool): True to draft the thanks again for series thanked
            already, deleting the drafts made before

    Return:
        int: 0 if the thanks were drafted, or there were none, else 1

    Raises:
        ValueError: No range is given and there is no record of the last one
    """
    state = read_state(git_dir)
    if not rng:
        if not state['last']:
            raise ValueError("No thanks have been drafted here yet: give the "
                             f"series or commits to thank, e.g. '{rules.upstream}~5.."
                             f"{rules.upstream}'")
        rng = f"{state['last']}..{rules.upstream}"
    start, end = rng.split('..', 1)
    commits = maint.read_commits(start, end, git_dir)
    apps = common.run(find_applied(rules, llist, commits,
                                   [] if redraft else state['thanked']))
    if not dry_run:
        state['last'] = command.output('git', 'rev-parse', end,
                                       cwd=git_dir).strip()
    return 0 if _draft(rules, llist, apps, closing, account, dry_run, state,
                       git_dir, quiet) else 1
