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

In short (``doc/guide/getting-started.rst`` goes through each step with the
output to expect):

1. Give patatt, which signs the messages, a signing key. For an existing PGP
   key::

       git config --global patatt.signingkey openpgp:<your-key-id>

   or create an ed25519 key with ``patatt genkey`` and add the ``[patatt]``
   section it prints to your git config. gpg must be able to sign without a
   prompt getting stuck; ``export GPG_TTY=$(tty)`` usually does it.

2. Set the endpoint for the project in ``~/.patman`` (or pass
   ``--send-endpoint-web <url>`` for a single run)::

       [u-boot_settings]
       send_endpoint_web: https://lkml.kernel.org/_b4_submit

3. Register your key and email address with the endpoint, once. It emails
   you a challenge to pass back::

       patman send --web-auth-new
       patman send --web-auth-verify <challenge>

4. Check it with ``patman send --reflect``, which has the endpoint mail the
   series back to you only. Then send for real with ``patman send``.

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

To make this script useful you must add tags like the following into any
commit. Most can only appear once in the whole series.

Series-to: email / alias
    Email address / alias to send patch series to (you can add this
    multiple times)

Series-cc: email / alias, ...
    Email address / alias to Cc patch series to (you can add this
    multiple times)

Series-version: n
    Sets the version number of this patch series

Series-prefix: prefix
    Sets the subject prefix. Normally empty but it can be RFC for
    RFC patches, or RESEND if you are being ignored. The patch subject
    is like [RFC PATCH] or [RESEND PATCH].
    In the meantime, git format.subjectprefix option will be added as
    well. If your format.subjectprefix is set to InternalProject, then
    the patch shows like: [InternalProject][RFC/RESEND PATCH]

Series-postfix: postfix
    Sets the subject "postfix". Normally empty, but can be the name of a
    tree such as net or net-next if that needs to be specified. The patch
    subject is like [PATCH net] or [PATCH net-next].

Series-name: name
    Sets the name of the series. You don't need to have a name, and
    patman does not yet use it, but it is convenient to put the branch
    name here to help you keep track of multiple upstreaming efforts.

Series-links: [id | version:id]...
    Set the ID of the series in patchwork. You can set this after you send
    out the series and look in patchwork for the resulting series. The
    URL you want is the one for the series itself, not any particular patch.
    E.g. for http://patchwork.ozlabs.org/project/uboot/list/?series=187331
    the series ID is 187331. This property can have a list of series IDs,
    one for each version of the series, e.g.

    ::

       Series-links: 1:187331 2:188434 189372

    Patman always uses the one without a version, since it assumes this is
    the latest one. When this tag is provided, patman can compare your local
    branch against patchwork to see what new reviews your series has
    collected ('patman status').

Series-patchwork-url: url
    This allows specifying the Patchwork URL for a branch. This overrides
    both the setting files ("patchwork_url") and the command-line argument.
    The URL should include the protocol and web site, with no trailing slash,
    for example 'https://patchwork.ozlabs.org/project'

Cover-letter:
    Sets the cover letter contents for the series. The first line
    will become the subject of the cover letter::

        Cover-letter:
        This is the patch set title
        blah blah
        more blah blah
        END

Cover-letter-cc: email / alias
    Additional email addresses / aliases to send cover letter to (you
    can add this multiple times)

Series-notes:
    Sets some notes for the patch series, which you don't want in
    the commit messages, but do want to send, The notes are joined
    together and put after the cover letter. Can appear multiple
    times::

        Series-notes:
        blah blah
        blah blah
        more blah blah
        END

Commit-notes:
    Similar, but for a single commit (patch). These notes will appear
    immediately below the ``---`` cut in the patch file::

        Commit-notes:
        blah blah
        blah blah
        more blah blah

Signed-off-by: Their Name <email>
    A sign-off is added automatically to your patches (this is
    probably a bug). If you put this tag in your patches, it will
    override the default signoff that patman automatically adds.
    Multiple duplicate signoffs will be removed.

Tested-by / Reviewed-by / Acked-by
    These indicate that someone has tested/reviewed/acked your patch.
    When you get this reply on the mailing list, you can add this
    tag to the relevant commit and the script will include it when
    you send out the next version. If 'Tested-by:' is set to
    yourself, it will be removed. No one will believe you.

    Example::

        Tested-by: Their Name <fred@bloggs.com>
        Reviewed-by: Their Name <email>
        Acked-by: Their Name <email>

