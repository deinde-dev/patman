# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2025 Canonical Ltd.
# Written by Simon Glass <simon.glass@canonical.com>
#

# pylint: disable=C0302,R0914

"""AI-powered patch review using Claude.

Fetches patches from patchwork, applies them to a local branch using
a Claude agent, and (in later stages) reviews each patch and creates
Gmail draft replies.
"""

from collections import namedtuple
from concurrent import futures
from datetime import datetime, timedelta
from email.utils import (formataddr, getaddresses, parseaddr,
                         parsedate_to_datetime)
import fcntl
import os
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import aiohttp

from u_boot_pylib import claude as claude_mod
from u_boot_pylib import gitutil
from u_boot_pylib import terminal
from u_boot_pylib import tools
from u_boot_pylib import tout

import patman
from patman import coverity
from patman import cser_helper
from patman import database
from patman import gmail
from patman import patchstream
from patman import workflow
from patman.review import apply
from patman.review import common
from patman.review import drafts
from patman.review import emails
from patman.review import lookup
from patman.review import prompts
from patman.review import voice


def _read_context(spec):
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


async def _run_cover_review(ctx, all_commits):
    """Run the review agent on the cover letter and parse its output

    Args:
        ctx (ReviewContext): Review context (uses cover_content etc.)
        all_commits (list): (seq, hash, subject) tuples

    Returns:
        tuple or None: (greeting, verdict, comments), or None if the agent
            failed or chose to skip
    """
    tout.notice('Reviewing series (cover letter)...')
    prompt = prompts.build_cover_review_prompt(
        ctx, all_commits, previous_review=ctx.previous_reviews.get(0))
    options = common.agent_options(
        allowed_tools=['Bash', 'Read', 'Grep'], cwd=ctx.repo_path,
        max_buffer_size=claude_mod.MAX_BUFFER_SIZE)
    success, log = await claude_mod.run_agent_collect(prompt, options)
    if not success or not log.strip():
        return None
    greeting, verdict, comments = emails.parse_review_output(log)
    if verdict == 'skip':
        return None
    return greeting, verdict, comments


async def _review_cover_letter(ctx, all_commits):
    """Review the cover letter / series as a whole

    Args:
        ctx (ReviewContext): Review context (uses cover_content etc.)
        all_commits (list): (seq, hash, subject) tuples

    Returns:
        str or None: Review body, or None if skipped
    """
    result = await _run_cover_review(ctx, all_commits)
    if result is None:
        return None
    greeting, verdict, comments = result
    return emails.format_review_email(ctx, greeting, verdict, comments)


async def _run_patch_review(ctx, cmt, seq, all_commits):
    """Run the review agent on one patch and parse its output

    Args:
        ctx (ReviewContext): Review context
        cmt: Commit object with hash, subject, msg, rtags
        seq (int): Patch sequence number (1-based)
        all_commits (list): (seq, hash, subject) tuples

    Returns:
        tuple or None: (greeting, verdict, comments, commit_msg), or None
            if the agent failed
    """
    body = cmt.msg.strip()
    if body.startswith(cmt.subject):
        commit_msg = body
    else:
        commit_msg = (cmt.subject + '\n' + body).strip()
    ctx.diffstat = gitutil.diff_stat(f'{cmt.hash}~..{cmt.hash}',
                                     ctx.repo_path).strip()

    previous_review = ctx.previous_reviews.get(seq)
    prompt = prompts.build_review_prompt(ctx, cmt.hash, seq, all_commits,
                                         previous_review)
    options = common.agent_options(allowed_tools=['Bash', 'Read', 'Grep'],
        cwd=ctx.repo_path, max_buffer_size=claude_mod.MAX_BUFFER_SIZE)
    success, log = await claude_mod.run_agent_collect(prompt, options)
    if not success or not log.strip():
        return None
    greeting, verdict, comments = emails.parse_review_output(log)
    return greeting, verdict, comments, commit_msg


async def _review_single_patch(ctx, cmt, seq, all_commits):
    """Review a single patch

    Args:
        ctx (ReviewContext): Review context
        cmt: Commit object with hash, subject, msg, rtags
        seq (int): Patch sequence number (1-based)
        all_commits (list): (seq, hash, subject) tuples

    Returns:
        str or None: Review body text, or None if there is nothing to say
    """
    result = await _run_patch_review(ctx, cmt, seq, all_commits)
    if result is None:
        return '(Review failed — please review manually)'
    greeting, verdict, comments, commit_msg = result
    # A non-approval with no comments has nothing to say (just a greeting
    # and the quoted commit message); drop it rather than sending an empty
    # review
    if verdict != 'approved' and not comments:
        return None
    return emails.format_review_email(
        ctx, greeting, verdict, comments, commit_msg)


async def review_patches(ctx):
    """Run AI review on each patch in the applied branch

    Args:
        ctx (ReviewContext): Review context (uses branch_name,
            upstream_branch, patch_count, cover_content,
            previous_reviews, repo_path, patch_selection, etc.)

    Returns:
        dict: Map of patch index (1-based) to review body
    """
    if not claude_mod.check_available():
        return {}

    commit_range = f'{ctx.upstream_branch}..{ctx.branch_name}'
    # The branch is a shared ref, so use the main repo's .git directory
    # — the worktree's .git is a pointer file, not a real git_dir
    git_dir = os.path.join(ctx.main_repo, '.git')
    series = patchstream.get_metadata_for_list(commit_range, git_dir=git_dir)
    all_commits = [(i + 1, cmt.hash, cmt.subject)
                   for i, cmt in enumerate(series.commits)]
    commits = [c[1] for c in all_commits]

    if len(commits) != ctx.patch_count:
        tout.warning(f'Expected {ctx.patch_count} patches but found '
                     f'{len(commits)} commits on {ctx.branch_name}')

    review_bodies = {}

    patch_sel = getattr(ctx, 'patch_selection', None)

    if ctx.cover_content and ctx.patch_count > 1 and not patch_sel:
        body = await _review_cover_letter(ctx, all_commits)
        if body:
            review_bodies[0] = body
    # Check which patches already have stored reviews
    existing_reviews = set()
    if hasattr(ctx, 'svid') and ctx.svid:
        for rev in ctx.cser.db.review_get_for_version(ctx.svid):
            existing_reviews.add(rev.seq)

    # Map each commit to its patchwork patch number by subject. The
    # applied branch may hold fewer commits than the series has patches
    # (e.g. one failed to apply), so a positional index would attach a
    # review to the wrong patchwork patch
    patches = ctx.series_data.get('patches', [])
    seq_by_subject = {}
    for i, patch in enumerate(patches):
        subject = lookup.clean_series_name(patch.get('name', ''))
        seq_by_subject.setdefault(subject, i + 1)

    total = ctx.patch_count or len(commits)
    reviewer_tag = ctx.reviewer_tag
    for i, cmt in enumerate(series.commits):
        seq = seq_by_subject.get(cmt.subject)
        if seq is None:
            tout.warning(f"Commit '{cmt.subject}' matches no patchwork "
                         'patch; attaching by position')
            seq = i + 1

        if patch_sel and seq not in patch_sel:
            continue

        if seq in existing_reviews:
            tout.notice(f'Skipping patch {seq}/{total}'
                        ' (already in database)')
            continue

        if (reviewer_tag in cmt.rtags.get('Reviewed-by', set()) or
                reviewer_tag in cmt.rtags.get('Tested-by', set())):
            tout.notice(f'Skipping patch {seq}/{total}'
                        ' (already reviewed)')
            continue

        tout.notice(f'Reviewing patch {seq}/{total}...')

        body = await _review_single_patch(ctx, cmt, seq, all_commits)
        if body:
            review_bodies[seq] = body
        else:
            tout.notice(f'  Nothing to say on patch {seq}; skipping')

    return review_bodies


