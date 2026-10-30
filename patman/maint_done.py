# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Finish with a series which a maintainer has applied

Once a series is upstream and the thanks for it are sent, 'patman maint
done' checks that this is so, says if any issue which the commits close is
still open, and archives the branch of your own series, after asking. It
notes each series it has finished with, so that 'maint done' with no series
can go through those which are thanked but not finished with yet.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
import json
from urllib.error import URLError
from urllib.parse import quote
import urllib.request

from u_boot_pylib import command

from patman import cseries
from patman import gmail
from patman import maint
from patman import maint_apply
from patman import maint_ci
from patman import maint_thanks

# Seconds in a day: a search of the sent folder starts the day before the
# draft was made, so that time zones do not matter
DAY = 24 * 60 * 60

# The states of the thanks for a series, as found in Gmail
SENT, UNSENT, DELETED, UNKNOWN = 'sent', 'unsent', 'deleted', 'unknown'


def issue_open(match):
    """Check whether an issue is open

    Args:
        match (re.Match): Link to the issue, from maint.RE_ISSUE

    Return:
        bool or None: True if it is open, False if closed, or None if this
            cannot be told, e.g. as the project is private
    """
    try:
        if match['host'] == 'github.com':
            state = command.output(
                'gh', 'api', f"repos/{match['repo']}/issues/{match['num']}",
                '-q', '.state', raise_on_error=False).strip()
        else:
            url = (f"{maint_ci.GITLAB_API}/projects/"
                   f"{quote(match['repo'], safe='')}/issues/{match['num']}")
            with urllib.request.urlopen(url, timeout=30) as resp:
                state = json.load(resp).get('state')
    except (URLError, OSError, ValueError):
        return None
    if state in ('open', 'opened'):
        return True
    return False if state == 'closed' else None


def open_issues(cmts):
    """Find the issues which some commits close, but which are still open

    Args:
        cmts (list of maint.Commit): Commits

    Return:
        list of str: Link to each issue which is open, or which cannot be
            checked, with ' (cannot check)' after it
    """
    found = []
    for cmt in cmts:
        for key, value in cmt.trailers:
            match = key.lower() == 'closes' and maint.RE_ISSUE.match(
                value.strip())
            if not match or match['kind'] != 'issues' or value in found:
                continue
            state = issue_open(match)
            if state is None:
                found.append(f'{value} (cannot check)')
            elif state:
                found.append(value)
    return found


def thanks_state(rules, llist, app, state, account):
    """Check whether the thanks for a series were sent

    Each draft is looked for in Gmail. One which has gone was sent if a
    message from us is found in reply to the same message, as for reviews.

    Args:
        rules (Rules): Rules to use
        llist (LoreList): List to read the series from
        app (maint_thanks.Applied): Series
        state (dict): State of the thanks, from maint_thanks.read_state()
        account (str or None): Gmail account to use

    Return:
        str: SENT, UNSENT, DELETED, or UNKNOWN if the drafts are not known
    """
    drafts = maint_thanks.drafts_of(state, app.root)
    if not drafts:
        return UNKNOWN
    emails = maint_thanks.plan_thanks(rules, llist, app, None)
    if len(emails) != len(drafts):
        emails = [({'subject': None}, '')] * len(drafts)
    items = []
    for num, (draft, (env, text)) in enumerate(zip(drafts, emails)):
        when = draft['time']
        items.append(SimpleNamespace(
            idnum=num, draft_id=draft['id'], body=text, thread_id=None,
            subject=env['subject'], since=when, check_sent=False,
            after=(datetime.fromtimestamp(when - DAY, timezone.utc).strftime(
                '%Y/%m/%d') if when else None)))
    sent, deleted, _ = gmail.sync_drafts(gmail.get_service(account), items)
    if len(sent) == len(items):
        return SENT
    return DELETED if deleted else UNSENT


