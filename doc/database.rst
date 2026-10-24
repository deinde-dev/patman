.. SPDX-License-Identifier: GPL-2.0+

The patman database
===================

Patman stores series tracking, review and workflow state in a SQLite
database (``.patman.db``) in the top-level git directory. The schema is
versioned and migrated automatically when patman starts; it is currently at
v19. The :doc:`db` chapter of the manual covers where the database is
kept, the separate review database and sharing it between patman runs.

The database allows patman to track a patch series across multiple
versions, recording which patches belong to each version and how they
map to patchwork entries. This means patman can detect when a new
version of a series arrives, carry forward review tags and change logs
from earlier versions, and show upstream progress without re-querying
patchwork each time.

For assisted review of other people's patches, the database stores
the generated review text, Gmail draft IDs and thread state so that
patman can resume where it left off  - showing stored reviews, creating
drafts for reviews that were not yet sent, detecting when drafts have
been sent or deleted, and providing previous review context when a new
version of the series is posted. Review-handling notes from the
``handle-reviews`` workflow are also stored per-version so the agent has
context about what was changed and why.

Entity relationships
--------------------

::

    upstream 1---* patchwork        (patchwork.upstream references
                                     upstream.name)

    series   1---* ser_ver          (ser_ver.series_id -> series.id)

    ser_ver  1---* pcommit          (pcommit.svid -> ser_ver.id)

    ser_ver  1---* review           (review.svid -> ser_ver.id)

    series   1---* workflow          (workflow.series_id -> series.id)

    upstream 1---* inbox            (inbox.upstream references
                                     upstream.name)

A **series** represents a named patch series across all its versions.
Each **ser_ver** is one version of that series (e.g. v1, v2), linked to
patchwork by a series link. Each ser_ver has one **pcommit** per patch
and zero or more **review** records (one per drafted review).

Tables
------

series
~~~~~~