Series-changes: n
    This can appear in any commit. It lists the changes for a
    particular version n of that commit. The change list is
    created based on this information. Each commit gets its own
    change list and also the whole thing is repeated in the cover
    letter (where duplicate change lines are merged if
    Series-process-log includes uniq).

    By adding your change lists into your commits it is easier to
    keep track of what happened. When you amend a commit, remember
    to update the log there and then, knowing that the script will
    do the rest.

    Example::

        Series-changes: n
        - Guinea pig moved into its cage
        - Other changes ending with a blank line
        <blank line>

Commit-changes: n
    This tag is like Series-changes, except changes in this changelog will
    only appear in the changelog of the commit this tag is in. This is
    useful when you want to add notes which may not make sense in the cover
    letter. For example, you can have short changes such as "New" or
    "Lint".

    Example::

        Commit-changes: n
        - This line will not appear in the cover-letter changelog
        <blank line>

Cover-changes: n
    This tag is like Series-changes, except changes in this changelog will
    only appear in the cover-letter changelog. This is useful to summarize
    changes made with Commit-changes, or to add additional context to
    changes.

    Example::

        Cover-changes: n
        - This line will only appear in the cover letter
        <blank line>

Commit-added-in: n
    Add a change noting the version this commit was added in. This is
    equivalent to::

        Commit-changes: n
        - New

        Cover-changes: n
        - <commit subject>

    It is a convenient shorthand for suppressing the '(no changes in vN)'
    message.

Patch-cc / Commit-cc: Their Name <email>
    This copies a single patch to another email address. Note that the
    Cc: used by git send-email is ignored by patman, but will be
    interpreted by git send-email if you use it.

Series-process-log: sort, uniq
    This tells patman to sort and/or uniq the change logs. Changes may be
    multiple lines long, as long as each subsequent line of a change begins
    with a whitespace character. For example,

    Example::

        - This change
          continues onto the next line
        - But this change is separate

    Use 'sort' to sort the entries, and 'uniq' to include only
    unique entries. If omitted, no change log processing is done.
    Separate each tag with a comma.

Change-Id:
    This tag is used to generate the Message-Id of the emails that
    will be sent. When you keep the Change-Id the same you are
    asserting that this is a slightly different version (but logically
    the same patch) as other patches that have been sent out with the
    same Change-Id. The Change-Id tag line is removed from outgoing
    patches, unless the `keep_change_id` settings is set to `True`.

Various other tags are silently removed, like these Chrome OS and
Gerrit tags::

    BUG=...
    TEST=...
    Review URL:
    Reviewed-on:
    Commit-xxxx: (except Commit-notes)

Exercise for the reader: Try adding some tags to one of your current
patch series and see how the patches turn out.


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

To set this up, add a Series-link tag to one of the commits in your series
(see above).

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


Series Management
-----------------

Sometimes you might have several series in flight at the same time. Each of
these receives comments and you want to create a new version of each series with
those comments addressed.

Patman provides a few subcommands which are helpful for managing series.

Series and branches
~~~~~~~~~~~~~~~~~~~

'patman series' works with the concept of a series. It maintains a local
database (.patman.db in your top-level git tree) and uses that to keep track of
series and patches.

Each series goes through muliple versions. Patman requires that the first
version of your series is in a branch without a numeric suffix. Branch names
like 'serial' and 'video' are OK, but 'part3' is not. This is because Patman
uses the number at the end of the branch name to indicate the version.

If your series name is 'video', then you can have a 'video' branch for version
1 of the series, 'video2' for version 2 and 'video3' for version 3. All three
branches are for the same series. Patman keeps track of these different
versions. It handles the branch naming automatically, but you need to be aware
of what it is doing.

You will have an easier time if the branch names you use with 'patman series'
are short, no more than 15 characters. This is the amount of columnar space in
listings. You can add a longer description as the series description. If you
are used to having very descriptive branch names, remember that patman lets you
add metadata into commit which is automatically removed before sending.

This documentation uses the term 'series' to mean all the versions of a series
and 'series/version' to mean a particular version of a series.

Updating commits
~~~~~~~~~~~~~~~~

Since Patman provides quite a bit of automation, it updates your commits in
some cases, effectively doing a rebase of a branch in order to change the tags
in the commits. It never makes code changes.

In extremis you can use 'git reflog' to revert something that Patman did.


Series subcommands
~~~~~~~~~~~~~~~~~~

Note that 'patman series ...' can be abbreviated as 'patman s' or 'patman ser'.

