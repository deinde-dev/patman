.. SPDX-License-Identifier: GPL-2.0+

Reviewing from the page
=======================

This starts with the tabs of the ``patman serve`` page, then follows the
review of a series from start to finish: finding it in the inbox, starting
the review, reading the reviews and changing them, responding to a comment
on patchwork, and sending them. It then answers a reply to an earlier
review.

Everything here can also be done with ``patman review``, as in
:doc:`reviewing`; the page runs the same commands as jobs. The pictures are
of a demo server (``doc/demo/serve_demo.py``) with made-up series and
people, which fakes the agent, patchwork and Gmail, so that the pictures can
be taken again when the page changes. The reviews are of the kind the agent
writes.

The tabs
--------

The tabs along the bottom of the page take you between its parts:

.. image:: images/serve-tabs.png
   :width: 660px
   :alt: The tabs: Series, Reviews, Inbox, Jobs and Todo, with Inbox
         highlighted and a 1 on Jobs

Three of them are for reviewing:

Inbox
    The newest series on patchwork. A review starts here

Reviews
    The series you have reviewed, each with its reviews, and any replies to
    them. This is where you read and change the reviews and send them

Jobs
    The reviews and other commands started from the page, with their output.
    The number on the tab is how many are waiting or running: here, the
    review just started from the inbox

The other two, Series and Todo, are for your own series, as in
:doc:`serve`.

The tab you are in is highlighted. Each tab remembers the screen you last
looked at in it, so you can go to Jobs to follow a review, then tap Reviews
to get back to the series you were reading. Tapping it again goes to the
list of reviews.

Choosing a series
-----------------

The Inbox tab lists the newest series on patchwork. 'To review' shows those
which you have not reviewed and which are not yours (see :ref:`review_own`),
and 'New versions' those for which you reviewed an earlier version, such as
the Ethernet driver here. Each series also says how far it has got: the board
support is partly applied upstream, and changes have been requested to the
bootflow series:

.. image:: images/serve-inbox.png
   :width: 660px
   :alt: The Inbox tab, listing four series to review, one marked 'applied
         2/3' and one 'changes-requested'

The server reads the list from patchwork at most every ten minutes, and
fetches the upstream at most every hour, unless you tap 'Refresh'; the top of
the inbox says when it did each. 'Load more' at the bottom reads the next 50
series, and going to another tab and back keeps them.

For a series which is too old to be among them, tap 'Add a series' and search
for it by name. 'Add' keeps it at the top of the inbox until you remove it
(see :ref:`adding a series <serve_search>`).

A series is 'applied' once all its patches are on the upstream's main or next
branch, found by their subjects in the git repo which the server runs in, or
are accepted on patchwork, e.g. into a custodian's tree. A series which is
fully applied leaves 'To review', as the build-guide fix has here; 'Applied'
lists every series with a patch applied. Tap one to see which of its patches
are applied, and where:

.. image:: images/serve-inbox-applied.png
   :width: 644px
   :alt: The board series opened under 'Applied', with two of its three
         patches on us/next

An older version of a series whose patches came in a newer one is marked
'later version applied'. Other states on patchwork, such as
'changes-requested' or 'superseded', show beside each patch, and on the series
when all its patches have the same one; a series whose patches are all
superseded or rejected leaves 'To review' too. patman learns these states
from the patches it reads and from the project's events, so a patch which has
not changed since patman began following them shows no state (see
:doc:`the Caching chapter </caching>`).

A new version says which version you reviewed, such as 'v3 reviewed' on the
Ethernet driver, and how many replies to that review are waiting for you. The
earlier version is often too old to be among the pages loaded, so tapping the
new one shows your review of the earlier one below its patches: each part,
what became of your review of it, and any replies waiting. This comes from
patman's database, so it reads nothing from patchwork:

.. image:: images/serve-inbox-earlier.png
   :width: 644px
   :alt: The Ethernet driver v4 opened, with the review of v3 below its
         patches, showing one reply waiting

Starting the review
-------------------

Tap a series to see its cover letter and patches; tap one of them to read
it. To try the series first, 'Try it' shows the command which checks it out
in your tree, ready to copy (see :ref:`review_checkout`):

.. image:: images/serve-try.png
   :width: 644px
   :alt: The series opened in the inbox, with the command which checks it
         out and a button to copy it

'Review' opens a box for anything the agent should know, such as what to
look at closely, or that the series is an RFC. This is passed to ``patman
review`` with ``-c``, and can be left empty:

.. image:: images/serve-inbox-review.png
   :width: 644px
   :alt: The series opened in the inbox, with the patches and a note for the
         agent

'Start the review' runs ``patman review`` as a job, creating a Gmail draft
of each review (``-d``). The inbox stays where it is, so you can start
reviews of other series too; up to five run at once. The series shows how
its review is getting on, and offers 'Being reviewed…' rather than 'Review'
until it finishes:

.. image:: images/serve-inbox-busy.png
   :width: 644px
   :alt: The series in the inbox, marked 'review running'

Tap 'review running', or the Jobs tab, to follow the job's output. Once it
finishes, 'Reviews' goes to the reviews:

.. image:: images/serve-job.png
   :width: 660px
   :alt: The review job, finished, with its output

Reading the reviews
-------------------

The page for a series has the buttons which act on all its reviews at the
top, then a card for each review:

.. image:: images/serve-review.png
   :width: 660px
   :alt: The reviews of the series, with the cover letter and patch 1