def review_patches_sync(ctx):
    """Synchronous wrapper for review_patches()

    Returns:
        dict: Map of patch index (1-based) to review body text
    """
    return common.run(review_patches(ctx))


def _run_patch_review_sync(ctx, cmt, seq, all_commits):
    """Synchronous wrapper for _run_patch_review()"""
    return common.run(_run_patch_review(ctx, cmt, seq, all_commits))


def _run_cover_review_sync(ctx, all_commits):
    """Synchronous wrapper for _run_cover_review()"""
    return common.run(_run_cover_review(ctx, all_commits))


def _store_finding(ctx, seq, subject, verdict, comments, timestamp):
    """Store and show the findings of a quick review of one part of a series

    Commit-message comments are put before code comments.

    Args:
        ctx (ReviewContext): Review context (uses cser, svid, patch_count)
        seq (int): Patch number, or 0 for the cover letter
        subject (str): Subject of the patch, or 'cover letter'
        verdict (str): Verdict from the agent, e.g. 'approved'
        comments (list of tuple): (quote, comment) for each comment
        timestamp (str): Time of the review

    Returns:
        int: Number of comments
    """
    comments = sorted(comments, key=lambda hc: emails.is_code_comment(hc[0]))
    body = emails.cleanup_review_text(emails.format_findings(verdict,
                                                             comments))
    ctx.cser.db.review_add(ctx.svid, seq, body, verdict == 'approved',
                           timestamp)
    emails.print_finding(seq, ctx.patch_count, subject, verdict, comments)
    return len(comments)


def review_series(cser, series_id, svid, version, branch, series,
                  spelling='British', context=None):
    """AI-review a local series' commits and store the reviews

    Reviews each commit on the branch in place (no worktree, since the
    branch is already checked out) and stores the findings in the
    database, keyed by the ser_ver id, so they can be viewed later with
    'patman series info -r'. Reviews from the previous version, if any,
    are provided as context.

    Args:
        cser (Cseries): Open cseries instance
        series_id (int): Series ID
        svid (int): ser_ver id to attach the reviews to
        version (int): Series version number
        branch (str): Branch holding the series
        series (Series): Series object with a .commits list
        spelling (str): Spelling convention for comments
        context (str or None): Extra context for the review agent
    """
    if not claude_mod.check_available():
        raise ValueError(
            "The review agent is not available; install the 'review' extra "
            '(pip install patch-manager[review])')

    commits = series.commits
    if not commits:
        tout.notice('No commits to review')
        return

    repo = gitutil.get_top_level()
    ctx = common.ReviewContext(None, cser, {})
    ctx.main_repo = repo
    ctx.repo_path = repo
    ctx.branch_name = branch
    ctx.upstream_branch = f'{branch}~{len(commits)}'
    ctx.patch_count = len(commits)
    ctx.series_id = series_id
    ctx.svid = svid
    ctx.version = version
    ctx.spelling = spelling
    ctx.context = _read_context(context) if context else None
    if series.cover:
        ctx.cover_content = '\n'.join(series.cover)
    for rev in cser.db.review_get_previous(series_id, version):
        ctx.previous_reviews[rev.seq] = rev.body

    all_commits = [(i + 1, cmt.hash, cmt.subject)
                   for i, cmt in enumerate(commits)]
    timestamp = datetime.now().isoformat()
    total = len(commits)
    n_comments = 0

    if ctx.cover_content and total > 1:
        result = _run_cover_review_sync(ctx, all_commits)
        if result is not None:
            _, verdict, comments = result
            n_comments += _store_finding(ctx, 0, 'cover letter', verdict,
                                         comments, timestamp)

    for i, cmt in enumerate(commits):
        seq = i + 1
        tout.notice(f'Reviewing patch {seq}/{total}...')
        result = _run_patch_review_sync(ctx, cmt, seq, all_commits)
        if result is None:
            tout.warning(f'  Review failed for patch {seq}')
            continue
        _, verdict, comments, _ = result
        n_comments += _store_finding(ctx, seq, cmt.subject, verdict,
                                     comments, timestamp)
    cser.commit()

    tout.notice('')
    tout.notice(f"Stored review of '{branch}' v{version}: {total} patch(es), "
                f"{n_comments} comment(s). View with 'patman series info -r'")


def _show_reviews(reviews, series_data):
    """Display review bodies

    Args:
        reviews: Either an iterable of Review records (with seq, body,
            approved attributes) or a dict mapping seq to body text
        series_data (dict): Series data from patchwork
    """
    patches = series_data.get('patches', [])
    cover = series_data.get('cover_letter')

    if isinstance(reviews, dict):
        items = [(seq, body, 'Reviewed-by:' in body)
                 for seq, body in sorted(reviews.items())]
    else:
        items = [(rev.seq, rev.body, rev.approved)
                 for rev in reviews]

    for seq, body, approved in items:
        if seq == 0:
            label = 'Cover letter'
            name = cover.get('name', '') if cover else 'Cover'
        elif seq <= len(patches):
            label = f'Patch {seq}/{len(patches)}'
            name = patches[seq - 1].get('name', '')
        else:
            label = f'Patch {seq}'
            name = ''
        colour = (terminal.Color.GREEN if approved else
                  terminal.Color.YELLOW)
        terminal.tprint(f'\n--- {label}: {name} ---', colour=colour)
        print('---email start---')
        print(body)
        print('---email end---')
        print()


