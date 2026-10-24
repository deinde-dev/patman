.. SPDX-License-Identifier: GPL-2.0+

Web server
==========

``patman serve`` runs a small web server in your git tree, so that you can
check on your series and work on reviews from a phone or another computer. The
page has these tabs:

- **Series**: your series with the state of each patch in the latest version.
  Tap a series to see each version's patches, then tap a patch or the cover
  letter to read its comments, which are fetched from patchwork
- **Reviews**: series you have reviewed with ``patman review``, showing the
  review of each patch and whether it has been drafted, sent or replied to.
  A series which is applied but not reviewed, e.g. with ``--apply-only``,
  says 'not reviewed'
- **Inbox**: recent series on patchwork, showing which you have reviewed
  and which are applied upstream
- **Jobs**: reviews and other commands started from the page, and the syncs,
  with their output
- **Todo**: series marked with ``patman workflow todo``, due ones first

The page uses the JSON API described in
:doc:`The patman serve protocol </serve-protocol>`, which other tools can use
too.

Starting the server
-------------------

The server only listens on the local machine by default::

    patman serve

then browse to http://localhost:8040/ on the same machine. To use it from a
phone, either forward the port over ssh (``ssh -L 8040:localhost:8040 <host>``)
or use a VPN such as Tailscale and listen on that address with a token::

    patman serve --host 100.64.1.2 --token <secret>

Open the URL that patman prints (which includes the token) once on the phone;
the page remembers the token after that. With Tailscale, ``tailscale serve
8040`` provides HTTPS, which lets the browser install the page as an app.

The options are:

- ``--host ADDR``: address to listen on (default 127.0.0.1)
- ``--port PORT``: port to listen on (default 8040)
- ``-t``, ``--token TOKEN``: token which clients must give; use this when
  listening on any address other than a loopback one
- ``-s``, ``--sync [MINUTES]``: sync with patchwork every MINUTES minutes
  (default 60); see below
- ``-r``, ``--review-sync``: also run ``patman review --sync`` in each sync
- ``--review-db-dir DIR``: read reviews from a separate database (the
  ``review_db_dir`` setting)
- ``--max-reviews N``: run at most N reviews at once (default 5; the
  ``max_reviews`` setting); see `Jobs`_

Keeping the status up to date
-----------------------------

Since the status of each patch only changes in the database when patman asks
patchwork, the server can do this periodically::

    patman serve --sync        # every hour
    patman serve --sync 20     # every 20 minutes
    patman serve --sync -r     # also run 'patman review --sync'

Each sync runs ``patman series gather-all -G`` in a separate process. The
``-G`` flag means that review tags are not gathered into your branches, so
nothing changes in your git tree while you are working in it. Use ``patman
series gather`` yourself when you are ready to pick up the tags. The ``-r``
flag also runs ``patman review --sync`` to track sent review drafts and
replies; this needs Gmail access (see
:doc:`the Assisted patch review chapter </review>`).

Each series is read from the patchwork server of its upstream, so a server
which is down only affects its own series.

Before gathering, the sync runs ``git fetch`` for the default upstream and the
upstreams of reviewed series, where these are git remotes, so that reviews
apply patches to up-to-date branches. This only updates the remote branches,
not your own.

Tap the sync indicator at the top of the page to follow the output of a sync as
it runs, to start one or to cancel one.

The first sync reads every patch of every series, along with its comments, so
with a lot of series it sends thousands of requests to patchwork. Later syncs
read much less, since patman keeps what it reads in a cache, follows the
project's events and reads the replies from lore (see
:doc:`the Caching chapter </caching>`). Even so, a busy server may block your
machine. To avoid this, leave out ``--sync`` and sync just the series you are
interested in: tap 'Sync' on a series to run ``patman series gather -G`` for
the version shown. Without ``--sync``, 'Sync now' on the sync page needs a
second tap, since it reads all the series.

The sync page also shows how many requests patman has sent to each patchwork
server, and to lore, this hour, today and in the last seven days, how much
data came back and how many reads the cache answered, with the requests
broken down by kind. This counts every patman command using the same
database, not just the server, so it shows whether patman is sending more
than a busy server may like.

Reading patches
---------------

Each patch and cover letter can be read on the page: tap 'Read' on a series, a
patch name in a review, or a patch in the inbox to see the commit message, the
diff (with a summary of the files changed) and the comments on patchwork.

To see the code around a chunk of a diff, tap '5 more' above or below it, as
often as needed, or 'All' to fill the gap. Tap 'View whole file', at the top of
each file in the diff or between its chunks, to see all of the file, with line
numbers, at that point. The code is read from the git repo where the server
runs, using the blobs named in the diff's 'index' line, so it is only available
if the repo has them: the file as it is before the patch is there if the series
is based on a commit the repo has, and the file with the patch applied is there
once the series has been reviewed.

