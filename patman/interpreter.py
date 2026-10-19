# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Find the Python interpreter which is running patman

patman runs itself, and some test scripts, with the same Python, so that a
child process sees the same packages. This is normally sys.executable, but
Python sets that from argv[0], which a wrapper can set to its own name.
Guix's wrap-script does this, so that sys.executable is the 'patman' script
rather than a Python interpreter.
"""

import functools
import os
import sys

# File giving the program which is running this process, on Linux
SELF_EXE = '/proc/self/exe'


@functools.lru_cache(maxsize=None)
def get_python():
    """Get the path to the Python interpreter which is running patman

    If sys.executable is a script, i.e. starts with '#!', it is a wrapper
    rather than Python itself. Use the program which is actually running
    instead, if the system says what it is.

    Return:
        str: Path to the Python interpreter
    """
    exe = sys.executable
    try:
        with open(exe, 'rb') as inf:
            if inf.read(2) != b'#!':
                return exe
        return os.readlink(SELF_EXE)
    except OSError:
        return exe
