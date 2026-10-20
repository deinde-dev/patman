# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Keep count of the requests which patman sends to patchwork servers

A busy patchwork server may block a client which sends it a lot of
requests, so patman notes how many it sends, and how much it receives, in
the 'traffic' table of the database, in hourly totals for each server and
kind of request, e.g. 'patch-comments' (see patchwork.request_kind()).
The counts are kept in memory and written out when patman exits, or by the
server after each request it handles, since patchwork may be read after
the database is closed.
"""

import atexit
from collections import defaultdict
from datetime import datetime, timedelta
import os

# Counts not yet written to the database
#   key (tuple of str): URL of the patchwork server, kind of request
#   value (list of int): [number of requests, bytes received]
_counts = defaultdict(lambda: [0, 0])

# Path to the database which receives the counts at exit, or None if none
# has been opened
_db_path = None  # pylint: disable=C0103

# Days of counts to keep in the database
KEEP_DAYS = 90


def add(url, requests=0, nbytes=0, kind=''):
    """Count requests sent to a patchwork server, or bytes received

    Args:
        url (str): URL of the patchwork server
        requests (int): Number of requests sent
        nbytes (int): Number of bytes received
        kind (str): Kind of request, e.g. 'patch-comments', or '' if not
            known
    """
    item = _counts[url, kind]
    item[0] += requests
    item[1] += nbytes


def take():
    """Get the counts not yet written out, and forget them

    Return:
        dict: key (tuple of str): URL of the patchwork server, kind of
                request
            value (tuple): (number of requests, bytes received)
    """
    out = {key: tuple(item) for key, item in _counts.items()}
    _counts.clear()
    return out


def hour_key(when=None):
    """Get the key of the hour which holds a time, in the traffic table

    Args:
        when (datetime or None): Time, or None for now

    Return:
        str: Hour, e.g. '2026-10-19 08'
    """
    return (when or datetime.now()).strftime('%Y-%m-%d %H')


def save(db):
    """Write the counts not yet written out to a database

    Args:
        db (Database): Open database
    """
    counts = take()
    if not counts:
        return
    now = datetime.now()
    db.traffic_add(hour_key(now), counts,
                   hour_key(now - timedelta(days=KEEP_DAYS)))
    db.commit()


def set_db(db_path):
    """Note the database to write the counts to when patman exits

    The first database opened is used, which is the main one.

    Args:
        db_path (str): Path to the database
    """
    global _db_path  # pylint: disable=W0603

    if _db_path is None:
        _db_path = db_path
        atexit.register(_save_at_exit)


def _save_at_exit():
    """Write any counts not yet written out to the database

    Nothing is written if the database is still open, since a command may
    have left changes in it which it did not mean to keep, nor if it has
    gone, as happens in tests.
    """
    # pylint: disable=C0415
    from patman.database import Database

    if not _counts or not _db_path or not os.path.exists(_db_path):
        return
    db, _ = Database.get_instance(_db_path)
    if db.is_open:
        return
    try:
        db.open_it()
        save(db)
    except Exception:  # pylint: disable=W0718
        pass
    finally:
        if db.is_open:
            db.close()


def get_summary(db, now=None, cache=None):
    """Get the traffic to each patchwork server over recent periods

    Args:
        db (Database): Open database
        now (datetime or None): Current time, or None for now
        cache (pwcache.Cache or None): Cache whose counts of the reads it
            answers are added, or None for none

    Return:
        list of dict: One item for each server, ordered by URL, with:
            url (str): URL of the patchwork server
            hour, today, week (list of int): [requests, bytes] this hour,
                since midnight and in the last seven days
            cached (dict): Number of reads answered by the cache in each of
                these periods, keyed by 'hour', 'today' and 'week'
            kinds (dict): The requests of each kind, e.g. 'patch-comments',
                today and in the last seven days: key (str): kind
                value (dict): [requests, bytes] keyed by 'today' and 'week'.
                Requests counted before kinds were recorded are left out
    """
    now = now or datetime.now()
    periods = {
        'hour': hour_key(now),
        'today': now.strftime('%Y-%m-%d 00'),
        'week': hour_key(now - timedelta(days=7)),
    }
    out = {}

    def item(url):
        return out.setdefault(url, {
            'url': url, 'hour': [0, 0], 'today': [0, 0], 'week': [0, 0],
            'cached': {'hour': 0, 'today': 0, 'week': 0}, 'kinds': {}})

    for name, since in periods.items():
        for url, (requests, nbytes) in db.traffic_get(since).items():
            item(url)[name] = [requests, nbytes]
        if name != 'hour':
            for (url, kind), counts in db.traffic_get_kinds(since).items():
                if kind:
                    item(url)['kinds'].setdefault(
                        kind, {'today': [0, 0], 'week': [0, 0]})[name] = \
                        list(counts)
        if cache:
            for url, count in cache.hits_since(since).items():
                item(url)['cached'][name] = count
    return [out[url] for url in sorted(out)]
