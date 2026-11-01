# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""What the patches of a series say, for the rules which suggest series

The rules which need the patches of a series, such as which files it
touches, read them as an mbox, in one request for the whole series. This
finds what those rules need from it.
"""

from collections import namedtuple
import email.utils
import re

from patman import lore
from patman.review import maintainers


# What the tests which need the patches know about a series:
#   files (list of str): Files which the patches touch
#   changed (list of str): Lines which they add or remove, without the + or -
#   tags (set of str): Tags in their commit messages, in lower case, e.g.
#       'reviewed-by', including those patchwork collected from replies
#   to_me (bool): True if you are in To: or Cc: of a patch
#   roles (set of str): Your roles in MAINTAINERS for the files, 'M' or 'R'
#   fixes (list of str): Commits named by Fixes: tags, as given
#   removed (dict): Lines which the patches remove or change, for 'blame':
#       key (str): File, as it is before the patch
#       value (list of int): Line numbers in that file, from 1
#   base (str or None): Commit which the series is based on, from a
#       'base-commit:' trailer
#   own_fixes (list of str): Those of 'fixes' which are your commits, once
#       the history has been checked (see history.own_commits())
PatchInfo = namedtuple(
    'PatchInfo', 'files,changed,tags,to_me,roles,fixes,removed,base,own_fixes',
    defaults=([], {}, None, []))

# What a patch says, from parse_patch(): its 'files', 'changed' lines and
# 'tags', with the 'fixes' and 'removed' lines, as for PatchInfo
Patch = namedtuple('Patch', 'files,changed,tags,fixes,removed')

# The first line of the diff of a file
RE_DIFF = re.compile(r'^diff --git a/(\S+) b/(\S+)')

# The start of a hunk, with the first old line and the number of old and new
# lines in it
RE_HUNK = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+\d+(?:,(\d+))? @@')

# A tag in a commit message, e.g. 'Reviewed-by: Fred <fred@x.org>'
RE_TAG = re.compile(r'^([A-Za-z][A-Za-z0-9-]*):\s')

# A Fixes: tag, with the commit it names, e.g. 'Fixes: 0123456789ab ("...")'
RE_FIXES = re.compile(r'^Fixes:\s*([0-9a-f]{7,40})\b', re.I)

# A 'base-commit:' trailer, added by 'git format-patch --base', b4 and patman
RE_BASE = re.compile(r'^base-commit:\s*([0-9a-f]{7,40})\s*$', re.M)


def _msg_line(line, out):
    """Note what a line of the commit message of a patch says

    Args:
        line (str): Line
        out (Patch): What the patch says so far, to add the line's tag to

    Returns:
        bool: True if the message goes on, False if the line ends it
    """
    if line == '---':
        return False
    match = RE_TAG.match(line)
    if match:
        out.tags.add(match.group(1).lower())
        fixes = RE_FIXES.match(line)
        if fixes:
            out.fixes.append(fixes.group(1))
    return True


def parse_patch(text):
    """Find the files, changed lines and tags of a patch

    Args:
        text (str): The patch, as in its email: the commit message, then
            '---' and the diff

    Returns:
        Patch: What the patch says
    """
    out = Patch([], [], set(), [], {})
    in_msg = True
    old = new = 0
    for line in text.splitlines():
        match = RE_DIFF.match(line)
        if match:
            in_msg = False
            old = new = 0
            removed = out.removed.setdefault(match.group(1), [])
            for name in match.groups():
                if name not in out.files:
                    out.files.append(name)
        elif in_msg:
            in_msg = _msg_line(line, out)
        elif RE_HUNK.match(line):
            start, *counts = RE_HUNK.match(line).groups()
            old, new = (int(num) if num else 1 for num in counts)
            num = int(start)
        elif old or new:
            if line.startswith('-'):
                old -= 1
                out.changed.append(line[1:])
                removed.append(num)
                num += 1
            elif line.startswith('+'):
                new -= 1
                out.changed.append(line[1:])
            elif not line.startswith('\\'):
                old -= 1
                new -= 1
                num += 1
    for name in [name for name, lines in out.removed.items() if not lines]:
        del out.removed[name]
    return out


def _body(msg):
    """Get the text of an email

    Args:
        msg (email.message.EmailMessage): Email

    Returns:
        str: Its plain-text body, or '' if it has none which can be read
    """
    part = msg.get_body(preferencelist=('plain',)) or msg
    try:
        return part.get_content()
    except (KeyError, LookupError, ValueError):
        payload = part.get_payload(decode=True) or b''
        return payload.decode('utf-8', errors='replace')


def _add_patch(total, patch):
    """Add what a patch says to what the series says

    Args:
        total (Patch): What the patches so far say, to add to
        patch (Patch): What the patch says
    """
    total.files.extend([name for name in patch.files
                        if name not in total.files])
    total.changed.extend(patch.changed)
    total.tags.update(patch.tags)
    total.fixes.extend([cid for cid in patch.fixes if cid not in total.fixes])
    for name, lines in patch.removed.items():
        total.removed.setdefault(name, []).extend(lines)


def patch_info(mbox, emails, sections):
    """Get what the tests which need the patches know about a series

    Args:
        mbox (bytes): The patches of the series
        emails (set of str): Your addresses, in lower case
        sections (list of Section or None): MAINTAINERS sections, as from
            maintainers.read(), or None if no rule needs them

    Returns:
        PatchInfo: What is known
    """
    total = Patch([], [], set(), [], {})
    to_me = False
    base = None
    for msg in lore.parse_mbox(mbox):
        body = _body(msg)
        _add_patch(total, parse_patch(body))
        match = RE_BASE.search(body)
        base = base or (match.group(1) if match else None)
        heads = [str(val) for val in msg.get_all('to', []) +
                 msg.get_all('cc', [])]
        to_me = to_me or any(addr.lower() in emails
                             for _, addr in email.utils.getaddresses(heads))
    roles = (maintainers.roles(sections, total.files, emails) if sections
             else set())
    return PatchInfo(total.files, total.changed, total.tags, to_me, roles,
                     total.fixes, total.removed, base)


# What each wildcard in a file pattern matches
GLOB_PARTS = {'**': '.*', '*': '[^/]*', '?': '[^/]'}


def glob_match(pattern, path):
    """Check whether a file matches a pattern from a rule

    '**' matches any characters, '*' and '?' any but '/', and a pattern
    ending in '/' matches everything below it

    Args:
        pattern (str): Pattern, e.g. 'drivers/i2c/**' or 'boot/*.c'
        path (str): Path of the file from the top of the tree

    Returns:
        bool: True if it matches

    >>> glob_match('drivers/i2c/**', 'drivers/i2c/busses/x.c')
    True
    >>> glob_match('drivers/i2c/*', 'drivers/i2c/busses/x.c')
    False
    >>> glob_match('drivers/i2c/', 'drivers/i2c/busses/x.c')
    True
    """
    if pattern.endswith('/'):
        pattern += '**'
    out = ''.join(GLOB_PARTS.get(part) or re.escape(part)
                  for part in re.split(r'(\*\*|\*|\?)', pattern))
    return bool(re.fullmatch(out, path))