# Only ask the agent to suggest a response to replies sent in this many days
SUGGEST_DAYS = 14


def _is_recent(date, days):
    """Check whether an email's Date header is within a number of days

    Args:
        date (str): Date header, e.g. 'Thu, 15 Oct 2026 10:00:00 +0000'
        days (int): Number of days

    Returns:
        bool: True if it is recent, False if not or if it cannot be read
    """
    try:
        when = parsedate_to_datetime(date)
    except (TypeError, ValueError):
        return False
    if not when.tzinfo:
        when = when.astimezone()
    return datetime.now().astimezone() - when <= timedelta(days=days)


def _handle_replies(service, ctx):
    """Record replies to our sent reviews and suggest responses

    Each reply is recorded once. A reply which we have already answered is
    recorded with our answer. The agent then suggests a response to each
    other new reply sent within SUGGEST_DAYS, or decides that none is
    needed. Older replies are left for the user, so that a backlog of
    replies does not cause a lot of work for the agent.

    Args:
        service: Gmail API service
        ctx (ReviewContext): Review context

    Return:
        int: Number of new replies
    """
    db = ctx.cser.db
    cutoff = (datetime.now() - timedelta(days=drafts.REPLY_DAYS)).isoformat()
    reviews = [rev for status in (database.REVIEW_SENT,
                                  database.REVIEW_REPLIED)
               for rev in db.review_get_by_status(status, need_thread=True)
               if (rev.timestamp or '') >= cutoff]

    new = []
    for rev in reviews:
        for reply in gmail.fetch_thread_replies(
                service, rev.gmail_thread_id, rev.gmail_msg_id):
            reply_id = db.reply_add(
                rev.idnum, reply['msg_id'], reply['message_id'],
                reply['references'], reply['from'], reply['to'],
                reply['cc'], reply['subject'], reply['date'], reply['body'],
                datetime.now().isoformat())
            if reply_id is None:
                continue
            db.review_set_replied(rev.idnum)
            if reply['answer']:
                db.reply_set_response(reply_id, reply['answer'],
                                      database.REPLY_SENT)
                tout.notice(f"Review {rev.idnum}: reply {reply_id} from "
                            f"{reply['from']}, already answered")
                continue
            if _is_recent(reply['date'], SUGGEST_DAYS):
                new.append(reply_id)
            tout.notice(f"Review {rev.idnum}: reply {reply_id} from "
                        f"{reply['from']}")
    ctx.cser.commit()

    for reply_id in new:
        _suggest_response(ctx, reply_id)
    if new:
        tout.notice(f'{len(new)} new reply(ies)')
    return len(new)


def _suggest_response(ctx, reply_id):
    """Ask the agent to suggest a response to a reply, and record it

    Args:
        ctx (ReviewContext): Review context
        reply_id (int): ID of the reply
    """
    db = ctx.cser.db
    reply = db.reply_get(reply_id)
    rev = db.review_get(reply.review_id)
    status, response = handle_reply_sync(ctx, rev.body, reply.sender,
                                         reply.body)
    if not status:
        tout.warning(f'Reply {reply_id}: no response could be suggested')
        return
    db.reply_set_response(reply_id, response, status)
    ctx.cser.commit()
    if response:
        tout.notice(f'Reply {reply_id}: response suggested')
        print(response)
    else:
        tout.notice(f'Reply {reply_id}: no response needed')


def _reply_cc(reply, own):
    """Work out who to copy on a response to a reply

    Everyone the reply went to is copied, except ourselves and the person
    who sent it, who receives the response directly.

    Args:
        reply (Reply): Reply being responded to
        own (set of str): Our own email addresses, in lower case

    Return:
        str: Cc header, or '' if nobody
    """
    skip = set(own) | {parseaddr(reply.sender)[1].lower()}
    out = []
    for name, addr in getaddresses([reply.to_addrs or '',
                                    reply.cc_addrs or '']):
        if addr and addr.lower() not in skip:
            skip.add(addr.lower())
            out.append(formataddr((name, addr)))
    return ', '.join(out)


def _draft_response(service, ctx, reply_id, account=None):
    """Create a Gmail draft of our response to a reply

    The draft is threaded as a reply to it. Any earlier draft of the
    response is deleted.

    Args:
        service: Gmail API service
        ctx (ReviewContext): Review context
        reply_id (int): ID of the reply
        account (str or None): Gmail account in use

    Raises:
        ValueError: There is no response, or it has already been sent
    """
    db = ctx.cser.db
    reply = db.reply_get(reply_id)
    if reply.status == database.REPLY_SENT:
        raise ValueError(f'The response to reply {reply_id} has been sent')
    if not (reply.response or '').strip():
        raise ValueError(f'There is no response to reply {reply_id}')
    if reply.draft_id:
        gmail.delete_draft(service, reply.draft_id)

    subject = reply.subject or ''
    if not subject.lower().startswith('re:'):
        subject = f'Re: {subject}'
    refs = ' '.join(item for item in (reply.refs, reply.msgid) if item)
    own = {addr.lower() for addr in (ctx.reviewer_email, account) if addr}
    sender = None
    if ctx.reviewer_name and ctx.reviewer_email:
        sender = formataddr((ctx.reviewer_name, ctx.reviewer_email))
    draft = gmail.create_draft(
        service, reply.sender, subject, reply.response,
        in_reply_to=reply.msgid or None, references=refs or None,
        cc=_reply_cc(reply, own) or None, sender=sender)
    db.reply_set_status(reply_id, database.REPLY_DRAFT, draft['id'])
    ctx.cser.commit()
    tout.notice(f'Reply {reply_id}: created Gmail draft of the response')


def _send_response(service, ctx, reply_id, account=None):
    """Send our response to a reply

    If the response has no Gmail draft, or has been changed since its draft
    was created, a new draft is created first. The draft is read just before
    it is sent, since it may have been changed in Gmail, and the text which
    is sent is recorded.

    Args:
        service: Gmail API service
        ctx (ReviewContext): Review context
        reply_id (int): ID of the reply
        account (str or None): Gmail account in use

    Raises:
        ValueError: There is no response, it has already been sent or its
            draft has been deleted in Gmail
    """
    db = ctx.cser.db
    reply = db.reply_get(reply_id)
    if reply.status != database.REPLY_DRAFT or not reply.draft_id:
        _draft_response(service, ctx, reply_id, account)
        reply = db.reply_get(reply_id)
    result = gmail.send_draft(service, reply.draft_id)
    if not result:
        db.reply_set_status(reply_id, database.REPLY_SUGGESTED)
        ctx.cser.commit()
        raise ValueError(f'The draft of the response to reply {reply_id} '
                         'has been deleted, so it is not sent')
    body = result[0]
    db.reply_set_response(reply_id, body or reply.response,
                          database.REPLY_SENT)
    ctx.cser.commit()
    tout.notice(f'Reply {reply_id}: sent the response')


