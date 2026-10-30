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

    Returns:
        dict: Map of seq to the ID of the draft created for it, empty for a
            dry run
    """
    to_draft = dict(review_bodies)
    if not to_draft:
        tout.notice('All reviews already have Gmail drafts')
        return {}

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

    envelopes = {}
    draft_ids = gmail.create_review_drafts(ctx.series_data, to_draft,
        patch_headers=patch_headers, dry_run=args.dry_run,
        account=args.gmail_account,
        sender=_sender(args, getattr(ctx, 'reviewer_name', ''),
                       ctx.reviewer_email),
        envelopes=envelopes)
    where = _draft_location(ctx)
    if args.dry_run:
        tout.notice(f'Dry run: would create {len(to_draft)} draft(s){where}')
        return {}
    # Keep the headers, so that the review can be drafted again without
    # asking patchwork for them
    for seq, env in envelopes.items():
        if seq in review_ids:
            ctx.cser.db.review_set_envelope(review_ids[seq], env)
    for seq, draft_id in draft_ids.items():
        if seq in review_ids:
            ctx.cser.db.review_set_draft_id(review_ids[seq], draft_id)
    ctx.cser.commit()
    tout.notice(f'Created {len(draft_ids)} Gmail draft(s){where}')
    return draft_ids


def replace_drafts(ctx, args, reviews):
    """Create Gmail drafts for reviews, then delete any they had before

    The old drafts are deleted only once the new ones are made, so that a
    review is not left with no draft if patchwork or Gmail fails part way.

    Args:
        ctx (ReviewContext): Review context
        args (Namespace): Command-line arguments (for gmail_account, dry_run)
        reviews (list of Review): Reviews to draft, as they were before the
            new drafts are made, so with their old draft IDs
    """
    draft_ids = create_drafts(ctx, args, {rev.seq: rev.body for rev in reviews},
                              {rev.seq: rev.idnum for rev in reviews})
    delete_gmail_drafts(args, [rev for rev in reviews if rev.seq in draft_ids])


def _sender(args, reviewer_name, reviewer_email):
    """Get the From identity for review drafts

    Args:
        args (Namespace): Command-line arguments (for gmail_account)
        reviewer_name (str): Reviewer's name
        reviewer_email (str): Reviewer's email address

    Returns:
        str or None: 'Name <email>' if the reviewer's address differs from
            the Gmail account, else None to send as the account
    """
    account = getattr(args, 'gmail_account', None)
    if (reviewer_email and account and
            reviewer_email.lower() != account.lower()):
        return f'{reviewer_name} <{reviewer_email}>'
    return None


def _reviewer_from_bodies(reviews):
    """Find the reviewer from the Reviewed-by tag in some reviews

    Args:
        reviews (list of Review): Review records

    Returns:
        tuple: (name, email), each '' if no review has a Reviewed-by tag
    """
    for rev in reviews:
        match = re.search(r'Reviewed-by:\s*(.+?)\s*<(.+?)>', rev.body)
        if match:
            return match.group(1).strip(), match.group(2).strip()
    return '', ''


def _reviews_to_draft(args, reviews):
    """Pick out the stored reviews which should get a Gmail draft

    Args:
        args (Namespace): Command-line arguments (for redraft, patches)
        reviews (list of Review): Review records

    Returns:
        list of Review: Reviews to draft
    """
    sel = common.parse_patch_selection(getattr(args, 'patches', None))
    return [rev for rev in reviews
            if needs_draft(rev, args.redraft) and (not sel or rev.seq in sel)]


def redraft_from_db(args, cser, link):
    """Recreate the Gmail drafts of a reviewed series from the database

    Each review keeps the headers it needs to be sent as a reply to its
    patch, once it has had a draft, so its draft can be created again
    without asking patchwork for the series and the patches' headers.

    Args:
        args (Namespace): Command-line arguments
        cser (Cseries): Cseries instance holding the reviews
        link (str): Patchwork series link/ID

    Returns:
        bool: True if the drafts were created, False if patchwork is needed,
            since the series is not in the database or a review to be
            drafted has no stored headers
    """
    existing = cser.db.series_find_by_link(link)
    if not existing:
        return False
    reviews = cser.db.review_get_for_version(existing[3])
    need_draft = _reviews_to_draft(args, reviews)
    if not need_draft:
        tout.notice('No reviews to draft: all have been sent')
        return True
    if not has_envelopes(cser, need_draft):
        return False
    tout.notice(f"Drafting stored reviews of '{existing[1]}' v{existing[2]}")
    redraft_reviews(args, cser, need_draft, link)
    return True


def has_envelopes(cser, reviews):
    """Check whether reviews can be drafted without asking patchwork

    Args:
        cser (Cseries): Cseries instance holding the reviews
        reviews (list of Review): Reviews to check

    Returns:
        bool: True if each review has the headers for its reply
    """
    return all(cser.db.review_get_envelope(rev.idnum) for rev in reviews)


def redraft_reviews(args, cser, reviews, link):
    """Recreate the Gmail drafts of reviews, using their stored headers

    Any existing drafts of the reviews are deleted once the new ones are
    made, so that they do not linger as duplicates in the same Gmail thread
    but are kept if the new ones cannot be made.

    Args:
        args (Namespace): Command-line arguments (uses dry_run and
            gmail_account)
        cser (Cseries): Cseries instance holding the reviews
        reviews (list of Review): Reviews to draft, each of which must have
            its headers stored (see has_envelopes())
        link (str): Patchwork series link/ID, for messages
    """
    envelopes = {rev.seq: cser.db.review_get_envelope(rev.idnum)
                 for rev in reviews}
    name, email = _reviewer_from_bodies(reviews)
    draft_ids = gmail.create_drafts_from_envelopes(
        envelopes, {rev.seq: rev.body for rev in reviews},
        dry_run=args.dry_run, account=getattr(args, 'gmail_account', None),
        sender=_sender(args, name, email))
    if args.dry_run:
        tout.notice(f'Dry run: would create {len(reviews)} draft(s) '
                    f'(link {link})')
        return
    for rev in reviews:
        if rev.seq in draft_ids:
            cser.db.review_set_draft_id(rev.idnum, draft_ids[rev.seq])
    cser.commit()
    delete_gmail_drafts(args, [rev for rev in reviews if rev.seq in draft_ids])
    tout.notice(f'Created {len(draft_ids)} Gmail draft(s) (link {link})')


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

    Detects sent and deleted drafts, including those of reviews which have
    been edited since their draft was made, and updates the database. Notes
    the thread of each draft which is still pending, so that it can be found
    there once sent. Refines the voice profile if the sent text of a recent
    review differs from the draft.

    Args:
        service: Gmail API service
        cser (Cseries): Open cseries instance

    Returns:
        bool: True if there were drafts to sync
    """
    db = cser.db
    reviews = (db.review_get_by_status(database.REVIEW_DRAFT) +
               db.review_get_by_status(database.REVIEW_EDITED))
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
            # An edited review no longer has the text of its draft, so the
            # differences are not all edits made in Gmail
            if (draft.status == database.REVIEW_DRAFT and
                    body.strip() != draft.body.strip() and
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
    need_draft = _reviews_to_draft(args, reviews)
    if not need_draft:
        tout.notice('All reviews already have Gmail drafts')
        return
    ctx = common.ReviewContext(pwork, cser, series_data)
    ctx.reviewer_name, ctx.reviewer_email = _reviewer_from_bodies(need_draft)
    replace_drafts(ctx, args, need_draft)
