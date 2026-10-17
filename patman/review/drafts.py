# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2025 Canonical Ltd.
# Written by Simon Glass <simon.glass@canonical.com>
#

# pylint: disable=R0914

"""Gmail drafts of reviews, and syncing them with what was sent

Each review can be turned into a Gmail draft which replies to the patch.
Syncing checks which drafts have since been sent, records them and uses
any edits made before sending to refine the reviewer's voice.
"""

from datetime import datetime, timedelta
import re
from types import SimpleNamespace

import aiohttp

from u_boot_pylib import tout

from patman import database
from patman import gmail
from patman.review import common
from patman.review import voice


def _draft_location(ctx):
    """Describe the branch and series a batch of drafts belongs to

    Args:
        ctx (ReviewContext): Review context

    Returns:
        str: A ' (branch <name>, link <id>)' suffix with whichever parts
            are known, or '' if neither is (e.g. a re-draft with no branch)
    """
    parts = []
    if getattr(ctx, 'branch_name', None):
        parts.append(f'branch {ctx.branch_name}')
    link = ctx.series_data.get('id') if ctx.series_data else None
    if link:
        parts.append(f'link {link}')
    return f' ({", ".join(parts)})' if parts else ''


def create_drafts(ctx, args, review_bodies, review_ids):
    """Create Gmail drafts for review emails

    If the reviewer's email differs from the Gmail account, a From header is
    set on the draft so the email is sent with the correct identity.

    Args:
        ctx (ReviewContext): Review context
        args (Namespace): Command-line arguments (for gmail_account, dry_run)
        review_bodies (dict): Map of seq to review body text
        review_ids (dict): Map of seq to review record ID
    """
    to_draft = dict(review_bodies)
    if not to_draft:
        tout.notice('All reviews already have Gmail drafts')
        return

    patch_headers = {}
    patches = ctx.series_data.get('patches', [])

    async def _fetch_patch_headers():
        async with aiohttp.ClientSession() as client:
            # Fetch the cover letter's headers too (index 0) so its review
            # reply carries the original To and Cc -- a full reply-all --
            # rather than going just to the submitter and the list
            cover = ctx.series_data.get('cover_letter')
            if cover and cover.get('id'):
                data = await ctx.pwork.get_cover(client, str(cover['id']))
                patch_headers[0] = data.get('headers', {})
            for i, patch in enumerate(patches):
                data = await ctx.pwork.get_patch(client, str(patch['id']))
                patch_headers[i + 1] = data.get('headers', {})

    common.run(_fetch_patch_headers())

    sender = None
    if ctx.reviewer_email and args.gmail_account:
        if ctx.reviewer_email.lower() != args.gmail_account.lower():
            sender = ctx.reviewer_tag

    draft_ids = gmail.create_review_drafts(ctx.series_data, to_draft,
        patch_headers=patch_headers, dry_run=args.dry_run,
        account=args.gmail_account, sender=sender)
    where = _draft_location(ctx)
    if args.dry_run:
        tout.notice(f'Dry run: would create {len(to_draft)} draft(s){where}')
    else:
        for seq, draft_id in draft_ids.items():
            if seq in review_ids:
                ctx.cser.db.review_set_draft_id(review_ids[seq], draft_id)
        ctx.cser.commit()
        tout.notice(f'Created {len(draft_ids)} Gmail draft(s){where}')


# Number of drafts to check before saving progress
SYNC_CHUNK = 50


# Only refine the voice profile from reviews written in this many days, and
# from at most this many reviews in each sync
VOICE_DAYS = 14


MAX_VOICE_REFINE = 3


def _gmail_date(timestamp):
    """Get the day before a timestamp, for use in a Gmail 'after:' search

    Args:
        timestamp (str or None): ISO timestamp, e.g. '2026-03-30T07:57:15'

    Returns:
        str or None: Date, e.g. '2026/03/29', or None if there is none
    """
    try:
        when = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return None
    return (when - timedelta(days=1)).strftime('%Y/%m/%d')


def _epoch(timestamp):
    """Convert a timestamp to seconds since the epoch

    Args:
        timestamp (str or None): ISO timestamp, in local time

    Returns:
        float or None: Seconds since the epoch, or None if there is none
    """
    try:
        return datetime.fromisoformat(timestamp).timestamp()
    except (TypeError, ValueError):
        return None


def _review_subject(db, rev):
    """Get the subject of the patch or cover letter which a review is about

    Args:
        db (Database): Database holding the review
        rev (Review): Review record

    Returns:
        str or None: Subject, or None if not known
    """
    if not rev.seq:
        svr = db.ser_ver_get_by_id(rev.svid)
        title = svr.name or db.series_get_info(svr.series_id)[1]
        return f'[v{svr.version}] {title}' if title else None
    for pcm in db.pcommit_get_list(rev.svid):
        if pcm.seq == rev.seq - 1:
            return pcm.subject
    return None


