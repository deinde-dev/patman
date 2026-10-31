.. SPDX-License-Identifier: GPL-2.0+

.. _patchwork_cache:

Caching
=======

Patchwork servers can be slow, and a busy one such as
https://patchwork.ozlabs.org may block a client which sends it a lot of
requests. Patchwork's API also makes this easy to do: there is no way to ask
which patches have changed, so finding the state and comments of a series
means reading each of its patches, and the comments on each, one request at a
time.

patman reads less from patchwork in four ways:

- it keeps what it reads in a cache, and uses it again rather than asking
  each time
- it follows the project's events on patchwork, to learn which patches have
  changed state, so the rest can come from the cache
- it reads the replies to a series from lore.kernel.org, in one request, to
  learn which patches have new comments, so only those are read from
  patchwork
- ``patman serve`` shows what it sends to each server, so you can see whether
  it is more than a busy server may like

The cache file
--------------

The cache is a file of its own, ``~/.cache/patman/patchwork.db`` (or under
``$XDG_CACHE_HOME``), separate from patman's database. It is shared by all
your repos, by each patman command and by ``patman serve``, so a review job
started from the server finds what the server has just read, and the cache
survives a restart of the server.

``patman patchwork cache`` shows what the cache holds and how much it saves
(see `Seeing what the cache holds`_).

The cache holds nothing which cannot be read again, so it is safe to delete,
e.g. if you suspect it holds something wrong; patman makes a new one. Entries
which are not used for 90 days are dropped, so the file does not keep
growing.

To use no cache at all, use ``--no-cache``, e.g. to check that the cache is
not the cause of a problem::

    patman --no-cache status

This reads everything from patchwork and leaves the cache as it is. With
``patman --no-cache serve``, the syncs and reviews which the server starts do
not use the cache either. Setting ``PATMAN_PWCACHE`` to ``off`` does the same,
or set it to the path of another file to use that instead.

If the cache cannot be used, e.g. because the disk is full or the file is
read-only, patman says so once and reads patchwork instead.

.. _patchwork_cache_show:

Seeing what the cache holds
---------------------------

``patman patchwork cache`` shows what the cache holds for each server: how
many entries of each kind, how much space they take and how long ago the
newest was read. It then shows the requests sent to the server and the reads
which the cache answered, this hour, today and in the last seven days, and
lists the projects whose events are followed (see `Following events`_)::

    $ patman patchwork cache
    Cache: /home/me/.cache/patman/patchwork.db, 15.2 MB

    https://patchwork.ozlabs.org
      Kind             Entries      Size  Newest
      cover-comments        42  415.9 KB  17h ago
      patch                538    3.0 MB  17h ago
      patch-comments       538  927.1 KB  17h ago
      recent-series          5   75.4 KB  3m ago
      series               188  130.6 KB  3m ago
      Period           Requests  Received  Cache hits     Saved
      This hour               2   80.1 KB           4  585.1 KB
      Today                   8    1.1 MB           5  709.9 KB
      Last 7 days          3698   56.3 MB         650    1.5 MB

    Events followed:
      https://patchwork.ozlabs.org 'uboot': since 2d ago, checked 5m ago

The kinds are those which the traffic table on the ``patman serve`` page
uses, e.g. ``recent-series`` for a page of the inbox. The requests are those
counted in the database which the command uses, so run it where you run
``patman serve`` to see the server's traffic.

What is kept, and for how long
------------------------------

How long something is used from the cache depends on whether it can change:

- a patch: an hour, since its text does not change but its state can
- a cover letter: a day, since it has no state
- a series: ten minutes while patchwork is still receiving its patches. Once
  it has them all, the series does not change, so it is kept until it has
  not been used for 90 days
- the comments on a patch or cover letter: five minutes
- a series' mbox, used to apply a series for a review: an hour, since
  patchwork adds review tags to it as they arrive
- the list of projects: a day
- a page of the inbox in ``patman serve``, which ``patman review --scan``
  also reads: ten minutes, since new series arrive all the time

Each series on a page of the inbox is also kept as a series, under its ID, so
reviewing one from the inbox does not read it again.

Each entry is kept under the server and the request which read it, e.g.
``patches/2322466/``, so two servers never share entries.

These are never cached, so always ask patchwork:

- searches for a series by its name, as used by ``patman series autolink``
  to find the link of a series you have sent and by ``patman review -S``, so
  that a new series is found as soon as it is there
