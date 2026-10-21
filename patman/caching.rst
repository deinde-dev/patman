.. SPDX-License-Identifier: GPL-2.0+

.. _patchwork_cache:

Caching
=======

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
- a page of the inbox in ``patman serve``: ten minutes. Each series on it is
  also kept as a series, as above, so reviewing one needs no request

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