def sync_drafts(service, cser):
    """Sync draft status with Gmail

    Detects sent and deleted drafts, and updates the database. Notes the
    thread of each draft which is still pending, so that it can be found
    there once sent. Refines the voice profile if the sent text of a recent
    review differs from the draft.

    Args:
        service: Gmail API service
        cser (Cseries): Open cseries instance

    Returns:
        bool: True if there were drafts to sync
    """
    db = cser.db
    reviews = db.review_get_by_status('draft')
    if not reviews:
        tout.notice('No pending drafts to sync')
        return False

    recent = (datetime.now() - timedelta(days=VOICE_DAYS)).isoformat()
    cutoff = (datetime.now() - timedelta(days=REPLY_DAYS)).isoformat()
    total_sent = total_deleted = refined = 0
    for start in range(0, len(reviews), SYNC_CHUNK):
        chunk = reviews[start:start + SYNC_CHUNK]
        sent, deleted, threads = gmail.sync_drafts(service, [
            SimpleNamespace(idnum=rev.idnum, draft_id=rev.draft_id,
                            body=rev.body, thread_id=rev.gmail_thread_id,
                            subject=_review_subject(db, rev),
                            after=_gmail_date(rev.timestamp),
                            since=_epoch(rev.timestamp),
                            check_sent=(rev.timestamp or '') >= cutoff)
            for rev in chunk])
        by_id = {rev.idnum: rev for rev in chunk}
        for review_id, (body, msg_id, thread_id) in sent.items():
            db.review_set_sent(review_id, body, msg_id, thread_id)
            tout.notice(f'Review {review_id}: sent')
            draft = by_id[review_id]
            if (body.strip() != draft.body.strip() and
                    (draft.timestamp or '') >= recent and
                    refined < MAX_VOICE_REFINE):
                tout.notice(f'Review {review_id}: edits detected, '
                            'refining voice...')
                voice.refine_voice_sync(draft.body, body)
                refined += 1
        for review_id in deleted:
            db.review_set_deleted(review_id)
            tout.notice(f'Review {review_id}: deleted (not sent)')
        for review_id, thread_id in threads.items():
            db.review_set_thread(review_id, thread_id)
        cser.commit()
        total_sent += len(sent)
        total_deleted += len(deleted)

    pending = len(reviews) - total_sent - total_deleted
    if pending:
        tout.notice(f'{pending} draft(s) still pending')
    if total_sent or total_deleted:
        tout.notice(f'Synced {total_sent} sent, {total_deleted} deleted')
    return True


# Only look for replies to reviews sent in this many days
REPLY_DAYS = 90


def delete_gmail_drafts(args, reviews):
    """Delete the Gmail drafts recorded for the given reviews

    Used when reviews are being replaced -- a forced re-review or a
    redraft -- so the previous drafts do not linger in Gmail as
    duplicates alongside the new ones. Reviews with no recorded draft
    are ignored, and nothing happens if Gmail is unavailable.

    Args:
        args (Namespace): Command-line arguments (for gmail_account)
        reviews (list of Review): Reviews whose recorded drafts to delete
    """
    draft_ids = [rev.draft_id for rev in reviews if rev.draft_id]
    if not draft_ids:
        return
    if not gmail.check_available():
        return
    service = gmail.get_service(getattr(args, 'gmail_account', None))
    for draft_id in draft_ids:
        gmail.delete_draft(service, draft_id)
    tout.notice(f'Deleted {len(draft_ids)} old Gmail draft(s)')


def needs_draft(rev, redraft):
    """Check whether a stored review should get a Gmail draft

    A review which has been sent never gets a new draft. Without redraft,
    only reviews which have never had a draft get one; with it, existing
    drafts are recreated and deleted drafts are brought back.

    Args:
        rev (Review): Review record
        redraft (bool): True to recreate drafts which exist or were deleted

    Returns:
        bool: True if a draft should be created
    """
    if rev.status in (database.REVIEW_SENT, database.REVIEW_REPLIED):
        return False
    if redraft:
        return True
    return not rev.draft_id and rev.status != database.REVIEW_DELETED


def draft_stored_reviews(args, reviews, series_data, pwork, cser):
    """Create Gmail drafts from stored review records

    Drafts reviews that do not already have a draft_id. With --redraft it
    drafts every stored review, recreating drafts that already exist so a
    failed or lost draft can be regenerated from the database. Reviews which
    have been sent are never drafted again. With -i, only the selected
    patches are drafted.

    Args:
        args (Namespace): Command-line arguments
        reviews (list): Review records
        series_data (dict): Series data from patchwork
        pwork (Patchwork): Patchwork instance
        cser (Cseries): Cseries instance
    """
    sel = common.parse_patch_selection(getattr(args, 'patches', None))
    need_draft = [rev for rev in reviews
                  if needs_draft(rev, args.redraft) and
                  (not sel or rev.seq in sel)]
    if not need_draft:
        tout.notice('All reviews already have Gmail drafts')
        return
    if args.redraft:
        # Remove the drafts we are about to recreate so they do not linger
        # as duplicates in the same Gmail thread
        delete_gmail_drafts(args, need_draft)
    review_bodies = {rev.seq: rev.body for rev in need_draft}
    review_ids = {rev.seq: rev.idnum
                  for rev in need_draft}
    rname = remail = None
    for rev in need_draft:
        match = re.search(r'Reviewed-by:\s*(.+?)\s*<(.+?)>', rev.body)
        if match:
            rname = match.group(1).strip()
            remail = match.group(2).strip()
            break
    ctx = common.ReviewContext(pwork, cser, series_data)
    ctx.reviewer_name = rname or ''
    ctx.reviewer_email = remail or ''
    create_drafts(ctx, args, review_bodies, review_ids)