- searches for a patch by its title, as used by ``patman review -P``
- the states of the patches in a series, which ``patman review --scan`` reads
  for a newer version of a series you reviewed, where the events have not
  shown them
- the patches and comments which ``patman review --learn-voice`` reads to
  learn how you write reviews

Some commands need the latest version, so they read patchwork even when the
cache has one. What they read still goes into the cache, though, so that
other commands which can make do with a slightly older copy need not read it
again:

- ``patman status`` and the sync read the comments on each patch and cover
  letter, unless lore shows that there is nothing new (see below). For the
  next five minutes, a review, or reading the patch on the ``patman serve``
  page, uses those comments from the cache
- the sync reads a series which is still arriving, so that its new patches
  are seen. For the next ten minutes, a review of the series uses that copy
- ``patman patchwork set-project`` reads the list of projects, since the
  project may be new. For the next day, ``patman review --learn-voice`` uses
  that list

Following events
----------------

``patman status`` and the sync need the current state of each patch, but an
hour-old copy from the cache may be out of date. Rather than read every patch
again, they follow the project's events on patchwork, which say when a
patch's state or delegate changes. Each sync reads the events since the last
one, usually in a single request, drops the patches which have changed from
the cache and uses the rest from it, however old they are.

The inbox in ``patman serve`` reads them too, so that it can show the state
of each patch. Each event's new state is recorded, as is the state of each
patch which patman reads, and kept for 90 days after it was last seen.

The events are read at most once a minute. Only patches read since patman
began following the events are used in this way, so the first sync still
reads every patch. If the events are not checked for a week, or there are too
many to read, following begins again and everything is read once more.

When following begins, the recorded states may have changed without patman
seeing it, so all but those which a patch rarely leaves, 'accepted',
'superseded' and 'rejected', are dropped, to be learned again as patches are
read or change.

Patchwork before version 3.1, which includes patchwork.ozlabs.org, has no
events for new comments, so these are found with lore instead (see
`Patchwork versions`_).

Reading replies from lore
-------------------------

Every comment on patchwork is also an email, and `lore.kernel.org
<https://lore.kernel.org>`_ serves the whole thread of a series, with every
reply, in one request. So ``patman status`` and the sync read the thread from
lore and use it to see which patches have new replies. Only those are read
from patchwork, along with any whose comments have not been read from
patchwork for a week, in case lore lacks a reply, as it sometimes does. The
comments themselves still come from patchwork, so the review tags are the
same.

Lore's ``all`` archive finds the thread on any list which lore carries. If
lore does not have the thread, or is missing one of the patches, e.g. because
it is a few minutes behind, the comments are read from patchwork as before. A
thread which lore does not have is remembered for a day, so lore is not asked
for it on every sync.

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

Lore also supplies a patch which patchwork missed, when reviewing a series
(see :doc:`the Patchwork chapter </patchwork>`).

Caching in patman serve
-----------------------

The server uses the same cache, and says when what it shows was read:

- the inbox says when it was read from patchwork, and 'Refresh' reads it
  again at once, and fetches the upstream to see which patches are applied;
  'Load more' uses a later page from the cache only if it is
  no older than the first page, so the pages follow on from each other
- a patch or cover letter which you read on the page says when it was read,
  and its 'Refresh' reads it and its comments again, e.g. to see its current
  state

The sync page shows the traffic to each server, including lore, for this
hour, today and the last seven days: the number of requests and how much data
came back, and how many reads the cache answered and how much data that
saved. A table below breaks the requests down by kind, such as
``patch-comments`` or ``series-search``, to show what the traffic is made of.
This counts every patman command using the same database, not just the
server.

Patchwork versions
------------------

What patman can do to read less depends on the version of patchwork which
the server runs:

- patman uses version 1.2 of patchwork's REST API, so it needs Patchwork 2.2
  or later. https://patchwork.ozlabs.org runs Patchwork 2.2.0
- the events which patman follows, for a patch's state and delegate, came
  with the REST API in Patchwork 2.0, and the comments on a patch or cover
  letter with Patchwork 2.1 (API 1.1), so every server which patman can use
  has them
- Patchwork 3.1 (API 1.3) adds events for new comments,
  'patch-comment-created' and 'cover-comment-created', which would show which
  patches have new comments without lore. patman does not use them yet:
  patchwork.ozlabs.org does not have them, and an older server gives no error
  for a category which it does not know, but returns no events, which looks
  the same as no new comments
- patchwork.ozlabs.org sends no ETag or Last-Modified headers, so patman
  cannot ask whether something has changed since it was read, only read it
  again
