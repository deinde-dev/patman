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


How to configure it
-------------------

If your project ships a git mail-alias file (U-Boot, for example,
provides 'doc/git-mailrc'), patman can use it to supply the email
aliases you need. To make this work, tell git where to find the file
by typing this once::

    git config sendemail.aliasesfile doc/git-mailrc

Projects in the U-Boot and Linux kernel style provide a
'scripts/get_maintainer.pl' that works out where to send patches. For
other projects, you may want to specify a different script to be run,
for example via a project-specific `.patman` file::

    # .patman configuration file at the root of some project

    [settings]
    get_maintainer_script: etc/teams.scm get-maintainer

The `get_maintainer_script` option corresponds to the
`--get-maintainer-script` argument of the `send` command.  It is
looked relatively to the root of the current git repository, as well
as on PATH.  It can also be provided arguments, as shown above.  The
contract is that the script should accept a patch file name and return
a list of email addresses, one per line, like `get_maintainer.pl`
does.

During the first run patman creates a config file for you by taking the default
user name and email address from the global .gitconfig file.

To add your own, create a file `~/.patman` like this::

    # patman alias file

    [alias]
    me: Simon Glass <sjg@chromium.org>

    u-boot: U-Boot Mailing List <u-boot@lists.denx.de>
    wolfgang: Wolfgang Denk <wd@denx.de>
    others: Mike Frysinger <vapier@gentoo.org>, Fred Bloggs <f.bloggs@napier.net>

As hinted above, Patman will also look for a `.patman` configuration
file at the root of the current project git repository, which makes it
possible to override the `project` settings variable or anything else
in a project-specific way. The values of this "local" configuration
file take precedence over those of the "global" one.

Config is read from three files, each overriding the previous one on a
per-key basis (lowest to highest priority):

