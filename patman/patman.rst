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
mobile-friendly web page, where you can also work on reviews (see the 'Web
server' chapter).

The series tracking and stored reviews are kept in an optional local
database (a ``.patman.db`` file at the top of your git tree), which
lets patman follow a series across versions and remember past reviews.
Creating and sending patches works without it.

It is intended to automate the whole process and make it less
error-prone. It works with any git project that sends patches by email;
the checkpatch and get_maintainer steps follow the U-Boot and Linux
kernel conventions and can be skipped for projects that do not use them.

It is configured almost entirely by tags it finds in your commits.
This means that you can work on a number of different branches at
once, and keep the settings with each branch rather than having to
git format-patch, git send-email, etc. with the correct parameters
each time. So for example if you put::

    Series-to: fred.blogs@napier.co.nz

in one of your commits, the series will be sent there.

In Linux and U-Boot this will also call get_maintainer.pl on each of your
patches automatically (unless you use -m to disable this).


Installation
------------

You can install patman using::

   pip install patch-manager

The name is chosen since patman conflicts with an existing package.

The source code is hosted at https://github.com/deinde-dev/patman

How to use this tool
--------------------

This tool requires a certain way of working:

- Maintain a number of branches, one for each patch series you are
  working on
- Add tags into the commits within each branch to indicate where the
  series should be sent, cover letter, version, etc. Most of these are
  normally in the top commit so it is easy to change them with 'git
  commit --amend'
- Each branch tracks the upstream branch, so that this script can
  automatically determine the number of commits in it (optional)
- Check out a branch, and run this script to create and send out your
  patches. Weeks later, change the patches and repeat, knowing that you
  will get a consistent result each time.

The guide in ``doc/guide/`` shows how to work this way, step by step, with a
real U-Boot series: setting up, sending a first series, checking its status and
collecting review tags, sending the next version, and tracking it all with the
``patman series`` database. Start with ``doc/guide/index.rst``.

The command reference in ``doc/reference/`` lists every command and its
options, as does ``patman <command> -h``.

The chapters which follow cover configuring patman, sending a series,
checking its status on patchwork, the tags patman reads, series management,
assisted review, the web server and managing the database.

