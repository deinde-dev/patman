.. SPDX-License-Identifier: GPL-2.0+

=========================
The patman serve protocol
=========================

``patman serve`` runs a web server in a git tree which provides a JSON API over
HTTP, covering the patman database and, where needed, the patchwork servers of
the upstreams. This document describes that API, for anyone working on the
server or writing a client. See the 'Web server' chapter of the manual for how
to run the server. The page which the server provides is one such client; see
`The page`_.

Overview
--------

The API is under ``/api/``. Each response is a JSON value, encoded in UTF-8,
and each request which takes a body expects a JSON object.

Your own series are read from the patman database in the git tree. Reviews made
with ``patman review`` are in the same database. Comments on patches and cover
letters are not stored, so these are read from patchwork when asked for.

Series IDs are those of the patman database: ``/api/series/<id>`` takes the ID
of one of your series and ``/api/reviews/<id>`` the ID of a reviewed series.
IDs from patchwork, such as a patch or cover-letter ID, are named as such.

Times are local, in the form ``YYYY-MM-DD HH:MM:SS``, except where they come
from patchwork or email, which give their own form.

Access
------

The server listens on ``127.0.0.1`` port 8040 by default (``--host`` and
``--port``). When it is given a token with ``--token``, each request to a path
under ``/api/`` must provide it, either in an ``Authorization: Bearer <token>``
header or as a ``token`` query parameter. A request which does not is refused
with status 401 and ``{"error": "Unauthorised"}``. The server warns if it
listens on an address other than a loopback one without a token.

Errors
------

An error is reported as a JSON object with an ``error`` string, using these
status codes:

- 401: the token is missing or wrong
- 404: the thing asked for does not exist, or patchwork does not have it
- 409: the request conflicts with the current state, e.g. starting a sync while
  one is running
- 502: patchwork could not be reached; the message starts with ``Patchwork:``

The sync endpoints give their usual response with status 409, rather than an
error object.

A path which does not exist, or the wrong method, gets aiohttp's own 404 or 405
response, which is plain text.

Common objects
--------------

These objects appear in several responses.

Patch
~~~~~

- ``seq`` (int): position in the series, starting at 1
- ``subject`` (str): subject of the commit
- ``state`` (str or null): patchwork state, e.g. ``accepted``
- ``patch_id`` (int or null): patchwork patch ID
- ``num_comments`` (int): number of comments on patchwork

Version
~~~~~~~

A version of a series:

- ``svid`` (int): ID of the version in the database
- ``version`` (int): version number
- ``link`` (str or null): patchwork series ID
- ``desc`` (str or null): description of the version
- ``cover_id`` (int or null): patchwork cover-letter ID
- ``cover_name`` (str or null): name of the cover letter
- ``cover_num_comments`` (int): number of comments on the cover letter
- ``archive_tag`` (str or null): tag holding the archived branch

A ``states`` object maps each patchwork state to the number of patches in it,
using ``unknown`` for patches with no state.

Comment
~~~~~~~

A comment on patchwork; any field may be null:

- ``date`` (str): when it was sent
- ``name`` (str): name of the sender
- ``email`` (str): email address of the sender
- ``subject`` (str): subject
- ``content`` (str): text of the comment
- ``web_url`` (str): URL of the comment on patchwork

Review
~~~~~~

A review of one patch, or of the cover letter:

- ``seq`` (int): patch number, or 0 for the cover letter
- ``body`` (str): text of the review
- ``approved`` (bool): true if the review gives a Reviewed-by tag
- ``timestamp`` (str): when it was written
- ``status`` (str or null): ``new``, ``draft``, ``sent``, ``replied`` or
  ``deleted``

Your series
-----------

``GET /api/series``
~~~~~~~~~~~~~~~~~~~

Your series which are not archived, sorted by name. Each is an object with:

- ``id`` (int): series ID
- ``name`` (str): name of the series (its branch)
- ``desc`` (str): description
- ``upstream`` (str or null): upstream the series is for
- ``version`` (int or null): latest version
- ``link`` (str or null): patchwork series ID of the latest version
- ``count`` (int): number of patches in the latest version
- ``states`` (object): states of those patches
- ``num_comments`` (int): number of comments on those patches
- ``cover_num_comments`` (int): number of comments on the cover letter
- ``sent`` (str or null): when the series was last sent