Here is a short overview of the available subcommands:

    add
        Add a new series. Use this on an existing branch to tell Patman about it.

    archive (ar)
        Archive a series when you have finished upstreaming it. Archived series
        are not shown by most commands. This creates a dated tag for each
        version of the series, pointing to the series branch, then deletes the
        branches. It puts the tag names in the database so that it can
        'unarchive' to restore things how they were.

    unarchive (unar)
        Unarchive a series when you decide you need to do something more with
        it. The branches are restored and tags deleted.

    autolink (au)
        Search patchwork for the series link for your series, so Patman can
        track the status

    autolink-all
        Same but for all series

    inc
        Increase the series number, effectively creating a new branch with the
        next highest version number. The new branch is created based on the
        existing branch. So if you use 'patman series inc' on branch 'video2'
        it will create branch 'video3' and add v3 into its database

    dec
        Decrease the series number, thus deleting the current branch and
        removing that version from the data. If you use this comment on branch
        'video3' Patman will delete version 3 and branch 'video3'.

    find
        Search the database for series matching a subject fragment.
        Matches against the series description, per-version description,
        and individual patch subjects. Use ``-A`` to include archived
        series. Example::

            patman series find 'fs loader'

    get-link
        Shows the Patchwork link for a series/version

    info
        Show detailed information about a series, including each
        version's link, description, patches and any stored reviews.
        The output is colour-coded by patchwork state (e.g. ``new``,
        ``accepted``). Use ``-r`` to include review text; pass a list
        of patch numbers (``-r 1 3``) to limit the review text to those
        patches.

    ls
        Lists the series in the database. Use ``-r`` to show only
        review series (series fetched by ``patman review``).

    mark
        Mark a series with 'Change-Id' tags so that Patman can track patches
        even when the subject changes. Unmarked patches just use the subject to
        decided which is which.

    unmark
        Remove 'Change-Id' tags from a series.

    open (o)
        Open a series in Patchwork using your web browser

    patches
        Show the patches in a particular series/version

    progress (p)
        Show upstream progress for your series, or for all series

    rm
        Remove a series entirely, including all versions

    rm-version (rmv)
        Remove a particular version of a series. This is similar to 'dec'
        except that any version can be removed, not just the latest one.

    scan
        Scan the local branch and update the database with the set of patches
        in that branch. This throws away the old patches.

    send
        Send a series out as patches. This is similar to 'patman send' except
        that it can send any series, not just the current branch. It also
        waits a little for patchwork to see the cover letter, so it can find
        out the patchwork link for the series.

    set-link
        Sets the Patchwork link for a series-version manually.

    status (st)
        Run 'patman status' on a series. This is similar to 'patman status'
        except that it can get status on any series, not just the current
        branch

    summary
        Shows a quick summary of series with their status and description.

    sync
        Sync the status of a series with Pathwork, so that
        'patman series progress' can show the right information.

    sync-all
        Sync the status of all series.


Patman series workflow
~~~~~~~~~~~~~~~~~~~~~~

These are the commands for the life of a series, in order.
``doc/guide/series-workflow.rst`` goes through them with a real U-Boot series
and the output of each.

1. Tell patman the patchwork project for the upstream remote, once for each
   clone. The remote defaults to the one the current branch tracks, or the
   only remote::

       patman patchwork set-project U-Boot

2. On the branch for version 1, e.g. 'video', add the series. Its
   description comes from the cover letter. Use ``-m`` to add ``Change-Id:``
   tags to the commits, so patches can be followed even if their subjects
   change, or ``-M`` to match them by subject::

       patman series add -M

3. Send it. patman then waits for the series to appear on patchwork and
   records its link. If it does not appear in time, run
   ``patman series autolink`` later::

       patman series send

4. Follow the reviews. ``gather`` records the state of each patch and adds
   new review tags to the commits; ``status`` just shows them. Use ``-c`` and
   ``-C`` to see the comments on the patches and the cover letter::

       patman series gather
       patman series status -cC

5. See where all your series stand::

       patman series progress

6. Start the next version. ``inc`` creates the 'video2' branch, sets
   ``Series-version: 2`` and adds v2 to the database. After changing the
   commits, use ``scan`` to update the database from the branch, then send
   as before::

       patman series inc
       patman series scan -M
       patman series send

   Alternatively, create 'video2' yourself and run ``patman series add``, or
   collect the tags into it with ``patman status -d video2``.

7. Once the series is merged, archive it. This keeps each version's branch
   as a tag and deletes the branches::

       patman series archive

Here is a sample 'progress' view:

.. image:: pics/patman.jpg
  :width: 800
  :alt: Patman showing the progress view

Multiple upstreams
~~~~~~~~~~~~~~~~~~

