.. SPDX-License-Identifier: GPL-2.0+

.. _maint:

Maintaining a project
=====================

A maintainer applies other people's patches to the project's tree, then pushes
them upstream. Each project has its own rules for this, such as which trailers
each commit ends with, and whether merge commits are allowed. Following them
is mechanical, so patman holds them as settings, applies patches by them and
checks the commits against them, rather than leaving you (or an AI assistant)
to read them.

Applying a series
-----------------

``patman maint apply`` applies a series from the mailing list to the push
branch, ``main-push`` for an upstream branch of ``main``, which it makes from
the upstream branch if there is none. Give it the Message-ID of the cover
letter, or of the first patch if there is none, or a link to it on lore::

    $ patman maint apply 20261102093012.81234-1-fred@x.org
       1 [v2,1/2] libfdt: Use fdt_ro_probe_() in fdt_path_offset()
           + Reviewed-by: David Gibson <david@gibson.dropbear.id.au>
       2 [v2,2/2] libfdt: Drop an unused variable
           + Reviewed-by: David Gibson <david@gibson.dropbear.id.au>
           + Tested-by: Herve Codina <herve.codina@bootlin.com>
    Created main-push from us/main

    Checking 2 commits in us/main..main-push
      ok   4c9e1a7b2d30 libfdt: Use fdt_ro_probe_() in fdt_path_offset()
      ok   a81f6d02c5e9 libfdt: Drop an unused variable
    2 of 2 patches applied to main-push

For one of your own series, give its name, or nothing for the series on the
current branch: patman applies the latest version which it sent, using the
Message-ID it recorded then::

    $ patman maint apply -n
    Series 'readme-meson' v1: 20261028112007.3980089-1-sjg@chromium.org
       1 [1/2] README: Explain how to build with meson
           + Reviewed-by: David Gibson <david@gibson.dropbear.id.au>
           + Reviewed-by: Herve Codina <herve.codina@bootlin.com>
       2 [2/2] README: Use meson in the Python section
           + Reviewed-by: David Gibson <david@gibson.dropbear.id.au>
           + Reviewed-by: Herve Codina <herve.codina@bootlin.com>
    Would apply 2 of 2 patches to main-push

It reads the series from the upstream's list on lore (see
:ref:`series_lore`), with the Reviewed-by, Acked-by and Tested-by tags given
in reply to each patch, or to the cover letter, which go on every patch. Only
the replies to this version count: a tag given to an earlier version must be
added to the new one by its author. Each patch gets these tags after its own,
then the trailers which ``trailers_end`` gives, e.g. its ``Message-ID:`` and
your sign-off, leaving out your sign-off on your own patches if
``signoff_own`` is ``no``. Then the push branch is checked, as below.

The patches are applied in a temporary worktree, so the tree you are working
in is not touched, whatever is checked out and whatever changes it has. If the
push branch is checked out somewhere, they are applied there instead, once it
has no changes. A patch which does not apply is tried again with a three-way
merge; if that fails too, nothing is applied.

It refuses a series which is not all on the list yet, or which has a newer
version, unless given ``-f``. A series which is already upstream or on the
push branch has nothing to apply, so it says so; if only some patches are
there, it says which ``-P`` applies the others. ``-P`` applies only some
patches, e.g. ``-P 1-3,5``, and ``-n`` says what would be applied without
applying it.

To apply a series again, e.g. once more tags have come in, or to throw away
what is on the push branch, give ``-R``. This starts the push branch again
from upstream, dropping everything on it, then applies the series. It says
how many commits it drops and where the push branch was; with ``-n`` it only
says what it would drop. If the series then does not apply, the push branch is
put back as it was::

    $ patman maint apply -R 20261102093012.81234-1-fred@x.org
       1 [v2,1/2] libfdt: Use fdt_ro_probe_() in fdt_path_offset()
           + Reviewed-by: David Gibson <david@gibson.dropbear.id.au>
           + Acked-by: Rob Herring <robh@kernel.org>
       2 [v2,2/2] libfdt: Drop an unused variable
           + Reviewed-by: David Gibson <david@gibson.dropbear.id.au>
           + Tested-by: Herve Codina <herve.codina@bootlin.com>
    Starting main-push again from us/main, dropping 2 commits (was a81f6d02c5e9)

    Checking 2 commits in us/main..main-push
      ok   7d2b90e4f1a8 libfdt: Use fdt_ro_probe_() in fdt_path_offset()
      ok   e05c3a9b6d12 libfdt: Drop an unused variable
    2 of 2 patches applied to main-push

Checking the queue
------------------

``patman maint check`` checks the queue: the commits which are not upstream
yet. It checks the branch which is checked out, if that has commits of its
own, else the push branch, or the branch given with ``-b``. Run it before
pushing, however the patches were applied, e.g. with ``b4 am``::

    $ patman maint check
    Checking 3 commits in us/main..main
      ok   1c3f0a2b9d4e libfdt: Add fdt_get_alias_namelen()
      ok   7e21d9c0a3b5 tests: Check fdt_get_alias_namelen()
      FAIL 0b9a8f3e6c12 dtc: Warn about a missing #size-cells
           - No Signed-off-by from the author, Fred Bloggs <fred@x.org>
    1 of 3 with problems

It exits with 1 if any commit has a problem, so a script can stop before
pushing.

Testing the queue
-----------------

``patman maint test`` runs the project's tests, which the ``test`` setting
gives, on the same branch as ``maint check``. The tests run one after the
other in a temporary worktree of the branch's last commit, so they see only
what is committed, the build starts clean and the tree you are working in is
not touched. They stop at the first which fails, showing the end of its
output::

    $ patman maint test
    Testing main-push at e5353eb4e214
      ok   make check
      ok   meson setup build
      ok   meson test -C build