Each card gives the review's status, which is amber while the review is
still to be sent ('draft') and green once it has gone ('sent'), and
'Reviewed-by' if the review gives the patch that tag. Long quotes are folded,
e.g. '6 quoted lines', and open when tapped.

The agent only reviews the cover letter when it has something to say about
the series as a whole. Here it did not, so the cover letter has a card of
its own, with 'Review it' to have the agent review just the cover letter.

'Show the patch' shows the commit message and diff just below the review, so
that the two can be read together:

.. image:: images/serve-patch.png
   :width: 614px
   :alt: The commit message and diff of patch 1

For a change to an existing file, 'View whole file' shows all of it, and the
buttons between the chunks of the diff show more of the lines around them.

Viewing the code
----------------

Each comment quotes the code it is about, starting with the ``diff --git``
and ``@@`` lines which say where it is. 'View code' under the comment shows
the whole file, at the last line quoted. On a wide screen the file is in a
panel beside the review, so that the two can be read together:

.. image:: images/serve-code-panel.png
   :width: 100%
   :alt: The review of patch 1, with the driver which its first comment is
         about in a panel at the right

'Full page' shows the file on its own, and the cross closes the panel. On a
narrower screen, such as a phone, the file has a page of its own, as here
for the Kconfig which the last comment is about:

.. image:: images/serve-code.png
   :width: 660px
   :alt: The Kconfig with the patch applied, at the lines which the comment
         quotes

The file is read from the git repo, with the patch applied once the series
has been reviewed. For a file which the patch changes, rather than adds, the
link at the top shows the same line in a source browser: Elixir for U-Boot
and Linux, where each name links to where it is defined and used. Its copy of
the file is from the latest release, so it may be a little different.

Changing a comment
------------------

Under each comment are 'Revise this' and 'Edit'. 'Edit' opens the comment in
place, between the lines it quotes and the rest of the review, for you to
change:

.. image:: images/serve-edit.png
   :width: 644px
   :alt: The second comment of the review, open for editing

With 'Update the Gmail draft' ticked, saving also updates the draft. If it
is not ticked, the review shows 'draft out of date', and sending it creates
the draft again first, so that your change is sent. Saving an empty comment
drops it, along with the lines it quotes.

'Edit' at the bottom of the card changes the whole review. Whether the
review approves the patch follows from whether it has a ``Reviewed-by:``
line.

Asking the agent to change it
-----------------------------

'Revise this' asks the agent to change just that comment, following a note:

.. image:: images/serve-revise.png
   :width: 644px
   :alt: The form to revise a comment, with the note 'Ask whether a shorter
         delay would do'

This runs ``patman review --revise`` as a job. The page stays where it is:
the card shows how the job is getting on, then the new text, with 'Show
changes' to see what was there before:

.. image:: images/serve-revised.png
   :width: 644px
   :alt: The review after the agent revised the second comment

'Revise' at the bottom of the card applies a note to the whole review, and
'Revise all' at the top to every unsent review of the series, e.g. 'drop the
comments about style'.

Responding to a comment
-----------------------

Other people may comment on the series too. 'Comments' under a review lists
the comments on its patch on patchwork, and 'Respond' asks the agent to
suggest a response, optionally saying what you would like to say:

.. image:: images/serve-comments.png
   :width: 644px
   :alt: A comment on patch 2 from Tom Tester, with a note for the response

The response then appears below the review, to change, put in a Gmail draft
or send:

.. image:: images/serve-response.png
   :width: 614px
   :alt: The suggested response to Tom's comment

Sending the reviews
-------------------

'Send' on a card sends that review's Gmail draft, and 'Send 3 drafts' at the
top sends them all. Each needs a second tap, so a stray tap does nothing:

.. image:: images/serve-send.png
   :width: 644px
   :alt: The buttons at the top of the series, with 'Tap to confirm'

As each review is sent, its status turns green. A review which you decide
not to send can be deleted, with its Gmail draft, with 'Delete', which also
needs a second tap.

To find the reviews you have not finished with, use the filters at the top of
the Reviews tab: 'To send' lists the series with a Gmail draft waiting to be
sent, and 'Not drafted' those with a review which has no draft yet. Both
include hidden series, so a forgotten draft still turns up.

Answering replies
-----------------

When the server runs with ``-r``, each sync looks for replies to your sent
reviews. The Reviews tab shows each series with a reply, or a comment
response, waiting, and 'Needs reply' lists just those:

.. image:: images/serve-reviews.png
   :width: 660px
   :alt: The Reviews tab, with replies waiting on two series

Here Jo has replied to the review of v3 of the Ethernet driver. Tap the
series to see the reply below the review, then 'Suggest a response' to have
the agent suggest one. The card of each review with a reply says how many,
e.g. '1 reply', in amber while one still needs a response and in blue once
they are all dealt with. Tap it to go to the first reply, below the review:

.. image:: images/serve-reply.png
   :width: 644px
   :alt: Jo's reply to the review, with the suggested response

As with a comment, you can then change the response with 'Edit response',
put it in a Gmail draft or send it. 'No reply needed' marks the reply as
dealt with.

Tidying up
----------

Once you have finished with a series, tap 'hide' on it in the Reviews tab,
or 'Hide' at the top of its reviews, to move it to the Hidden list, so that
the list shows just the reviews you are working on. A hidden series comes
back by itself when a new version of it is reviewed or someone replies.

:doc:`The web server chapter </serve>` describes each of these, and the
:doc:`assisted patch review chapter </review>` covers the review itself in
full.