def _sync_reply_drafts(service, cser):
    """Check whether drafts of our responses have been sent or deleted

    Args:
        service: Gmail API service
        cser (Cseries): Open cseries instance
    """
    pending = cser.db.reply_get_by_status([database.REPLY_DRAFT])
    if not pending:
        return
    sent, deleted, _ = gmail.sync_drafts(service, [
        SimpleNamespace(idnum=rep.idnum, draft_id=rep.draft_id,
                        body=rep.response, thread_id=None,
                        subject=rep.subject, after=None, since=None,
                        check_sent=False)
        for rep in pending])
    for reply_id, (body, _, _) in sent.items():
        cser.db.reply_set_response(reply_id, body, database.REPLY_SENT)
        tout.notice(f'Reply {reply_id}: response sent')
    for reply_id in deleted:
        cser.db.reply_set_status(reply_id, database.REPLY_SUGGESTED)
        tout.notice(f'Reply {reply_id}: draft deleted (not sent)')
    cser.commit()


def _make_reply_ctx(args, cser):
    """Set up a review context for handling replies

    Args:
        args (Namespace): Command-line arguments
        cser (Cseries): Open cseries instance

    Return:
        ReviewContext: Context
    """
    ctx = common.ReviewContext(None, cser, {})
    ctx.reviewer_name, ctx.reviewer_email = common.parse_reviewer(args)
    ctx.repo_path = gitutil.get_top_level()
    ctx.signoff = getattr(args, 'signoff', '') or None
    if ctx.signoff:
        ctx.signoff = ctx.signoff.replace('\\n', '\n')
    ctx.spelling = getattr(args, 'spelling', 'British')
    return ctx


def _list_replies(cser):
    """Show the replies which still need a response

    Args:
        cser (Cseries): Open cseries instance

    Return:
        int: 0
    """
    replies = cser.db.reply_get_by_status(list(database.REPLY_WAITING))
    if not replies:
        tout.notice('No replies need a response')
        return 0
    for reply in replies:
        state = ('response suggested' if reply.status ==
                 database.REPLY_SUGGESTED else 'no response yet')
        print(f'{reply.idnum:5}  {reply.sender}: {reply.subject} ({state})')
    return 0


def _do_reply_cmd(args, cser):
    """Handle --replies, --suggest-reply, --draft-reply and --send-reply

    Args:
        args (Namespace): Command-line arguments
        cser (Cseries): Open cseries instance

    Return:
        int: 0 on success, 1 on failure
    """
    if args.list_replies:
        return _list_replies(cser)
    ctx = _make_reply_ctx(args, cser)
    if args.suggest_reply:
        _suggest_response(ctx, args.suggest_reply)
    if args.draft_reply or args.send_reply:
        if not gmail.check_available():
            return 1
        account = getattr(args, 'gmail_account', None)
        service = gmail.get_service(account)
        if args.draft_reply:
            _draft_response(service, ctx, args.draft_reply, account)
        if args.send_reply:
            _send_response(service, ctx, args.send_reply, account)
    return 0


def _do_sync(args, cser):
    """Sync sent drafts and handle replies

    Args:
        args (Namespace): Command-line arguments
        cser (Cseries): Open cseries instance

    Returns:
        int: 0 on success, 1 on failure
    """
    if not gmail.check_available():
        return 1
    account = getattr(args, 'gmail_account', None)
    service = gmail.get_service(account)

    drafts.sync_drafts(service, cser)
    _sync_reply_drafts(service, cser)
    _handle_replies(service, _make_reply_ctx(args, cser))
    return 0


def _register_series(cser, clean_name, version, link, series_data,
                     upstream=None):
    """Register a series in the database for review

    Creates or finds the series record, adds a ser_ver entry and pcommit
    records for each patch.

    Args:
        cser (Cseries): Open cseries instance
        clean_name (str): Cleaned series name
        version (int): Series version number
        link (str): Patchwork series link/ID
        series_data (dict): Series data from patchwork
        upstream (str or None): Upstream name for branch naming

    Returns:
        tuple or None: (series_id, svid) or None if already reviewed
    """
    existing = cser.db.series_find_by_link(link)
    if existing:
        return None

    # Adding the series row, its version and its patches must be atomic.
    # These are all uncommitted until the commit() below, so if anything
    # in between raises we roll back; otherwise a freshly-added series row
    # would linger uncommitted and be flushed by the next unrelated
    # commit() as an orphan -- a series with no ser_ver row
    try:
        prev = cser.db.series_find_review_by_name(clean_name)
        if prev:
            series_id, db_name, prev_version = prev
            tout.notice(f"Previously reviewed '{db_name}' v{prev_version};"
                        f" adding v{version}")
        else:
            branch_name = lookup.make_review_name(link, upstream)
            series_id = cser.db.series_find_by_name(
                branch_name, include_archived=True)
            if not series_id:
                # Store the cleaned title (stable across versions) as the
                # desc, not the raw '[vN,0/M] ...' cover-letter subject, so
                # later versions link via series_find_review_by_name()
                series_id = cser.db.series_add(branch_name, clean_name,
                                               ups=upstream)
            cser.db.series_set_source(series_id, 'review')

        svid = cser.db.ser_ver_add(series_id, version, link=str(link))
        cover = series_data.get('cover_letter')
        if cover and cover.get('id'):
            cser.db.ser_ver_set_info(database.SerVer(
                svid, None, None, None, cover['id'], 0, cover.get('name'),
                None, None, None))

        patches = series_data.get('patches', [])
        pcommits = []
        for i, patch in enumerate(patches):
            pcommits.append(database.Pcommit(idnum=None, seq=i,
                subject=patch.get('name', ''), svid=svid, change_id=None,
                state=None, patch_id=patch.get('id'), num_comments=0))
        if pcommits:
            cser.db.pcommit_add_list(svid, pcommits)

            # pcommit_add_list only stores seq/subject/change_id; update
            # patch_id from the patchwork data
            pclist = cser.db.pcommit_get_list(svid)
            for pcm, patch in zip(pclist, patches):
                patch_id = patch.get('id')
                if patch_id:
                    cser.db.pcommit_update(database.Pcommit(
                        idnum=pcm.idnum, seq=pcm.seq, subject=pcm.subject,
                        svid=svid, change_id=pcm.change_id, state=pcm.state,
                        patch_id=patch_id, num_comments=pcm.num_comments))

        cser.commit()
    except Exception:
        cser.rollback()
        raise
    tout.notice(f"Added series '{clean_name}' v{version} to database")
    return series_id, svid


