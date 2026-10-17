.. SPDX-License-Identifier: GPL-2.0+

Setting up patman
=================

This covers the setup needed to send patches with patman, for the simple
case of turning a branch of commits into a series and emailing it. The
example uses U-Boot, but other projects with a ``scripts/checkpatch.pl``
and ``scripts/get_maintainer.pl``, such as Linux, work the same way. Once
this is done, :doc:`first-series` walks through sending a real series.

What you need
-------------

- Python 3.9 or later
- git, with ``git send-email``, which patman uses to send the email
- Perl, which U-Boot's ``checkpatch.pl`` and ``get_maintainer.pl`` need
- a clone of the project you are sending patches to

Installing patman
-----------------

Install it from PyPI::

    pip install patch-manager

This installs the command as both ``patman`` and ``patch-manager``. The
Debian package installs only ``patch-manager``, since Debian already has an
unrelated ``patman`` command; use that name in the commands below if needed.

Setting up git
--------------

patman takes your name and email address from git, so set them if you have
not already::

    git config --global user.name "Your Name"
    git config --global user.email you@example.com

These go into the ``Signed-off-by:`` line which patman adds to each patch
that does not already have one, and into the ``From:`` of each email.

``git send-email`` is often packaged separately from git, e.g. on Debian and
Ubuntu::

    sudo apt install git-email

Tell it how to reach your mail server. For a server which takes a login on
port 587::

    git config --global sendemail.smtpserver smtp.example.com
    git config --global sendemail.smtpserverport 587
    git config --global sendemail.smtpencryption tls
    git config --global sendemail.smtpuser you@example.com

``git send-email`` asks for the password when it sends. For Gmail, use
``smtp.gmail.com`` with an app password, created in your Google account
settings, rather than your normal password. See ``git help send-email``
for the other options.

Check that sending works by mailing a patch to yourself::

    git format-patch -1
    git send-email --to=you@example.com --suppress-cc=all 0001-*.patch
    rm 0001-*.patch

``--suppress-cc=all`` stops git copying the people named in the patch's
trailers. If the patch arrives, git is ready. If you cannot send email from
your machine at all, patman can post the series to a web relay instead;
see 'Sending through a web relay' in the manual.

Setting up the tree
-------------------

Clone U-Boot and name the remote ``us`` (for upstream), which is the name
used in these examples::

    git clone -o us https://git.u-boot-project.org/u-boot/u-boot
    cd u-boot

The remote can have any name. With an existing clone, rename its remote
with ``git remote rename origin us``, or just use ``origin`` in place of
``us``.

U-Boot provides email aliases for its mailing list and many developers in
``doc/git-mailrc``. Tell git to use them, once in each clone::

    git config sendemail.aliasesfile doc/git-mailrc

patman looks the aliases up in this file, so that a tag such as
``Series-to: u-boot`` sends the series to the U-Boot mailing list.

Start each series on its own branch, based on the upstream branch::

    git checkout -b my-series us/master

Since the branch starts from ``us/master``, git sets it to track that
branch, which lets patman work out which commits are in the series. For a
branch made some other way, set this with::

    git branch --set-upstream-to=us/master

The ~/.patman config file
-------------------------

The first time it runs, patman creates ``~/.patman`` with an alias for you,
taken from your git name and email::

    [alias]
    me: Your Name <you@example.com>

Add aliases here for people you often send to, which can then be used in
tags such as ``Series-cc:``::

    [alias]
    me: Your Name <you@example.com>
    heinrich: Heinrich Schuchardt <xypron.glpk@gmx.de>

An alias can also be a list of addresses, separated by commas, and aliases
can refer to other aliases.

Nothing else is needed for the simple case: patman finds U-Boot's
``scripts/checkpatch.pl`` to check the patches and its
``scripts/get_maintainer.pl`` to find who to copy. The other settings are
described in 'How to configure it' in the manual.

Checking the setup
------------------

With a branch of commits checked out, do a dry run::

    patman send -n

This creates the patch files in the current directory, checks them and
shows who they would be sent to, without sending anything. If patman cannot
work out how many commits are in the series, it says so; set the upstream
branch as above, or give the count with ``-c``, e.g. ``patman send -c3 -n``.

:doc:`first-series` shows what the output looks like for a real series, and
how to add the tags which say where it goes.
