.. SPDX-License-Identifier: GPL-2.0+

Sending the next version
========================

Most series need more than one version: reviewers ask for changes, and
the series is sent again with a change log saying what is different. This
follows on from :doc:`status-and-tags`, where Quentin Schulz's review of
v1 of ``binj-us`` asked for the docstrings to be reworded and for the tests
to use the bintools. v2 made those changes and added a fourth patch. It was
sent in May 2026 and all four patches were merged in v2026.07. All the output
is from running patman on the real v2 branch, ``binj-us2``.

A branch for the new version
----------------------------

Keep the old version as it is and start a new branch for the next one,
named with the version number at the end::

    git checkout -b binj-us2 binj-us
    git branch --set-upstream-to=us/master

The number matters: ``patman status`` uses it to pick the patchwork link
for that version (see `After sending`_). A branch made from another local
branch does not track ``us/master``, hence the second command.

This is also a good time to rebase onto the latest upstream, so that the
new version applies cleanly::

    git fetch us
    git rebase us/master

``binj-us`` was based on a commit from 21 April; ``binj-us2`` is based on
one from 4 May.

Changing the commits
--------------------

Make the changes the reviewers asked for, e.g. with ``git rebase -i
us/master`` and ``edit`` on each commit to change. As you change each
commit, add a ``Series-changes:`` tag to it, giving the version and what
changed. The first commit of ``binj-us2`` ends::

    Extend the docstring with a short description of what p11-kit does and
    how binman uses it, so the generated documentation has useful content.

    Series-changes: 2
    - Reword the docstring per Quentin's review: binman uses this bintool
      only as an availability check, not for runtime PKCS#11 operations

    Suggested-by: Heinrich Schuchardt <xypron.glpk@gmx.de>
    Signed-off-by: Simon Glass <sjg@chromium.org>

The other two docstring commits have the same change, so they have the same
``Series-changes:`` entry. A change can run over several lines, as long as
each line after the first starts with a space. Writing the change log as you
go is easier than remembering it all at the end.

Adding a patch
--------------