``GET /api/series/<id>``
~~~~~~~~~~~~~~~~~~~~~~~~

A series with all its versions:

- ``id``, ``name``, ``desc``, ``upstream``: as above
- ``patchwork_url`` (str or null): URL of the patchwork server for the
  upstream, falling back to the ``-P`` setting
- ``versions`` (list of Version): newest first, each with ``patches`` (list of
  Patch, in order) and ``states``

Patchwork
---------

These read from patchwork each time. Those which take an optional ``ups`` query
parameter use the patchwork server of that upstream, falling back to the ``-P``
setting.

``GET /api/comments/<kind>/<id>``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The comments on a patch (``kind`` is ``patch``) or cover letter (``cover``),
given its patchwork ID: a list of Comment.

Reviews
-------

``GET /api/reviews``
~~~~~~~~~~~~~~~~~~~~

Series reviewed with ``patman review`` which are not archived, most recently
reviewed first. Each is an object with:

- ``id``, ``name``, ``desc``, ``upstream``, ``version``, ``link``: as for ``GET
  /api/series``
- ``num_reviews`` (int): number of reviews of the latest version
- ``approved`` (int): how many of them approve the patch
- ``statuses`` (object): number of reviews with each status, using ``new`` for
  those with none
- ``timestamp`` (str or null): when the latest review was written

``GET /api/reviews/<id>``
~~~~~~~~~~~~~~~~~~~~~~~~~

A reviewed series with all its versions:

- ``id``, ``name``, ``desc``: as above
- ``upstream`` (str or null): upstream of the series
- ``patchwork_url`` (str or null): as for ``GET /api/series/<id>``
- ``versions`` (list of Version): newest first, each with ``notes`` (str or
  null; notes from handling reviews of the series) and ``reviews`` (list of
  Review, in order)

Todo
----

``GET /api/todo``
~~~~~~~~~~~~~~~~~

Series marked with ``patman workflow todo``, earliest first, each an object
with ``series_id``, ``name``, ``desc``, ``timestamp`` (when it is due) and
``due`` (true if that time has passed).

Sync
----

A sync updates the database from patchwork. With ``--sync``, one runs when the
server starts and then every so many minutes; one can also be started through
the API. It runs these commands in turn:

- ``patman series gather-all -G``
- ``patman review --sync``, if the server is given ``-r``

``GET /api/sync``
~~~~~~~~~~~~~~~~~

The state of the most recent sync:

- ``running`` (bool): true while a sync is running
- ``cancelled`` (bool): true if the sync was cancelled
- ``started`` (str or null): when it started
- ``finished`` (str or null): when it finished
- ``results`` (list): one object for each command which has started, with
  ``cmd``, ``returncode`` and ``output``, which grows as the command runs;
  ``returncode`` is null while it runs
- ``interval`` (int): minutes between syncs, or 0 if they are only run when
  asked
- ``review_sync`` (bool): true if the sync runs ``patman review --sync``

``POST /api/sync``
~~~~~~~~~~~~~~~~~~

Start a sync, giving status 202 and the sync state, or 409 and the sync state
if a sync is already running.

``POST /api/sync/cancel``
~~~~~~~~~~~~~~~~~~~~~~~~~

Cancel the sync which is running, stopping its command, giving status 202 and
the sync state, or 409 if there is none.

The page
--------

``GET /`` gives the page, a single HTML file which needs no build step and
loads nothing from other sites, and ``/static/<file>`` gives the files it uses,
such as its web-app manifest and icon. Neither needs the token.

The page takes the token from a ``token`` query parameter in its own URL, as
printed by ``patman serve``, keeps it in the browser's local storage and
removes it from the URL. It sends the token in an ``Authorization`` header and
asks for it if a request is refused with status 401.

While a sync is running, the page asks for its state every few seconds to show
its progress, and otherwise checks the sync state once a minute.