1. ``.patman-defaults`` at the root of the git repository. This is
   tracked in git and shipped by the project to provide sensible
   defaults (such as the patchwork URL or the get_maintainer.pl
   invocation). Because it is the lowest layer, anyone can override it,
   so it never takes control away from the user. In a tree whose
   ``.gitignore`` ignores dot-files (as U-Boot's does), force-add it
   with ``git add -f .patman-defaults``.
2. ``~/.patman``, the user's global config.
3. ``.patman`` at the root of the git repository, the user's per-repo
   override (not tracked).

Command-line arguments override all three. A project shipping
``.patman-defaults`` might contain::

    [settings]
    patchwork_url: https://patchwork.ozlabs.org
    get_maintainer_script: scripts/get_maintainer.pl --norolestats

A user who sets ``patchwork_url`` in their own ``~/.patman`` (or passes
``-P``) overrides it; anything they leave unset falls back to these
defaults.

Aliases are recursive.

If the project provides a checkpatch.pl (in its 'scripts/' or 'tools/'
directory, as U-Boot and Linux do) patman will locate and use it.
Failing that you can put it into your path or ~/bin/checkpatch.pl

If you want to avoid sending patches to email addresses that are picked up
by patman but are known to bounce you can add a [bounces] section to your
.patman file. Unlike the [alias] section these are simple key: value pairs
that are not recursive::

    [bounces]
    gonefishing: Fred Bloggs <f.bloggs@napier.net>


If you want to change the defaults for patman's command-line arguments,
you can add a [settings] section to your .patman file.  This can be used
for any command line option by referring to the "dest" for the option in
patman.py.  For reference, the useful ones (at the moment) shown below
(all with the non-default setting)::

    [settings]
    ignore_errors: True
    process_tags: False
    verbose: True
    smtp_server: /path/to/sendmail
    patchwork_url: https://patchwork.ozlabs.org
    patchwork_timeout: 60

``patchwork_timeout`` is the number of seconds to wait for each request to
patchwork before trying again (default 30). A busy server can stall a
request, so patman retries a request which times out, has its connection
reset or refused, or gets a 502, 503 or 504 status, up to three times.

By default patman adds ``base-commit:`` and ``branch:`` trailers to the
generated patches (or the cover letter). These record the commit that the
series applies on top of. If you work on a local or downstream branch whose
base commit does not exist in the upstream tree, these trailers are
misleading, so you can suppress them with ``--no-base-commit`` on the command
line, or permanently via::

    [settings]
    insert_base_commit: False

If you want to adjust settings (or aliases) that affect just a single
project you can add a section that looks like [project_settings] or
[project_alias].  If you want to use tags for your linux work, you could do::

    [linux_settings]
    process_tags: True


How to run it
-------------

First do a dry run:

.. code-block:: bash

    patman send -n

If it can't detect the upstream branch, try telling it how many patches
there are in your series

.. code-block:: bash

    patman -c5 send -n

This will create patch files in your current directory and tell you who
it is thinking of sending them to. Take a look at the patch files:

.. code-block:: bash

    patman -c5 -s1 send -n

Similar to the above, but skip the first commit and take the next 5. This
is useful if your top commit is for setting up testing.


Sending through a web relay
---------------------------

If you have no working outbound email (SMTP blocked, or a provider that
mangles patches), patman can post the series to a web submission
endpoint that sends the mail for you, instead of using ``git
send-email``. This is the same mechanism b4 uses, and works with the
kernel.org endpoint for kernel.org-hosted projects such as U-Boot.

Setting it up
~~~~~~~~~~~~~

The guide goes through the setup step by step, with the output to expect:
see 'Sending through the kernel.org web relay' in
``doc/guide/getting-started.rst``. In brief:

- give patatt, which signs the messages, a signing key, e.g.
  ``git config --global patatt.signingkey openpgp:<your-key-id>``
- set ``send_endpoint_web: https://lkml.kernel.org/_b4_submit`` in the
  ``[u-boot_settings]`` section of ``~/.patman``, or pass
  ``--send-endpoint-web <url>`` for a single run
- register your key and email address once, with
  ``patman send --web-auth-new`` and then
  ``patman send --web-auth-verify <challenge>``
- check it with ``patman send --reflect``, which has the endpoint mail the
  series back to you only

Notes
~~~~~

With the endpoint set, ``patman send`` builds an email per patch (with
the usual To/Cc), signs each with patatt and posts them. The endpoint
authenticates each submission by its patatt signature -- there is no
password or token stored.

Once the endpoint is configured it is used for every ``patman send`` in
that project. Pass ``--no-relay`` to send with ``git send-email`` for a
single run.

Threading (``--thread``) and ``--in-reply-to`` work with the relay just
as with ``git send-email``: the patches reply to the cover letter, which
can in turn reply to an earlier message.


How to add tags
---------------

patman reads tags from your commit messages to know where to send the series,
what its cover letter says, its version, its change logs and so on. It
removes them from the patches it sends. For example, the top commit of a
series might end::

    Series-to: u-boot
    Series-cc: heinrich
    Series-version: 2
    Cover-letter:
    binman: Tidy up some missing bintool docs
    Some of the bintools are missing documentation. This series adds the
    missing pieces to bring things up to scratch.
    END

Most tags can only appear once in the series, and they usually go in the top
commit, where ``git commit --amend`` reaches them. Change logs go in the
commit they describe. The 'Tags' chapter lists every tag.


Where Patches Are Sent
----------------------

Once the patches are created, patman sends them using git send-email. The
whole series is sent to the recipients in Series-to: and Series-cc.
You can Cc individual patches to other people with the Patch-cc: tag. Tags
in the subject are also picked up to Cc patches. For example, a commit like
this::

    commit 10212537b85ff9b6e09c82045127522c0f0db981
    Author: Mike Frysinger <vapier@gentoo.org>
    Date:    Mon Nov 7 23:18:44 2011 -0500

    x86: arm: add a git mailrc file for maintainers

    This should make sending out e-mails to the right people easier.

    Patch-cc: sandbox, mikef, ag
    Patch-cc: afleming

will create a patch which is copied to x86, arm, sandbox, mikef, ag and
afleming.

If you have a cover letter it will get sent to the union of the Patch-cc
lists of all of the other patches. If you want to sent it to additional
people you can add a tag::

    Cover-letter-cc: <list of addresses>

These people will get the cover letter even if they are not on the To/Cc
list for any of the patches.


Patchwork Integration
---------------------

Patman has a very basic integration with Patchwork. If you point patman to
your series on patchwork it can show you what new reviews have appeared since
you sent your series.

To set this up, add a Series-links tag to one of the commits in your series
(see the 'Tags' chapter).

Then you can type:

.. code-block:: bash

    patman status

and patman will show you each patch and what review tags have been collected,
for example::

    ...
     21 x86: mtrr: Update the command to use the new mtrr
        Reviewed-by: Wolfgang Wallner <wolfgang.wallner@br-automation.com>
      + Reviewed-by: Bin Meng <bmeng.cn@gmail.com>
     22 x86: mtrr: Restructure so command execution is in
        Reviewed-by: Wolfgang Wallner <wolfgang.wallner@br-automation.com>
      + Reviewed-by: Bin Meng <bmeng.cn@gmail.com>
    ...

This shows that patch 21 and 22 were sent out with one review but have since
attracted another review each. If the series needs changes, you can update
these commits with the new review tag before sending the next version of the
series.

To automatically pull into these tags into a new branch, use the -d option:

.. code-block:: bash

    patman status -d mtrr4

This will create a new 'mtrr4' branch which is the same as your current branch
but has the new review tags in it. The tags are added in alphabetic order and
are placed immediately after any existing ack/review/test/fixes tags, or at the
end. The new branch does not track an upstream branch, so set one with
'git branch --set-upstream-to' before sending from it.

There is also a -c option to list the comments received for each patch, and a
-C option to list those on the cover letter.

For a worked example using real series, see ``doc/guide/status-and-tags.rst``.


Example Work Flow
-----------------

The basic workflow is to create your commits, add some tags to the top
commit, and run 'patman send' to check and send them. The guide in
``doc/guide/`` goes through this with a real series, from the first version
to the last, with the output at each step. A few more points:

Leaving out commits
    If the branch has commits which should not be sent, such as a
    work-in-progress commit at the top for testing, use ``-s`` to skip that
    many commits from the top, or ``-e`` to skip commits at the bottom, e.g.

    .. code-block:: bash

        patman send -s1 -n

    Tags in a skipped commit are not seen, so keep them in a commit which is
    sent.

Fixing problems
    If the dry run reports checkpatch errors or warnings, fix the commit,
    e.g. with 'git rebase -i' and 'edit', then run the dry run again until it
    is clean. Use ``-i`` to send in spite of the errors, or ``--no-check`` to
    skip checkpatch.

Dropping commits
    When a commit is no longer needed, e.g. because the patch it depends on
    has been merged, drop it with 'git rebase -i' before sending the next
    version.


Tracking series
---------------

If you have several series in flight, 'patman series' keeps track of them for
you in a database: each version, its patchwork link and the state of each
patch. It can find the links, gather the review tags, start each new version
and archive a series once merged. It can also send each series to the right
tree when you work with more than one upstream. The 'Series Management'
chapter describes all this.


Tips
----

#. Everything about a series, such as its version, who it goes to and its
   change logs, is kept in the commits. When you come back to a branch weeks
   later, there is nothing to remember.
#. Add the change log to a commit as you change it, rather than later when
   you cannot remember what changed. You can always go back and edit it.
#. To keep each version as sent, so you can compare them later, use a new
   branch for each version (as ``patman series inc`` does), or tag the
   branch before changing it, e.g. ``git tag sent/video-v2``.
#. You can change the patches in your editor before ``git send-email`` sends
   them, but be careful.
#. To run ``git send-email`` yourself, use ``-n``, which prints the command
   patman would have run.
#. Some mailing lists limit the size of a message, which binary content
   easily exceeds. Use ``--no-binary`` to leave it out of the patches, and
   give a link to a git repository in ``Commit-notes``, ``Series-notes`` or
   the cover letter so the maintainer can fetch the commit.
#. A patch which did not change in the latest version gets a note saying
   so. For example, a commit with::

        Series-version: 5
        Series-changes: 2
        - Some change

        Series-changes: 4
        - Another change

   has the change log::

        (no changes since v4)

        Changes in v4:
        - Another change

        Changes in v2:
        - Some change

#. Tags are case sensitive: ``series-to:`` is not recognised.


The database
============

Patman stores series tracking, review and workflow state in a SQLite
database (``.patman.db``) in the top-level git directory. The schema is
versioned and migrated automatically when patman starts. For what the
database records and the schema of each table, see ``doc/database.rst``.

Set the ``PATMAN_DB_DIR`` environment variable to use a database elsewhere
(``~`` is expanded). The variable names a *directory* in which patman looks
for ``.patman.db``, mirroring the default layout. This is useful if you keep
a single 'main' patman database in one repo but want to run, for example,
``patman review`` in another worktree or repo: the DB path is overridden but
git operations still run against the current repo. For instance::

    export PATMAN_DB_DIR=~/u-boot
    cd ~/some-other-tree
    patman review -s my-series

Reviews of other people's series can be kept in a separate database, so they
do not mix with your own series. Set ``review_db_dir`` in the ``[settings]``
section of ``~/.patman`` (or use ``--review-db-dir``) to the directory which
holds it::

    [settings]
    review_db_dir: ~/dev/u-boot-review

``patman review`` then stores its reviews there, while upstreams and their
patchwork servers are still read from the main database. ``patman serve``
shows the reviews from the same place.

To move existing reviews into the review database, or back again::

    patman database -n split    # show what would be moved
    patman database split       # move reviews to the review database
    patman database join        # move them back to the main database

Series can also be copied from another database, such as one from a
different machine, with ``patman database merge DIR`` (or the path of the
database file). Use ``--own`` or ``--reviews`` to copy just one kind of
series, and ``-R`` to copy into the review database. The other database is
not changed. Missing upstreams are added to the main database too.

Your own series and series you have reviewed are always kept apart: if a
series has the same name as one of the other kind, it is left alone and
reported. When a series is in both databases, its new versions are added,
but a version which is in both is left alone. When moving, a series which
cannot be moved completely (for example because it has a version which is
already in the other database) is left where it is, so nothing is lost. Each database
which is changed is first backed up to ``.patman.db.bak`` alongside it.

The connection is opened in WAL mode (``journal_mode=WAL``,
``synchronous=NORMAL``) with a 30-second busy timeout, which lets multiple
patman invocations share the database safely: concurrent readers do not block
each other, and a writer only briefly excludes other writers. This means it is
fine to run, for example, ``patman status`` in one terminal while a long
``patman review`` is running in another. WAL creates ``.patman.db-wal`` and
``.patman.db-shm`` sidecar files alongside the database; they are managed by
SQLite and removed on a clean close, so copy or sync all three together if you
back the database up by hand.
