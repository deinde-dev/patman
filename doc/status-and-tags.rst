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

Comments without tags still matter. Add ``-c`` to show the comments on each
patch, along with the lines they quote::

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

Make any changes the reviewers asked for, then add ``Series-version: 2`` and a
``Series-changes: 2`` tag to each changed commit, and send it with
``patman send``; :doc:`next-version` goes through this in detail. The
collected tags go out with it, so the reviewers can see that their tags were
picked up.

Once v2 is on patchwork, add its ID to ``Series-links``, keeping the v1 link,
e.g. ``Series-links: 1:508846 2:<id>``. ``patman status`` looks up the link
for the version in the branch name, or the series' ``Series-version`` if the
name has no number. So on ``align-fix2`` it reports that there is no link for
v2 until this is done.

Comments on the cover letter
----------------------------

Replies to the cover letter are about the whole series, and ``-c`` does not
show them. Use ``-C`` for those. Here it shows that ``align-fix`` needed no
v2, since the maintainer applied it::

    $ patman status -C
    Cov [0/2] Repair boards broken by the 6.1 kbuild bump
    From: Tom Rini <trini@konsulko.com>: 2026-06-15T18:52:59
    On Sat, 13 Jun 2026 07:05:11 -0600, Simon Glass wrote:

    > Commit bd3f9ee679b4 added FORCE to several if_changed rules whose
    > recipes expand $^, so FORCE leaks in as a stray argument and the
    > command fails. Use $< to restore the Edison and i.MX23/28 images.
    ...

    Applied to u-boot/master, thanks!

    [1/2] x86: edison: Avoid passing FORCE to the align command
          commit: 895d3c67cd58fee6815d0e2799ea7d22474accb9
    [2/2] arm: mxs: Avoid passing FORCE to commands
          commit: 3ac35afdd2cc73db0ac3911937ce75b58a6f90b8

      1 x86: edison: Avoid passing FORCE to the align comm
    ...

The cover letter is listed first, as ``Cov``, followed by each reply to it
and then the patches as before. Use ``-c`` and ``-C`` together to see all
the comments. The tag was added to the first patch when it was applied, so
it reached mainline even though no v2 was sent.

A reviewer may also give a tag for the whole series by replying to the cover
letter. On v2 of ``binj-us``, Quentin Schulz did just that::

    $ patman status -C
    Cov [v2,0/4] binman: Tidy up some missing bintool docs
    From: Quentin Schulz <quentin.schulz@cherry.de>: 2026-05-08T12:01:32
    ...
    It's a detail and I don't have anything else to suggest for rephrasing.

    Reviewed-by: Quentin Schulz <quentin.schulz@cherry.de>
    ...
      4 binman: Use bintool wrappers for PKCS#11 tools in
    0 new responses available in patchwork

``patman status`` collects only the tags given on each patch, so it does not
count this one and ``-d`` does not add it. If you send another version, add
such a tag to each commit by hand.

To keep track of a series across versions, with its patchwork links and
review status in a database, see 'Series Management' in the manual, which
covers the ``patman series`` commands.
