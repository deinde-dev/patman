# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Gather the information which the page shows, from the database

Each function returns plain dicts and lists, ready to be sent as JSON.
"""

import asyncio
from collections import Counter
from datetime import datetime
from importlib import metadata
import os
import re
import subprocess

from patman import database
from patman.review import lookup
from patman.server.params import BadRequest
from patman.workflow import Wtype


# How many days a series can wait for a new version, after you ask for changes,
# before it is stale
STALE_DAYS = 28

# Statuses of a review which is not sent yet
REVIEW_UNSENT = (database.REVIEW_NEW, database.REVIEW_DRAFT,
                 database.REVIEW_EDITED)

# Directory above the patman package, which is the top of the checkout when
# patman runs from its git repository
TOP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def get_version(top=TOP_DIR):
    """Get the version of patman which is running

    When patman runs from a checkout, this is the version in its
    pyproject.toml, followed by what 'git describe' says if that is not the
    release itself, e.g. '0.4.0 (v0.4.0-3-g1234abc)', so that it is clear
    which commit is running. Otherwise it is the installed package's version.

    Args:
        top (str): Directory which may hold patman's pyproject.toml

    Return:
        str or None: Version, or None if it is not known
    """
    try:
        with open(os.path.join(top, 'pyproject.toml'), encoding='utf-8') as inf:
            text = inf.read()
    except OSError:
        text = ''
    found = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if found and re.search(r'^name\s*=\s*"patch-manager"', text, re.MULTILINE):
        version = found.group(1)
        if not os.path.exists(os.path.join(top, '.git')):
            return version
        try:
            result = subprocess.run(['git', 'describe', '--tags', '--dirty'],
                                    cwd=top, capture_output=True, text=True,
                                    timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return version
        desc = result.stdout.strip()
        if result.returncode or not desc or desc == f'v{version}':
            return version
        return f'{version} ({desc})'
    try:
        return metadata.version('patch-manager')
    except metadata.PackageNotFoundError:
        return None


def _state_counts(pcommits):
    """Count the patchwork states of a list of patches

    Args:
        pcommits (list of Pcommit): Patches to count

    Return:
        dict: key (str): state, or 'unknown' if not set
            value (int): number of patches in that state
    """
    return dict(Counter(pcm.state or 'unknown' for pcm in pcommits))


def _patch_info(pcm):
    """Convert a pcommit record to a JSON-friendly dict

    Args:
        pcm (Pcommit): Record to convert

    Return:
        dict: Patch information
    """
    return {
        'seq': pcm.seq + 1,
        'subject': pcm.subject,
        'state': pcm.state,
        'patch_id': pcm.patch_id,
        'num_comments': pcm.num_comments or 0,
    }


def _version_info(svr):
    """Convert a ser_ver record to a JSON-friendly dict

    Args:
        svr (SerVer): Record to convert

    Return:
        dict: Version information, without the patches
    """
    return {
        'svid': svr.idnum,
        'version': svr.version,
        'link': svr.link,
        'desc': svr.desc,
        'cover_id': svr.cover_id,
        'cover_name': svr.name,
        'cover_num_comments': svr.cover_num_comments or 0,
        'archive_tag': svr.archive_tag,
    }


def review_info(rev):
    """Convert a review record to a JSON-friendly dict

    Args:
        rev (Review): Record to convert

    Return:
        dict: Review information
    """
    return {
        'id': rev.idnum,
        'seq': rev.seq,
        'body': rev.body,
        'approved': bool(rev.approved),
        'timestamp': rev.timestamp,
        'status': rev.status,
    }


def mail_info(data):
    """Pick out the useful fields from a patchwork patch or cover letter

    Args:
        data (dict): Patch or cover letter as returned by the patchwork API

    Return:
        dict: Information about the patch or cover letter
    """
    submitter = data.get('submitter') or {}
    return {
        'id': data.get('id'),
        'name': data.get('name'),
        'date': data.get('date'),
        'submitter': submitter.get('name') or submitter.get('email'),
        'state': data.get('state'),
        'content': data.get('content') or '',
        'diff': data.get('diff') or '',
        'web_url': data.get('web_url'),
        'series': [{'id': ser.get('id'), 'name': ser.get('name'),
                    'version': ser.get('version')}
                   for ser in data.get('series') or []],
    }

# A blob hash, as in the 'index' line of a diff
RE_BLOB = re.compile(r'^[0-9a-f]{7,40}$')

# Largest file which can be read from the git repo
MAX_BLOB = 4 << 20


async def read_blob(topdir, hashes):
    """Read a file from the git repo

    A diff names the blobs of each file before and after the change, but
    the repo may only hold one of them, so this tries each in turn.

    Args:
        topdir (str): Top directory of the git repo
        hashes (list of str): Blob hashes, possibly abbreviated, to try in
            order

    Return:
        tuple:
            str: Hash of the blob found, as given
            str: Contents of the file

    Raises:
        ValueError: A hash is invalid, none of the blobs is in the repo or
            the file is too large or not text
    """
    for oid in hashes:
        if not RE_BLOB.match(oid):
            raise BadRequest(f"Invalid blob hash '{oid}'")
        proc = await asyncio.create_subprocess_exec(
            'git', 'cat-file', 'blob', oid, cwd=topdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        data, _ = await proc.communicate()
        if proc.returncode:
            continue
        if len(data) > MAX_BLOB:
            raise ValueError(f'File is too large ({len(data)} bytes)')
        if b'\0' in data:
            raise ValueError('File is not text')
        return oid, data.decode('utf-8', errors='replace')
    raise ValueError('The file is not in the git repo')


def reply_info(reply):
    """Convert a reply record to a JSON-friendly dict

    Args:
        reply (Reply): Record to convert

    Return:
        dict: Reply information
    """
    return {
        'id': reply.idnum,
        'sender': reply.sender,
        'subject': reply.subject,
        'date': reply.date,
        'body': reply.body,
        'response': reply.response,
        'status': reply.status,
        'timestamp': reply.timestamp,
        'seq': reply.seq,
        'comment_id': reply.comment_id,
    }


def comment_info(comment, own=frozenset()):
    """Pick out the useful fields from a patchwork comment

    Args:
        comment (dict): Comment as returned by the patchwork API
        own (set of str): Our own email addresses, in lower case

    Return:
        dict: Comment information
    """
    submitter = comment.get('submitter') or {}
    email = submitter.get('email')
    return {
        'id': comment.get('id'),
        'date': comment.get('date'),
        'name': submitter.get('name'),
        'email': email,
        'subject': comment.get('subject'),
        'content': comment.get('content'),
        'web_url': comment.get('web_url'),
        'own': bool(email) and email.lower() in own,
    }


def _latest_versions(cser, reviewed=False):
    """Get the ser_ver record for the latest version of each series

    Args:
        cser (Cseries): Open cseries instance
        reviewed (bool): True to consider only the versions with a review

    Return:
        dict: key (int): series ID
            value (SerVer): latest version of that series
    """
    svdict = cser.get_ser_ver_dict()
    found = (cser.db.series_get_max_reviewed_versions() if reviewed
             else cser.db.series_get_all_max_versions())
    return {series_id: svdict[svid] for svid, series_id, _ in found}


def _last_sent(cser):
    """Get the time that each series was last sent

    Args:
        cser (Cseries): Open cseries instance

    Return:
        dict: key (int): series ID
            value (str): timestamp, 'YYYY-MM-DD HH:MM:SS'
    """
    # Entries come back in time order, so later ones win
    return {series_id: timestamp for series_id, _, _, timestamp in
            cser.db.workflow_get_by_type(Wtype.SENT)}


def get_series_list(cser):
    """Get a summary of the user's own (non-archived) series

    Args:
        cser (Cseries): Open cseries instance

    Return:
        list of dict: One item for each series, sorted by name
    """
    latest = _latest_versions(cser)
    sent = _last_sent(cser)
    out = []
    for ser in cser.db.series_get_dict().values():
        svr = latest.get(ser.idnum)
        pcommits = cser.db.pcommit_get_list(svr.idnum) if svr else []
        out.append({
            'id': ser.idnum,
            'name': ser.name,
            'desc': ser.desc,
            'upstream': ser.upstream,
            'version': svr.version if svr else None,
            'link': svr.link if svr else None,
            'count': len(pcommits),
            'states': _state_counts(pcommits),
            'num_comments': sum(pcm.num_comments or 0 for pcm in pcommits),
            'cover_num_comments': (svr.cover_num_comments or 0) if svr else 0,
            'sent': sent.get(ser.idnum),
        })
    return sorted(out, key=lambda item: item['name'])


def get_series_detail(cser, idnum):
    """Get all versions of a series, with their patches

    Args:
        cser (Cseries): Open cseries instance
        idnum (int): Series ID

    Return:
        dict: Series information, with 'versions' newest first

    Raises:
        ValueError: The series does not exist
    """
    name, desc, ups = cser.db.series_get_info(idnum)
    versions = []
    for svr in cser.db.ser_ver_get_for_series(idnum):
        info = _version_info(svr)
        pcommits = cser.db.pcommit_get_list(svr.idnum)
        info['patches'] = [_patch_info(pcm)
                           for pcm in sorted(pcommits, key=lambda p: p.seq)]
        info['states'] = _state_counts(pcommits)
        versions.append(info)
    versions.sort(key=lambda item: item['version'], reverse=True)
    return {'id': idnum, 'name': name, 'desc': desc, 'upstream': ups,
            'versions': versions}


def get_series_link(cser, idnum, version=None):
    """Check that a version of a series can be synced from patchwork

    Args:
        cser (Cseries): Open cseries instance
        idnum (int): Series ID
        version (int or None): Version, or None for the latest

    Return:
        tuple:
            str: Series name
            int: Version

    Raises:
        ValueError: The series or version does not exist, or has no
            patchwork link
    """
    name, _, _ = cser.db.series_get_info(idnum)
    svrs = {svr.version: svr for svr in cser.db.ser_ver_get_for_series(idnum)}
    if version is None and svrs:
        version = max(svrs)
    svr = svrs.get(version)
    if not svr:
        raise ValueError(f"Series '{name}' has no version {version}")
    if not svr.link:
        raise ValueError(f"Series '{name}' v{version} has no patchwork link")
    return name, version


def get_gather_count(cser):
    """Get the number of series which 'patman series gather-all' reads

    Args:
        cser (Cseries): Open cseries instance

    Return:
        int: Number of series, i.e. the latest version of each unarchived
            series which has a patchwork link
    """
    to_fetch, _ = cser._get_fetch_dict(False)  # pylint: disable=W0212
    return len(to_fetch)


def get_review_list(cser):
    """Get a summary of the series reviewed with 'patman review'

    Args:
        cser (Cseries): Open cseries instance

    Return:
        list of dict: One item for each series, most recent review first
    """
    latest = _latest_versions(cser)
    waiting = cser.db.reply_count_waiting()
    hidden = cser.db.series_get_hidden()
    out = []
    for ser in cser.db.series_get_dict(reviews_only=True).values():
        svr = latest.get(ser.idnum)
        reviews = cser.db.review_get_for_version(svr.idnum) if svr else []
        out.append({
            'id': ser.idnum,
            'name': ser.name,
            'desc': ser.desc,
            'upstream': ser.upstream,
            'version': svr.version if svr else None,
            'link': svr.link if svr else None,
            'num_reviews': len(reviews),
            'approved': sum(1 for rev in reviews if rev.approved),
            'statuses': dict(Counter(rev.status or 'new' for rev in reviews)),
            'timestamp': max((rev.timestamp for rev in reviews
                              if rev.timestamp), default=None),
            'hidden': ser.idnum in hidden,
            'replies_waiting': waiting.get(ser.idnum, 0),
        })
    return sorted(out, key=lambda item: item['timestamp'] or '',
                  reverse=True)


def get_review_detail(cser, idnum):
    """Get the reviews for each version of a reviewed series

    Args:
        cser (Cseries): Open cseries instance
        idnum (int): Series ID

    Return:
        dict: Series information, with 'versions' newest first

    Raises:
        ValueError: The series does not exist
    """
    name, desc, ups = cser.db.series_get_info(idnum)
    versions = []
    for svr in cser.db.ser_ver_get_for_series(idnum):
        info = _version_info(svr)
        info['notes'] = svr.notes
        info['reviews'] = []
        for rev in cser.db.review_get_for_version(svr.idnum):
            item = review_info(rev)
            # The message the review replies to, which locates the patch on
            # the list, e.g. one which patchwork lacks
            env = cser.db.review_get_envelope(rev.idnum)
            item['msgid'] = (env or {}).get('in_reply_to')
            item['replies'] = [reply_info(reply) for reply in
                               cser.db.reply_get_for_review(rev.idnum)]
            info['reviews'].append(item)
        info['patches'] = [_patch_info(pcm) for pcm in sorted(
            cser.db.pcommit_get_list(svr.idnum), key=lambda pcm: pcm.seq)]
        # Comments on patchwork which we have been asked to respond to
        info['comments'] = [reply_info(reply) for reply in
                            cser.db.reply_get_comments(svr.idnum)]
        versions.append(info)
    versions.sort(key=lambda item: item['version'], reverse=True)
    return {'id': idnum, 'name': name, 'desc': desc, 'upstream': ups,
            'hidden': idnum in cser.db.series_get_hidden(),
            'versions': versions}


def _norm_title(title):
    """Normalise a series title so that versions of a series can be matched

    Args:
        title (str): Title, e.g. '[v2] video: Add a driver'

    Return:
        str: Normalised title, e.g. 'video: add a driver'
    """
    return re.sub(r'^(\[[^]]*\]\s*)+', '', title or '').strip().lower()


def norm_subject(subject):
    """Normalise the subject of a patch, so that it can be found upstream

    Args:
        subject (str): Subject, as patchwork gives it, e.g.
            '[v2,3/5] video: Add a driver', or as a commit has it

    Return:
        str: Normalised subject, e.g. 'video: add a driver'
    """
    return ' '.join(_norm_title(subject).split())


async def get_upstream_commits(topdir, refs, since):
    """Get the commits on upstream branches since a date, by their subject

    This finds the patches which have been applied, without asking patchwork,
    whose patch states are often not updated when a patch is applied.

    Args:
        topdir (str): Top directory of the git repo
        refs (list of str): Branches to look at, e.g. ['us/main', 'us/next'];
            a commit on more than one is given with the first
        since (str): Earliest commit date to look at, e.g. '2026-10-01'

    Return:
        dict: key (str): Normalised subject of the commit (see norm_subject())
            value (tuple): Newest commit with that subject:
                str: Hash of the commit
                str: Branch it is on
    """
    out = {}
    for ref in refs:
        proc = await asyncio.create_subprocess_exec(
            'git', 'log', '--no-merges', '--format=%H %s', f'--since={since}',
            ref, '--', cwd=topdir, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        data, _ = await proc.communicate()
        if proc.returncode:
            continue
        for line in data.decode('utf-8', 'replace').splitlines():
            commit, _, subject = line.partition(' ')
            out.setdefault(norm_subject(subject), (commit, ref))
    return out


# What is known about a patch in the inbox when nothing is
UNKNOWN_STATUS = {'commit': None, 'branch': None, 'state': None}


def get_patch_status(series_list, commits, states):
    """Get what is known about the patches in the inbox, other than their names

    Args:
        series_list (list of dict): Series from patchwork
        commits (dict): Commits upstream, by subject (see
            get_upstream_commits())
        states (dict): key (int): Patch ID
            value (str): State on patchwork, as last seen, e.g. 'accepted'

    Return:
        dict: key (int): Patch ID
            value (dict): 'commit' and 'branch' upstream which have the
                patch's subject, or None, and its 'state', or None if it is
                not known
    """
    out = {}
    for ser in series_list:
        for pat in ser.get('patches') or []:
            commit, branch = commits.get(norm_subject(pat.get('name')),
                                         (None, None))
            out[pat['id']] = {'commit': commit, 'branch': branch,
                              'state': states.get(pat['id'])}
    return out


def _reviews_by_title(rcser, reviews):
    """Get the reviewed series by title, to find new versions of them

    Args:
        rcser (Cseries): Open cseries instance with the reviews
        reviews (list of Series): Reviewed series

    Return:
        dict: key (str): Normalised title (see _norm_title())
            value (tuple):
                int: Series ID
                SerVer: Latest version reviewed
                int: Number of replies waiting for a response
    """
    latest = _latest_versions(rcser, reviewed=True)
    waiting = rcser.db.reply_count_waiting()
    by_title = {}
    for ser in reviews:
        # A series may have been applied but not reviewed, e.g. with -a
        svr = latest.get(ser.idnum)
        if svr:
            by_title.setdefault(_norm_title(ser.desc), (
                ser.idnum, svr, waiting.get(ser.idnum, 0)))
    return by_title


def _earlier_info(rcser, found):
    """Get your review of an earlier version of a series in the inbox

    This comes from the database, so that the inbox can show the earlier
    version without reading it from patchwork

    Args:
        rcser (Cseries): Open cseries instance with the reviews
        found (tuple or None): Reviewed series, as from _reviews_by_title()

    Return:
        dict or None: None if not found, else the series 'id', the 'version'
            reviewed, its patchwork 'link', the 'date' of the latest review,
            the number of 'replies' waiting for a response and its 'parts'.
            Each part has its 'seq' (0 for the cover letter), 'name', the
            patchwork 'mail_id' of the patch or cover letter, the 'status'
            of your review of it (None if there is none), whether it is
            'approved' and the number of 'replies' waiting
    """
    if not found:
        return None
    idnum, svr, replies = found
    parts, date = _review_parts(rcser, svr)
    return {'id': idnum, 'version': svr.version, 'link': svr.link,
            'date': date, 'replies': replies, 'parts': parts}


def _review_parts(rcser, svr):
    """Get the parts of a version which you reviewed, with your reviews

    Args:
        rcser (Cseries): Open cseries instance with the reviews
        svr (SerVer): Version reviewed

    Return:
        tuple:
            list of dict: One for each part, with its 'seq' (0 for the cover
                letter), 'name', the patchwork 'mail_id' of the patch or
                cover letter, the 'status' of your review of it (None if
                there is none), whether it is 'approved' and the number of
                'replies' waiting
            str or None: When the latest review was written, if any
    """
    out = []
    reviews = {rev.seq: rev for rev in
               rcser.db.review_get_for_version(svr.idnum)}
    waiting = Counter(rp.seq for rp in rcser.db.reply_get_comments(svr.idnum)
                      if rp.status in database.REPLY_WAITING)
    for rev in reviews.values():
        waiting[rev.seq] += sum(
            rp.status in database.REPLY_WAITING
            for rp in rcser.db.reply_get_for_review(rev.idnum))
    parts = {pcm.seq + 1: (pcm.subject, pcm.patch_id)
             for pcm in rcser.db.pcommit_get_list(svr.idnum)}
    if svr.cover_id or 0 in reviews:
        parts[0] = ('Cover letter', svr.cover_id)
    for seq in sorted(set(parts) | set(reviews)):
        name, mail_id = parts.get(seq, (f'Patch {seq}', None))
        rev = reviews.get(seq)
        out.append({
            'seq': seq, 'name': name, 'mail_id': mail_id,
            'status': (rev.status or 'new') if rev else None,
            'approved': bool(rev and rev.approved),
            'replies': waiting[seq]})
    return out, max((rev.timestamp for rev in reviews.values()
                     if rev.timestamp), default=None)


def _count(num, one, many):
    """Give a count of things, e.g. '1 patch' or '2 patches'

    Args:
        num (int): Number of things
        one (str): Name of one, e.g. 'patch'
        many (str): Name of more than one, e.g. 'patches'

    Return:
        str: The count
    """
    return f'{num} {one if num == 1 else many}'


def _waiting_on_me(parts):
    """Say why a series which you reviewed is waiting on you, if it is

    Args:
        parts (list of dict): Parts of the version, from _review_parts()

    Return:
        dict or None: None if it is not, else the 'state', which is 'me',
            and 'why'
    """
    replies = sum(part['replies'] for part in parts)
    if replies:
        return {'state': 'me', 'why': f"{_count(replies, 'reply', 'replies')} "
                'waiting for your response'}
    unsent = sum(part['status'] in REVIEW_UNSENT for part in parts)
    if unsent:
        return {'state': 'me',
                'why': f"{_count(unsent, 'review', 'reviews')} not sent"}
    return None


def next_move(parts, sent, newer, now):
    """Say who has the next move on a series which you reviewed

    The series is waiting on you if a reply needs your response or a review
    is not sent yet. Otherwise it is ready if your reviews give each patch a
    Reviewed-by tag, or waiting on the submitter for a new version if any
    asks for changes, and stale once it has waited for STALE_DAYS since you
    sent the review. A series with a newer version has no next move: the
    newer one has it

    Args:
        parts (list of dict): Parts of the version, from _review_parts()
        sent (str or None): When the last review was sent, e.g.
            '2026-10-01 10:00:00'
        newer (bool): True if there is a newer version of the series
        now (datetime): Time now

    Return:
        dict or None: None if nothing is waiting, else the 'state' ('me',
            'submitter', 'stale' or 'ready') and 'why', saying why
    """
    mine = _waiting_on_me(parts)
    if mine or newer:
        return mine
    patches = [part for part in parts if part['seq']]
    if patches and all(part['approved'] for part in patches):
        return {'state': 'ready',
                'why': 'Your reviews give every patch a Reviewed-by tag'}
    asked = sum(part['status'] in database.REVIEW_DONE and not part['approved']
                for part in patches)
    if not asked:
        return None
    why = (f"You asked for changes to {_count(asked, 'patch', 'patches')} and "
           'there is no new version yet')
    if sent:
        days = (now - datetime.strptime(sent[:19],
                                        '%Y-%m-%d %H:%M:%S')).days
        if days >= STALE_DAYS:
            return {'state': 'stale',
                    'why': f'{why}, {days} days after your review'}
    return {'state': 'submitter', 'why': why}


def _newest_versions(series_list):
    """Get the newest version of each series in a list

    Args:
        series_list (list of dict): Series from patchwork

    Return:
        dict: key (str): Normalised title (see _norm_title())
            value (int): Newest version of the series with that title
    """
    newest = {}
    for data in series_list:
        title = _norm_title(data.get('name'))
        newest[title] = max(newest.get(title, 0), data.get('version') or 1)
    return newest


def _reviewed_next(rcser, svid, data, newest):
    """Say who has the next move on a version which you reviewed

    Args:
        rcser (Cseries): Open cseries instance with the reviews
        svid (int): ser_ver ID of the version
        data (dict): The version, from patchwork
        newest (dict): Newest version of each series, from
            _newest_versions()

    Return:
        dict or None: As from next_move()
    """
    parts, _ = _review_parts(rcser, rcser.db.ser_ver_get_by_id(svid))
    newer = (newest[_norm_title(data.get('name'))] >
             (data.get('version') or 1))
    return next_move(parts, rcser.db.review_last_sent(svid), newer,
                     rcser.get_now())


def get_inbox(cser, rcser, series_list, status=None,
              own_emails=frozenset()):
    """Get information about recent series on patchwork, for review

    Args:
        cser (Cseries): Open cseries instance with your own series
        rcser (Cseries): Open cseries instance with the reviews
        series_list (list of dict): Series from patchwork, newest first
        status (dict or None): What is known about each patch, as returned
            by get_patch_status(), or None if nothing is
        own_emails (set of str): Your email addresses, in lower case

    Return:
        list of dict: One item for each series. Each of its 'patches' has
            its 'id' and 'name', with the 'commit', 'branch' and 'state' from
            'status', or None. 'earlier' is None, or your review of an
            earlier version (see _earlier_info()). 'mine' is True if you
            sent the series (see lookup.is_own_series()). 'next' says who
            has the next move on a series which you reviewed, or is None
            (see next_move())
    """
    status = status or {}
    reviews = rcser.db.series_get_dict(include_archived=True,
                                       reviews_only=True).values()
    review_ids = {ser.idnum for ser in reviews}
    by_title = _reviews_by_title(rcser, reviews)
    newest = _newest_versions(series_list)
    out = []
    for data in series_list:
        link = str(data['id'])
        found = rcser.db.series_find_by_link(link)
        # A version which was applied but not reviewed, e.g. with -a, can
        # be reviewed as if it were new
        reviewed = (found[0] if found and found[0] in review_ids and
                    rcser.db.review_get_for_version(found[3]) else None)
        submitter = data.get('submitter') or {}
        out.append({
            'id': data['id'],
            'name': data.get('name') or '',
            'version': data.get('version'),
            'date': data.get('date'),
            'submitter': submitter.get('name') or submitter.get('email'),
            'total': data.get('total'),
            'received_all': data.get('received_all'),
            'web_url': data.get('web_url'),
            'cover_id': (data.get('cover_letter') or {}).get('id'),
            'patches': [{'id': pat['id'], 'name': pat.get('name'),
                         **status.get(pat['id'], UNKNOWN_STATUS)}
                        for pat in data.get('patches') or []],
            'reviewed': reviewed,
            'earlier': (None if reviewed else _earlier_info(
                rcser, by_title.get(_norm_title(data.get('name'))))),
            'mine': lookup.is_own_series(data, own_emails, cser.db),
            'next': reviewed and _reviewed_next(rcser, found[3], data, newest),
        })
    return out


def get_todo_list(cser):
    """Get the scheduled todos, whether due or not

    Args:
        cser (Cseries): Open cseries instance

    Return:
        list of dict: One item per todo, earliest first
    """
    now = cser.get_now()
    out = []
    for series_id, name, desc, timestamp in cser.db.workflow_get_by_type(
            Wtype.TODO):
        when = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
        out.append({'series_id': series_id, 'name': name, 'desc': desc,
                    'timestamp': timestamp, 'due': when <= now})
    return out
