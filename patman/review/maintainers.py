# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Finding who looks after a file, from the MAINTAINERS files in a repo

A MAINTAINERS file has a section for each part of the tree, with a title
line followed by lines such as 'M: Name <address>' for a maintainer, 'R:'
for a reviewer, 'F:' for the files it covers, 'X:' for files it does not
and 'N:' for a regular expression which the path of a file it covers
matches. U-Boot has many of these files, e.g. one for each board, with paths
from the top of the tree, so all of them are read.

Patterns are matched as scripts/get_maintainer.pl matches them: '*' and '?'
are wildcards, a pattern ending in '/' covers everything below it, and
otherwise the pattern must be as deep as the file, so 'drivers/net/*' covers
the files in drivers/net but not those below it.
"""

from collections import namedtuple
import email.utils
import os
import re

from u_boot_pylib import command

# A section of a MAINTAINERS file:
#   title (str): Its title, e.g. 'ARM BROADCOM BCM283X'
#   people (list of tuple): (role, address) for each M: and R: line, where
#       the role is 'M' or 'R' and the address is in lower case
#   files (list of str): F: patterns
#   regexes (list of re.Pattern): N: patterns
#   excludes (list of str): X: patterns
Section = namedtuple('Section', 'title,people,files,regexes,excludes')

# A line of a section, e.g. 'F:	drivers/i2c/'
RE_ENTRY = re.compile(r'^([A-Z]):\s*(.*?)\s*$')


def _pattern_re(pattern):
    """Turn an F: or X: pattern into a regular expression

    Args:
        pattern (str): Pattern, e.g. 'drivers/net/*'

    Returns:
        re.Pattern: Expression which matches the start of a path
    """
    out = ''.join('.*' if char == '*' else '.' if char == '?' else
                  re.escape(char) for char in pattern)
    return re.compile(out)


def file_matches(pattern, path):
    """Check whether an F: or X: pattern covers a file

    Args:
        pattern (str): Pattern, e.g. 'drivers/net/' or 'drivers/net/*.c'
        path (str): Path of the file from the top of the tree

    Returns:
        bool: True if the pattern covers the file

    >>> file_matches('drivers/net/', 'drivers/net/phy/x.c')
    True
    >>> file_matches('drivers/net/*', 'drivers/net/x.c')
    True
    >>> file_matches('drivers/net/*', 'drivers/net/phy/x.c')
    False
    >>> file_matches('drivers/net', 'drivers/net/x.c')
    False
    >>> file_matches('*/net/*', 'drivers/net/x.c')
    True
    """
    if not _pattern_re(pattern).match(path):
        return False
    return pattern.endswith('/') or pattern.count('/') == path.count('/')


def parse(text):
    """Parse the sections of a MAINTAINERS file

    Lines before the first section, such as the description of the entries
    at the top of the main file, are passed over, since they are not
    sections: a section starts with a title line followed by entries

    Args:
        text (str): Content of the file

    Returns:
        list of Section: Sections, each with at least one entry
    """
    sections = []
    title = None
    cur = None
    for line in text.splitlines():
        match = RE_ENTRY.match(line)
        if not line.strip():
            title = cur = None
        elif not match:
            title = line.strip()
            cur = None
        elif title is not None:
            if cur is None:
                cur = Section(title, [], [], [], [])
                sections.append(cur)
            tag, value = match.groups()
            if tag in ('M', 'R'):
                _, addr = email.utils.parseaddr(value)
                if addr:
                    cur.people.append((tag, addr.lower()))
            elif tag == 'F':
                cur.files.append(value)
            elif tag == 'X':
                cur.excludes.append(value)
            elif tag == 'N':
                try:
                    cur.regexes.append(re.compile(value))
                except re.error:
                    pass
    return sections


def read(git_dir):
    """Read all the MAINTAINERS files in a git repo

    Args:
        git_dir (str): Top of the repo

    Returns:
        list of Section: Sections of all the files
    """
    out = command.output('git', 'ls-files', '--', 'MAINTAINERS',
                         '*/MAINTAINERS', cwd=git_dir, raise_on_error=False)
    sections = []
    for fname in (out or '').split():
        try:
            with open(os.path.join(git_dir, fname), encoding='utf-8',
                      errors='replace') as inf:
                sections += parse(inf.read())
        except OSError:
            pass
    return sections


def covers(section, path):
    """Check whether a section covers a file

    Args:
        section (Section): Section to check
        path (str): Path of the file from the top of the tree

    Returns:
        bool: True if the section covers it
    """
    if any(file_matches(pat, path) for pat in section.excludes):
        return False
    return (any(file_matches(pat, path) for pat in section.files) or
            any(pat.search(path) for pat in section.regexes))


def roles(sections, paths, emails):
    """Find your roles for some files

    Args:
        sections (list of Section): Sections, as from read()
        paths (list of str): Paths of the files from the top of the tree
        emails (set of str): Your addresses, in lower case

    Returns:
        set of str: 'M' if you are a maintainer of any of the files, 'R' if
            a reviewer of any
    """
    found = set()
    for section in sections:
        mine = {role for role, addr in section.people if addr in emails}
        if mine - found and any(covers(section, path) for path in paths):
            found |= mine
    return found