Reviewing from the page
-----------------------

The Reviews tab shows the active reviews: tap 'hide' on a series once you have
finished with it, or 'Hide' at the top of its reviews, to move it to the
Hidden list. 'All' shows them all, and 'Needs reply' shows those with replies
waiting, hidden or not. 'To send' shows those with a Gmail draft waiting to be
sent, including one which is out of date, and 'Not drafted' those with a
review which is not sent and has no draft, e.g. because the review was run
without drafts or its draft was deleted from Gmail; both include hidden
series. A hidden series becomes active again when a new
version of it is reviewed or someone replies to a review of it.

On the Reviews tab, 'Review a series' takes a patchwork series ID, the URL of a
series or patch, or words from a cover-letter title, and runs ``patman review``
(with ``-d`` to create Gmail drafts, if selected). 'Scan for new versions' runs
``patman review --scan``. Each reviewed series has buttons to recreate its
Gmail drafts, either all of them or one patch's, and to review it again.

Wherever a review is started, whether by 'Review a series', 'Review' in the
inbox, 'Review again' or 'Review it' for a cover letter, there is a box for
context to give the agent, such as 'this is an RFC, so ignore style', which is
passed with ``-c``.

The Inbox tab lists the most recent series on patchwork for each upstream.
Series which you have reviewed, or which are your own, are marked, as are new
versions of series you reviewed earlier, so it is easy to see what needs a
review. A new version says which version you reviewed, e.g. 'v2 reviewed', and
how many replies to that review are waiting for your response; tap either to
see the review. Tap a series to see its patches and start a review, or 'Try
it' for the command which checks it out in your tree (see
:ref:`review_checkout`). A new version also shows your review of the earlier
one below its patches: each part with the status of your review and any
replies waiting, taken from the database, so that the earlier version need
not be among the pages loaded.

A series whose patches are applied upstream is marked 'applied', or e.g.
'applied 2/5' if only some are, and each such patch shows the branch which has
it. patman finds these in the upstream's main and next branches, e.g.
``us/main`` and ``us/next``, by the subject of each patch, as patchwork's patch
states are often not updated when a patch is applied. This uses the git repo
which the server runs in, so it sends nothing to patchwork. The inbox fetches
the upstream when it is shown, if it has not done so for an hour, and the top
of the inbox says when it last did; 'Refresh' fetches it at once. The fetch
runs while the inbox is read from patchwork, and the inbox waits up to 20
seconds in all for it, then shows what it has while the fetch carries on. An
older version of a series whose patches came in
a newer version is marked 'later version applied' instead.

A patch which is accepted on patchwork counts as applied too, since a
custodian may accept it into their own tree well before it reaches the
upstream's branches. Other states, such as 'superseded' or
'changes-requested', are shown beside each patch, and on the series when all
its patches have the same one. patman learns these states from the patches it
reads and from the project's events (see :ref:`the cache <patchwork_cache>`),
which the inbox reads at most once a minute, so a patch which has not changed
since patman began following the events shows no state. Series which are fully
applied, or whose patches are all superseded or rejected, are left out of 'To
review', and 'Applied' shows each series with a patch applied.

The inbox is read from patchwork with one request for the 50 newest series.
The server keeps what it reads for ten minutes, so going back to the Inbox tab,
opening it on another device or starting the server again does not read it
again; the top of the inbox says when it was read, and 'Refresh' reads it again
at once, and fetches the upstream. Nothing more is read until you tap 'Load more' at the bottom, which
reads the next 50; the button goes once patchwork has no more. Going back to
the Inbox tab keeps the pages already loaded: only the first is read from
patchwork again, if it is more than ten minutes old, and the others are shown
as they were. Tapping a series
uses what was read already. A patch read in the last hour, and its comments
read in the last five minutes, are shown from patman's cache of patchwork (see
:ref:`the cache <patchwork_cache>`); the page says when the patch was read,
and its 'Refresh' reads both again, e.g. to see the current state.

.. _serve_lore:

For a project whose list is on lore and which has no patchwork server, such as
dtc (see :ref:`review_lore`), run the server in that project's tree. The inbox
reads the newest patches and cover letters sent to the list from lore's feed,
200 messages at a time, and gathers them into series by following the replies
back to each cover letter, or first patch; 'Load more' reads the next 200. Each
series is known by the Message-ID of its cover letter or first patch. Lore has
no patch states, so only patches applied upstream are marked, and a large
series which spans two pages may show only some of its patches. The patches,
cover letters and comments are read from lore, and 'Review' and 'Try it' work
as they do for patchwork.

The top of the page, and the name of the browser tab, give the project which
the server is for, from the ``project`` setting in the ``[review]`` section of
the config (see :ref:`review_project`), so that the servers for two projects
are easy to tell apart.