The fourth patch, which makes the tests use the bintools, is a new commit
on top. Since it is now the top commit, the series tags move into it, so
that ``git commit --amend`` still reaches them; tags work in any commit, but
keeping them together at the top is easier. The new commit, with its tags::

    binman: Use bintool wrappers for PKCS#11 tools in tests

    The PKCS#11 signing tests in ftest.py call tools.run('softhsm2-util', ...)
    directly (and the equivalent for pkcs11-tool and p11-kit), even though
    the test setup has already constructed the corresponding Bintool
    instances. As Quentin Schulz observed on v1, the bintool wrapper for
    these tools is currently used only as an "is this installed?" probe.

    Route the eight remaining call sites in ftest.py through
    <bintool>.run_cmd(...), which the Bintool base class already provides.
    The change is test-side only; no production binman code calls these
    tools.

    Series-to: u-boot
    Series-cc: heinrich
    Series-process-log: sort, uniq
    Cover-letter:
    binman: Tidy up some missing bintool docs
    Some of the bintools are missing documentation. This series adds the
    missing pieces to bring things up to scratch.
    END

    Series-links: 1:500902
    Series-version: 2

    Cover-changes: 2
    - Add patch to route PKCS#11 test fixtures through the bintools

    Series-changes: 2
    - New patch in v2 (per Quentin's review): replace tools.run() of
      softhsm2-util, pkcs11-tool and p11-kit in ftest.py with the
      Bintool.run_cmd() wrapper that already exists

    Suggested-by: Quentin Schulz <quentin.schulz@cherry.de>
    Signed-off-by: Simon Glass <sjg@chromium.org>

The new tags are:

``Series-version: 2``
    The version, which goes into the subject of each patch, as
    ``[PATCH v2 n/4]``

``Cover-changes: 2``
    A change which belongs only in the cover letter, here the summary that
    a patch was added

``Series-changes: 2``
    The change log of this commit. For a new patch, say that it is new and
    why

``Series-process-log: sort, uniq``
    Sorts the change log in the cover letter and merges duplicate entries
    (see `The change log in the cover letter`_)

``Series-links: 1:500902`` stays, now with the version, since there will
soon be a v2 link too. ``Suggested-by: Quentin`` credits the reviewer who
suggested the new patch.

Checking the series
-------------------

Do a dry run as before::

    $ patman send -n
    Cleaned 4 patches
    Cc processing complete
    Dry run, so not doing much. But I would do this:

    Send a total of 4 patches with a cover letter.
       0001-binman-Flesh-out-the-p11-kit-bintool-docstring.patch
       ...
       0004-binman-Use-bintool-wrappers-for-PKCS-11-tools-in-tes.patch
       ...
    To:       u-boot@lists.denx.de
    Cc:       Heinrich Schuchardt <xypron.glpk@gmx.de>
    Version:  2
    ...

``Version: 2`` shows that patman picked up the new version. Each patch now
has its change log just below the ``---`` line, where it is seen by the
reviewers but dropped when the patch is applied::

    Subject: [PATCH v2 1/4] binman: Flesh out the p11-kit bintool docstring

    ...
    Suggested-by: Heinrich Schuchardt <xypron.glpk@gmx.de>
    Signed-off-by: Simon Glass <sjg@chromium.org>
    ---

    Changes in v2:
    - Reword the docstring per Quentin's review: binman uses this bintool
      only as an availability check, not for runtime PKCS#11 operations

     tools/binman/btool/p11_kit.py | 11 ++++++++++-
     1 file changed, 10 insertions(+), 1 deletion(-)

The change log in the cover letter
----------------------------------

The cover letter gathers the change logs of all the patches, with the
``Cover-changes:`` entries::

    Subject: [PATCH v2 0/4] binman: Tidy up some missing bintool docs

    Some of the bintools are missing documentation. This series adds the
    missing pieces to bring things up to scratch.

    Changes in v2:
    - Add patch to route PKCS#11 test fixtures through the bintools
    - New patch in v2 (per Quentin's review): replace tools.run() of
      softhsm2-util, pkcs11-tool and p11-kit in ftest.py with the
      Bintool.run_cmd() wrapper that already exists
    - Reword the docstring per Quentin's review: binman uses this bintool
      only as an availability check, not for runtime PKCS#11 operations

    Simon Glass (4):
    ...

``Series-process-log: sort, uniq`` sorts the entries and merges the three
identical 'Reword the docstring' entries into one. Without it, the same
cover letter has::

    Changes in v2:
    - Reword the docstring per Quentin's review: binman uses this bintool
      only as an availability check, not for runtime PKCS#11 operations
    - Reword the docstring per Quentin's review: binman uses this bintool
      only as an availability check, not for runtime PKCS#11 operations
    - Reword the docstring per Quentin's review: binman uses this bintool
      only as an availability check, not for runtime PKCS#11 operations
    - Add patch to route PKCS#11 test fixtures through the bintools
    - New patch in v2 (per Quentin's review): replace tools.run() of
      softhsm2-util, pkcs11-tool and p11-kit in ftest.py with the
      Bintool.run_cmd() wrapper that already exists

Use ``uniq`` alone to merge the duplicates but keep the order.

Sending it
----------

Send it with ``patman send``, answering git's questions as in
:doc:`first-series`. To send the new version as a reply to the old one, as
some projects prefer, give the Message-ID of the old cover letter with
``-r``::

    patman send -r '<message-id>'

After sending
-------------

v2 appeared on patchwork as series 502878. Add it to ``Series-links``,
keeping the v1 link::

    Series-links: 1:500902 2:502878

``patman status`` on ``binj-us2`` now looks up the v2 link, since the branch
name ends in 2, and ``binj-us`` still finds v1. On v2, Quentin replied to the
cover letter with a ``Reviewed-by:`` for the whole series, which ``patman
status -C`` shows (see :doc:`status-and-tags`).

No v3 was needed: the maintainer applied all four patches and they were
merged in v2026.07. If another version is needed, repeat these steps with
``binj-us3``, ``Series-version: 3`` and ``Series-changes: 3`` entries, which
go alongside the ``Series-changes: 2`` ones; each version's changes are kept
in the patches, so the reviewers can see the whole history.

Other tags which help with new versions:

``Commit-changes: n``
    A change which goes only in that commit's change log, not in the cover
    letter, e.g. a small fix-up which is not worth listing there

``Commit-notes:`` ... ``END``
    Notes for the reviewers, shown below the ``---`` of that patch

``Series-prefix: RFC``
    Sends the series as ``[RFC PATCH]``, e.g. for an early version which is
    not ready to be applied

These and the other tags are described in 'How to add tags' in the manual.
To have patman track the versions of a series for you, with the patchwork
links and review status in a database, see 'Series Management' in the
manual, which covers the ``patman series`` commands.