Patman tracks series that you are working on so it can keep an eye on
any feedback from other people (from patchwork). Series that are being
reviewed (other people's patches) are also stored here with
``source='review'``.

==========  ========  ==========================================
Column      Type      Description
==========  ========  ==========================================
id          INTEGER   Primary key (auto-increment)
name        TEXT      Series name (unique)
desc        TEXT      Series description / cover-letter title
archived    BIT       True if series is archived
upstream    TEXT      Upstream name (added v5)
source      TEXT      'review' for reviewed series (added v8)
hidden      BIT       True if a reviewed series is hidden from the
                      list of reviews (added v16)
==========  ========  ==========================================

ser_ver
~~~~~~~

Each time you send a new version of a series, patman creates a ser_ver
record linking it to patchwork. This lets patman track review tags,
change logs and upstream progress separately for each version. For
reviewed series, review-handling notes are stored here so the next
version has context about what was changed.

==================  ========  ==========================================
Column              Type      Description
==================  ========  ==========================================
id                  INTEGER   Primary key (auto-increment)
series_id           INTEGER   FK -> series.id
version             INTEGER   Version number (1, 2, ...)
link                TEXT      Patchwork series link/ID
cover_id            INTEGER   Patchwork cover letter ID (added v3)
cover_num_comments  INTEGER   Number of comments on cover (added v3)
name                TEXT      Cover letter name (added v3)
archive_tag         TEXT      Git tag for archived version (added v4)
desc                TEXT      Version description (added v6)
notes               TEXT      Review-handling notes (added v10)
==================  ========  ==========================================

pcommit
~~~~~~~

Each patch in a series version gets a pcommit record. Patman uses the
Change-Id to match patches across versions (so it knows which patch in
v2 corresponds to which patch in v1) and tracks the patchwork state and
comment count so ``patman series progress`` can show upstream status.

==============  ========  ==========================================
Column          Type      Description
==============  ========  ==========================================
id              INTEGER   Primary key (auto-increment)
svid            INTEGER   FK -> ser_ver.id
seq             INTEGER   Patch sequence (0-based)
subject         TEXT      Patch subject line
patch_id        INTEGER   Patchwork patch ID
change_id       TEXT      Change-Id tag value
state           TEXT      Patchwork state (e.g. 'new', 'accepted')
num_comments    INTEGER   Number of patchwork comments
==============  ========  ==========================================

review
~~~~~~

When patman reviews someone else's patches, it stores the generated
review email text here  - one record per patch plus optionally the cover
letter. This allows patman to show the review again later, create Gmail
drafts from stored reviews, detect when drafts have been sent or
deleted, and provide the previous review as context when a new version
of the series arrives. When ``--sync`` detects that a draft was sent,
the body is updated to the final text the user actually sent (after any
manual edits), so the stored review reflects what was really posted to
the mailing list.

================  ========  ==========================================
Column            Type      Description
================  ========  ==========================================
id                INTEGER   Primary key (auto-increment)
svid              INTEGER   FK -> ser_ver.id
seq               INTEGER   Patch sequence (0=cover, 1..N=patches)
body              TEXT      Review email body text
approved          BIT       True if Reviewed-by was given
timestamp         TEXT      ISO datetime of review creation
draft_id          TEXT      Gmail draft ID (added v9)
status            TEXT      'new', 'draft', 'edited' (the draft is
                            older than the text), 'sent', 'deleted',
                            'replied' (added v9)
gmail_msg_id      TEXT      Gmail message ID after sending (added v9)
gmail_thread_id   TEXT      Gmail thread ID for replies (added v9)
session_id        TEXT      ID of the agent session which wrote the
                            review, so that a revision can carry on
                            from it (added v12)
envelope          TEXT      JSON object with the headers for sending
                            the review as a reply to its patch:
                            ``subject``, ``in_reply_to``,
                            ``references``, ``to`` and ``cc``, stored
                            when its first draft is created so that
                            it can be drafted again without patchwork
                            (added v13)
================  ========  ==========================================

reply
~~~~~

When someone replies to one of your sent reviews, ``patman review --sync``
records the reply here (added v11), along with your response to it. The
response can be suggested by the agent, edited and then turned into a Gmail
draft which is threaded as a reply.

A comment on patchwork which you respond to with ``patman review
--respond-comment`` is recorded here too (added v15). It has no review_id or
gmail_msg_id, but records the version and patch it is about, and its ID on
patchwork.

================  ========  ==========================================
Column            Type      Description
================  ========  ==========================================
id                INTEGER   Primary key (auto-increment)
review_id         INTEGER   FK -> review.id, or NULL for a comment
gmail_msg_id      TEXT      Gmail message ID of the reply (unique), or
                            NULL for a comment
msgid             TEXT      Message-ID header, for threading a response
refs              TEXT      References header
sender            TEXT      From header
to_addrs          TEXT      To header
cc_addrs          TEXT      Cc header
subject           TEXT      Subject header
date              TEXT      Date header
body              TEXT      Text of the reply
response          TEXT      Text of your response, if any
status            TEXT      'new', 'suggested', 'skip', 'draft', 'sent'
draft_id          TEXT      Gmail draft ID of the response
timestamp         TEXT      When the reply was recorded
svid              INTEGER   For a comment, FK -> ser_ver.id (added v15)
seq               INTEGER   For a comment, the patch it is about, 0 for
                            the cover letter (added v15)
comment_id        INTEGER   For a comment, its ID on patchwork (added
                            v15)
================  ========  ==========================================

upstream
~~~~~~~~

Patman needs to know about the upstream repositories you send patches
to. Each upstream has a git remote name, URL and optional patchwork
settings. One upstream can be marked as the default so you do not need
to specify ``-U`` on every command.

================  ========  ==========================================
Column            Type      Description
================  ========  ==========================================
name              TEXT      Git remote name (unique, e.g. 'us')
url               TEXT      Repository URL
is_default        BIT       True if this is the default upstream
patchwork_url     TEXT      Patchwork server URL (added v6)
identity          TEXT      Git sendemail identity (added v6)
series_to         TEXT      Default To: alias for series (added v6)
no_maintainers    BIT       Skip get_maintainer.pl (added v6)
no_tags           BIT       Skip subject-tag processing (added v6)
lore_url          TEXT      Mailing list on lore (added v18)
no_feed           BIT       The inbox lists only the series added to
                            it (added v19)
================  ========  ==========================================

patchwork
~~~~~~~~~

Each upstream can have a patchwork project associated with it. This
stores the project name, ID and link name so patman can query patchwork
for series status, review tags and comments. Multiple upstreams can
point to different patchwork projects (e.g. U-Boot mainline vs a
custodian tree).

==============  ========  ==========================================
Column          Type      Description
==============  ========  ==========================================
name            TEXT      Project name (e.g. 'U-Boot')
proj_id         INTEGER   Patchwork project ID
link_name       TEXT      Patchwork link name (e.g. 'uboot')
upstream        TEXT      Upstream name, or NULL for default
==============  ========  ==========================================

workflow
~~~~~~~~

Patman records workflow events such as when a series was sent, when
review feedback arrived and what needs attention. The ``patman workflow
todo`` command uses this to show outstanding tasks across all your
series.

==============  ========  ==========================================
Column          Type      Description
==============  ========  ==========================================
id              INTEGER   Primary key (auto-increment)
series_id       INTEGER   FK -> series.id
action          TEXT      Workflow action (e.g. 'sent', 'todo')
timestamp       TEXT      ISO datetime
data            TEXT      JSON payload
ser_ver_id      INTEGER   FK -> ser_ver.id (added v7b)
==============  ========  ==========================================

traffic
~~~~~~~

The number of requests which patman sends to each patchwork server, and
how much it receives, in hourly totals, so that it is easy to see whether
patman is sending a lot of them. Each patman process adds its counts when
it exits; ``patman serve`` adds them as it goes. Counts older than 90 days
are dropped. Added in v14.

==============  ========  ==========================================
Column          Type      Description
==============  ========  ==========================================
hour            TEXT      Local hour, e.g. '2026-10-19 08'
url             TEXT      URL of the patchwork server
requests        INTEGER   Number of requests sent, including retries
bytes           INTEGER   Number of bytes received in responses
==============  ========  ==========================================

inbox
~~~~~

The series which you have added to the inbox of an upstream on the
``patman serve`` page, from a search of its patchwork server or of lore.
They stay in the inbox until you remove them. Added in v19.

==============  ========  ==========================================
Column          Type      Description
==============  ========  ==========================================
id              INTEGER   Primary key (auto-increment)
upstream        TEXT      Upstream name
link            TEXT      Patchwork series ID, or the Message-ID of
                          the series on lore
name            TEXT      Name of the series, as found by the search
timestamp       TEXT      When it was added
==============  ========  ==========================================

Each series is only in the inbox of an upstream once.

schema_version
~~~~~~~~~~~~~~

A single-row table that records which schema version the database is at.
Patman checks this on startup and runs any needed migrations
automatically.

==============  ========  ==========================================
Column          Type      Description
==============  ========  ==========================================
version         INTEGER   Current schema version (currently 19)
==============  ========  ==========================================