def _do_stored(args, pwork, cser, link):
    """Handle --send and --revise, which act on stored reviews

    Args:
        args (Namespace): Command-line arguments
        pwork (Patchwork): Configured patchwork instance
        cser (Cseries): Open cseries instance
        link (str): Patchwork series link/ID

    Returns:
        int: 0 on success, 1 on failure
    """
    if args.send:
        return _do_send(args, cser, link)
    return _do_revise(args, pwork, cser, link)


def _do_send(args, cser, link):
    """Send the Gmail drafts of the reviews of a series

    The cover letter's review is sent first, then the patches' in order.
    With -i, only those patches' drafts are sent. Each review is recorded as
    sent, with the text which was sent, since the draft may have been
    changed in Gmail.

    Args:
        args (Namespace): Command-line arguments
        cser (Cseries): Open cseries instance
        link (str): Patchwork series link/ID

    Returns:
        int: 0 on success, 1 if any draft could not be sent
    """
    existing = cser.db.series_find_by_link(link)
    if not existing:
        raise ValueError(f'Series {link} has not been reviewed')
    _, name, version, svid = existing
    sel = common.parse_patch_selection(args.patches)
    reviews = sorted((rev for rev in cser.db.review_get_for_version(svid)
                      if rev.status == database.REVIEW_DRAFT and
                      rev.draft_id and (not sel or rev.seq in sel)),
                     key=lambda rev: rev.seq)
    if not reviews:
        raise ValueError(f"No Gmail drafts to send in '{name}' v{version}")
    if args.dry_run:
        for rev in reviews:
            tout.notice(f'Dry run: would send the review of '
                        f'{_review_label(rev.seq)}')
        return 0
    if not gmail.check_available():
        return 1

    service = gmail.get_service(getattr(args, 'gmail_account', None))
    failed = 0
    for rev in reviews:
        label = _review_label(rev.seq)
        try:
            result = gmail.send_draft(service, rev.draft_id)
        except Exception as exc:  # pylint: disable=W0718
            tout.error(f'Cannot send the review of {label}: {exc}')
            failed += 1
            continue
        if not result:
            tout.warning(f'The draft of the review of {label} has gone; '
                         'not sent')
            failed += 1
            continue
        body, msg_id, thread_id = result
        cser.db.review_set_sent(rev.idnum, body or rev.body, msg_id,
                                thread_id)
        cser.commit()
        tout.notice(f'Sent the review of {label}')
    tout.notice(f'Sent {len(reviews) - failed} of {len(reviews)} review(s)')
    return 1 if failed else 0


def _review_label(seq):
    """Describe which part of a series a review is for

    Args:
        seq (int): Patch number, 0 for the cover letter

    Returns:
        str: e.g. 'patch 2' or 'the cover letter'
    """
    return f'patch {seq}' if seq else 'the cover letter'


def _review_commits(args, cser, link, ups):
    """Get the commits holding the patches of a reviewed series

    The review branch is left in place after a review, so the patches can be
    read from it. It may be based on next or master (see series_base()), so
    the base is whichever of these leaves the fewest commits on the branch.

    Args:
        args (Namespace): Command-line arguments (for base_branch)
        cser (Cseries): Open cseries instance
        link (str): Patchwork series link/ID
        ups (str or None): Upstream name

    Returns:
        list of str: One line for each commit, 'hash subject', oldest first,
            or an empty list if the branch is not present
    """
    branch = lookup.make_review_name(link, ups)
    git_dir = gitutil.get_top_level()
    if not gitutil.ref_exists(branch, git_dir):
        return []
    bases = [apply.get_upstream_branch(args, cser)]
    if ups:
        bases += [f'{ups}/next', apply.main_branch(ups, git_dir)]
    counts = {}
    for base in bases:
        count = gitutil.count_revs(git_dir, f'{base}..{branch}')
        if count:
            counts[base] = count
    if not counts:
        return []
    base = min(counts, key=counts.get)
    out = subprocess.run(
        ['git', 'log', '--reverse', '--format=%H %s', f'{base}..{branch}'],
        capture_output=True, text=True, check=False, cwd=git_dir).stdout
    return out.splitlines()


async def revise_reviews(ctx, reviews, note, commits):
    """Ask the agent to change some reviews, following a note from the reviewer

    Args:
        ctx (ReviewContext): Review context (uses repo_path, reviewer_tag
            and spelling)
        reviews (dict): Map of seq to review body text
        note (str): What the reviewer wants changed
        commits (list of str): 'hash subject' for each patch, in order

    Returns:
        dict or None: Map of seq to new review body text, for each review
            which changed, or None if the agent is not available or failed
    """
    if not claude_mod.check_available():
        return None
    parts = []
    for seq in sorted(reviews):
        parts += [f'---SEQ {seq}---', reviews[seq]]
    if commits:
        lines = '\n'.join(f'Patch {seq}: {line}'
                          for seq, line in enumerate(commits, 1))
        patches = f'The patches are these commits:\n{lines}'
    else:
        patches = 'The patches are not available in the repository.'
    with tempfile.NamedTemporaryFile('w', prefix='patman_revise_',
                                     suffix='.txt', delete=False) as outf:
        outf.write('\n'.join(parts))
        reviews_path = outf.name
    try:
        prompt = prompts.REVISE_PROMPT.format(
            note=note, reviews_path=reviews_path, patches=patches,
            tag=ctx.reviewer_tag, spelling=ctx.spelling)
        options = common.agent_options(
            allowed_tools=['Bash', 'Read', 'Grep'], cwd=ctx.repo_path,
            max_buffer_size=claude_mod.MAX_BUFFER_SIZE)
        tout.notice('Revising reviews...')
        success, log = await claude_mod.run_agent_collect(prompt, options)
    finally:
        os.unlink(reviews_path)
    if not success:
        return None
    return {seq: body for seq, body in emails.parse_refined_output(log).items()
            if seq in reviews and body and body != reviews[seq].strip()}


def revise_reviews_sync(ctx, reviews, note, commits):
    """Synchronous wrapper for revise_reviews()"""
    return common.run(revise_reviews(ctx, reviews, note, commits))