'Review' on a series starts the review as a job, but the inbox stays where it
is, so you can start reviews of other series too. Each series shows how its
review is getting on, with a link to the job's output, and offers 'Being
reviewed…' rather than 'Review' until the job finishes, even if the review was
started elsewhere. A series cannot be reviewed twice at once.

Each review has 'Show the patch' (or 'Show the cover letter'), which shows the
commit message and diff just below the review, so the two can be read together.

The agent only reviews the cover letter when it has something to say about the
series as a whole. When there is no review of the cover letter, a card for it
still appears above the reviews, with 'Show the cover letter' and 'Review it',
which asks the agent to review just the cover letter (``patman review -i 0``),
creating a Gmail draft if it has comments.

A review which has not been sent can be edited on the page. Saving it can also
update its Gmail draft, so that the draft matches. If it does not, the review
shows 'draft out of date', and sending it creates the draft again first, so
that your change is sent. Whether the review approves the patch follows from
whether the text includes a ``Reviewed-by:`` line.

To change just one comment, tap 'Edit' under it. The comment opens for editing
in place, between the lines it quotes and the rest of the review. Saving it
with no text drops the comment along with the lines it quotes.

To have the agent change a review which has not been sent, tap 'Revise' under
it and say what should change, or tap 'Revise this' under one comment to talk
about just that part. 'Revise all' at the top applies a note to every unsent
review of the series, e.g. 'drop the comments about style'. These run ``patman
review --revise``, updating the Gmail drafts if selected.

'Send' under a review sends its Gmail draft, and 'Send N drafts' at the top
sends them all; each needs a second tap to confirm. 'Delete' under a review
which has not been sent deletes it, with its Gmail draft (``patman review
--delete``), also after a second tap.

These run as jobs (see `Jobs`_), but the page stays where it is, so you can
carry on reading the series. Each review a job acts on shows how the job is
getting on, with a link to its output. When the job finishes, the review
shows its new text, and 'Show changes' shows the text from before.

Replies to reviews
------------------

When someone replies to one of your reviews (found by ``review --sync``, so use
``serve -r``), the reply is shown below the review, along with any response the
agent suggests. The Reviews tab has a 'Needs reply' filter. You can edit the
response, or write one, then create a Gmail draft which is threaded as a reply,
or send it. You can also tap 'Suggest a response' to have the agent suggest one
(again), or note that no reply is needed. These run as jobs, but the page stays
where it is, showing how each is getting on below the reply, then the reply
again.

To respond to other comments on patchwork, tap 'Comments' under a review, or on
the cover-letter card, to list the comments on that patch or cover letter.
Your own comments, such as the review itself, are left out, since they need no
response.
'Respond' on a comment asks the agent to suggest a response, optionally saying
what you would like to say (``patman review --respond-comment``). The response
then appears below the review, like a response to a reply, to change, draft or
send.

Jobs
----

Commands started from the page run as jobs, one at a time in the order they are
started, along with the syncs. Reviews of series are the exception: since each
has its own worktree and mostly waits for the agent, up to five run at once
(see ``--max-reviews``), alongside the other jobs. The Jobs tab shows each job with its output as it
runs, and lets you cancel it. A job acting on a reviewed series shows the
series' name, with a link to its reviews. While jobs are waiting or running,
the tab shows how many there are.

Each tab remembers the screen you last looked at in it, so tapping Reviews
goes back to the series you were reviewing; tapping it again goes to the list
of reviews. Each screen also keeps its scroll position, so going back to it
finds it where you left it.

Jobs are recorded in ``.patman-serve-jobs.json`` next to the database, with
their output, so the Jobs tab, and the job numbers, carry on when the server
restarts. The most recent 20 are kept, along with any which have not finished.
If the server stops before a job finishes, for example because the service is
restarted, the job is run again from the start, keeping its number, when the
server next starts. A job is resumed at most three times. A sync is not resumed,
since one runs when the server starts.

Running it as a service
-----------------------

To keep the server running, for example on a machine which you reach over a
VPN, run it as a systemd user service. Put the token in a file which only you
can read, such as ``~/.config/patman-serve.env``::

    PATMAN_TOKEN=<secret>

then create ``~/.config/systemd/user/patman-serve.service``, changing the
directory, the path to patman and the address to suit::

    [Unit]
    Description=patman web server
    After=network-online.target

    [Service]
    WorkingDirectory=%h/src/u-boot
    EnvironmentFile=%h/.config/patman-serve.env
    ExecStart=%h/.local/bin/patman serve --host 100.64.1.2 \
        -t ${PATMAN_TOKEN} -s -r
    Restart=on-failure
    RestartSec=30

    [Install]
    WantedBy=default.target

and start it with::

    systemctl --user daemon-reload
    systemctl --user enable --now patman-serve

Use ``journalctl --user -u patman-serve`` to see its output. Run ``loginctl
enable-linger`` once if the server should keep running while you are not logged
in.