Running CI
----------

``patman maint ci`` checks the branch as ``maint check`` does, then pushes it
to each place which the ``ci_push`` setting gives and waits for CI to finish
on the commit pushed, on GitHub (using ``gh``) and GitLab, as the ``github``
and ``gitlab`` settings say. Any run for that commit counts, wherever it was
started from::

    $ patman maint ci
    Checking 2 commits in us/main..main-push
      ok   a2f78f549e91 README: Explain how to build with meson
      ok   e5353eb4e214 README: Use meson in the Python section
    Pushed e5353eb4e214 to david +main-push:ci, gl +main-push:ci
      ok      github https://github.com/dgibson/dtc/actions/runs/40843398095
      ok      gitlab https://gitlab.com/dgibson/dtc/-/pipelines/3002858044

Nothing is pushed if the check fails. It exits with 1 unless CI passes
everywhere, including when CI does not start within five minutes, or does
not finish within an hour. ``-n`` says where the branch would be pushed, and
``-W`` pushes it without waiting.

The rules
---------

The rules go in the ``[maint]`` section of the settings, which is best kept
in the tree's ``.patman``, or in a tracked ``.patman-defaults`` so that every
maintainer of the project has them. For dtc:

.. code-block:: ini

    [maint]
    upstream: git.kernel.org/pub/scm/utils/dtc/dtc.git main
    history: prefer-linear
    trailers_end: message-id signoff
    pr_trailers_end: link signoff
    signoff_own: no
    require: author-signoff
    issue_trailers: Closes Link
    github: dgibson/dtc
    gitlab: dgibson/dtc
    test: make check; meson setup build; meson test -C build
    ci_push: david +{branch}:ci; gl +{branch}:ci

The settings are:

``upstream``
    The upstream remote and its branch, which the queue goes on top of. The
    remote may be given by its name, e.g. ``us main``, or by its URL without
    the scheme, as here, so that the rules suit everyone whatever they call
    their remotes. This one is needed

``push_branch``
    The branch which ``patman maint apply`` applies patches to (default: the
    upstream branch with ``-push`` after it, e.g. ``main-push``)

``signoff``
    Your sign-off, e.g. ``Simon Glass <sjg@chromium.org>``. The default is
    your name and email address in git

``history``
    ``linear`` (the default) allows no merge commits, ``prefer-linear``
    allows one only with ``--allow-merge``, so that it is not pushed by
    mistake, and ``any`` allows them

``trailers_end``
    The trailers which each patch from the mailing list ends with, in order:
    ``message-id`` for its ``Message-ID:``, ``link`` for a ``Link:`` to the
    message, e.g. on lore, and ``signoff`` for your sign-off. The default is
    ``signoff``

``pr_trailers_end``
    The same for the last commit of a pull request on GitHub, or a merge
    request on GitLab, where ``link`` is a ``Link:`` to the request. The
    other commits of the request end with your sign-off. The default is
    ``signoff``

``signoff_own``
    ``no`` if your own patches do not end with your sign-off, since you
    signed them off as their author, e.g. when you apply them without
    ``git am -s``. The default is ``yes``

``require``
    Other rules: ``author-signoff`` refuses a commit which its author has not
    signed off, for the Developer's Certificate of Origin

``issue_trailers``
    The trailers which may link to an issue (default ``Closes Link``), e.g.
    ``Closes:`` for an issue which the commit fixes and ``Link:`` for one
    which it relates to. A link to an issue in any other trailer, such as
    ``Fixes:``, is a problem

``github``, ``gitlab``
    The project's repos, e.g. ``dgibson/dtc``. A link to an issue must be to
    one of them, and a pull or merge request is recognised by its link to
    one of them. ``maint ci`` waits for CI in each of them

``test``
    The commands which ``maint test`` runs, separated by semicolons. Each runs
    in the shell, at the top of the tree

``ci_push``
    Where ``maint ci`` pushes the branch to run CI, separated by semicolons:
    each is a remote and a refspec, with ``{branch}`` for the branch, e.g.
    ``david +{branch}:ci``. The ``+`` forces the push, since a branch which
    only runs CI is usually replaced each time

What is checked
---------------

The branch must contain the upstream branch, so that it can be pushed without
forcing. If it does not, the check says how to rebase it, then checks its
commits anyway. A branch which is behind the upstream, with no commits of its
own, has nothing to check; the check says how to bring it up to date. Each
commit is checked:

- A patch from the list is one with a ``Message-ID:``, or a ``Link:`` to a
  message. Its trailers end as ``trailers_end`` says, and it has only one
  ``Message-ID:``
- A commit with a ``Link:`` to a pull or merge request is the last of that
  request, and the commits before it, back to the last patch from the list,
  are the rest of it. They end as ``pr_trailers_end`` says
- Your own commit, i.e. whose author has your email address, need not have
  been sent to the list, and ends with your sign-off
- Any other commit is a problem, since nothing says where it came from
- Links to issues are in the right trailers and to the project's repos
- patman's own tags, such as ``Series-links:``, are not there: ``patman send``
  removes them from the patches it sends, but a push would not
- With ``require: author-signoff``, the author has signed off
- Merge commits follow ``history``

Tags given in reply to a patch, such as ``Reviewed-by:``, come before the
``Message-ID:``, where ``b4 am`` puts them, since the commit ends with the
``Message-ID:`` and your sign-off.