def _do_revise(args, pwork, cser, link):
    """Change stored reviews of a series, following a note from the reviewer

    Only reviews which have not been sent are changed. With -i, only those
    patches' reviews are given to the agent. With -d, the Gmail drafts of the
    reviews which change are recreated.

    Args:
        args (Namespace): Command-line arguments
        pwork (Patchwork): Configured patchwork instance
        cser (Cseries): Open cseries instance
        link (str): Patchwork series link/ID

    Returns:
        int: 0 on success, 1 on failure
    """
    existing = cser.db.series_find_by_link(link)
    if not existing:
        raise ValueError(f'Series {link} has not been reviewed')
    _, name, version, svid = existing
    sel = common.parse_patch_selection(args.patches)
    reviews = [rev for rev in cser.db.review_get_for_version(svid)
               if rev.status not in (database.REVIEW_SENT,
                                     database.REVIEW_REPLIED) and
               (not sel or rev.seq in sel)]
    if not reviews:
        raise ValueError(f"No unsent reviews to revise in '{name}' "
                         f'v{version}')

    ctx = common.ReviewContext(pwork, cser, {})
    ctx.reviewer_name, ctx.reviewer_email = common.parse_reviewer(args)
    ctx.repo_path = gitutil.get_top_level()
    ctx.spelling = args.spelling
    ups = pwork.upstream if pwork else None
    commits = _review_commits(args, cser, link, ups)
    changed = revise_reviews_sync(
        ctx, {rev.seq: rev.body for rev in reviews}, args.revise, commits)
    if changed is None:
        tout.error('Could not revise the reviews')
        return 1
    if not changed:
        tout.notice('No reviews needed changing')
        return 0

    for rev in reviews:
        if rev.seq in changed:
            body = changed[rev.seq]
            cser.db.review_set_body(rev.idnum, body,
                                    'Reviewed-by:' in body)
            tout.notice(f'Revised the review of {_review_label(rev.seq)}')
            print(body)
            print()
    cser.commit()

    if args.create_drafts:
        redo = [cser.db.review_get(rev.idnum) for rev in reviews
                if rev.seq in changed]
        drafts.delete_gmail_drafts(args, redo)
        ctx.series_data = lookup.fetch_series(pwork, link)[0]
        drafts.create_drafts(ctx, args, {rev.seq: rev.body for rev in redo},
                             {rev.seq: rev.idnum for rev in redo})
    return 0


def _run_coverity(ctx, args):
    """Analyse the series with Coverity and summarise the new defects

    Builds and analyses the base branch and the patched branch, then
    returns a bullet-list summary of the defects the series introduces,
    for use as review context.

    Args:
        ctx (ReviewContext): Review context (uses main_repo, repo_path,
            upstream_branch)
        args (Namespace): Command-line arguments (coverity_defconfig)

    Returns:
        str or None: Summary of new defects, or None if Coverity is
            unavailable or finds nothing new
    """
    if not coverity.check_available():
        tout.warning('Coverity tools (cov-build/cov-analyze) not found on '
                     'PATH; skipping --coverity')
        return None

    defconfig = getattr(args, 'coverity_defconfig', None) or \
        coverity.DEFAULT_DEFCONFIG
    tout.notice(f'Running Coverity ({defconfig}) on the base and the '
                'series; this builds twice and may take a while...')
    with tempfile.TemporaryDirectory() as tmp:
        base_wt = os.path.join(tmp, 'base')
        # Check out the base detached in its own worktree so the build
        # does not disturb the review worktree
        subprocess.run(
            ['git', '-C', ctx.main_repo, 'worktree', 'add', '--detach',
             base_wt, ctx.upstream_branch],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True)
        try:
            base = coverity.analyze(base_wt, defconfig,
                                    os.path.join(tmp, 'cov-base'))
            patched = coverity.analyze(ctx.repo_path, defconfig,
                                       os.path.join(tmp, 'cov-patched'))
        finally:
            gitutil.remove_worktree(ctx.main_repo, base_wt)

    new = coverity.find_new_defects(base, patched)
    if not new:
        tout.notice('Coverity: no new defects introduced by the series')
        return None
    tout.notice(f'Coverity: {len(new)} new defect(s) introduced by the '
                'series')
    return coverity.format_defects(new)


def _run_and_store_reviews(ctx, args):
    """Run AI review, refine, store and display results

    Args:
        ctx (ReviewContext): Review context with all fields populated
        args (Namespace): Command-line arguments (for create_drafts flag)
    """
    prev_db = ctx.cser.db.review_get_previous(ctx.series_id, ctx.version)
    for rev in prev_db:
        ctx.previous_reviews[rev.seq] = rev.body

    if ctx.cover_content is None:
        ctx.cover_content = lookup.fetch_cover_content(
            ctx.pwork, ctx.series_data)

    review_bodies = review_patches_sync(ctx)

    if ctx.comments_path and os.path.exists(ctx.comments_path):
        os.unlink(ctx.comments_path)

    for seq in review_bodies:
        review_bodies[seq] = emails.cleanup_review_text(review_bodies[seq])
    review_bodies = emails.refine_reviews_sync(review_bodies, ctx.spelling)

    timestamp = datetime.now().isoformat()
    review_ids = {}
    for seq, body in review_bodies.items():
        approved = 'Reviewed-by:' in body
        review_ids[seq] = ctx.cser.db.review_add(ctx.svid, seq, body, approved,
                                                 timestamp)
    ctx.cser.commit()

    _show_reviews(review_bodies, ctx.series_data)

    if args.create_drafts:
        drafts.create_drafts(ctx, args, review_bodies, review_ids)
    else:
        tout.notice('Use --create-drafts to create Gmail review drafts.')


def _find_or_register(ctx, args, clean_name, link):
    """Register a series, handling existing reviews

    If the series was already reviewed, shows the stored reviews and
    optionally creates drafts. With --force, deletes old reviews and
    re-registers.

    Args:
        ctx (ReviewContext): Review context (uses pwork, cser, series_data,
            version)
        args (Namespace): Command-line arguments
        clean_name (str): Cleaned series name
        link (str): Patchwork series link/ID

    Returns:
        tuple or None: (series_id, svid) or None if already reviewed and
            not forcing
    """
    ups = ctx.pwork.upstream if ctx.pwork else None
    result = _register_series(ctx.cser, clean_name, ctx.version, link,
                              ctx.series_data, upstream=ups)
    if result is not None:
        return result

    existing = ctx.cser.db.series_find_by_link(link)
    if not existing:
        return None

    series_id, _, _, svid = existing
    reviews = ctx.cser.db.review_get_for_version(svid)

    if not reviews:
        # Interrupted previous attempt — resume with existing record
        tout.notice('Resuming incomplete review')
        return series_id, svid

    # When reviewing specific patches, allow adding to existing reviews
    patch_sel = common.parse_patch_selection(args.patches)
    if patch_sel:
        reviewed_seqs = {r.seq for r in reviews}
        new_seqs = patch_sel - reviewed_seqs
        if new_seqs:
            tout.notice(f'Adding review for patch(es) '
                        f'{", ".join(str(s) for s in sorted(new_seqs))}')
            return series_id, svid

    if not args.force:
        _, db_name, db_version, _ = existing
        tout.notice(f"Already reviewed: '{db_name}' v{db_version}")
        _show_reviews(reviews, ctx.series_data)
        if args.create_drafts or args.redraft:
            drafts.draft_stored_reviews(args, reviews, ctx.series_data,
                                        ctx.pwork, ctx.cser)
        return None

    if args.create_drafts:
        # The old drafts would otherwise be orphaned in Gmail when the
        # review records that track them are deleted below
        drafts.delete_gmail_drafts(args, reviews)
    ctx.cser.db.review_delete_for_version(svid)
    ctx.cser.commit()
    tout.notice('Re-reviewing (forced)')
    return series_id, svid


