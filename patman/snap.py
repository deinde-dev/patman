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
import shlex
import shutil
import subprocess

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

# Credential helpers which come with git, and so are in the snap
GIT_HELPERS = ('cache', 'store')

# Reported when an agent is needed, since the snap leaves out the Claude Agent
# SDK and Claude Code
NO_AGENT = ('The patman snap does not include the review agent; install '
            'patman with pip to review patches with it')


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


def _git_config(key, cwd):
    """Read a git setting

    Args:
        key (str): Name of the setting, e.g. 'sendemail.smtpServer'
        cwd (str or None): Directory to run git in

    Returns:
        list of str: Values of the setting, empty if it is not set
    """
    res = subprocess.run(['git', 'config', '--get-all', key], cwd=cwd,
                         capture_output=True, text=True, check=False)
    return res.stdout.splitlines()


def _runnable(cmd):
    """Check whether a command names a program which can be run

    Args:
        cmd (str): Command line, e.g. 'emacs -nw'

    Returns:
        bool: True if its first word is a program on the PATH, or a path to
            one
    """
    words = shlex.split(cmd)
    return bool(words) and bool(shutil.which(words[0]))


def _check_editor(cwd):
    """Check that git send-email can run the editor

    Raises:
        ValueError: It cannot
    """
    res = subprocess.run(['git', 'var', 'GIT_EDITOR'], cwd=cwd,
                         capture_output=True, text=True, check=False)
    editor = res.stdout.strip()
    if editor and not _runnable(editor):
        raise limit(f"Cannot run your editor '{editor}', which git "
                    'send-email --annotate opens: the patman snap cannot '
                    'run programs from the host. Set GIT_EDITOR or '
                    'core.editor to vim, vi or nano, which come with the snap')


def _check_smtp(identity, smtp_server, cwd):
    """Check that git send-email can send mail from within the snap

    Args:
        identity (str or None): sendemail identity, or None for the default
        smtp_server (str or None): SMTP server given to patman, if any
        cwd (str or None): Directory to run git in

    Raises:
        ValueError: git send-email would use a program from the host, or a
            credential helper which the snap does not have
    """
    def get(name):
        """Read a sendemail setting, preferring the identity's own"""
        for key in ([f'sendemail.{identity}.{name}'] if identity else []) + [
                f'sendemail.{name}']:
            vals = _git_config(key, cwd)
            if vals:
                return vals[-1]
        return None

    identity = identity or get('identity')
    server = smtp_server or get('smtpServer')
    if not server:
        raise limit('No SMTP server is set, so git send-email would look '
                    "for the host's sendmail, which the patman snap cannot "
                    'run. Set sendemail.smtpServer to the name of your mail '
                    'server, or to localhost for one on this machine')
    if server.startswith('/') and not shutil.which(server):
        raise limit(f"Cannot run '{server}', which sendemail.smtpServer "
                    'names: the patman snap cannot run programs from the '
                    'host. Set it to the name of your mail server instead')
    if server.startswith('/') or not get('smtpUser') or get('smtpPass'):
        return
    for helper in _git_config('credential.helper', cwd):
        if not helper:
            continue
        if helper.startswith('!'):
            ok = _runnable(helper[1:])
        else:
            name = shlex.split(helper)[0]
            ok = (name in GIT_HELPERS or
                  _runnable(name if '/' in name else f'git-credential-{name}'))
        if not ok:
            raise limit(f"Cannot run the credential helper '{helper}', which "
                        'git send-email asks for your SMTP password: the '
                        'patman snap cannot run programs from the host. Set '
                        'credential.helper to store or cache, or set '
                        'sendemail.smtpPass')


def check_send(identity, smtp_server, cwd):
    """Check that git send-email can work within the snap

    This does nothing unless patman runs in a strictly confined snap.

    Args:
        identity (str or None): sendemail identity, or None for the default
        smtp_server (str or None): SMTP server given to patman, if any
        cwd (str or None): Directory to run git in

    Raises:
        ValueError: git send-email cannot work, saying why
    """
    if not is_strict():
        return
    _check_editor(cwd)
    _check_smtp(identity, smtp_server, cwd)
