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

from patman.server.params import BadRequest
from patman.workflow import Wtype


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


def comment_info(comment):
    """Pick out the useful fields from a patchwork comment

    Args:
        comment (dict): Comment as returned by the patchwork API

    Return:
        dict: Comment information
    """
    submitter = comment.get('submitter') or {}
    return {
        'id': comment.get('id'),
        'date': comment.get('date'),
        'name': submitter.get('name'),
        'email': submitter.get('email'),
        'subject': comment.get('subject'),
        'content': comment.get('content'),
        'web_url': comment.get('web_url'),
    }


def _latest_versions(cser):
    """Get the ser_ver record for the latest version of each series

    Args:
        cser (Cseries): Open cseries instance

    Return:
        dict: key (int): series ID
            value (SerVer): latest version of that series
    """
    svdict = cser.get_ser_ver_dict()
    return {series_id: svdict[svid]
            for svid, series_id, _ in cser.db.series_get_all_max_versions()}


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
            'versions': versions}


def _norm_title(title):
    """Normalise a series title so that versions of a series can be matched

    Args:
        title (str): Title, e.g. '[v2] video: Add a driver'

    Return:
        str: Normalised title, e.g. 'video: add a driver'
    """
    return re.sub(r'^(\[[^]]*\]\s*)+', '', title or '').strip().lower()


def get_inbox(cser, rcser, series_list):
    """Get information about recent series on patchwork, for review

    Args:
        cser (Cseries): Open cseries instance with your own series
        rcser (Cseries): Open cseries instance with the reviews
        series_list (list of dict): Series from patchwork, newest first

    Return:
        list of dict: One item for each series
    """
    reviews = rcser.db.series_get_dict(include_archived=True,
                                       reviews_only=True).values()
    review_ids = {ser.idnum for ser in reviews}
    by_title = {}
    for ser in reviews:
        by_title.setdefault(_norm_title(ser.desc), ser.idnum)
    out = []
    for data in series_list:
        link = str(data['id'])
        found = rcser.db.series_find_by_link(link)
        reviewed = found[0] if found and found[0] in review_ids else None
        own = cser.db.series_find_by_link(link)
        cover = data.get('cover_letter')
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
            'cover_id': cover['id'] if cover else None,
            'patches': [{'id': pat['id'], 'name': pat.get('name')}
                        for pat in data.get('patches') or []],
            'reviewed': reviewed,
            'earlier': (None if reviewed else
                        by_title.get(_norm_title(data.get('name')))),
            'mine': bool(own) and (rcser is not cser or
                                   own[0] not in review_ids),
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
