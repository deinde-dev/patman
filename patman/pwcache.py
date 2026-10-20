# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Keep a copy of what patman reads from patchwork servers

Patchwork servers can be slow and may block a client which sends them a lot
of requests, so patman keeps what it reads in a cache, to use again rather
than asking the server each time.

The cache is an SQLite database of its own, separate from the .patman.db
databases, since it holds nothing which cannot be read again: it can be
deleted at any time, and its layout can change without a migration. It is
shared by every repo and database, and by 'patman serve' and the jobs it
runs, which SQLite allows since each write is a short transaction.

Each entry is a response from the patchwork API, keyed by the server's URL
and the path of the request, e.g. 'patches/123/', and stored as patchwork
returns it, compressed. The caller says how old an entry may be when it reads
one, since that depends on what it wants: the text of a patch never changes,
but its state does.

Each read which the cache answers is counted, in hourly totals for each
server, for showing with the patchwork traffic (see traffic).

The cache also records how far it has followed the events of a project on
a server, e.g. a patch's state changing, so that a patch read since it began
following them can be used however old it is, being dropped when an event
says that it has changed (see Patchwork.follow_events()). Entries which
are not used for KEEP_DAYS are dropped, which is checked once a day.

If the cache stops working, e.g. because the disk is full or the file is
damaged, this is reported once and the cache is then left alone, so that
patman reads patchwork as it would with no cache, rather than failing.

PATMAN_PWCACHE sets the file to use; set it to 'off' to use no cache.
"""

from datetime import datetime
import functools
import json
import os
import sqlite3
import time
import zlib

from u_boot_pylib import tout

from patman import traffic

# Version of the layout of the cache. When this changes, the old cache is
# thrown away rather than migrated
LAYOUT = 1

# Days for which an entry is kept since it was last used, and for which the
# counts of reads answered by the cache are kept
KEEP_DAYS = 90

# Seconds between checks for entries to drop
PRUNE_INTERVAL = 24 * 60 * 60

# The cache in use, or None if not yet opened, or False if there is none
_cache = None  # pylint: disable=C0103

# Errors which mean that the cache cannot be used: SQLite failing, e.g.
# because the disk is full, or an entry which cannot be decoded
CACHE_ERRORS = (sqlite3.Error, zlib.error, ValueError)


def _safe(default):
    """Make a method of Cache act as an empty cache if the cache fails

    The first failure is reported, and the cache is not used after that,
    so that patman goes on as it would with no cache.

    Args:
        default: Value to return in place of the method's result

    Returns:
        Decorator for the method
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            if self.broken:
                return default
            try:
                return func(self, *args, **kwargs)
            except CACHE_ERRORS as exc:
                self.broken = True
                tout.warning(f"Cannot use the patchwork cache '{self.path}': "
                             f'{exc}; reading patchwork instead')
                return default
        return wrapper
    return decorator


