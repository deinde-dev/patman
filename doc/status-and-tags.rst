.. SPDX-License-Identifier: GPL-2.0+

Checking status and collecting tags
===================================

Once a series is sent, reviewers reply on the mailing list and patchwork
picks up their comments and tags, such as ``Reviewed-by:``. ``patman status``
shows these for each patch, and can copy the new tags into your commits,
ready for the next version.

This follows on from :doc:`first-series`, using the same series (``binj-us``,
three binman patches) to show the status and comments. That series gathered
comments but no tags, so the part on collecting tags uses another small
series of two patches, ``align-fix``, which was also merged in v2026.07. All
the output is from running patman on these branches.

Linking the branch to patchwork
-------------------------------

patman needs to know which series on patchwork belongs to the branch. Find
the series on patchwork; for U-Boot that is https://patchwork.ozlabs.org. The
v1 series in the worked example is at::

    https://patchwork.ozlabs.org/project/uboot/list/?series=500902

The number at the end is the series ID. Add it to the top commit with a
``Series-links:`` tag, alongside the other tags, using ``git commit
--amend``::

    Series-to: u-boot
    Series-cc: heinrich
    Series-links: 500902
    Cover-letter:
    ...

Use the ID of the series itself, not of one of its patches. patman removes
this tag from the patches it sends, like the others. U-Boot's
``.patman-defaults`` tells patman to use patchwork.ozlabs.org, so nothing
else is needed; for another project, set ``patchwork_url`` in ``~/.patman``
or use ``-P``.

Checking the status
-------------------

With the branch checked out, run::

    $ patman status
      1 binman: Flesh out the p11-kit bintool docstring
      2 binman: Flesh out the pkcs11-tool bintool docstrin
      3 binman: Flesh out the softhsm2-util bintool docstr
    0 new responses available in patchwork

Each patch is listed by number, with its subject cut short to fit. Below
each one come the tags it already has, then any new tags from patchwork,
marked with ``+``. This series has none, so the last line says there are no
new responses.

Reading the comments
--------------------

Comments without tags still matter. Add ``-c`` to show them, along with the
lines they quote::

    $ patman status -c
      1 binman: Flesh out the p11-kit bintool docstring
      2 binman: Flesh out the pkcs11-tool bintool docstrin
      3 binman: Flesh out the softhsm2-util bintool docstr
    Review: Quentin Schulz <quentin.schulz@cherry.de>
        > File: tools/binman/btool/softhsm2_util.py
        > Line: 7 / 7: from binman import bintool
        >
        > I would remove the last sentence. Maybe we should tell the user ...
        ...

    Review: Quentin Schulz <quentin.schulz@cherry.de>
        Hi Simon,
        ...
        Welllllllll... Binman currently doesn't use it for anything but
        checking if the tool is installed on the system and if not downloads
        it.
        ...

    0 new responses available in patchwork

Each review shows who sent it, then their reply. This is the review which led
to v2 of the series, described at the end of :doc:`first-series`.

Collecting tags
---------------

The ``align-fix`` series fixes two boards broken by a kbuild update. Its
branch has two commits, each with a ``Fixes:`` tag, and the top one has
``Series-links: 1:508846``. After it was sent, Andy Shevchenko reviewed the
first patch::

    $ patman status
      1 x86: edison: Avoid passing FORCE to the align comm
        Fixes: bd3f9ee679b4 ("kbuild: Bump the build system to 6.1")
      + Reviewed-by: Andy Shevchenko <andriy.shevchenko@linux.intel.com>
      2 arm: mxs: Avoid passing FORCE to commands
        Fixes: bd3f9ee679b4 ("kbuild: Bump the build system to 6.1")
    1 new response available in patchwork (use -d to write them to a new branch)

The ``Fixes:`` lines are tags the commits already have. The ``+`` line is a
new tag, which the first commit does not have yet.

Rather than adding it by hand, use ``-d`` to create a new branch with the new
tags added to the commits::

    $ patman status -d align-fix2
      1 x86: edison: Avoid passing FORCE to the align comm
        Fixes: bd3f9ee679b4 ("kbuild: Bump the build system to 6.1")
      + Reviewed-by: Andy Shevchenko <andriy.shevchenko@linux.intel.com>
      2 arm: mxs: Avoid passing FORCE to commands
        Fixes: bd3f9ee679b4 ("kbuild: Bump the build system to 6.1")
    1 new response available in patchwork
    1 response added from patchwork into new branch 'align-fix2'

The original branch is left as it is. In the new branch, the first commit
now ends::

    Use $< instead of $^, which excludes FORCE and restores the prefix.

    Fixes: bd3f9ee679b4 ("kbuild: Bump the build system to 6.1")
    Reviewed-by: Andy Shevchenko <andriy.shevchenko@linux.intel.com>

The new tags are added in alphabetical order at the end of the commit's
first block of tags, here just after ``Fixes:``, or at the end of the message
if it has none. Use ``-f`` to overwrite the new branch if it already exists.

Sending the next version
------------------------

The new branch is named for the next version, so carry on there. It does not
track an upstream branch yet, so set that too, to let patman count its
commits::

    git checkout align-fix2
    git branch --set-upstream-to=us/master

Make any changes the reviewers asked for, then add ``Series-version: 2`` and
a ``Series-changes: 2`` tag to each changed commit, as at the end of
:doc:`first-series`, and send it with ``patman send``. The collected tags go
out with it, so the reviewers can see that their tags were picked up.

Once v2 is on patchwork, add its ID to ``Series-links``, keeping the v1 link,
e.g. ``Series-links: 1:508846 2:<id>``. ``patman status`` looks up the link
for the version in the branch name, or the series' ``Series-version`` if the
name has no number. So on ``align-fix2`` it reports that there is no link for
v2 until this is done.

This series needed no v2: the tag was added when it was applied, and it was
merged in v2026.07. To keep track of a series across versions, with its
patchwork links and review status in a database, see 'Series Management' in
the manual, which covers the ``patman series`` commands.
