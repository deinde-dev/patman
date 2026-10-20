.. SPDX-License-Identifier: GPL-2.0+
.. Copyright (c) 2011 The Chromium OS Authors
.. Simon Glass <sjg@chromium.org>
.. Maxim Cournoyer <maxim.cournoyer@savoirfairelinux.com>
.. v1, v2, 19-Oct-11
.. revised v3 24-Nov-11
.. revised v4 Independence Day 2020, with Patchwork integration
.. revised 2026, with assisted review, series tracking and web-relay sending

Patman patch manager
====================

Patman helps you create, send, track and review patch series for
projects that take patches by email, such as U-Boot and the Linux
kernel.

When creating and sending a series it:

- Creates patches directly from your branch
- Cleans them up by removing unwanted tags
- Inserts a cover letter with change logs
- Runs the patches through checkpatch.pl and its own checks
- Emails them to the right people, either with git send-email or,
  when you have no working outbound SMTP, through a web relay (as b4
  does)
- Links the series to Patchwork once sent

Once a series is out for review it helps you track it on Patchwork:

- Manage your local series and follow their status across versions
- Show review tags from Patchwork and gather them into your commits
- List the comments a series has received

It can also draft reviews of patches for you, using a Claude agent:

- Review any Patchwork series, or your own series before you send it
- Optionally build Gmail drafts, run Coverity or learn your writing
  voice from past reviews

``patman serve`` shows the status of your series and your reviews on a
mobile-friendly web page, where you can also work on reviews (see
:doc:`the Web server chapter </serve>`).

The series tracking and stored reviews are kept in an optional local
database (a ``.patman.db`` file at the top of your git tree), which
lets patman follow a series across versions and remember past reviews.
Creating and sending patches works without it.

Patman works with any git project which takes patches by email. Its
checkpatch and get_maintainer steps follow the U-Boot and Linux kernel
conventions, and can be turned off for projects which do not use them.

Where to start
--------------

A quick look
    There is a short introduction, with a demo, at https://deinde.dev/patman/

New to patman
    :doc:`The guide </guide/index>` shows how to use it, step by step, with a
    real U-Boot series: setting up, sending a first series, checking its
    status and collecting review tags, sending the next version, and tracking
    it all with the ``patman series`` database.

Looking up a command
    :doc:`The command reference </reference/index>` lists every command and
    its options, as does ``patman <command> -h``.

Reviewing other people's patches
    :doc:`The Assisted patch review chapter </review>` explains how to set it
    up and use it, and :doc:`the Web server chapter </serve>` how to follow
    and work on reviews from a phone.

The full manual
    The chapters which follow cover configuring patman, sending a series,
    the tags patman reads, patchwork and checking the review status, series
    management, assisted review, the web server and managing the database.
    ``patman -H`` shows them all in the terminal.

Installation
------------

Install patman with pip::

    pip install patch-manager

The package is called patch-manager because the name patman is already
taken. Some features need extra packages, which you can install along with
it::

    pip install patch-manager[review]   # patch review using a Claude agent
    pip install patch-manager[gmail]    # Gmail drafts of reviews and replies
    pip install patch-manager[all]      # everything

To try changes before they are released, install the latest code from
GitHub instead, with any extras inside the brackets::

    pip install "patch-manager[all] @ git+https://github.com/deinde-dev/patman.git"

The version stays the same between releases, so to pick up newer changes
later, add ``--force-reinstall --no-deps``, which reinstalls patman without
reinstalling the packages it uses::

    pip install --force-reinstall --no-deps \
        "patch-manager @ git+https://github.com/deinde-dev/patman.git"

Some distributions, such as recent Debian and Ubuntu, do not let pip install
into the system's Python, failing with 'externally-managed-environment'. There,
install patman with pipx, which gives it its own environment::

    pipx install "patch-manager[all]"         # the latest release
    pipx install "patch-manager[all] @ git+https://github.com/deinde-dev/patman.git"

To update it, use ``pipx upgrade patch-manager`` for a release, or
``pipx reinstall patch-manager`` for the latest code from GitHub.

The source code is at https://github.com/deinde-dev/patman

How patman works
----------------

Patman expects you to work in a particular way:

- Keep a branch for each series you are working on
- Put tags in the commits of each branch to say where the series goes, its
  version, its cover letter and so on. Most go in the top commit, where
  ``git commit --amend`` reaches them, e.g.::

      Series-to: u-boot
      Series-cc: heinrich

- Have each branch track its upstream branch, so that patman can count the
  commits in the series
- Check out a branch and run patman to create and send the patches. Weeks
  later, change them and send them again, knowing that you will get a
  consistent result each time

Since the settings are kept in the commits, you can work on several branches
at once without having to remember the right options for git format-patch
and git send-email for each one. In U-Boot and Linux, patman also runs
get_maintainer.pl on each patch to find who to copy, unless you use -m.
