.. SPDX-License-Identifier: GPL-2.0+

A first series: a worked example
================================

This walks through sending a small series with patman, from a branch of
commits to the email on the mailing list. It uses a real series: three
binman documentation patches for U-Boot, sent in April 2026 and merged in
v2026.07 (the first is commit 4dc8f10edf9 in mainline).

Nothing here needs the ``patman series`` database: plain ``patman send``
is enough to turn a branch of commits into a series and send it. If patman
is not set up yet, see :doc:`getting-started` first.

The branch
----------

Each series lives on its own branch. This one is called ``binj-us`` and
holds three commits on top of ``us/master``, where ``us`` is the git remote
for upstream U-Boot::

    $ git log --oneline us/master..binj-us
    a717454aa46 binman: Flesh out the softhsm2-util bintool docstring
    42dd64eafda binman: Flesh out the pkcs11-tool bintool docstring
    26d2ef563ca binman: Flesh out the p11-kit bintool docstring

The branch tracks ``us/master``, which lets patman work out how many
commits are in the series::

    git branch --set-upstream-to=us/master binj-us

Without that, give the count with ``-c``, e.g. ``patman send -c3 -n``.

Each commit is an ordinary commit, with a subject, a message and a
sign-off, e.g. the first one::

    binman: Flesh out the p11-kit bintool docstring

    The Sphinx-generated bintools.rst currently produces an empty section
    for this bintool, since its class docstring is only a single line and
    so the body under the heading is blank.

    Extend the docstring with a short description of what p11-kit does and
    how binman uses it, so the generated documentation has useful content.

    Suggested-by: Heinrich Schuchardt <xypron.glpk@gmx.de>
    Signed-off-by: Simon Glass <sjg@chromium.org>

Adding the tags
---------------

patman reads tags from the commit messages to know where to send the
series and what the cover letter says. They usually go in the top commit,
so that ``git commit --amend`` is enough to change them. Here is the top
commit of the series, with its tags::

    binman: Flesh out the softhsm2-util bintool docstring

    The Sphinx-generated bintools.rst currently produces an empty section
    for this bintool, since its class docstring is only a single line and
    so the body under the heading is blank.

    Extend the docstring with a short description of what softhsm2-util
    does and how binman uses it, so the generated documentation has useful
    content.

    Series-to: u-boot
    Series-cc: heinrich
    Cover-letter:
    binman: Tidy up some missing bintool docs
    Some of the bintools are missing documentation. This series adds the
    missing pieces to bring things up to scratch.
    END

    Suggested-by: Heinrich Schuchardt <xypron.glpk@gmx.de>
    Signed-off-by: Simon Glass <sjg@chromium.org>

``Series-to: u-boot``
    Sends the series to the U-Boot mailing list. ``u-boot`` is an alias,
    found in U-Boot's ``doc/git-mailrc``

``Series-cc: heinrich``
    Copies Heinrich, who suggested the change. ``heinrich`` is an alias in
    the ``[alias]`` section of ``~/.patman``::

        [alias]
        heinrich: Heinrich Schuchardt <xypron.glpk@gmx.de>

    A full address can be used instead of an alias.

``Cover-letter:`` ... ``END``
    The cover letter: its first line is the subject and the rest is the
    body. patman adds the list of patches and the diffstat

patman removes these tags from the patches it creates, so they never
reach the mailing list. The full list of tags is in :doc:`/tags`.

Checking the series
-------------------

With the branch checked out, do a dry run first. This creates the patch
files, checks them and shows who they would go to, without sending
anything::

    $ patman send -n
    Cleaned 3 patches
    Cc processing complete
    Dry run, so not doing much. But I would do this:

    Send a total of 3 patches with a cover letter.
       0001-binman-Flesh-out-the-p11-kit-bintool-docstring.patch
          Cc:  Alper Nebi Yasak <alpernebiyasak@gmail.com>
          Cc:  Neha Malcom Francis <n-francis@ti.com>
          Cc:  Peng Fan <peng.fan@nxp.com>
          Cc:  Philippe Reynes <philippe.reynes@softathome.com>
          Cc:  Simon Glass <sjg@chromium.org>
          Cc:  Stefan Herbrechtsmeier <stefan.herbrechtsmeier@weidmueller.com>
          Cc:  Tom Rini <trini@konsulko.com>
          Cc:  Wojciech Dubowik <Wojciech.Dubowik@mt.com>
       0002-binman-Flesh-out-the-pkcs11-tool-bintool-docstring.patch
          ...
       0003-binman-Flesh-out-the-softhsm2-util-bintool-docstring.patch
          ...
    To:       u-boot@lists.denx.de
    Cc:       Heinrich Schuchardt <xypron.glpk@gmx.de>
    Version:  None
    Prefix:   None
    Postfix:          None
    Cover: 3 lines
          Cc:  Alper Nebi Yasak <alpernebiyasak@gmail.com>
          ...
    Git command: git send-email --annotate --to u-boot@lists.denx.de
    --cc "Heinrich Schuchardt <xypron.glpk@gmx.de>" --cc-cmd "patman send
    --cc-cmd /tmp/patman.1877444" 0000-cover-letter.patch 0001-binman-...