class ReviewInProgressError(Exception):
    """Raised when another review of the same series is already running"""


def _acquire_review_lock(repo, branch_name):
    """Take an exclusive lock for a series review

    The lock is a file locked with flock(), so the kernel releases it
    automatically when patman exits or crashes, leaving no stale locks.
    It stops two reviews of the same series running at once, whether from
    --scan, parallel workers or a separate patman invocation.

    Args:
        repo (str): Top-level dir of the main checkout
        branch_name (str): Review branch name, identifying the series

    Returns:
        int: Open file descriptor holding the lock; pass it to
            _release_review_lock() when done

    Raises:
        ReviewInProgressError: if the lock is already held
    """
    path = os.path.join(repo, '.git', 'patman', 'locks',
                        f'{branch_name}.lock')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise ReviewInProgressError(branch_name) from exc
    return fd


def _release_review_lock(fd):
    """Release a review lock taken with _acquire_review_lock()

    Args:
        fd (int): File descriptor returned by _acquire_review_lock()
    """
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def _review_link(args, pwork, cser, link):
    """Run the main review flow for a single patchwork series

    Fetches the series, registers it (detecting an already-reviewed series
    or a new version), applies the patches in a worktree and reviews them.
    The series is locked for the duration so a concurrent review of the
    same series is refused rather than corrupting its worktree or records.

    Args:
        args (Namespace): Command-line arguments
        pwork (Patchwork): Configured patchwork instance
        cser (Cseries): Open cseries instance
        link (str): Patchwork series link/ID

    Returns:
        int: 0 on success, 1 if applying the patches failed

    Raises:
        ValueError: if the series is incomplete (not all patches present)
    """
    ups = pwork.upstream if pwork else None
    branch_name = lookup.make_review_name(link, ups)
    main_repo = gitutil.get_top_level()
    try:
        lock_fd = _acquire_review_lock(main_repo, branch_name)
    except ReviewInProgressError:
        tout.warning(
            f"A review of '{branch_name}' is already in progress; skipping")
        return 0

    try:
        series_data, clean_name, version, patch_count = \
            lookup.fetch_series(pwork, link)

        if not getattr(args, 'any_state', False):
            states = lookup.get_patch_states(pwork, link)
            if not any(state in lookup.ACTIVE_STATES for state in states):
                shown = ', '.join(sorted({s for s in states if s})) or 'none'
                raise ValueError(
                    f"Series '{clean_name}' is not active on patchwork "
                    f'(patch states: {shown}); use --any-state to review it '
                    'anyway')

        ctx = common.ReviewContext(pwork, cser, series_data)
        ctx.version = version
        ctx.patch_count = patch_count

        result = _find_or_register(ctx, args, clean_name, link)
        if result is None:
            return 0
        ctx.series_id, ctx.svid = result

        ctx.upstream_branch = apply.get_upstream_branch(args, cser)
        ctx.main_repo = main_repo
        if not args.base_branch:
            ctx.upstream_branch = apply.series_base(
                ctx, args.upstream or cser.db.upstream_get_default(),
                ctx.upstream_branch) or ctx.upstream_branch
        ctx.branch_name = branch_name
        wt_path = cser_helper.review_worktree_path(ctx.main_repo,
                                                   ctx.branch_name)
        tout.notice(f'Using review worktree {wt_path}')
        ctx.repo_path = gitutil.ensure_worktree(
            ctx.main_repo, wt_path, ctx.branch_name, ctx.upstream_branch)

        apply_ok = apply.apply_and_check(ctx, link)
        if apply_ok is None:
            # Every patch is already upstream; nothing to review
            return 0
        if not apply_ok:
            return 1

        if args.apply_only:
            tout.notice('Apply-only mode; skipping review')
            return 0

        ctx.patch_selection = common.parse_patch_selection(args.patches)
        ctx.reviewer_name, ctx.reviewer_email = common.parse_reviewer(args)
        ctx.signoff = args.signoff or None
        if ctx.signoff:
            ctx.signoff = ctx.signoff.replace('\\n', '\n')
        ctx.spelling = args.spelling
        ctx.context = _read_context(args.context) if args.context else None
        ctx.comments_path = lookup.write_comments_file(series_data, pwork)

        if getattr(args, 'coverity', False):
            ctx.coverity_text = _run_coverity(ctx, args)

        _run_and_store_reviews(ctx, args)
        workflow.reviewed(cser, ctx.series_id, ctx.svid)
        gitutil.remove_worktree(ctx.main_repo, wt_path)

        return 0
    finally:
        _release_review_lock(lock_fd)


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
    """
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
    cmd = [sys.executable, '-m', 'patman']
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


def _child_env():
    """Build the environment for a 'python -m patman' child process

    The child re-invokes patman with 'sys.executable -m patman'. When
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
                          env=_child_env())
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


def _do_scan(args, pwork, cser):
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


def _do_relink(args, cser):
    """Merge review series that were split across versions

    Older patman stored each version of a series under its own record,
    keyed by the raw '[vN,0/M] ...' cover-letter subject, so the versions
    of one series did not link and follow-up reviews had no earlier
    feedback for context. Group the review series by their cleaned title,
    merge each group into a single series holding all the versions and
    clean the stored description. The database is backed up first.

    Args:
        args (Namespace): Command-line arguments (unused)
        cser (Cseries): Open cseries instance

    Returns:
        int: 0 on success
    """
    backup = f'{cser.db.db_path}.bak'
    shutil.copy2(cser.db.db_path, backup)
    tout.notice(f'Backed up database to {backup}')

    series = cser.db.series_get_dict(include_archived=True, reviews_only=True)
    groups = {}
    for ser in series.values():
        groups.setdefault(lookup.clean_series_name(ser.desc), []).append(ser)

    merged = 0
    cleaned = 0
    for clean, group in groups.items():
        group.sort(key=lambda ser: ser.idnum)
        canon = group[0]
        if canon.desc != clean:
            cser.db.series_set_desc(canon.idnum, clean)
            cleaned += 1
        for other in group[1:]:
            for svid in cser.db.ser_ver_get_svids(other.idnum):
                cser.db.ser_ver_set_series(svid, canon.idnum)
            cser.db.series_remove(other.idnum)
            merged += 1
    cser.commit()
    tout.notice(f'Relinked {merged} duplicate series record(s); cleaned '
                f'{cleaned} description(s)')
    return 0


