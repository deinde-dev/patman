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
PatchInfo = namedtuple('PatchInfo', 'files,changed,tags,to_me,roles')

# The first line of the diff of a file
RE_DIFF = re.compile(r'^diff --git a/(\S+) b/(\S+)')

# The start of a hunk, with the number of old and new lines in it
RE_HUNK = re.compile(r'^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@')

# A tag in a commit message, e.g. 'Reviewed-by: Fred <fred@x.org>'
RE_TAG = re.compile(r'^([A-Za-z][A-Za-z0-9-]*):\s')


def parse_patch(text):
    """Find the files, changed lines and tags of a patch

    Args:
        text (str): The patch, as in its email: the commit message, then
            '---' and the diff

    Returns:
        tuple:
            list of str: Files touched, in order
            list of str: Lines added or removed, without the + or -
            set of str: Tags in the commit message, in lower case
    """
    files = []
    changed = []
    tags = set()
    in_msg = True
    old = new = 0
    for line in text.splitlines():
        match = RE_DIFF.match(line)
        if match:
            in_msg = False
            old = new = 0
            for name in match.groups():
                if name not in files:
                    files.append(name)
        elif in_msg:
            if line == '---':
                in_msg = False
            elif RE_TAG.match(line):
                tags.add(RE_TAG.match(line).group(1).lower())
        elif RE_HUNK.match(line):
            counts = RE_HUNK.match(line).groups()
            old, new = (int(num) if num else 1 for num in counts)
        elif old or new:
            if line.startswith('-'):
                old -= 1
                changed.append(line[1:])
            elif line.startswith('+'):
                new -= 1
                changed.append(line[1:])
            elif not line.startswith('\\'):
                old -= 1
                new -= 1
    return files, changed, tags


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
    files = []
    changed = []
    tags = set()
    to_me = False
    for msg in lore.parse_mbox(mbox):
        pfiles, pchanged, ptags = parse_patch(_body(msg))
        files += [name for name in pfiles if name not in files]
        changed += pchanged
        tags |= ptags
        heads = [str(val) for val in msg.get_all('to', []) +
                 msg.get_all('cc', [])]
        to_me = to_me or any(addr.lower() in emails
                             for _, addr in email.utils.getaddresses(heads))
    roles = maintainers.roles(sections, files, emails) if sections else set()
    return PatchInfo(files, changed, tags, to_me, roles)


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
