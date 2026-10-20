.. SPDX-License-Identifier: GPL-2.0+

Patchwork
=========

Patchwork collects the patches sent to a mailing list, along with the
replies and review tags they receive. patman uses it to find your series once
it is sent, to show what reviews it has received, and to collect the review
tags into your commits.

Setting up the project
----------------------

patman uses the patchwork server given by the ``patchwork_url`` setting (see
:doc:`the Configuration chapter </configuration>`) or ``-P``, or the server of
the series' upstream, if it has one. U-Boot's ``.patman-defaults`` sets
https://patchwork.ozlabs.org

Commands which search patchwork for a series, such as
``patman series autolink``, also need to know its project. Set this once for
each remote::

    $ patman patchwork set-project U-Boot us
    Project 'U-Boot' patchwork-ID 18 link-name 'uboot' remote 'us'

The remote can be left out if the current branch tracks it, or if it is the
only remote. ``patman patchwork get-project`` shows the project for a remote,
``ls`` lists the projects and ``rm`` removes one::

    $ patman patchwork ls
    Project                 ID  Link name        Remotes
    --------------------  ----  ---------------  ---------------
    U-Boot                  18  uboot            us
    --------------------  ----  ---------------  ---------------

    $ patman patchwork rm us
    Deleted patchwork project for upstream 'us'

With no remote, ``rm`` removes the project which has no upstream.

Checking the review status
--------------------------

Once your series is on patchwork, patman can show you what new reviews have
appeared since you sent it.

To set this up, add a Series-links tag to one of the commits in your series
(see :doc:`the Tags chapter </tags>`).

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

There is also a -c option to list the comments received for each patch, and a
-C option to list those on the cover letter.

Collecting the tags
-------------------

To automatically pull these tags into a new branch, use the -d option:

.. code-block:: bash

    patman status -d mtrr4

This will create a new 'mtrr4' branch which is the same as your current branch
but has the new review tags in it. The tags are added in alphabetic order and
are placed immediately after any existing ack/review/test/fixes tags, or at the
end. A tag given in a reply to the cover letter, such as a Reviewed-by for the
whole series, is added to every patch which does not already have it. The new branch does not track an upstream branch, so set one with
``git branch --set-upstream-to`` before sending from it.

For a worked example using real series, see
:doc:`Checking status and collecting tags </guide/status-and-tags>`.

.. _patchwork_cache:

The cache
---------

Patchwork servers can be slow, and may block a client which sends them a lot
of requests, so patman keeps what it reads in a cache and uses it again rather
than asking each time. The cache is a file of its own,
``~/.cache/patman/patchwork.db`` (or under ``$XDG_CACHE_HOME``), shared by all
your repos and by ``patman serve``. It holds nothing which cannot be read
again, so it is safe to delete.

How long something is used from the cache depends on whether it can change:

- a patch: an hour, since its text does not change but its state can
- a cover letter: a day, since it has no state
- a series: for good once patchwork has all its patches, or ten minutes
  before that
- comments: five minutes
- a series' mbox, used to apply a series for a review: an hour, since
  patchwork adds review tags to it as they arrive
- the list of projects: a day

These are never cached, so always ask patchwork:

- searches for a series by its name, as used by ``patman series autolink``
  to find the link of a series you have sent, by ``patman review -S`` and by
  ``patman review --scan``, so that a new series is found as soon as it is
  there
- searches for a patch by its title, as used by ``patman review -P``
- the states of the patches in a series, which ``patman review --scan`` reads
  for a newer version of a series you reviewed
- the patches and comments which ``patman review --learn-voice`` reads to
  learn how you write reviews

These are always read from patchwork, though what is read is kept for
others to use:

- the comments on each patch and on the cover letter, in ``patman status``
  and the sync, which need them to be current; these do use a series from
  the cache once it is complete, and patches as below
- a series which is still arriving, when the sync reads it, so that its new
  patches are seen
- the list of projects, when setting the project with
  ``patman patchwork set-project``, since the project may be new

``patman status`` and the sync also need the current state of each patch.
Rather than read every patch again, they follow the project's events on
patchwork, which say when a patch's state or delegate changes: each sync
reads the events since the last one, usually in a single request, drops the
patches which have changed from the cache and uses the rest from it. Only
patches read since patman began following the events are used in this way,
and if the events are not checked for a week, following begins again, so
everything is read once more.

Entries which are not used for 90 days are dropped. If the cache cannot be
used, e.g. because the disk is full, patman says so once and reads patchwork
instead. To use another file, set ``PATMAN_PWCACHE`` to its path, or set it
to ``off`` to use no cache.

Reading replies from lore
-------------------------

Patchwork cannot say which patches have new comments, so ``patman status`` and
the sync would read the comments on every patch. Since each comment is also an
email, patman reads the thread of the series from `lore.kernel.org
<https://lore.kernel.org>`_ instead, which gives every reply in one request,
and uses it to see which patches have new replies. Only those are read from
patchwork, along with any whose comments have not been read from patchwork for
a week, in case lore lacks a reply, as it sometimes does. The comments
themselves still come from patchwork, so the review tags are the same.

Lore's ``all`` archive finds the thread on any list which lore carries. If
lore does not have the thread, or is missing one of the patches, e.g. because
it is a few minutes behind, the comments are read from patchwork as before.

The ``--lore`` option, or the ``lore`` setting, chooses how lore is used:

``hint`` (the default)
    Use lore to see which patches have new replies, as above

``use``
    Take the comments from lore itself, which saves more requests, but misses
    any reply which lore lacks

``off``
    Do not use lore; ``--no-lore`` does the same

For example, to take the comments from lore::

    [settings]
    lore: use

Tracking several series
-----------------------

If you have several series in flight, 'patman series' keeps track of them for
you in a database: each version, its patchwork link and the state of each
patch. It can find the links, gather the review tags, start each new version
and archive a series once merged. The
:doc:`Series management chapter </series>` describes this.
