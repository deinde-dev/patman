# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Talk through a reply with the agent, to agree on a response

'patman review --discuss-reply ID' starts an interactive Claude session
with Remote Control, so that it can be used from claude.ai/code or the
Claude app. It is given the reply, our review and any response so far, and
carries on from the session which wrote the review, where it can. Once a
response is agreed, the session stores it with --set-response, to be
drafted or sent from patman as usual.

The session runs in a detached tmux session, since Claude Code needs a
terminal. What it was given, and the review worktree it runs in, are kept in
a directory of their own until the discussion ends.
"""

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

from u_boot_pylib import gitutil
from u_boot_pylib import tout

from patman import database
from patman import lore
from patman.review import common
from patman.review import stored

# How long to wait for the session to give its Remote Control link
URL_WAIT_S = 30

# The link to the session, which Claude Code shows when it starts
RE_URL = re.compile(r'https://claude\.ai/code/session_\w+')

# What Claude Code shows when it asks whether to trust the directory
RE_TRUST = re.compile(r'trust this folder', re.I)

# Longest name for the session, as shown in the Claude app
NAME_LEN = 60

PROMPT = '''{reviewer} would like to talk through a reply to their review of \
a patch series with you, to agree on a response to it.{carry_on}

The series is '{desc}'. {reply_from} replied to the review of {what}.
- The review: {review_path}
- The reply: {reply_path}
- The response drafted so far, if any: {response_path}
- The thread, on lore: {thread_url}

Read these, then tell {reviewer} briefly what the reply says and what you \
think the response should cover, and ask what they would like to say. Talk \
it through with them, checking facts in the code or on the mailing list as \
needed, e.g. with git log, git show, Read and Grep. Do not change any files \
in the tree and do not send any email.

Once {reviewer} agrees on the response, write the complete email body to \
{agreed_path}, in the format of a review email: the greeting, the quoted \
lines starting with '>', the comments after each quote and the sign-off, \
using {spelling} spelling. Then store it in patman with:

  {store_cmd}

which also puts it in a Gmail draft, replacing any earlier one. Tell \
{reviewer} that it is stored and drafted, so they can check and send it from \
patman or Gmail. If they want more changes afterwards, change the file and \
run the command again.
'''


def _key(cser):
    """Get a short key for the review database, to tell its sessions apart

    Args:
        cser (Cseries): Open cseries instance holding the replies

    Returns:
        str: Key, e.g. '3fa9c1'
    """
    path = os.path.realpath(cser.db.db_path)
    return hashlib.sha1(path.encode('utf-8')).hexdigest()[:6]


def session_name(cser, reply_id):
    """Get the name of the tmux session for discussing a reply

    Args:
        cser (Cseries): Open cseries instance holding the replies
        reply_id (int): ID of the reply

    Returns:
        str: Name, e.g. 'patman-3fa9c1-reply-12'
    """
    return f'patman-{_key(cser)}-reply-{reply_id}'


def state_dir(name):
    """Get the directory which holds what a discussion was given

    Args:
        name (str): Name of the tmux session

    Returns:
        str: Path of the directory
    """
    base = os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache')
    return os.path.join(base, 'patman', 'discuss', name)


def _tmux(*args):
    """Run a tmux command

    Args:
        args (str): Arguments for tmux

    Returns:
        subprocess.CompletedProcess: Result, with its output as text
    """
    return subprocess.run(['tmux', *args], capture_output=True, text=True,
                          check=False)


def _running():
    """Get the names of the tmux sessions which are running

    Returns:
        set of str: Names, empty if tmux has no sessions or is missing
    """
    try:
        res = _tmux('list-sessions', '-F', '#{session_name}')
    except FileNotFoundError:
        return set()
    return set(res.stdout.split()) if res.returncode == 0 else set()


def _read_state(name):
    """Read the state of a discussion

    Args:
        name (str): Name of the tmux session

    Returns:
        dict or None: State, or None if there is none
    """
    try:
        with open(os.path.join(state_dir(name), 'state.json'),
                  encoding='utf-8') as inf:
            return json.load(inf)
    except (OSError, ValueError):
        return None


def _write_state(name, state):
    """Write the state of a discussion

    Args:
        name (str): Name of the tmux session
        state (dict): State to write
    """
    with open(os.path.join(state_dir(name), 'state.json'), 'w',
              encoding='utf-8') as outf:
        json.dump(state, outf)


def _clean_up(name, state):
    """Remove what a discussion leaves behind, once it has ended

    Args:
        name (str): Name of the tmux session
        state (dict or None): State of the discussion
    """
    if state and state.get('added'):
        gitutil.remove_worktree(state['git_dir'], state['cwd'])
    shutil.rmtree(state_dir(name), ignore_errors=True)


def discussions(cser):
    """Find the replies being discussed, tidying up after any which ended

    Args:
        cser (Cseries): Open cseries instance holding the replies

    Returns:
        dict: key (int): reply ID
            value (dict): 'name' (str), the name of the session in the
                Claude app, and 'url' (str or None), its link
    """
    prefix = f'patman-{_key(cser)}-reply-'
    base = state_dir('')
    try:
        names = [name for name in os.listdir(base) if name.startswith(prefix)]
    except OSError:
        return {}
    running = _running() if names else set()
    found = {}
    for name in names:
        state = _read_state(name)
        if name not in running:
            _clean_up(name, state)
        elif state:
            found[int(name[len(prefix):])] = {'name': state['title'],
                                              'url': state.get('url')}
    return found


def _title(reply):
    """Get the name to show for the session in the Claude app

    Args:
        reply (Reply): Reply being discussed

    Returns:
        str: Name, e.g. 'patman: Doug Anderson on [PATCH 0/7] arm64: ...'
    """
    sender = re.sub(r'\s*<.*>', '', reply.sender or '').strip('" ')
    subject = re.sub(r'^(re:\s*)+', '', reply.subject or '', flags=re.I)
    title = f'patman: {sender or "reply"} on {subject}'
    return title if len(title) <= NAME_LEN else title[:NAME_LEN - 3] + '...'


def _session_file(cwd, session):
    """Get the file in which Claude Code keeps a session

    Args:
        cwd (str): Directory the session ran in
        session (str): Session ID

    Returns:
        str: Path of the file
    """
    project = re.sub(r'[^A-Za-z0-9]', '-', os.path.realpath(cwd))
    return os.path.join(os.path.expanduser('~/.claude/projects'), project,
                        f'{session}.jsonl')


def _where(cser, rev, git_dir):
    """Decide where the session runs, and which session it carries on from

    The session which wrote the review can be carried on from its review
    worktree, which is checked out again if needed.

    Args:
        cser (Cseries): Open cseries instance holding the replies
        rev (Review or None): Review which was replied to
        git_dir (str): Top-level dir of the main checkout

    Returns:
        tuple:
            str: Directory to run in
            bool: True if the worktree is added here, so is to be removed
                when the discussion ends
            str or None: Session to carry on from
    """
    session = rev and cser.db.review_get_session(rev.idnum)
    if not session:
        return git_dir, False, None
    svr = cser.db.ser_ver_get_by_id(rev.svid)
    ups = cser.db.series_get_info(svr.series_id)[2]
    branch = stored.review_branch(svr.link, ups, git_dir)
    if not branch:
        return git_dir, False, None
    path, added = stored.open_review_worktree(git_dir, branch)
    if not path:
        return git_dir, False, None
    if not os.path.exists(_session_file(path, session)):
        tout.notice('The session which wrote the review has gone, so a new '
                    'one is used')
        session = None
    return path, added, session


def _store_cmd(args, reply_id, agreed_path, git_dir):
    """Get the command which stores the agreed response

    The response is drafted in Gmail too, replacing any earlier draft, so
    that it is ready to send.

    Args:
        args (Namespace): Command-line arguments (uses review_db_dir)
        reply_id (int): ID of the reply
        agreed_path (str): File holding the agreed response
        git_dir (str): Top-level dir of the main checkout, where patman must
            run to find its database

    Returns:
        str: Shell command
    """
    main = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), '__main__.py')
    cmd = [sys.executable, main, 'review', '--set-response', str(reply_id),
           '--file', agreed_path, '-d']
    if getattr(args, 'review_db_dir', None):
        cmd += ['--review-db-dir', os.path.abspath(
            os.path.expanduser(args.review_db_dir))]
    return f'cd {shlex.quote(git_dir)} && {shlex.join(cmd)}'


def _write(path, text):
    """Write a file of the discussion's context

    Args:
        path (str): Path of the file
        text (str): Contents

    Returns:
        str: The path
    """
    with open(path, 'w', encoding='utf-8') as outf:
        outf.write(text)
    return path


def _prompt(args, cser, reply, state):
    """Write out the context for a discussion and give its first message

    Args:
        args (Namespace): Command-line arguments
        cser (Cseries): Open cseries instance holding the replies
        reply (Reply): Reply to discuss
        state (dict): State of the discussion, with 'dir', the directory to
            write the context in, 'git_dir', the top-level dir of the main
            checkout, and 'session', the session it carries on from, if any

    Returns:
        str: First message for the session
    """
    sdir = state['dir']
    db = cser.db
    rev = db.review_get(reply.review_id) if reply.review_id else None
    svid = rev.svid if rev else reply.svid
    seq = rev.seq if rev else reply.seq
    svr = db.ser_ver_get_by_id(svid)
    desc = svr.desc or db.series_get_info(svr.series_id)[1]
    name, _ = common.parse_reviewer(args)
    msgid = lore.bare_msgid(reply.msgid or '')
    agreed = os.path.join(sdir, 'agreed.txt')
    return PROMPT.format(
        reviewer=name.split()[0], desc=desc,
        carry_on=(' This carries on from the session in which you wrote the '
                  'review.' if state['session'] else ''),
        reply_from=reply.sender,
        what='the cover letter' if not seq else f'patch {seq}',
        review_path=_write(os.path.join(sdir, 'review.txt'),
                           rev.body if rev else '(none)'),
        reply_path=_write(os.path.join(sdir, 'reply.txt'), reply.body or ''),
        response_path=_write(os.path.join(sdir, 'response.txt'),
                             reply.response or '(none yet)'),
        thread_url=f'{lore.LORE_URL}/{msgid}/' if msgid else '(not known)',
        agreed_path=agreed,
        spelling=getattr(args, 'spelling', 'British'),
        store_cmd=_store_cmd(args, reply.idnum, agreed, state['git_dir']))


def _wait_for_url(name):
    """Wait for the session to show its Remote Control link

    Args:
        name (str): Name of the tmux session

    Returns:
        tuple:
            str or None: Link, or None if it is not shown in time
            bool: True if Claude Code is asking whether to trust the
                directory, so the session cannot go on
    """
    for _ in range(URL_WAIT_S):
        out = _tmux('capture-pane', '-p', '-J', '-t', f'={name}:').stdout or ''
        match = RE_URL.search(out)
        if match:
            return match.group(0), False
        if RE_TRUST.search(out):
            return None, True
        if name not in _running():
            return None, False
        time.sleep(1)
    return None, False


def _launch(name, state, prompt):
    """Start the session in tmux and wait for its link

    Args:
        name (str): Name of the tmux session
        state (dict): State of the discussion, whose 'url' is set
        prompt (str): First message for the session

    Returns:
        str or None: Error message, or None if the session started
    """
    cmd = ['claude', f"--remote-control={state['title']}"]
    if state['session']:
        cmd += ['--resume', state['session'], '--fork-session']
    res = _tmux('new-session', '-d', '-s', name, '-x', '200', '-y', '50',
                '-c', state['cwd'], '--', *cmd, prompt)
    if res.returncode:
        return f'Cannot start the session: {res.stderr.strip()}'
    state['url'], untrusted = _wait_for_url(name)
    if untrusted:
        _tmux('kill-session', '-t', f'={name}')
        return (f"Claude Code does not trust {state['cwd']} yet: run 'claude' "
                'there once and say that you trust it')
    if name not in _running():
        return 'The session stopped as it started'
    return None


def start(args, cser, reply_id):
    """Start a discussion of a reply, or say where the current one is

    Args:
        args (Namespace): Command-line arguments
        cser (Cseries): Open cseries instance holding the replies
        reply_id (int): ID of the reply

    Returns:
        int: 0 on success, 1 if the session could not be started

    Raises:
        ValueError: The response has been sent, or tmux is missing
    """
    reply = cser.db.reply_get(reply_id)
    if reply.status == database.REPLY_SENT:
        raise ValueError(f'The response to reply {reply_id} has been sent')
    if not shutil.which('tmux'):
        raise ValueError('Discussing a reply needs tmux')
    name = session_name(cser, reply_id)
    found = discussions(cser).get(reply_id)
    if found:
        tout.notice(f"Reply {reply_id} is already being discussed in "
                    f"'{found['name']}': {found['url'] or 'no link yet'}")
        return 0

    git_dir = gitutil.get_top_level()
    rev = cser.db.review_get(reply.review_id) if reply.review_id else None
    cwd, added, session = _where(cser, rev, git_dir)
    state = {'title': _title(reply), 'dir': state_dir(name), 'cwd': cwd,
             'git_dir': git_dir, 'added': added, 'session': session,
             'url': None}
    os.makedirs(state['dir'], exist_ok=True)
    _write_state(name, state)
    error = _launch(name, state, _prompt(args, cser, reply, state))
    if error:
        _clean_up(name, state)
        tout.error(error)
        return 1
    _write_state(name, state)
    tout.notice(f"Reply {reply_id}: discussing it in '{state['title']}'" +
                (f": {state['url']}" if state['url'] else
                 ', which has not given its link yet'))
    return 0


def end(cser, reply_id):
    """End the discussion of a reply

    Args:
        cser (Cseries): Open cseries instance holding the replies
        reply_id (int): ID of the reply

    Returns:
        int: 0
    """
    name = session_name(cser, reply_id)
    state = _read_state(name)
    if name in _running():
        _tmux('kill-session', '-t', f'={name}')
    if state or os.path.isdir(state_dir(name)):
        _clean_up(name, state)
        tout.notice(f'Reply {reply_id}: ended the discussion')
    else:
        tout.notice(f'Reply {reply_id} is not being discussed')
    return 0


def set_response(cser, reply_id, fname):
    """Store the response to a reply, as given

    Args:
        cser (Cseries): Open cseries instance holding the replies
        reply_id (int): ID of the reply
        fname (str): File holding the response, or '-' for stdin

    Raises:
        ValueError: The response is empty or has been sent
    """
    if fname == '-':
        text = sys.stdin.read()
    else:
        with open(fname, encoding='utf-8') as inf:
            text = inf.read()
    text = text.strip('\n').rstrip()
    if not text.strip():
        raise ValueError('The response is empty')
    reply = cser.db.reply_get(reply_id)
    if reply.status == database.REPLY_SENT:
        raise ValueError(f'The response to reply {reply_id} has been sent')
    cser.db.reply_set_response(reply_id, text, database.REPLY_SUGGESTED)
    cser.commit()
    tout.notice(f'Reply {reply_id}: stored the response')
