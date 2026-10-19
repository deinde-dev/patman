# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2025 Canonical Ltd.
# Written by Simon Glass <simon.glass@canonical.com>
#

# pylint: disable=R0914

"""Replies to reviews

When the author replies to a review, patman can list the replies, have an
agent suggest a response, and draft or send that response as a threaded
reply. Syncing finds new replies, as well as reviews which have been sent.
"""

from datetime import datetime, timedelta
from email.utils import (formataddr, getaddresses, parseaddr,
                         parsedate_to_datetime)
import os
import tempfile
from types import SimpleNamespace

from u_boot_pylib import claude as claude_mod
from u_boot_pylib import gitutil
from u_boot_pylib import tools
from u_boot_pylib import tout

from patman import database
from patman import gmail
from patman.review import common
from patman.review import drafts
from patman.review import emails
from patman.review import prompts


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
    ctx.em_dash = getattr(args, 'em_dash', '-')
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


def do_reply_cmd(args, cser):
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


def do_sync(args, cser):
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


async def handle_reply(ctx, review_body, reply_from, reply_body):
    """Generate a response to a reply on our review

    Args:
        ctx (ReviewContext): Review context (uses reviewer_name,
            reviewer_email, repo_path, signoff, spelling, em_dash)
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
    return database.REPLY_SUGGESTED, emails.fix_dashes(
        emails.format_review_email(reply_ctx, greeting, verdict, comments),
        ctx.em_dash)


def handle_reply_sync(ctx, review_body, reply_from, reply_body):
    """Synchronous wrapper for handle_reply()"""
    return common.run(handle_reply(ctx, review_body, reply_from, reply_body))
