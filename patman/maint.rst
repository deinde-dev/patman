.. SPDX-License-Identifier: GPL-2.0+

.. _maint:

Maintaining a project
=====================

A maintainer applies other people's patches to the project's tree, then pushes
them upstream. Each project has its own rules for this, such as which trailers
each commit ends with, and whether merge commits are allowed. Following them
is mechanical, so patman holds them as settings and checks the commits
against them, rather than leaving you (or an AI assistant) to read them.

``patman maint check`` checks the queue: the commits on the branch which is
checked out, or the one given with ``-b``, which are not upstream yet. Run it
before pushing, however the patches were applied, e.g. with ``b4 am``::

    $ patman maint check
    Checking 3 commits in us/main..main
      ok   1c3f0a2b9d4e libfdt: Add fdt_get_alias_namelen()
      ok   7e21d9c0a3b5 tests: Check fdt_get_alias_namelen()
      FAIL 0b9a8f3e6c12 dtc: Warn about a missing #size-cells
           - No Signed-off-by from the author, Fred Bloggs <fred@x.org>
    1 of 3 with problems

It exits with 1 if any commit has a problem, so a script can stop before
pushing.

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

The settings are:

``upstream``
    The upstream remote and its branch, which the queue goes on top of. The
    remote may be given by its name, e.g. ``us main``, or by its URL without
    the scheme, as here, so that the rules suit everyone whatever they call
    their remotes. This one is needed

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
    one of them

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
