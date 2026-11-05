# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Say where patman's strictly confined snap stops it working

A strictly confined snap can only reach the user's home directory (and
removable media) and cannot run programs from the host. It brings its own
git, git send-email, perl and editors instead. Most of patman works the
same, but some of the user's setup cannot: a repository elsewhere on disk,
a sendmail program, a credential helper or an editor which is only on the
host. The checks here find these cases, so that patman can say what the
limit is rather than fail with a confusing error from git.
"""

import os

# Where snapd puts a snap which cannot reach the directory it is run in
VOID = '/var/lib/snapd/void'

# Page describing the limits of the snap
GUIDE = 'https://patman.readthedocs.io/en/latest/guide/snap.html'

# Settings in the home directory, each with the snap's interface which lets
# it read them
PLUGS = (
    ('.patman', 'dot-patman'),
    ('.config/patman.d', 'dot-patman'),
    ('.gitconfig', 'dot-gitconfig'),
    ('.config/git', 'dot-gitconfig'),
)

def confinement():
    """Find how the snap which patman runs in is confined

    Returns:
        str or None: 'strict', 'classic' or 'devmode', or None if patman is
            not running in a snap
    """
    snap = os.environ.get('SNAP')
    if not snap or not os.environ.get('SNAP_NAME'):
        return None
    try:
        with open(os.path.join(snap, 'meta', 'snap.yaml'),
                  encoding='utf-8') as inf:
            for line in inf:
                if line.startswith('confinement:'):
                    return line.split(':', 1)[1].strip()
    except OSError:
        pass
    return 'strict'


def is_strict():
    """Check whether patman runs in a strictly confined snap

    Returns:
        bool: True if it does
    """
    return confinement() == 'strict'


def limit(msg):
    """Make an error for something which the snap cannot do

    Args:
        msg (str): What cannot be done, and why

    Returns:
        ValueError: Error giving the message and where to read more
    """
    return ValueError(f'{msg} (see {GUIDE})')


def _check_cwd():
    """Check that the snap can reach the directory patman is run in

    snapd runs a snap in an empty directory if it cannot reach the one it
    is run in, and sets PWD to match, so the name of that one is lost.

    Raises:
        ValueError: patman runs outside the directories which it can reach
    """
    if os.getcwd() == VOID:
        raise limit('The patman snap cannot reach the directory it is run '
                    'in: it can only use directories in your home directory, '
                    'and in /media and /mnt once removable-media is '
                    'connected')


def _check_plugs():
    """Check that the snap can read the settings in the home directory

    A file which the snap is not allowed to read looks the same as any other
    until it is opened, and Python's configparser skips one which it cannot
    open, so try each.

    Raises:
        ValueError: A file or directory exists but the interface which lets
            the snap read it is not connected
    """
    for rel, plug in PLUGS:
        path = os.path.join(os.path.expanduser('~'), rel)
        try:
            if os.path.isdir(path):
                os.listdir(path)
            else:
                with open(path, 'rb'):
                    pass
        except FileNotFoundError:
            pass
        except PermissionError as exc:
            raise limit(f'The patman snap cannot read ~/{rel}: connect its '
                        f"{plug} interface with 'sudo snap connect "
                        f"patman:{plug}'") from exc


def check_setup():
    """Check that patman can work where it is run, if it is in a strict snap

    Raises:
        ValueError: patman runs in a strict snap and the directory or the
            settings it needs are out of reach
    """
    if is_strict():
        _check_cwd()
        _check_plugs()