class Cache:
    """A cache of responses from patchwork servers

    Args:
        path (str): Path to the SQLite file, which is created if needed
        clock (callable): Function returning the time in seconds, for tests
    """
    def __init__(self, path, clock=time.time):
        self.path = path
        self._clock = clock
        # True once the cache has failed, after which it is not used
        self.broken = False
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        self.con = sqlite3.connect(path, timeout=10)
        self.con.execute('PRAGMA journal_mode=WAL')
        self._setup()
        self._prune_if_due()

    def _setup(self):
        """Create the tables, throwing away a cache with an old layout"""
        layout = self.con.execute('PRAGMA user_version').fetchone()[0]
        if layout != LAYOUT:
            with self.con:
                for table in ('objects', 'hits', 'follow', 'meta'):
                    self.con.execute(f'DROP TABLE IF EXISTS {table}')
                self.con.execute(f'PRAGMA user_version = {LAYOUT}')
        with self.con:
            self.con.execute(
                'CREATE TABLE IF NOT EXISTS objects (server TEXT, path TEXT, '
                'data BLOB, fetched REAL, used REAL, '
                'PRIMARY KEY (server, path))')
            self.con.execute(
                'CREATE TABLE IF NOT EXISTS hits (hour TEXT, server TEXT, '
                'count INTEGER, bytes INTEGER DEFAULT 0, '
                'PRIMARY KEY (hour, server))')
            # A cache made before the size of each read was counted
            cols = [row[1] for row in self.con.execute(
                'PRAGMA table_info(hits)')]
            if 'bytes' not in cols:
                self.con.execute(
                    'ALTER TABLE hits ADD COLUMN bytes INTEGER DEFAULT 0')
            self.con.execute(
                'CREATE TABLE IF NOT EXISTS follow (server TEXT, project TEXT, '
                'started REAL, last TEXT, checked REAL, '
                'PRIMARY KEY (server, project))')
            self.con.execute(
                'CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, '
                'value REAL)')

    def _prune_if_due(self):
        """Drop old entries, if this has not been done for a while"""
        row = self.con.execute(
            "SELECT value FROM meta WHERE key = 'pruned'").fetchone()
        now = self._clock()
        if row and now - row[0] < PRUNE_INTERVAL:
            return
        self.prune()
        with self.con:
            self.con.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('pruned', ?)",
                (now,))

    @_safe(None)
    # pylint: disable-next=R0913
    def get(self, server, path, max_age, keep=None, since=None):
        """Get a response from the cache, if there is a recent enough one

        Args:
            server (str): URL of the patchwork server
            path (str): Path of the request, e.g. 'patches/123/'
            max_age (float or None): Oldest entry to accept, in seconds, or
                None to accept any
            keep (callable or None): Function which is passed the response
                and returns True if it is current however old it is, e.g.
                because it cannot change
            since (float or None): If not None, accept an entry read at or
                after this time, however old, if 'keep' (when given) also
                accepts it; used for entries which events keep current

        Returns:
            dict or list or None: Response, or None if there is none recent
                enough
        """
        row = self.con.execute(
            'SELECT data, fetched FROM objects WHERE server = ? AND path = ?',
            (server, path)).fetchone()
        if not row:
            return None
        blob, fetched = row
        now = self._clock()
        data = json.loads(zlib.decompress(blob))
        too_old = max_age is not None and (max_age == 0 or
                                           now - fetched > max_age)
        if since is not None:
            # Events keep the entry current, if it is read since they did
            current = fetched >= since and (not keep or keep(data))
        else:
            current = bool(keep and keep(data))
        if too_old and not current:
            return None
        with self.con:
            self.con.execute(
                'UPDATE objects SET used = ? WHERE server = ? AND path = ?',
                (now, server, path))
        return data

    @_safe(None)
    def fetched(self, server, path):
        """Get when a response was read from patchwork

        Args:
            server (str): URL of the patchwork server
            path (str): Path of the request, e.g. 'patches/123/'

        Returns:
            float or None: Time it was read, in seconds since the epoch, or
                None if it is not in the cache
        """
        row = self.con.execute(
            'SELECT fetched FROM objects WHERE server = ? AND path = ?',
            (server, path)).fetchone()
        return row[0] if row else None

    def age(self, server, path):
        """Get how long ago a response was read from patchwork

        Args:
            server (str): URL of the patchwork server
            path (str): Path of the request, e.g. 'patches/123/'

        Returns:
            float or None: Age in seconds, or None if it is not in the cache
        """
        fetched = self.fetched(server, path)
        return None if fetched is None else self._clock() - fetched

    def now(self):
        """Get the time, as the cache sees it

        Returns:
            float: Time in seconds since the epoch
        """
        return self._clock()

    @_safe(None)
    def drop(self, server, path):
        """Drop an entry, e.g. because an event says that it has changed

        Args:
            server (str): URL of the patchwork server
            path (str): Path of the request, e.g. 'patches/123/'
        """
        with self.con:
            self.con.execute(
                'DELETE FROM objects WHERE server = ? AND path = ?',
                (server, path))

    @_safe(None)
    def get_follow(self, server, project):
        """Get how far the events of a project have been followed

        Args:
            server (str): URL of the patchwork server
            project (str): Project link name, e.g. 'uboot'

        Returns:
            tuple or None: None if they are not followed, else:
                float: Time when following began; entries read since then
                    are kept current by the events
                str: Date of the newest event seen, as patchwork gives it
                float: Time when the events were last checked
        """
        return self.con.execute(
            'SELECT started, last, checked FROM follow '
            'WHERE server = ? AND project = ?', (server, project)).fetchone()

    @_safe(None)
    # pylint: disable-next=R0913
    def set_follow(self, server, project, started, last, checked):
        """Record how far the events of a project have been followed

        Args:
            server (str): URL of the patchwork server
            project (str): Project link name, e.g. 'uboot'
            started (float): Time when following began
            last (str): Date of the newest event seen
            checked (float): Time when the events were checked
        """
        with self.con:
            self.con.execute(
                'INSERT OR REPLACE INTO follow '
                '(server, project, started, last, checked) '
                'VALUES (?, ?, ?, ?, ?)',
                (server, project, started, last, checked))

    @_safe(None)
    def put(self, server, path, data):
        """Store a response in the cache

        Args:
            server (str): URL of the patchwork server
            path (str): Path of the request, e.g. 'patches/123/'
            data (dict or list): Response
        """
        now = self._clock()
        blob = zlib.compress(json.dumps(data).encode('utf-8'))
        with self.con:
            self.con.execute(
                'INSERT OR REPLACE INTO objects '
                '(server, path, data, fetched, used) VALUES (?, ?, ?, ?, ?)',
                (server, path, blob, now, now))

    @_safe(0)
    def prune(self, keep_days=KEEP_DAYS):
        """Drop the entries which have not been used for a while

        Args:
            keep_days (float): Days since an entry was last used for it to be
                kept

        Returns:
            int: Number of entries dropped
        """
        now = self._clock()
        with self.con:
            cur = self.con.execute('DELETE FROM objects WHERE used < ?',
                                   (now - keep_days * 86400,))
            self.con.execute('DELETE FROM hits WHERE hour < ?', (self._hour(
                now - keep_days * 86400),))
        return cur.rowcount

    def _hour(self, when):
        """Get the key of the hour which holds a time, as used by traffic

        Args:
            when (float): Time, in seconds since the epoch

        Returns:
            str: Hour, e.g. '2026-10-19 08'
        """
        return traffic.hour_key(datetime.fromtimestamp(when))

    @_safe(None)
    def add_hit(self, server, nbytes=0):
        """Count a read which the cache answers

        Args:
            server (str): URL of the patchwork server
            nbytes (int): Size of the response, which patchwork would
                otherwise have sent
        """
        with self.con:
            self.con.execute(
                'INSERT INTO hits (hour, server, count, bytes) '
                'VALUES (?, ?, 1, ?) ON CONFLICT (hour, server) DO UPDATE '
                'SET count = count + 1, bytes = bytes + excluded.bytes',
                (self._hour(self._clock()), server, nbytes))

    @_safe({})
    def hits_since(self, since):
        """Get the number of reads which the cache answers, since an hour

        Args:
            since (str): First hour to include, e.g. '2026-10-19 08'

        Returns:
            dict: key (str): URL of the patchwork server
                value (list of int): [number of reads, bytes they hold]
        """
        return {server: [count, nbytes] for server, count, nbytes in
                self.con.execute(
                    'SELECT server, SUM(count), SUM(bytes) FROM hits '
                    'WHERE hour >= ? GROUP BY server', (since,))}

    def close(self):
        """Close the cache"""
        self.con.close()


def default_path():
    """Get the path of the cache to use

    Returns:
        str or None: Path, or None if no cache is to be used
    """
    path = os.environ.get('PATMAN_PWCACHE')
    if path == 'off':
        return None
    if path:
        return os.path.expanduser(path)
    base = (os.environ.get('XDG_CACHE_HOME') or
            os.path.expanduser('~/.cache'))
    return os.path.join(base, 'patman', 'patchwork.db')


def get():
    """Get the cache to use, opening it the first time

    Returns:
        Cache or None: Cache, or None if no cache is to be used, or it
            cannot be opened
    """
    global _cache  # pylint: disable=W0603

    if _cache is None:
        path = default_path()
        try:
            _cache = Cache(path) if path else False
        except (OSError, sqlite3.Error) as exc:
            # Without a cache, patchwork is read each time, which still works
            tout.warning(f"Cannot open the patchwork cache '{path}': {exc}; "
                         'reading patchwork instead')
            _cache = False
    return _cache or None
