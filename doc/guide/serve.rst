.. SPDX-License-Identifier: GPL-2.0+

Following it all from a phone
=============================

``patman serve`` runs a small web server in your git tree, showing your
series and your reviews on a page made for a phone. From the page you can
also read the patches, start reviews, work on them and send them.

This page starts the server and goes through its tabs. The next,
:doc:`serve-reviews`, reviews a series from the page.

Starting the server
-------------------

Run it in the git tree which holds the database::

    $ patman serve
    Serving on http://127.0.0.1:8040/

and open that address in a browser on the same machine. To use it from a
phone on your network, listen on all addresses and give a token, which the
page asks for the first time it is opened::

    $ patman serve --host 0.0.0.0 -t my-secret
    Serving on http://0.0.0.0:8040/?token=my-secret

Use the address of the machine, e.g. ``http://myhost:8040/?token=my-secret``;
the page remembers the token. If reviews are kept in a separate database
(``review_db_dir``), the server finds it in the same way as
``patman review``.

To keep the status of your series up to date, add ``-s``, which gathers it
from patchwork every hour, or every so many minutes with ``-s 30``. ``-r``
also syncs the Gmail drafts of your reviews and finds replies to them. With
a lot of series, a sync sends many requests to patchwork, so you may prefer
to leave out ``-s`` and tap 'Sync' on just the series you are following.

The tabs
--------

The tabs along the bottom of the page are:

Series
    Your own series, with the state of their patches on patchwork

Reviews
    The series you have reviewed with ``patman review``

Inbox
    The most recent series on patchwork, marking those you have reviewed and
    those which are applied upstream

Jobs
    Commands started from the page, with their output

Todo
    Series marked with ``patman workflow todo``, due ones first

Each tab remembers the screen you last looked at in it, so tapping Reviews
goes back to the series you were reviewing; tapping it again goes to the
list of reviews.

The pill at the top right shows how the last sync with patchwork went. Tap
it to follow a sync as it runs, start one, or see how many requests patman
has sent to each patchwork server.

Anything which takes a while, such as a review or sending a review, runs as
a job. The Jobs tab lists them, with their output, and shows how many are
waiting or running. Most of the time you need not go there, since the page
shows how each job is getting on next to whatever it acts on.

:doc:`The web server chapter </serve>` describes the page in full, and how
to run the server as a service, so that it is always there.
