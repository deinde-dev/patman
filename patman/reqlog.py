# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Log each request which patman sends to patchwork or lore

When a command fails because a server does not respond, or responds with an
error, the message says only what went wrong in the end. To find out what
happened before that, e.g. how many tries each request took, how long they
took and which other commands were reading the server at the time, patman
writes a line for each try to a log file shared by all its commands and by
'patman serve', e.g.:

    2026-11-01T10:14:05+00:00 pid=1234 review try=1 502 120B 0.84s
        https://patchwork.ozlabs.org/api/1.2/series/529790/

(on one line). The log is kept beside the patchwork cache. When it grows past
MAX_SIZE it is renamed with '.1' on the end, replacing the one before, so it
takes at most twice that.
"""

from datetime import datetime
import os
import time

from u_boot_pylib import tout

# Size at which the log is started again, keeping the old one as '<log>.1'
MAX_SIZE = 10 << 20

# Path of the log, None if not worked out yet, or False if there is none
_path = None  # pylint: disable=C0103

# What this process is doing, e.g. 'review' or 'series status'
_command = '-'  # pylint: disable=C0103


def default_path():
    """Get the path of the log to write

    Returns:
        str or None: Path, or None if no log is to be written
    """
    path = os.environ.get('PATMAN_REQLOG')
    if path == 'off':
        return None
    if path:
        return os.path.expanduser(path)
    base = (os.environ.get('XDG_CACHE_HOME') or
            os.path.expanduser('~/.cache'))
    return os.path.join(base, 'patman', 'requests.log')


def reset():
    """Work out the path of the log again, e.g. after PATMAN_REQLOG changes"""
    global _path  # pylint: disable=W0603

    _path = None


def set_command(command):
    """Say what this process is doing, for the lines it logs

    Only the name of the command is logged, not its arguments, since they
    may hold a token

    Args:
        command (str): Command, e.g. 'review' or 'series status'
    """
    global _command  # pylint: disable=W0603

    _command = command.replace(' ', '-') or '-'


def _describe(exc):
    """Describe an exception on one line

    Args:
        exc (BaseException): Exception

    Returns:
        str: Its type and message, e.g. 'TimeoutError'
    """
    msg = ' '.join(str(exc).split())
    return f'{type(exc).__name__}: {msg}' if msg else type(exc).__name__


def write(line):
    """Add a line to the log

    If the log cannot be written, patman says so once and logs no more

    Args:
        line (str): Line to add, without the newline
    """
    global _path  # pylint: disable=W0603

    if _path is None:
        _path = default_path() or False
    if not _path:
        return
    try:
        if os.path.exists(_path) and os.path.getsize(_path) > MAX_SIZE:
            os.replace(_path, f'{_path}.1')
        os.makedirs(os.path.dirname(_path), exist_ok=True)
        with open(_path, 'a', encoding='utf-8') as outf:
            outf.write(line + '\n')
    except OSError as exc:
        tout.warning(f"Cannot write the request log '{_path}': {exc}")
        _path = False


class Attempt:
    """One try at a request, which is logged when it ends

    Use it as a context manager around the request, calling done() when the
    response arrives. If the request raises an exception before that, the
    exception is logged instead, and still raised.

    Properties:
        url (str): URL being read
        tries (int): Number of this try, from 1
        start (float): time.monotonic() when the try started
        status (int or None): HTTP status of the response, if any
        nbytes (int): Number of bytes received
    """
    def __init__(self, url, tries):
        self.url = url
        self.tries = tries
        self.start = time.monotonic()
        self.status = None
        self.nbytes = 0

    def done(self, status, nbytes):
        """Note the response

        Args:
            status (int): HTTP status
            nbytes (int): Number of bytes received
        """
        self.status = status
        self.nbytes = nbytes

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self.status is not None:
            outcome = str(self.status)
        elif exc is not None:
            outcome = _describe(exc)
        else:
            outcome = 'no-response'
        when = datetime.now().astimezone().isoformat(timespec='seconds')
        secs = time.monotonic() - self.start
        write(f'{when} pid={os.getpid()} {_command} try={self.tries} '
              f'{outcome} {self.nbytes}B {secs:.2f}s {self.url}')
        return False


def attempt(url, tries):
    """Start logging a try at a request

    Args:
        url (str): URL being read
        tries (int): Number of this try, from 1

    Returns:
        Attempt: Context manager for the try
    """
    return Attempt(url, tries)
