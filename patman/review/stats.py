# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Stats about the reviews in the review database

This counts the reviews written, the emails sent and the replies received,
in all and for each of the last few weeks, for 'patman review --stats'.
"""

from datetime import date, datetime, timedelta, timezone
import email.utils

from patman import database

# Number of weeks to show by default
WEEKS = 8

# Rows of the totals, as (key, label, description)
TOTALS = (
    ('series', 'Series reviewed', 'Series with a review of any version'),
    ('patches', 'Patches reviewed', 'Reviews of patches'),
    ('covers', 'Cover letters', 'Reviews of cover letters'),
    ('approved', 'Reviewed-by given', 'Reviews which give a Reviewed-by tag'),
    ('emails', 'Emails sent', 'Reviews and responses sent'),
    ('drafts', 'Drafts to send', 'Reviews with a Gmail draft waiting'),
    ('not_drafted', 'Not drafted', 'Reviews with no Gmail draft'),
    ('deleted', 'Drafts deleted', 'Review drafts deleted without sending'),
    ('replies', 'Replies received',
     'Replies to your reviews, and comments on patchwork which you responded '
     'to'),
    ('waiting', 'Replies waiting', 'Replies which need a response'),
    ('skipped', 'No response needed', 'Replies which need no response'),
    )

# Columns of the weeks, as (key, heading)
WEEK_COLS = (
    ('series', 'Series'),
    ('patches', 'Patches'),
    ('sent', 'Sent'),
    ('replies', 'Replies'),
    ('responses', 'Responses'),
    )


def _count(counts, *statuses):
    """Add up the counts of some statuses

    Args:
        counts (dict): Number of records with each status
        statuses (list of str): Statuses to count

    Return:
        int: Number of records with any of the statuses
    """
    return sum(counts.get(status, 0) for status in statuses)


def _reply_day(sent, recorded):
    """Work out the day on which a reply was sent

    Args:
        sent (str or None): Date header of the reply, e.g.
            'Mon, 19 Oct 2026 12:20:45 -0600', or the time of a comment on
            patchwork, in UTC, e.g. '2026-10-19T18:20:45'
        recorded (str): When patman recorded it, e.g. '2026-10-19T12:30:00'

    Return:
        str: Local day, e.g. '2026-10-19'
    """
    try:
        when = email.utils.parsedate_to_datetime(sent)
    except (TypeError, ValueError):
        try:
            when = datetime.fromisoformat(sent)
        except (TypeError, ValueError):
            return recorded[:10]
    if not when.tzinfo:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone().date().isoformat()


def _get_totals(db):
    """Count the reviews, emails and replies in the database

    Args:
        db (Database): Review database

    Return:
        dict: Count for each key in TOTALS, along with:
            versions (int): Versions of series with a review
            sent (int): Reviews sent
            responses (int): Responses sent
            replied (int): Reviews which have a reply
            response_drafts (int): Responses with a Gmail draft waiting
            untimed (int): Reviews and responses sent before patman
                recorded when, which are not in the weeks
    """
    revs = db.stats_get_reviews()
    status = revs.pop('status')
    replies = db.stats_get_replies()
    sent = _count(status, *database.REVIEW_DONE)
    responses = _count(replies, database.REPLY_SENT)
    return {
        **revs,
        'emails': sent + responses,
        'sent': sent,
        'responses': responses,
        'drafts': _count(status, database.REVIEW_DRAFT,
                         database.REVIEW_EDITED),
        'not_drafted': _count(status, database.REVIEW_NEW),
        'deleted': _count(status, database.REVIEW_DELETED),
        'replied': _count(status, database.REVIEW_REPLIED),
        'replies': sum(replies.values()),
        'waiting': _count(replies, *database.REPLY_WAITING),
        'skipped': _count(replies, database.REPLY_SKIP),
        'response_drafts': _count(replies, database.REPLY_DRAFT),
        'untimed': db.stats_get_sent_untimed(),
        }


def _get_weeks(db, weeks, today):
    """Count what happened in each of the last few weeks

    Args:
        db (Database): Review database
        weeks (int): Number of weeks, including this one
        today (date): Today

    Return:
        list of dict: One for each week, oldest first, with:
            start (str): Monday which starts the week, e.g. '2026-10-19'
            and a count for each key in WEEK_COLS
    """
    first = today - timedelta(days=today.weekday() + 7 * (weeks - 1))
    result = [dict({key: 0 for key, _ in WEEK_COLS},
                   start=(first + timedelta(weeks=num)).isoformat())
              for num in range(weeks)]

    def week_of(day):
        num = (date.fromisoformat(day) - first).days // 7
        return result[num] if 0 <= num < weeks else None

    dates = db.stats_get_dates(first.isoformat())
    versions = set()
    for day, svid, seq in dates['reviews']:
        week = week_of(day)
        if week:
            if seq:
                week['patches'] += 1
            if (week['start'], svid) not in versions:
                versions.add((week['start'], svid))
                week['series'] += 1
    days = {'sent': dates['sent'], 'responses': dates['responses'],
            'replies': [_reply_day(*reply) for reply in dates['replies']]}
    for key, when in days.items():
        for day in when:
            week = week_of(day)
            if week:
                week[key] += 1
    return result


def get_stats(db, weeks=WEEKS, today=None):
    """Get the stats about the reviews in a database

    Args:
        db (Database): Review database
        weeks (int): Number of weeks to count, including this one
        today (date or None): Today, or None to use the real date

    Return:
        dict:
            totals (dict): Counts for all time, from _get_totals()
            weeks (list of dict): Counts for each week, from _get_weeks()
    """
    return {'totals': _get_totals(db),
            'weeks': _get_weeks(db, weeks, today or date.today())}


def _total_note(key, totals):
    """Give the detail which goes after a total, if any

    Args:
        key (str): Key of the total
        totals (dict): Totals, from _get_totals()

    Return:
        str: Detail, e.g. '(52 versions)', or '' if there is none
    """
    if key == 'series':
        return f"({totals['versions']} versions)"
    if key == 'emails':
        return f"({totals['sent']} reviews, {totals['responses']} responses)"
    if key == 'replies':
        return f"(to {totals['replied']} reviews)"
    if key == 'waiting' and totals['response_drafts']:
        return f"(and {totals['response_drafts']} responses drafted)"
    return ''


def show_stats(db, weeks=WEEKS, today=None):
    """Show the stats about the reviews in a database

    Args:
        db (Database): Review database
        weeks (int): Number of weeks to show, including this one
        today (date or None): Today, or None to use the real date
    """
    stats = get_stats(db, weeks, today)
    totals = stats['totals']
    width = max(len(label) for _, label, _ in TOTALS)
    for key, label, _ in TOTALS:
        note = _total_note(key, totals)
        print(f'{label:<{width}}  {totals[key]:>6}  {note}'.rstrip())

    print()
    print('Week of     ' + ''.join(f'{head:>11}' for _, head in WEEK_COLS))
    for week in stats['weeks']:
        print(f"{week['start']}  " +
              ''.join(f'{week[key]:>11}' for key, _ in WEEK_COLS))
    if totals['untimed']:
        print()
        print(f"{totals['untimed']} emails sent before patman recorded the "
              'time are only in the totals')
