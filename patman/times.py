# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Helpers for the times which patman records

Times in the database are local to the machine running patman, in the form
'YYYY-MM-DD HH:MM:SS', with no time zone, so that they sort as strings.
"""

from datetime import datetime

# Format of a time in the database
DB_FORMAT = '%Y-%m-%d %H:%M:%S'


def to_str(when):
    """Get a time in the form used in the database

    Args:
        when (datetime): Time

    Return:
        str: Time, e.g. '2026-10-19 08:30:00'
    """
    return when.strftime(DB_FORMAT)


def now_str():
    """Get the current time in the form used in the database

    Return:
        str: Time, e.g. '2026-10-19 08:30:00'
    """
    return to_str(datetime.now())


def utc_offset():
    """Get how far the local time zone is ahead of UTC

    Return:
        int: Offset in minutes, e.g. -360 for UTC-6
    """
    offset = datetime.now().astimezone().utcoffset()
    return int(offset.total_seconds() // 60)
