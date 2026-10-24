.. SPDX-License-Identifier: GPL-2.0+

Tracking a series with the database
===================================

The earlier examples use plain ``patman send`` and ``patman status``, with
the patchwork links and versions kept by hand in tags and branch names.
``patman series`` does this bookkeeping for you: it keeps a database of your
series, with each version, its patchwork link and the state of each patch,
and it finds the links, creates the branch for each new version and gathers
the tags.

This goes through the life of the same series as before, ``binj-us``, from
adding it to the database to archiving it once merged, and shows gathering
tags on ``align-fix``. All the output is from running patman on these
branches, starting with an empty database.

The database is ``.patman.db`` at the top of the git tree, so each clone has
its own. patman creates it the first time it is needed.

Setting up the project
----------------------

Tell patman which patchwork project the upstream remote belongs to, once for
each clone::

    $ patman patchwork set-project U-Boot us
    Project 'U-Boot' patchwork-ID 18 link-name 'uboot' remote 'us'

patman looks the project up on the patchwork server (patchwork.ozlabs.org for
U-Boot, from ``.patman-defaults``) and stores its ID, so that it can search
for your series there later. The remote, ``us`` here, can be left out if the
current branch tracks it or it is the only remote.

Adding the series
-----------------

Start with the branch as in :doc:`first-series`: three commits on
``us/master``, with ``Series-to:``, ``Series-cc:`` and a cover letter in the
top commit, and no ``Series-links:`` yet. Add it to the database::

    $ patman series add
    patman: ValueError: 3 commit(s) are unmarked; please use -m or -M

patman can track each patch by a ``Change-Id:`` tag, so that it can follow a
patch whose subject changes between versions. ``-m`` adds these tags to the
commits (patman removes them before sending); ``-M`` leaves the commits as
they are and matches patches by subject. Use ``-M`` here::

    $ patman series add -M
    Added series 'binj-us' v1 (3 commits)

The series takes its name from the branch and its description from the
cover letter::

    $ patman series ls
    Name             Description                               Accepted  Us  Versions
    ---------------  ----------------------------------------  --------  --  ---------------
    binj-us          binman: Tidy up some missing bintool docs      -/3      1
    ---------------  ----------------------------------------  --------  --  ---------------

``-/3`` means patman does not yet know the state of the three patches. The
``Us`` column shows the series' upstream, which is only needed with more than
one upstream (see :doc:`upstreams`).

The branch name gives the version: ``binj-us`` is v1, and v2 goes in
``binj-us2``. So the name for v1 must not end in a number.

Checking it before sending
--------------------------

Before sending the series, you can have the review agent look at it, as it
would at someone else's series (see :doc:`reviewing`). With the series'
branch checked out::

    $ patman series review

This reviews each patch, and the cover letter, and stores what it finds in
the database against v1, so that it can be read again at any time without
running the agent::

    $ patman series info -r

Fix what needs fixing in the commits, then send the series. A review of the
next version is given this one's findings, so that it can check they were
dealt with. See :ref:`review_before_send` for the options.

Sending it
----------

``patman series send`` sends the series like ``patman send``, with the same
options. ``-n`` comes before ``send``, since it belongs to ``patman series``::

    $ patman series -n send
    Cleaned 3 patches
    Cc processing complete
    Dry run, so not doing much. But I would do this:

    Send a total of 3 patches with a cover letter.
    ...

After sending for real, it waits for the series to appear on patchwork and
records its link. If patchwork is slow, or you sent the series with plain
``patman send``, find the link with ``autolink``::

    $ patman series autolink
    Setting link for series 'binj-us' v1 to 500902

This searches patchwork for the series and stores its link in the
database. It also adds ``Series-links: 1:500902`` to the top commit, so
that plain ``patman status`` works too; use ``--no-update`` to leave the
commit alone. Show the link with::

    $ patman series get-link
    500902

Following the reviews
---------------------

``patman series status`` works like ``patman status`` (see
:doc:`status-and-tags`), including ``-c`` and ``-C``, but for any series,
not just the current branch; use ``-s`` to pick one::

    $ patman series status
      1 binman: Flesh out the p11-kit bintool docstring
      2 binman: Flesh out the pkcs11-tool bintool docstrin
      3 binman: Flesh out the softhsm2-util bintool docstr
    0 new responses available in patchwork

``patman series gather`` does the same, but also records the state of each
patch in the database and adds any new tags to the commits in the branch::

    $ patman series gather
      1 binman: Flesh out the p11-kit bintool docstring
      2 binman: Flesh out the pkcs11-tool bintool docstrin
      3 binman: Flesh out the softhsm2-util bintool docstr
    3 patches updated (7 requests)

``binj-us`` gained no tags, but ``align-fix`` from :doc:`status-and-tags`
did. After adding that series in the same way::

    $ patman series gather
      1 x86: edison: Avoid passing FORCE to the align comm
        Fixes: bd3f9ee679b4 ("kbuild: Bump the build system to 6.1")
      + Reviewed-by: Andy Shevchenko <andriy.shevchenko@linux.intel.com>
      2 arm: mxs: Avoid passing FORCE to commands
        Fixes: bd3f9ee679b4 ("kbuild: Bump the build system to 6.1")
    2 patches updated (5 requests)
    Series 'align-fix' v1: all patches accepted upstream