def _problem(rules, llist, app, state, account):
    """Check whether a series can be finished with

    Args:
        rules (Rules): Rules to use
        llist (LoreList): List to read the series from
        app (maint_thanks.Applied): Series, with its commits upstream
        state (dict): State of the thanks, from maint_thanks.read_state()
        account (str or None): Gmail account to use

    Return:
        str or None: What is not finished, or None if all is
    """
    title = maint_apply.series_name(app.data)
    missing = [str(idx) for idx in range(1, len(app.data['patches']) + 1)
               if idx not in app.commits]
    if missing:
        return (f"'{title}' is only partly in {rules.upstream}, without "
                f"patch(es) {', '.join(missing)}")
    if app.root not in state['thanked']:
        return f"'{title}' is not thanked yet: run 'patman maint thanks'"
    sent = thanks_state(rules, llist, app, state, account)
    if sent == UNSENT:
        return f"The thanks for '{title}' are not sent yet"
    if sent == DELETED:
        return (f"The thanks for '{title}' were deleted without being sent: "
                "use 'patman maint thanks --redraft'")
    return None


# pylint: disable-next=R0913,R0917
def finish(rules, llist, link, name=None, force=False, yes=False,
           dry_run=False, account=None, test_db=None, git_dir=None):
    """Finish with one series

    Args:
        rules (Rules): Rules to use
        llist (LoreList): List to read the series from
        link (str): Message-ID of the cover letter or first patch, or a link
        name (str or None): Name of your series which this is, whose branch
            can be archived, or None if it is not yours
        force (bool): True to finish with it even if it is not all
            upstream, or the thanks were not sent
        yes (bool): True to archive the branch without asking
        dry_run (bool): True to say what would be done, without doing it
        account (str or None): Gmail account to use
        test_db (str or None): Directory of the test database, or None to
            use the normal one
        git_dir (str or None): Directory of the git repo, or None for the
            current one

    Return:
        bool: True if it is finished with, or would be in a dry run
    """
    app, cmts = maint_thanks.find_upstream(rules, llist, link, git_dir)
    title = maint_apply.series_name(app.data)
    state = maint_thanks.read_state(git_dir)
    problem = _problem(rules, llist, app, state, account)
    if problem and not force:
        print(f'{problem}; use -f to finish with it anyway')
        return False
    if problem:
        print(problem)
    print(''.join(f'Still open: {issue}\n' for issue in open_issues(cmts)),
          end='')
    if dry_run:
        print(f"Would finish with '{title}'" +
              (f", archiving series '{name}'" if name else ''))
        return True
    if name and (yes or input(f"Archive series '{name}'? [y/N] ").strip(
            ).lower() in ('y', 'yes')):
        _archive(name, test_db, git_dir)
    if app.root not in state['done']:
        state['done'].append(app.root)
        maint_thanks.write_state(state, git_dir)
    print(f"Finished with '{title}'")
    return True


def _archive(name, test_db, git_dir):
    """Archive a series, as 'patman series archive' does

    Args:
        name (str): Name of the series
        test_db (str or None): Directory of the test database, or None to
            use the normal one
        git_dir (str or None): Directory of the git repo, or None for the
            current one
    """
    cser = cseries.Cseries(git_dir, db_dir=test_db)
    cser.open_database()
    try:
        cser.archive(name)
    finally:
        cser.close_database()


# pylint: disable-next=R0913,R0917
def do_done(rules, llist, what=None, force=False, yes=False, dry_run=False,
            account=None, test_db=None, git_dir=None):
    """Finish with a series, or with each series which is thanked

    Args:
        rules (Rules): Rules to use
        llist (LoreList): List to read the series from
        what (str or None): Series, by its Message-ID, link or name, or None
            for the current branch's series if patman sent it, else each
            series which is thanked but not finished with
        force (bool): True to finish with a series even if it is not all
            upstream, or the thanks were not sent
        yes (bool): True to archive the branch without asking
        dry_run (bool): True to say what would be done, without doing it
        account (str or None): Gmail account to use
        test_db (str or None): Directory of the test database, or None to
            use the normal one
        git_dir (str or None): Directory of the git repo, or None for the
            current one

    Return:
        int: 0 if each is finished with, else 1
    """
    link = maint_apply.find_link(what, test_db, git_dir, need=bool(what),
                                 verb='finish with')
    if link:
        name = (None if what and ('@' in what or '/' in what)
                else what or maint.current_branch(git_dir))
        return 0 if finish(rules, llist, link, name, force, yes, dry_run,
                           account, test_db, git_dir) else 1
    state = maint_thanks.read_state(git_dir)
    todo = [root for root in state['thanked'] if root not in state['done']]
    if not todo:
        print('Nothing to finish with')
        return 0
    ok = [finish(rules, llist, root, None, force, yes, dry_run, account,
                 test_db, git_dir) for root in todo]
    return 0 if all(ok) else 1