Some points to note:

- Each patch is checked with U-Boot's ``scripts/checkpatch.pl``. It prints
  nothing here because there are no problems; otherwise each warning and
  error is listed, with the file and line it is about
- The ``To:`` and ``Cc:`` lines come from the tags. The ``Cc:`` lines under
  each patch come from ``scripts/get_maintainer.pl``, which finds the
  maintainers and reviewers of the files the patch changes. The cover
  letter goes to all of them. Use ``-m`` to leave these out
- ``Version: None`` means this is the first version, so the subjects are
  just ``[PATCH n/3]``

The patch files stay in the current directory, so they can be read before
sending. The cover letter, ``0000-cover-letter.patch``, starts::

    Subject: [PATCH 0/3] binman: Tidy up some missing bintool docs

    Some of the bintools are missing documentation. This series adds the
    missing pieces to bring things up to scratch.


    Simon Glass (3):
      binman: Flesh out the p11-kit bintool docstring
      binman: Flesh out the pkcs11-tool bintool docstring
      binman: Flesh out the softhsm2-util bintool docstring

     tools/binman/btool/p11_kit.py       | 10 +++++++++-
     tools/binman/btool/pkcs11_tool.py   | 10 +++++++++-
     tools/binman/btool/softhsm2_util.py | 11 ++++++++++-
     3 files changed, 28 insertions(+), 3 deletions(-)

    ---
    base-commit: 052988aa29bfd506d7ce207fbb3f5374a5dbecbb
    branch: binj-us

and the third patch has its tags removed, ending with just the two
trailers::

    Subject: [PATCH 3/3] binman: Flesh out the softhsm2-util bintool docstring

    ...
    does and how binman uses it, so the generated documentation has useful
    content.

    Suggested-by: Heinrich Schuchardt <xypron.glpk@gmx.de>
    Signed-off-by: Simon Glass <sjg@chromium.org>
    ---

If checkpatch reports a problem, fix the commit (e.g. with ``git rebase
-i``) and run the dry run again.

Sending it
----------

When the dry run looks right, send the series::

    patman send

This runs the ``git send-email`` command shown above. Because of
``--annotate``, git first opens the cover letter and the patches in your
editor (set by ``core.editor`` or ``$EDITOR``), as a last chance to read
or change them. Save and close the editor to carry on.

git then shows each message's headers in turn and asks before sending it::

    Send this email? ([y]es|[n]o|[e]dit|[q]uit|[a]ll):

The answers are:

``y``
    send this message
``n``
    skip this message, but carry on with the rest
``e``
    open this message in the editor again, then ask again
``q``
    stop, without sending this message or any after it
``a``
    send this message and all the rest without asking again

Answering ``a`` at the cover letter sends the whole series. Whether git asks
is controlled by the ``sendemail.confirm`` setting; see
``git help send-email``. The four messages go out as one thread, with each
patch a reply to the cover letter.

If you are sending through the web relay (see :doc:`getting-started`), there
is no editor and no question: ``patman send`` posts the whole series at once.

What happened next
------------------

The series appeared on patchwork as series 500902. A reviewer asked for the
docstrings to be reworded and for the tests to use the bintools, so a
second version followed. For that, the tags gained a version and a change
log, and a fourth patch was added::

    Series-version: 2

    Cover-changes: 2
    - Add patch to route PKCS#11 test fixtures through the bintools

with a ``Series-changes:`` tag in each commit saying what changed in it,
e.g.::

    Series-changes: 2
    - Reword the docstring per Quentin's review: binman uses this bintool
      only as an availability check, not for runtime PKCS#11 operations

``patman send`` then sends ``[PATCH v2 n/4]``, with the change logs added
below the ``---`` line of each patch and to the cover letter. All four
patches were merged in v2026.07.

To see the reviews and collect their tags before sending the next version,
see :doc:`status-and-tags`. To keep track of versions like this, with
patchwork links and review status, see :doc:`series-workflow`.