# Model aliases accepted by --model, most to least capable. Each resolves
# to the latest model in its tier, so this list does not go stale as new
# generations ship. The Claude SDK and CLI expose no way to enumerate
# models, and the REST models API needs an API key that need not match the
# subscription a review authenticates with, so a fixed alias list is both
# the most reliable answer and exactly what --model expects
_MODEL_ALIASES = (
    ('opus', 'Most capable; best for a thorough review'),
    ('sonnet', 'Balanced capability and speed'),
    ('haiku', 'Fastest and cheapest; light reviews'),
)


def _list_models():
    """Print the model aliases that --model accepts

    Returns:
        int: 0
    """
    print("Models you can pass to --model (or set as the 'model' setting):")
    print()
    for alias, desc in _MODEL_ALIASES:
        print(f'  {alias:<8} {desc}')
    print()
    print('Each alias selects the latest model in its tier. A full model')
    print('id (e.g. claude-sonnet-5) also works. With none set, your global')
    print('Claude default is used.')
    return 0


def do_review(args, pwork, cser):
    """Run the review command

    Dispatches to learn-voice, sync, scan, relink, or the main review
    flow which fetches, applies, reviews and optionally drafts.

    Args:
        args (Namespace): Command-line arguments
        pwork (Patchwork): Configured patchwork instance
        cser (Cseries): Open cseries instance
    """
    if getattr(args, 'list_models', False):
        return _list_models()

    # Fix the model for the whole run, so every agent uses the one chosen
    # with --model / the 'model' setting rather than the user's default
    common.set_agent_model(getattr(args, 'model', None))

    if args.learn_voice:
        return voice.do_learn_voice(args, pwork)

    if args.sync:
        return _do_sync(args, cser)

    if (getattr(args, 'list_replies', False) or
            getattr(args, 'suggest_reply', None) or
            getattr(args, 'draft_reply', None) or
            getattr(args, 'send_reply', None)):
        return _do_reply_cmd(args, cser)

    if args.relink:
        return _do_relink(args, cser)

    if args.scan:
        return _do_scan(args, pwork, cser)

    has_patch = getattr(args, 'patch', None)
    has_patch_title = getattr(args, 'patch_title', None)
    if not args.pw_link and not args.title and not has_patch and \
            not has_patch_title:
        raise ValueError("Please provide -s <series>, -S <title>, "
            "-p <patch-id> or -P <patch-title>")

    # -p/-P locate the series via one of its patches. By default the review
    # is then restricted to just that patch; -w reviews the whole series
    whole = getattr(args, 'whole_series', False)
    link = args.pw_link
    if not link and has_patch:
        link, patch_seq = lookup.lookup_patch_series(pwork, args.patch)
        if not whole:
            args.patches = str(patch_seq)
    elif not link and has_patch_title:
        link, patch_seq = lookup.search_patch(pwork, args.patch_title)
        if not whole:
            args.patches = str(patch_seq)
    elif not link:
        link = lookup.search_series(
            pwork, args.title, getattr(args, 'version', None))

    if getattr(args, 'send', False) or getattr(args, 'revise', None):
        return _do_stored(args, pwork, cser, link)
    return _review_link(args, pwork, cser, link)


async def handle_reply(ctx, review_body, reply_from, reply_body):
    """Generate a response to a reply on our review

    Args:
        ctx (ReviewContext): Review context (uses reviewer_name,
            reviewer_email, repo_path, signoff, spelling)
        review_body (str): Our original review text
        reply_from (str): Who replied (name <email>)
        reply_body (str): The reply text

    Returns:
        tuple:
            str or None: database.REPLY_SUGGESTED if there is a response,
                database.REPLY_SKIP if no response is needed, or None if the
                agent is not available or failed
            str or None: Response email body, if there is one
    """

    if not claude_mod.check_available():
        return None, None

    # Write our review and the reply to temp files
    review_path = os.path.join(tempfile.gettempdir(),
                               'patman_reply_our_review.txt')
    reply_path = os.path.join(tempfile.gettempdir(),
                              'patman_reply_their_reply.txt')
    tools.write_file(review_path, review_body, binary=False)
    tools.write_file(reply_path, reply_body, binary=False)

    prompt = prompts.REPLY_PROMPT.format(review_path=review_path,
        reply_path=reply_path, reply_from=reply_from,
        spelling=ctx.spelling)
    options = common.agent_options(allowed_tools=['Bash', 'Read', 'Grep'],
        cwd=ctx.repo_path, max_buffer_size=claude_mod.MAX_BUFFER_SIZE)

    success, log = await claude_mod.run_agent_collect(prompt, options)

    # Clean up
    for path in (review_path, reply_path):
        if os.path.exists(path):
            os.unlink(path)

    if not success or not log.strip():
        return None, None

    greeting, verdict, comments = emails.parse_review_output(log)
    if verdict == 'skip':
        return database.REPLY_SKIP, None

    if '<' in reply_from:
        reply_name = reply_from.split('<')[0].strip()
        reply_email = reply_from.split('<')[1].rstrip('>')
    else:
        reply_name = reply_from
        reply_email = ''

    # Build a context for the reply — the "author" here is the person
    # we are replying to, not the original series submitter
    reply_ctx = common.ReviewContext(ctx.pwork, ctx.cser,
        {'submitter': {'name': reply_name, 'email': reply_email}, 'date': ''})
    reply_ctx.reviewer_name = ctx.reviewer_name
    reply_ctx.reviewer_email = ctx.reviewer_email
    reply_ctx.signoff = ctx.signoff
    return database.REPLY_SUGGESTED, emails.format_review_email(
        reply_ctx, greeting, verdict, comments)


def handle_reply_sync(ctx, review_body, reply_from, reply_body):
    """Synchronous wrapper for handle_reply()"""
    return common.run(handle_reply(ctx, review_body, reply_from, reply_body))
