.. SPDX-License-Identifier: GPL-2.0+

Using the snap
==============

patman is packaged as a snap as well as on PyPI. The snap is strictly
confined: it can only open files in your home directory, and on removable
media, and it cannot run programs from the host, so it brings its own git,
``git send-email``, perl and editors. It leaves out the agent which reviews
patches. Most of patman works just as it does when installed with pip, but
some setups cannot work in the snap. This page lists them.

When patman runs into one of these limits it says so, with a link to this
page, rather than leaving git to fail. If a limit gets in your way, install
patman with pip instead, as described in :doc:`getting-started`.

Installing
----------

Install the snap and let it read your settings::

    sudo snap install patman
    sudo snap connect patman:dot-patman
    sudo snap connect patman:dot-gitconfig

``dot-patman`` lets patman read and write ``~/.patman`` and
``~/.config/patman.d``, and ``dot-gitconfig`` lets it read ``~/.gitconfig``
and ``~/.config/git``. Without them, patman stops::

    $ patman status
    patman: The patman snap cannot read ~/.patman: connect its dot-patman interface with 'sudo snap connect patman:dot-patman' (see ...)

To use repositories on removable media, or under ``/mnt``, connect that
too::

    sudo snap connect patman:removable-media

Where your repositories can be
------------------------------

The snap can use repositories anywhere in your home directory, except in a
hidden directory directly inside it, such as ``~/.src``. With
``removable-media`` connected it can also use those under ``/media`` and
``/mnt``. It cannot reach anywhere else, such as ``/src`` or ``/work``::

    $ cd /src/u-boot
    $ patman status
    patman: The patman snap cannot reach the directory it is run in: it can only use directories in your home directory, and in /media and /mnt once removable-media is connected (see ...)

The same goes for any other file which your settings name, such as a git
alias file or a file included from ``~/.gitconfig``: it must be somewhere
the snap can reach, and not hidden directly in your home directory.

patman's caches go in ``~/snap/patman/common/cache`` rather than
``~/.cache``.

When ``patman serve`` runs as a systemd service, its ``WorkingDirectory``
must be somewhere the snap can reach as well. See 'Running it as a service'
in :doc:`/serve`.

What programs are in the snap
-----------------------------

The snap has its own git, ``git send-email``, perl, vim (also as ``vi``)
and nano. The project's ``scripts/checkpatch.pl`` and
``scripts/get_maintainer.pl`` run with the snap's perl, so they work as
usual.

Anything in your git settings which names a program must name one of these,
or a program in your home directory. Programs installed on the host, such as
in ``/usr/bin``, cannot be run. In particular:

Editor
    ``git send-email --annotate`` opens your editor on the patches before
    sending them. If git would use an editor which the snap does not have,
    patman says so before sending::

        patman: ValueError: Cannot run your editor 'emacs', which git send-email --annotate opens: the patman snap cannot run programs from the host. Set GIT_EDITOR or core.editor to vim, vi or nano, which come with the snap (see ...)

Mail server
    ``git send-email`` must send through an SMTP server, so set
    ``sendemail.smtpServer`` to the name of your mail server, as shown in
    :doc:`getting-started`. With no server set, it would look for the
    host's ``sendmail``, and a server which names a program such as
    ``/usr/bin/msmtp`` cannot be run either. patman checks both before
    sending. A sendmail-style program in your home directory does work.
    The kernel.org web relay does not use ``git send-email`` at all.

Credential helper
    If ``git send-email`` would ask a credential helper for your SMTP
    password, the helper must be one which comes with git, ``store`` or
    ``cache``. Helpers such as ``libsecret`` or ``gh`` are not in the snap,
    so set ``sendemail.smtpPass`` or use ``store`` instead. patman checks
    this before sending too.

Hooks
    Git hooks in your repositories run inside the snap as well, so a hook
    which runs a program from the host fails.

ssh and gpg
    The snap cannot read your keys in ``~/.ssh``, so git cannot push over
    ssh, as ``patman maint`` may when publishing. Use an https remote for
    pushing. The snap has no gpg, so git cannot sign commits either.

Reviewing patches
-----------------

The snap does not include the agent which reviews patches, since it needs
Claude Code. A strict snap cannot run the Claude Code installed on the host,
and bringing its own would make the snap four times the size. Reviewing a
series, suggesting a reply or a response, and anything else which needs the
agent stops with::

    The patman snap does not include the review agent; install patman with pip to review patches with it

This applies to the same actions on the ``patman serve`` page. The rest of
``patman review`` works, such as suggesting series to review, syncing and
sending Gmail drafts, and listing replies. Discussing a reply with ``patman
review --discuss-reply``, or with 'Discuss' on the page, is not available
either.