If you send patches to more than one upstream tree (e.g. U-Boot mainline and
the U-Boot concept tree), you can configure patman with multiple upstreams so
that each series automatically uses the correct send settings for its
destination. The trees may use different mailing lists, patchwork servers and
SMTP credentials.

For a worked example with real output, see ``doc/guide/upstreams.rst``.

The key concepts are:

**Upstream**
    A remote git repository that you send patches to. Each upstream has a name
    (e.g. ``us``, ``ci``) and a URL. Upstreams can also carry send settings:
    a patchwork URL, a sendemail identity, a To address and flags controlling
    ``get_maintainer.pl`` and subject-tag processing. Use ``patman upstream``
    commands to manage these.

**Patchwork project**
    The patchwork server tracks patches for a project (e.g. ``U-Boot``).
    Each upstream can point to a different patchwork server, and patman needs
    to know the project name on that server so it can look up series links.
    Use ``patman patchwork set-project`` to configure this per upstream.

**Sendemail identity**
    Git supports multiple SMTP configurations via ``[sendemail "<name>"]``
    sections in ``.gitconfig``. An upstream can reference one of these
    identities so that patman passes ``--identity`` to ``git send-email``
    automatically. To pick one for a single run, or when you have no
    upstream configured, pass ``patman send -I <identity>``; it overrides
    the upstream's identity.

**Series upstream**
    Each series can be associated with an upstream. When you send the series,
    patman looks up the upstream's settings and applies them. This means the
    correct identity, To address and patchwork server are used without any
    extra flags on the command line.

Adding upstreams
................

Add each upstream with its git remote name, its URL and, optionally, its
patchwork project, along with its settings::

    patman upstream add us https://git.u-boot-project.org/u-boot/u-boot \
        U-Boot -p https://patchwork.ozlabs.org -t u-boot

    patman upstream add ci https://concept.u-boot.org/u-boot/u-boot.git \
        U-Boot -p https://patchwork.deinde.dev -t concept -m --no-tags

The options are:

``-p`` / ``--patchwork-url``
    URL of the patchwork server for this upstream

``-t`` / ``--series-to``
    Patman alias for the To address (from your ``~/.patman`` alias file)

``-I`` / ``--identity``
    Git sendemail identity (selects ``[sendemail "<identity>"]`` from
    ``.gitconfig``)

``-m`` / ``--no-maintainers``
    Skip running ``get_maintainer.pl``

``--no-tags``
    Skip subject-tag alias processing

Giving the project name looks it up on the patchwork server; otherwise set
it later with ``patman patchwork set-project U-Boot ci``. Each upstream has
its own project entry, since each patchwork server has its own project IDs.

Add the To aliases to the ``[alias]`` section of ``~/.patman``::

    [alias]
    concept: U-Boot Concept <concept@u-boot.org>

Use ``patman upstream ls`` to list the upstreams, ``patman upstream default
us`` to choose the default, and ``patman upstream set`` to change one, with
the same options as ``upstream add`` plus ``--maintainers`` and ``--tags`` to
turn those back on::

    patman upstream set ci -I concept

Sendemail identities
....................

If the upstreams use different SMTP servers or credentials, add a git
sendemail identity for each in ``.gitconfig``. The ``[sendemail]`` settings
are the default, and a ``[sendemail "<name>"]`` section overrides them for
that identity (the servers here are just examples)::

    [sendemail]
        smtpserver = smtp.example.com
        smtpserverport = 587
        smtpencryption = tls

    [sendemail "concept"]
        smtpserver = smtp.gmail.com
        smtpserverport = 587
        smtpencryption = tls
        smtpuser = user@gmail.com

An upstream with ``-I concept`` makes patman pass ``--identity=concept`` to
``git send-email``.

The upstream of a series
........................

When a series is added, it uses the upstream which its branch tracks, if
that remote is one of the upstreams. Otherwise give it with ``-S``, or set it
later::

    patman series add -S us
    patman series -s video set-upstream us

A series with no upstream is sent with the default settings, so set one for
every series which is not for the default tree.

Sending
.......

``patman series send`` applies the upstream's settings to the series:

- passes ``--identity`` to ``git send-email`` if configured
- sets the To address from ``--series-to`` if no ``Series-to:`` tag is present
- skips ``get_maintainer.pl`` if ``--no-maintainers`` is set
- skips tag processing if ``--no-tags`` is set

If the series has a ``Series-to:`` tag that does not match the upstream's
To address, patman refuses to send it, which catches a series about to go
to the wrong mailing list. Plain ``patman send`` does not use the upstream
settings, so use ``patman series send`` for these series.

The upstream's patchwork server is also used for the series' links, status
and tags.


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
