# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2025 Google LLC
#

"""Workflow types and operations for patman series management"""

from datetime import datetime, timedelta
import enum

from patman import times


class Wtype(str, enum.Enum):
    """Types of workflow entry"""
    SENT = 'sent'
    TODO = 'todo'
    REVIEWED = 'reviewed'


def friendly_time(now, when):
    """Format a timestamp in a human-friendly way

    Args:
        now (datetime): Current time
        when (datetime): Timestamp to format

    Return:
        str: Friendly string, e.g. 'Tue 15:34', '3d ago', '2w ago'
    """
    delta = now - when
    days = delta.days
    if days < 0:
        days = -days
        if days >= 14:
            return f'in {days // 7}w'
        if days >= 7:
            return f'in {days}d'
        return when.strftime('%a %H:%M')
    if days == 0:
        return when.strftime('%H:%M')
    if days < 7:
        return when.strftime('%a %H:%M')
    if days < 14:
        return f'{days}d ago'
    return f'{days // 7}w ago'


def sent(cser, series_id, ser_ver_id=None):
    """Record that a series was sent and create a follow-up todo

    Args:
        cser (CseriesHelper): Series helper with open database
        series_id (int): ID of the series that was sent
        ser_ver_id (int or None): ID of the ser_ver record that was sent
    """
    ts = times.to_str(cser.get_now())
    cser.db.workflow_archive(Wtype.SENT, series_id)
    cser.db.workflow_add(Wtype.SENT, series_id, ts, ser_ver_id=ser_ver_id)
    when = cser.get_now() + timedelta(days=7)
    todo_ts = times.to_str(when)
    cser.db.workflow_archive(Wtype.TODO, series_id)
    cser.db.workflow_add(Wtype.TODO, series_id, todo_ts)
    cser.commit()


def reviewed(cser, series_id, ser_ver_id=None):
    """Record that a series was reviewed and create a follow-up todo

    Args:
        cser (CseriesHelper): Series helper with open database
        series_id (int): ID of the series that was reviewed
        ser_ver_id (int or None): ID of the ser_ver record
    """
    ts = times.to_str(cser.get_now())
    cser.db.workflow_archive(Wtype.REVIEWED, series_id)
    cser.db.workflow_add(Wtype.REVIEWED, series_id, ts, ser_ver_id=ser_ver_id)
    when = cser.get_now() + timedelta(days=7)
    todo_ts = times.to_str(when)
    cser.db.workflow_archive(Wtype.TODO, series_id)
    cser.db.workflow_add(Wtype.TODO, series_id, todo_ts)
    cser.commit()


def todo(cser, series, days):
    """Mark a series as a todo item after a number of days

    Args:
        cser (CseriesHelper): Series helper with open database
        series (str): Name of series to use, or None for current branch
        days (int): Number of days from now to mark as due
    """
    ser = cser._parse_series(series)
    cser.db.workflow_archive(Wtype.TODO, ser.idnum)
    when = cser.get_now() + timedelta(days=days)
    ts = times.to_str(when)
    cser.db.workflow_add(Wtype.TODO, ser.idnum, ts)
    cser.commit()
    print(f"Series '{ser.name}' marked for todo on {ts}")


def todo_clear(cser, series):
    """Clear the todo marker for a series

    Args:
        cser (CseriesHelper): Series helper with open database
        series (str): Name of series to use, or None for current branch
    """
    ser = cser._parse_series(series)
    cser.db.workflow_archive(Wtype.TODO, ser.idnum)
    cser.commit()
    print(f"Todo cleared for series '{ser.name}'")


def todo_list(cser, show_all):
    """List series that are due (or scheduled) for attention

    Args:
        cser (CseriesHelper): Series helper with open database
        show_all (bool): True to show all scheduled todos, not just
            those that are due
    """
    now = times.to_str(cser.get_now())
    before = None if show_all else now
    entries = cser.db.workflow_get_by_type(Wtype.TODO, before=before)
    if not entries:
        if show_all:
            print('No todos scheduled')
        else:
            print('No todos due')
        return
    print(f"{'Series':17}  {'Due':>14}  Description")
    print(f"{'-' * 17}  {'-' * 14}  {'-' * 30}")
    for _sid, name, desc, ts in entries:
        when = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
        delta = when - cser.get_now()
        days = delta.days
        if days < 0:
            due = f'{-days}d overdue'
        elif days == 0:
            due = 'today'
        else:
            due = f'in {days}d'
        print(f"{name:17}  {due:>14}  {desc}")


def list_entries(cser, show_all):
    """List all workflow entries

    Args:
        cser (CseriesHelper): Series helper with open database
        show_all (bool): True to include archived entries
    """
    entries = cser.db.workflow_list(include_archived=show_all)
    if not entries:
        print('No workflow entries')
        return
    hdr = f"{'Type':6}  {'Series':17}  {'Ver':>3}  {'When':>10}"
    div = f"{'-' * 6}  {'-' * 17}  {'-' * 3}  {'-' * 10}"
    if show_all:
        hdr += '  A'
        div += '  -'
    hdr += '  Description'
    div += '  ' + '-' * 30
    print(hdr)
    print(div)
    now = cser.get_now()
    for wtype, name, desc, ts, archived, version in entries:
        when = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
        friendly = friendly_time(now, when)
        ver = f'v{version}' if version else ''
        line = f"{wtype:6}  {name:17}  {ver:>3}  {friendly:>10}"
        if show_all:
            line += f"  {'*' if archived else ' '}"
        line += f"  {desc}"
        print(line)