Unlike ``patman status -d``, this updates the branch itself: the first commit
of ``align-fix`` now has the ``Reviewed-by:`` tag, after its ``Fixes:`` tag.
Use ``-G`` to skip adding the tags. The last line shows that all the patches
have been applied.

To see where all your series stand::

    $ patman series progress
    Name             Description                               Count  Status
    ---------------  ----------------------------------------  -----  ---------------
    binj-us          binman: Tidy up some missing bintool docs     3  3:superseded
    ---------------  ----------------------------------------  -----  ---------------
    1 series                                                       3  3:superseded

The three v1 patches are ``superseded``, since v2 replaced them on patchwork.

Starting the next version
-------------------------

When the series needs another version, create the branch for it with::

    $ patman series inc
    Incremented series 'binj-us' to v2

This creates ``binj-us2`` from ``binj-us``, checks it out, sets it to track
``us/master`` and adds ``Series-version: 2`` to the top commit. It also adds v2
to the database::

    $ patman series ls
    Name             Description                               Accepted  Us  Versions
    ---------------  ----------------------------------------  --------  --  ---------------
    binj-us          binman: Tidy up some missing bintool docs      -/3      1 2
    ---------------  ----------------------------------------  --------  --  ---------------

Now make the changes as in :doc:`next-version`: rebase, change the commits
with their ``Series-changes: 2`` tags, and add the fourth patch. The database
still lists the patches as they were when ``inc`` ran, so update it from the
branch with ``scan``::

    $ patman series scan -M
    Scanned 4 commits (1 added)

    $ patman series patches
    Branch 'binj-us2' (total 4): 4:unknown
    Seq State      Com PatchId Commit     Subject
      0 unknown      -         a3cf5aba2b binman: Flesh out the p11-kit bintool docstring
      1 unknown      -         a5cb5acb4a binman: Flesh out the pkcs11-tool bintool docstring
      2 unknown      -         82b3c604fd binman: Flesh out the softhsm2-util bintool docstring
      3 unknown      -         f6a8165b33 binman: Use bintool wrappers for PKCS#11 tools in tests

Then send v2 with ``patman series send``. Once it is on patchwork, its link
is recorded as before, and the top commit gets both links::

    $ patman series autolink
    Setting link for series 'binj-us' v2 to 502878

    Series-links: 2:502878 1:500902

Gathering on v2, with ``-C`` to show the comments on the cover letter, shows
Quentin's review and that the series has been applied::

    $ patman series gather -C
    Cov [v2,0/4] binman: Tidy up some missing bintool docs
    From: Quentin Schulz <quentin.schulz@cherry.de>: 2026-05-08T12:01:32
    Hi Simon,
    ...
      4 binman: Use bintool wrappers for PKCS#11 tools in
    4 patches and cover letter updated (10 requests)
    Series 'binj-us' v2: all patches accepted upstream

``patman series info`` shows everything the database knows about a series::

    $ patman series info
    Series: binj-us
      Description: binman: Tidy up some missing bintool docs
      Upstream: (none)

      Version 1:
        Link: 500902
        Description: binman: Tidy up some missing bintool docs
        Patches: 3
          1: binman: Flesh out the p11-kit bintool docstring [superseded]
          2: binman: Flesh out the pkcs11-tool bintool docstring [superseded]
          3: binman: Flesh out the softhsm2-util bintool docstring [superseded]

      Version 2:
        Link: 502878
        Description: binman: Tidy up some missing bintool docs
        Cover: [v2,0/4] binman: Tidy up some missing bintool docs
        Patches: 4
          1: binman: Flesh out the p11-kit bintool docstring [accepted]
          2: binman: Flesh out the pkcs11-tool bintool docstring [accepted]
          3: binman: Flesh out the softhsm2-util bintool docstring [accepted]
          4: binman: Use bintool wrappers for PKCS#11 tools in tests [accepted]

Archiving it
------------

Once the series is merged, archive it::

    $ patman series -s binj-us archive
    Archived series 'binj-us' (2 versions)

This deletes the ``binj-us`` and ``binj-us2`` branches, keeping each as a tag
named with the date, here ``binj-us-17oct26`` and ``binj-us2-17oct26``. The
series no longer appears in most listings::

    $ patman series ls
    Name             Description                               Accepted  Us  Versions
    ---------------  ----------------------------------------  --------  --  ---------------
    align-fix        Repair boards broken by the 6.1 kbuild bu      2/2      1
    ---------------  ----------------------------------------  --------  --  ---------------

Use ``-A`` to include archived series, and ``patman series unarchive`` to
bring back the branches if you need to do more with it.

The other ``patman series`` commands, such as ``summary``, ``find``, ``open``
and ``rm``, are described in :doc:`/reference/series`.
