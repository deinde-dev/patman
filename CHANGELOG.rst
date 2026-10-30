Changelog
=========

All notable changes to this project are documented here. The format is
based on `Keep a Changelog <https://keepachangelog.com/en/1.1.0/>`_ and
the project follows `Semantic Versioning <https://semver.org/>`_.

Unreleased
----------

Added
~~~~~
- ``patman maint apply`` applies a series from the mailing list to the push
  branch, e.g. ``main-push``, by the project's rules in the ``[maint]``
  section: it adds the review tags given in reply to this version, then the
  trailers each patch ends with, such as its Message-ID and your sign-off, and
  checks the result with ``maint check``. It applies the patches in a
  temporary worktree, so the tree you are working in is not touched,
  refuses a series which is incomplete or has a newer version, and says when
  one is applied already. Without a Message-ID it applies the current
  branch's series, if patman has sent it. ``-R`` starts the push branch again
  from upstream first, dropping what is on it, to apply a series again.
  ``maint check`` now checks the push branch when the branch checked out has
  no commits of its own.
- ``patman maint check`` checks the commits which a maintainer is about to
  push, i.e. those on the branch which are not upstream yet, against the
  project's rules in the new ``[maint]`` section of the settings: which
  trailers each patch from the list and each pull or merge request ends with,
  whether the author has signed it off, which trailers may link to an issue
  and in which repos, and whether merge commits are allowed. A new chapter,
  Maintaining a project, describes the rules, with dtc's as the example.
- Each series which you reviewed in the inbox of the ``patman serve`` page
  says who has the next move on it: 'your move' while a reply waits for your
  response or a review is not sent, 'waiting' once you have asked for changes
  and there is no new version, 'stale' after 28 days of that, and 'ready' when
  your reviews give every patch a Reviewed-by tag. New 'Your move', 'Waiting'
  and 'Ready' filters list them, from the new 'next' field of each series in
  ``GET /api/inbox``.
- 'View code' under a comment in a review on the ``patman serve`` page shows
  the whole file at the code which the comment quotes, in a panel at the right
  on a wide screen. The file links to the line on Elixir for U-Boot and Linux,
  on GitHub for dtc, or on the source browser given by the new ``source_url``
  review setting.
- 'Context' at the top of a series' reviews on the ``patman serve`` page shows
  what the agent was given for each review, such as the earlier review of the
  same patch and your notes, from ``GET /api/reviews/<id>/context``. Patman
  records it with each review, which is database schema v21, and reads it from
  the agent's session for an older review.
- ``patman review --stats`` counts the series and patches reviewed, the
  Reviewed-by tags given, the emails sent, the drafts waiting and the replies,
  in all and for each of the last eight weeks, or as many as given. The
  ``patman serve`` page has a menu at the top right, whose Stats page shows the
  same, from ``GET /api/stats``, with a bar chart of the weeks. Patman records
  when each review and response is sent, for the weeks, which is database
  schema v20.
- ``patman series autolink``, ``autolink-all`` and ``series send`` find a
  series on the upstream's list on lore when the upstream has a lore URL and
  no patchwork URL, as for dtc. With neither, and no patchwork project, they
  use the list on lore which the series is sent to. The link is the Message-ID
  of the cover letter, or of the first patch. ``series status``, ``gather``
  and ``open`` then read the series, its replies and its review tags from
  lore.
- A ``voice`` setting in the ``[review]`` section names the file holding your
  writing voice, so that each person can keep their own, rather than always
  using ``~/.config/patman.d/voice.md``.
- A Projects chapter in the manual gives the setup for U-Boot, Linux and dtc:
  where each one's series are tracked, its checkpatch and maintainers, and
  its review settings, with example settings for each.
- The guide's page on reviewing from ``patman serve`` shows 'View code', with
  the code in the panel beside a review, and its pictures show the page as it
  is now. The demo which makes them quotes code as real reviews do, and its
  git repo has the files which the first patch changes.

Changed
~~~~~~~
- For a series on lore, as for dtc, ``patman series gather`` marks a patch
  'accepted' once the upstream's main branch has it, found by the Message-ID
  in a ``Message-ID:`` or ``Link:`` trailer, so ``series progress`` shows
  which patches are applied. Each patch was 'new' before, since lore has no
  patch states.
- ``patman series gather-all`` reads each series on lore from its upstream's
  list, and the others from patchwork, where it needed patchwork for all of
  them before. This marks the applied patches 'accepted' too.
- A review's card on the ``patman serve`` page says how many replies it has,
  e.g. '2 replies', counting the comments on patchwork which you are
  responding to, in amber while one still needs a response, and goes to the
  first when tapped. The card no longer says 'comments' for a review which
  gives no Reviewed-by tag, since that is clear from the review, and was
  easily taken to mean that someone had commented.
- ``patman serve`` compresses its responses of 1KB or more with gzip, for a
  browser which accepts it, so that the page loads faster on a phone over a
  mobile network. The reviews of a long series go from 210KB to 51KB and the
  page's script from 129KB to 36KB.
- ``patman series send`` updates the patches of the version in the database
  from its branch when they differ, e.g. after a patch is folded into another
  following ``series inc``, so that ``status`` and autolink use the patches
  which were sent.
- ``patman series status``, ``gather``, ``patches`` and autolink warn when the
  patches on the branch differ from those in the database, and suggest
  ``patman series scan``, since they read the wrong commits otherwise.
- ``patman series add`` and ``series send`` warn when the branch name suggests
  v2 or later but there is no Series-version tag, so that the patches go out
  as v1, and suggest the name without the number if there is no earlier
  version.

Fixed
~~~~~
- A new version of a series is reviewed with each patch's latest review
  from any earlier version, matching a patch whose subject changed, and
  the agent is told when a patch's code is unchanged since the last
  version reviewed, so it does not raise new issues in code which was
  already reviewed.
- 'review --revise -d' and 'review --redraft' delete the old Gmail drafts
  only once the new ones are made, so a patchwork error no longer leaves
  the reviews with no drafts. A revision makes its drafts from the headers
  stored with the reviews, so it does not need patchwork.
- When autolink cannot find a version on lore, its list of possible matches
  shows each series once, rather than again under the title of its first
  patch, with the Message-ID of its cover letter.
- ``patman upstream add`` and ``upstream set`` no longer give the upstream the
  ``patchwork_url`` from ``~/.patman``, which is the default for
  ``patman -P``, so ``upstream set us -l <url>`` changes only the lore URL.
- A review of a new version gives each patch the earlier review of the same
  patch, matched by subject, rather than of the patch with the same number,
  so that a patch added or moved between versions does not give the later
  patches the earlier reviews of others.
- ``patman review --send`` and ``--send-reply`` record a draft which was sent
  already from Gmail as sent, with the text which went out, rather than
  failing with "Message not a draft".
- ``patman series version-change`` reads the commits of the version being
  changed, rather than those of v1, so changing v2 of a series no longer
  rewrites v1's commits as the new version.
- ``patman series scan`` shows a patch dropped from the end of the branch as
  removed, with '-', and counts it, rather than showing it with '+' as if it
  were added.
- ``patman series -s <name> -V <n> scan`` scans version n, rather than
  ignoring -V and scanning the branch which the name gives, i.e. v1.
- ``patman series scan`` keeps the patchwork (or lore) ID, state and number
  of comments of each patch which is still in the branch, so that
  ``series progress`` still shows the series as it was, without gathering
  it again.

0.6.0 - 2026-10-24
------------------

Added
~~~~~
- The top of the ``patman serve`` page, and the name of the browser tab, give
  the project which the server is for, such as dtc or U-Boot, so that servers
  for different projects are easy to tell apart.
- The inbox on the ``patman serve`` page can find a series by searching the
  upstream's patchwork project, or its list on lore, and keep it at the top of
  the inbox until you remove it. ``patman upstream add --no-feed`` gives an
  inbox which lists only these, e.g. for Linux, with the whole of
  ``https://lore.kernel.org/all`` as its list. This is database schema v19.
- The ``patman serve`` page works for an upstream whose list is on lore, such
  as dtc: its inbox lists the newest series sent to the list, read from
  lore's feed, and their patches, cover letters and comments are read from
  lore.
- ``patman review`` can read a series from a mailing list on lore, for a
  project with no patchwork, such as dtc: give the list with
  ``patman upstream add --lore-url``, and the Message-ID of the cover letter,
  or of the first patch, with ``-s``.
- A ``[review]`` section in the config, e.g. in a tracked
  ``.patman-defaults``, gives the name of the project which reviews are for
  and a file of its conventions, for the agent to check each patch against.
  This allows reviewing projects other than U-Boot, which stays the default.
- ``patman review --checkout`` checks out a series in the tree, to try it:
  its branch from the review if it has one, or else a new branch with the
  series applied by ``git am``. On the ``patman serve`` page, 'Try it' on a
  series in the inbox or on the reviews shows the command, to copy.
- Tapping a new version of a series in the inbox on the ``patman serve``
  page shows your review of the earlier version below its patches, with the
  status of each part and the replies waiting, even when the earlier version
  is too old to be among the pages loaded.
- The Reviews tab on the ``patman serve`` page has 'To send' and 'Not
  drafted' filters, listing the series with a Gmail draft waiting to be sent
  and those with a review which has no draft.
- The inbox on the ``patman serve`` page fetches the upstream when it is
  shown, if it has not done so for an hour, and 'Refresh' fetches it at once,
  so that it shows which series are applied without a sync. The top of the
  inbox says when the upstream was fetched.
- ``patman patchwork cache`` shows what the patchwork cache holds for each
  server, the requests sent to it and the reads which the cache answered,
  and which projects' events are followed.
- ``--no-cache`` reads everything from patchwork rather than from the cache,
  e.g. to check whether the cache is the cause of a problem. With
  ``patman --no-cache serve``, the jobs which the server starts do not use
  it either.
- Reviewing a series from the inbox on the ``patman serve`` page no longer
  reads the series from patchwork again, since the inbox keeps each series
  it reads in the cache under its ID.
- ``patman review`` can review a series which patchwork has not received in
  full, as happens when it misses a patch, taking the missing patches from
  the thread on lore.kernel.org and applying the series from lore's copy.
  On the ``patman serve`` page, such a patch has a 'lore' link in place of
  'Show the patch'.
- The patchwork traffic on the ``patman serve`` page gives the size of the
  reads which the cache answers, e.g. '638 from the cache (8.1 MB)', as well
  as their number.
- ``patman status`` and the sync read the thread of each series from
  lore.kernel.org, in one request, and read the comments on a patch from
  patchwork only if lore shows new replies to it, or they have not been read
  for a week. With ``--lore use``, or the ``lore`` setting, the comments come
  from lore itself; ``--no-lore`` reads them all from patchwork as before.
- ``patman status`` and the sync follow the project's events on patchwork,
  which say when a patch's state or delegate changes, and use the other
  patches from the cache rather than reading each one again. This halves
  the requests a sync sends to patchwork.
- The patchwork traffic on the ``patman serve`` page lists the requests of
  each kind, such as ``patch-comments`` or ``series-search``, for today and
  the last seven days, to show what the traffic to each server is made of.
  This needs database v17.
- patman keeps what it reads from patchwork in a cache,
  ``~/.cache/patman/patchwork.db``, and uses it again rather than asking each
  time, for as long as suits what it is: a patch for an hour, comments for
  five minutes, a complete series until it is not used for 90 days. Searches,
  ``patman status`` and the sync still read the current state. On the
  ``patman serve`` page, a patch says when it was read, with 'Refresh' to read
  it again, the inbox is kept across a restart, and the patchwork traffic
  includes the reads which the cache answers. Set ``PATMAN_PWCACHE`` to
  ``off`` to use no cache.
- ``--keep-assist-tags``, or the ``keep_assist_tags`` setting, keeps the tags
  which a coding assistant adds, such as ``Co-developed-by: ...
  <noreply@...>``, in the patches sent, for projects which want them. Set it
  in a repo's ``.patman`` or ``.patman-defaults`` for one project.
- The inbox on the ``patman serve`` page marks a series whose patches are
  applied upstream, on the main or next branch, as 'applied', or e.g.
  'applied 2/5', with an 'Applied' filter, finding the patches in git by their
  subjects rather than asking patchwork. An older version whose patches came
  in a newer one is marked 'later version applied'.
- The inbox also counts a patch accepted on patchwork as applied, and shows
  the other states of patches, such as 'superseded', taken from the
  project's events and the patches patman reads, at most one request a
  minute. Series whose patches are all superseded or rejected leave 'To
  review'. When patman begins following the events again, e.g. after a week
  without a check, it drops the states which may have changed meanwhile.

Changed
~~~~~~~
- ``patman review`` does not review a series which you sent, since the
  review would go to you; ``--any-author`` reviews it anyway.
- The inbox on the ``patman serve`` page counts a series sent from your
  address as yours, not just one in your database.
- ``patman review`` does not apply a series which is for another project,
  such as a kernel series sent to the dtc list, i.e. one where more than
  half of the files which it changes are not in the tree. This saves the
  agent's time.
- A new version of a series in the inbox on the ``patman serve`` page says
  which version you reviewed, e.g. 'v2 reviewed', rather than 'earlier
  version reviewed', and shows the replies to that review which are waiting
  for your response.
- The installation instructions say how to install the latest code from
  GitHub, to try changes before they are released, and how to install with
  pipx where pip cannot install into the system's Python.
- Tapping the Inbox tab of the ``patman serve`` page shows the tab at once,
  saying that the inbox is loading while it is read from patchwork.
- The inbox on the ``patman serve`` page fetches the upstream while it
  reads patchwork, rather than afterwards, so it shows sooner when both are
  needed.

Fixed
~~~~~
- Going back to the Inbox tab on the ``patman serve`` page keeps the pages
  loaded with 'Load more', rather than showing only the first 50 series
  again. Only the first page is read from patchwork again, if it is more than
  ten minutes old.
- An AI review no longer carries a second Reviewed-by tag, with an address
  which the agent guesses; patman drops any tag which the agent writes and
  adds the reviewer's own.
- The ``patman serve`` page no longer says 'offline' for good after the
  server misses one check of the sync state, e.g. while restarting; it asks
  again ten seconds later.
- Settings for the options of a subcommand of a subcommand, such as
  ``keep_change_id`` or ``insert_base_commit`` for ``patman series send``,
  take effect. They were only used by ``patman send``.
- ``patman review --revise`` on a single review carries on from the agent
  session which wrote it again. The session belongs to the review's worktree,
  which is removed once the review is done, so the revision checks out the
  review branch there again for the agent, rather than running it in the main
  checkout, where Claude Code cannot find the session.
- On a reviewed series in the ``patman serve`` page, the buttons at the top,
  such as 'Send 3 drafts', leave out the reviews which a job is acting on and
  change once it finishes, rather than staying as they are until the page is
  shown again. A job which fails part way also shows which reviews it sent.
- The review agent uses the Claude Code in ``~/.local/bin`` when that is not
  on the PATH, as is common for a service such as the one running
  ``patman serve``, rather than the old copy bundled with claude-agent-sdk.
  If only the bundled copy is present, this is reported.
- A sync of series on more than one patchwork server follows the events
  of each server's project, rather than only those of the first server.
- ``patman review`` no longer leaves a series with no versions in the
  review database when there is nothing to review, e.g. a series for another
  project, or when applying it fails. The inbox on the ``patman serve`` page
  took such a series for an earlier review of the next version.
- ``patman review --relink`` removes the series with no versions from an
  older review database.
- The inbox on the ``patman serve`` page only counts a series as reviewed if
  it has a review, not if it was only applied, e.g. with ``--apply-only``,
  so it can be reviewed from there. A later version shows the review of the
  latest version which has one.
- The Reviews tab on the ``patman serve`` page says 'not reviewed' on a
  series which is applied but not reviewed, rather than '0/0 approved'.

0.5.0 - 2026-10-19
------------------

Added
~~~~~
- Reviewed series can be hidden from the Reviews tab of the ``patman serve``
  page, which then shows only the active ones by default, with filters for
  hidden ones and all of them. A hidden series shows again when a new version
  of it is reviewed or someone replies. This needs database v16.
- ``patman review --delete -i SEQ`` deletes the stored reviews of some
  patches, or the cover letter, with their Gmail drafts, if they have not
  been sent. On the ``patman serve`` page, each unsent review has 'Delete'.
- ``patman serve --max-reviews N``, or the ``max_reviews`` setting, sets how
  many reviews run at once (default 5).
- Starting a review on the ``patman serve`` page, from the inbox, the
  cover-letter card, 'Review again' or 'Review a series', asks for context to
  give the agent, as ``patman review -c`` does. ``POST /api/review`` takes
  ``context``.
- patman can respond to any comment on patchwork about a reviewed series,
  not just replies to your reviews: ``patman review --respond-comment ID -i
  SEQ`` has the agent suggest a response, which is then drafted and sent like
  a response to a reply. On the ``patman serve`` page, 'Comments' under each
  review, and on the cover-letter card, lists the comments, each with
  'Respond'. The reply table gains the version, patch and comment ID
  (database v15).
- patman counts the requests it sends to each patchwork server, and the
  data it receives, by the hour, in a new ``traffic`` table (database v14).
  The sync page of ``patman serve`` shows the totals for this hour, today
  and the last seven days, and ``GET /api/sync`` gives them.
- On the ``patman serve`` page, a series in the inbox lists its cover letter
  along with its patches, and a reviewed series with no review of its cover
  letter has a card for it, to read it or have the agent review it.
  ``patman review -i 0`` reviews just the cover letter.
- The buttons, links and tabs on the ``patman serve`` page have tooltips
  saying what they do, shown when hovering over them or, on a phone, with a
  long press. A button which needs a second tap says so.
- The ``patman serve`` page shows the version of patman below each screen,
  with the commit when the server runs from a git checkout which is not at a
  release. ``GET /api/version`` gives it.
- Requests to patchwork use the proxy given by ``HTTPS_PROXY`` or
  ``HTTP_PROXY`` in the environment, apart from hosts in ``NO_PROXY``, so
  patchwork can be reached through another machine when it cannot be
  reached directly.
- Each series on the ``patman serve`` page has a Sync button, which gathers
  the patch states and comments for just that version from patchwork.
  ``POST /api/series/<id>/sync`` does the same.
- ``snap/snapcraft.yaml`` packages patman as a snap, with classic
  confinement. CONTRIBUTING.rst says how to build it.
- The guide has a page on reviewing a series from the ``patman serve`` page,
  with a picture of each step: starting the review, changing the reviews,
  responding to comments and replies, and sending them.

Changed
~~~~~~~
- ``patman serve`` keeps the inbox it reads from patchwork for ten minutes,
  so that showing it again, on any device, sends no requests. The inbox says
  when it was read and has 'Refresh' to read it again. ``GET /api/inbox``
  gives ``fetched`` and takes ``refresh=1``.
- 'Comments' under a review on the ``patman serve`` page leaves out your own
  comments on patchwork, such as the review itself, since they need no
  response. ``GET /api/comments`` marks each comment from your git
  ``user.email`` address as ``own``.
- 'Suggest a response', 'Create Gmail draft' and 'Send' on a reply, on the
  ``patman serve`` page, leave the page where it is and show the job below the
  reply, rather than going to the job's page.
- On the ``patman serve`` page, the status of each review, and the counts in
  the list of reviews, are amber when the review is still to be sent and
  green once it has been sent, with a tooltip saying what the status means.
- ``patman serve`` keeps its jobs, with their output, when it restarts, so
  the Jobs tab and the job numbers carry on. A job which the server no longer
  knows about says so, rather than showing 'No job 1'.
- Reviews of series started from the ``patman serve`` page run alongside
  the other jobs, up to five at once, rather than one at a time.
- The Jobs tab of the ``patman serve`` page shows the name of the series
  which each job acts on, with a link to its reviews.
- 'Review' in the inbox of the ``patman serve`` page leaves the inbox open,
  so that other series can be reviewed too, and shows how each review is
  getting on, rather than going to the Jobs tab.
- The em dashes (—) which the review agent writes in reviews and suggested
  responses are replaced with a hyphen, which suits plain-text email. The
  ``em_dash`` setting, or ``--em-dash``, changes what is used, and ``—``
  keeps them. patman's prompts no longer use em dashes either.
- On the ``patman serve`` page, the button at the top of each file in a diff
  is called 'View whole file', as is the link between its chunks which goes
  to the same place, and looks like a button rather than part of the diff.
- ``patman review --redraft``, and Redraft on the ``patman serve`` page, no
  longer ask patchwork for the series and its patches' email headers. Each
  review stores the headers for its reply when its first draft is created,
  so its drafts can be recreated when patchwork is slow or cannot be
  reached. The database is updated to v13 to hold them.
- patman sends at most 4 requests to a patchwork server at once, rather
  than 50, since a busy server may block a client which opens a lot of
  connections.
- When ``patman serve`` runs without ``--sync``, 'Sync now' on the page
  needs a second tap, and the page says how many series it reads.

Fixed
~~~~~
- On the ``patman serve`` page, the status of a review, and whether it
  approves the patch, are updated when a job acting on it finishes, rather
  than only when the page is shown again, so sent reviews show as sent.
- The ``patman serve`` page no longer shows a stray 'false' below a
  response which has been sent, and the like elsewhere.
- Times on the ``patman serve`` page, such as when a job started, are right
  when the browser is in a different time zone from the server. Before, they
  were out by the difference.
- The inbox of the ``patman serve`` page shows when a series is being
  reviewed, rather than offering 'Review' again, even after the page is
  reloaded or when the review was started elsewhere. The server refuses to
  start a second review of a series which is being reviewed.
- A review which is changed after its Gmail draft is made, without
  updating the draft, is marked 'edited' ('draft out of date' on the
  ``patman serve`` page). ``patman review --send``, and Send on the page,
  create its draft again before sending it. Before, the old draft was sent
  and the change was lost.
- When a patchwork server does not respond, the error says which server and
  how long patman waited, rather than just ``TimeoutError``.
- ``patman test`` gives the same results whichever directory it is run
  from. The tests which run ``checkpatch.pl`` failed when run outside
  patman's sources, since they found another ``checkpatch.pl`` or missed
  the options in ``.checkpatch.conf``; they are now skipped when the
  sources are not present.
- A patchwork URL whose name does not resolve fails straight away when
  aiodns is installed, as it does without it, rather than being tried four
  times.
- When Python is started under another name, as a Guix wrapper does,
  ``patman serve`` and ``patman review --scan`` run their jobs with Python
  rather than with the wrapper, which fails. ``test_help``,
  ``test_full_help``, ``test_custom_get_maintainer_script`` and
  ``test_series_send`` failed in the same way.
- ``test_gmail_send_draft`` is skipped when the Gmail API
  (google-api-python-client) is not installed, rather than failing, since
  it is optional.

0.4.0 - 2026-10-18
------------------

Added
~~~~~
- ``patman status`` and ``patman series gather`` add a review tag given in
  a reply to the cover letter, e.g. a Reviewed-by for the whole series, to
  every patch which does not already have it.
- Revising, redrafting or sending reviews on the ``patman serve`` page keeps
  the page where it is, showing the job on each review it acts on, then the
  new text, with the text from before available.
- While a job is acting on a review, ``patman serve`` refuses to change the
  review or start another job on it, and the page disables its buttons, even
  after a reload. ``patman review --revise`` leaves a review alone if it
  changed while being revised, and shows the revised text instead.
- Each tab of the ``patman serve`` page remembers the screen last shown in
  it, each screen keeps its scroll position, and the Jobs tab shows how many
  jobs are waiting or running.
- ``patman review --revise``, and revising on the ``patman serve`` page,
  give the agent the cover letter, the other reviews of the series, the
  comments from patchwork and the reviewer's writing voice, so that the
  revised text fits with the rest.
- Each review records the agent session which wrote it, and revising a
  single review carries on from a copy of that session, falling back to a
  new session if it has gone. This needs a database migration to v12.
- A guide in ``doc/guide/`` shows how to use patman step by step with a
  real U-Boot series which was merged in v2026.07: setting up, sending a
  first series, checking its status and collecting review tags, sending the
  next version, tracking it all with ``patman series``, sending to more
  than one upstream, reviewing a series with ``patman review``, following
  it all from a phone with ``patman serve``, and dealing with common
  problems.
- The docs have a command reference generated from patman's options, a page
  on how patch review works, a map of the source, and the contributing
  notes. The manual's
  chapters link to each other and to the guide, and 'patman -H' shows the
  links as plain text.
- ``patman status -C`` shows the comments on the cover letter, as
  ``patman series status -C`` does.
- ``patman series add`` sets the series' upstream to the remote which its
  branch tracks, if that remote is an upstream and ``-S`` is not given.
- ``patman serve`` runs a web server showing series status, patchwork
  comments, draft reviews and todos on a mobile-friendly page, optionally
  syncing with patchwork periodically. Its JSON API is described in
  ``doc/serve-protocol.rst``.
- ``patman serve`` can start reviews, scans for new versions and redrafts,
  which run one at a time as jobs, with their output shown on the page.
- ``patman serve`` resumes the jobs which had not finished when it stopped.
- The ``patman serve`` page has an inbox of recent series on patchwork,
  marking those already reviewed.
- The ``patman serve`` page shows the commit message, diff and comments of
  each patch and cover letter, also beside its review, and the code around
  each chunk of a diff.
- Reviews which have not been sent can be edited on the ``patman serve`` page,
  either whole or one comment at a time.
- The ``patman serve`` page shows replies to reviews, and a response can be
  edited, drafted and sent there.
- The ``patman serve`` sync fetches the upstreams which reviews use, so that
  reviews apply patches to up-to-date branches.
- Reviews of other people's series can be kept in a separate database, given
  by the ``review_db_dir`` setting or ``--review-db-dir``.
- ``patman database`` (alias ``db``) moves reviews between the main and review
  databases with ``split`` and ``join``, and copies series from another
  database with ``merge``.
- ``review --sync`` records replies to sent reviews, suggesting a response to
  those sent in the last 14 days. ``review --replies`` lists them,
  ``--suggest-reply`` asks the agent for a response and ``--draft-reply``
  creates a Gmail draft of it, threaded as a reply.
- ``review --send-reply`` sends the response to a reply, creating its Gmail
  draft first if needed.
- ``review --revise`` has the agent change the unsent reviews of a series
  following a note, also from the ``patman serve`` page.
- ``review --send`` sends the Gmail drafts of a series' reviews, also from the
  ``patman serve`` page.

Changed
~~~~~~~
- The agents which refine reviews and suggest responses to replies follow
  the same rules for quoting as the review agents: single quotes for
  identifiers and other quoted text, and the author's text left as it is
  in quoted lines. They used to be told not to quote identifiers.
- The ``patman serve`` API gives status 400 for a request which is not
  valid, e.g. with a bad value or a missing field, rather than 404.
- Patchwork requests are retried when the connection fails, is reset or
  stalls, or the server returns a 502, 503 or 504 status. The timeout is set
  with ``--patchwork-timeout`` or the ``patchwork_timeout`` setting.
- ``series gather-all`` reads each series from the patchwork server of its
  upstream, and updates the series it can read even if others fail.
- ``review`` applies a series to the upstream's ``main`` branch, if it has
  one, rather than ``master``.
- Without ``-b``, ``review`` applies a series to the base commit or branch
  which it says it is based on.

Fixed
~~~~~
- ``patman review`` says that the review agent is not installed, and how to
  install it, before making a worktree, rather than failing to apply the
  patches.
- ``patman review`` records the upstream of each series it reviews, and
  names its branch after it, e.g. ``us-498633-review``. It did not, so the
  ``patman serve`` page read the series from the default upstream's
  patchwork server. Revising a review made before this still finds its
  ``pw-`` branch.
- In a review email, the first line of the quoted diffstat keeps its
  leading space, so that it lines up with the other lines.
- The tests no longer need /usr/bin/env, nor an installed test script
  to keep its executable bit, and give the same results whichever
  directory they are run from, which helps when packaging patman, e.g.
  for Guix. A command which fails in a test now shows its output.
- The columns of ``patman series summary`` line up with its header.
- ``patman status`` finds the patchwork link on a branch whose name has no
  version number, using the series' own version (from ``Series-version``,
  or 1), rather than reporting that there is no link for ``vNone``.
- ``patman status -T`` reads patchwork with a single thread, as its help
  says; the option is otherwise ignored.
- ``patman patchwork set-project`` and ``get-project`` use the remote which
  the current branch tracks, or the only remote, when none is given, rather
  than always asking for one. The error lists the remotes to choose from.
- Reviews which have been sent are not drafted again by ``review -d``,
  ``--scan`` or ``--redraft``, and ``--redraft`` only drafts the patches
  chosen with ``-i``.
- ``review --sync`` finds sent reviews reliably, including those answered in
  the thread rather than by sending the draft, and is much faster with many
  drafts.

0.3.3 - 2026-10-12
------------------

Added
~~~~~
- The command is also installed as ``patch-manager``, alongside ``patman``.
  Debian already has an unrelated ``patman`` command (the PatMaN sequence
  aligner), so the Debian package installs only ``patch-manager``.

0.3.2 - 2026-10-11
------------------

Changed
~~~~~~~
- The U-Boot Python library is now a dependency, from the ``u-boot-pylib``
  package, rather than a copy vendored into patman. The Debian package
  depends on ``python3-u-boot-pylib``.

0.3.1 - 2026-10-07
------------------

Fixed
~~~~~
- The bundled ``u_boot_pylib`` library is installed inside the ``patman``
  package rather than as a top-level package, where it shadowed U-Boot's own
  copy for U-Boot's tools in the same environment and clashed with other
  packages which provide it, such as ``binary-manager`` and ``buildman``.
- The cover-letter review reply now goes to the whole original audience.
  Its headers were never fetched, so the reply listed only the submitter
  and the mailing list and dropped the original To and Cc. Fetch the
  cover's headers, as the per-patch replies already do, so the reply is
  a proper reply-all.
- ``review`` no longer fails with "Failed to apply patches to branch"
  when a series is already applied upstream. If the apply step runs to
  completion but applies nothing (every patch is already present), that
  is now treated as "nothing to review" and the command exits cleanly
  instead of reporting an error.

0.3.0 - 2026-09-27
------------------

Added
~~~~~
- ``send -I`` / ``--identity`` selects the git send-email identity (the
  ``[sendemail "<identity>"]`` config section) for a single run,
  overriding any identity configured on the upstream.

Changed
~~~~~~~
- ``review --scan -d`` now also creates Gmail drafts for series reviewed
  by an earlier scan without ``-d``, drafting them from the stored
  reviews without reviewing again. ``-n`` previews which would be
  drafted, and the summary reports how many were.
- Review comments are no longer hard-wrapped: the agent is told to write
  each paragraph on a single line so the reader's mail client wraps it.
  Quoted lines (starting with ``>``) and indented code are left as they
  are.
- The ``Created N Gmail draft(s)`` line (and the dry-run equivalent) now
  names the review branch and the series link, so it is clear which
  series the drafts belong to.
- A review now answers each commit-message comment inline, right after
  the quoted line it refers to, instead of quoting the whole message at
  the top and then re-quoting individual lines again lower down. Code
  comments still follow the quoted message with their diff hunk.

Fixed
~~~~~
- Creating Gmail drafts no longer fails with "Invalid Cc header" when a
  patch's To/Cc headers were stored by patchwork as folded RFC 2047
  ``unknown-8bit`` encoded words: the headers are now unfolded and
  decoded (treating ``unknown-8bit`` as UTF-8), and the addresses
  de-duplicated and re-encoded.
- A review no longer restyles the author's quoted code: the cleanup
  that converts double quotes to single (to match our prose) skipped
  the ``> `` quoted lines, so a string literal such as ``"some_key"`` is
  reproduced faithfully instead of becoming ``'some_key'``.

0.2.0 - 2026-07-15
------------------

Added
~~~~~
- ``review --scan`` prints a per-series summary before its totals: one
  line per reviewed series giving the link, patch count, how many
  patches drew comments, how many were approved and the title.
- ``review --model`` chooses the Claude model to review with (e.g.
  ``sonnet``, ``opus`` or a full model id), overriding your global
  Claude default so a review need not use a personal default such as
  Fable. Set ``model`` in ``.patman`` to pin it for a project.
  ``review --list-models`` prints the aliases it accepts.
- ``review -w`` / ``--whole-series`` reviews the whole series when you
  locate it with ``-p`` / ``-P``, instead of just the one patch. Useful
  when you know a patch but not the cover letter.
- ``review -V`` selects which version of a series to review when
  searching by title (``-S``); it defaults to the most recent.
- ``send`` can post a series to a web submission endpoint (a relay, as
  b4 uses) instead of ``git send-email``, for contributors without
  working outbound SMTP. Set ``send_endpoint_web`` (or
  ``--send-endpoint-web``); each message is attested with patatt, and
  ``--reflect`` sends the series back to you only as a test.
  ``send --web-auth-new`` / ``--web-auth-verify`` register your key and
  identity with the endpoint, and ``--no-relay`` sends with git
  send-email for one run. Threading (``--thread``) and ``--in-reply-to``
  are honoured. Signing uses patatt, now a core dependency.

Fixed
~~~~~
- A branch whose name has digits in the middle, such as ``rv1106e2``,
  now takes its version from the trailing digits like any other. Its
  version was read as 1 rather than 2, so ``series add`` failed with
  "suggests version 1 but Series-version tag indicates 2".

0.1.0 - 2026-07-05
------------------

Added
~~~~~
- A tracked ``.patman-defaults`` file at the repo root provides
  project-shipped config defaults that ``~/.patman``, a local ``.patman``
  and command-line arguments all override, per key. It is the new
  lowest-priority config layer, so a project can ship a working setup
  without taking control away from the user.
- ``series review`` runs an AI review of your own series (added with
  ``series add``) -- the patches and the cover letter -- and stores the
  findings in the database, so you can check a series before sending it
  and re-read the review later with ``series info -r`` without paying
  for it again.
- ``review --coverity`` runs Coverity on the base and the patched series
  and feeds the newly introduced defects into the review as context.
  ``--coverity-defconfig`` selects the board to build (default
  sandbox_defconfig). Needs the cov-* tools on PATH.
- ``review --scan`` looks on patchwork for new versions of series that
  have already been reviewed and reviews the latest version, once it has
  fully appeared on patchwork. Reviews run in parallel, each in its own
  child process and worktree, up to ``--jobs`` at a time (default 4).
  ``-n`` / ``--dry-run`` shows which series would be reviewed, waiting
  or skipped, without launching any reviews.
- ``review --redraft`` recreates the Gmail drafts from the stored
  reviews even when a draft already exists, to recover after an error.
- ``review --relink`` repairs a database where versions of a series were
  stored as separate records, merging them so follow-up reviews see the
  earlier feedback. It backs up the database first.

Fixed
~~~~~
- A review stored each series version under its raw '[vN,0/M] ...' title,
  so versions of one series did not link and each follow-up review ran
  without the earlier feedback and raised fresh points. Store the cleaned
  title instead, so versions link. Existing databases can be repaired
  with ``review --relink``.
- Prior-review context now comes from the most recent earlier version
  that has reviews, rather than strictly the immediately previous one, so
  a gap in the version history no longer drops the context.
- ``review -f`` and ``review --redraft`` delete the Gmail drafts they
  replace, instead of leaving the old ones orphaned as duplicates in the
  same thread.
- A patch the review agent has nothing to say about (no comments and not
  an approval) no longer produces an empty greeting-only review; the
  patch is left unreviewed instead.
- Comments on the commit message are now placed before comments on the
  code in a review, rather than in whatever order the agent emitted them.
- Reviews attach to the correct patchwork patch by subject rather than by
  position. When a patch fails to apply, leaving fewer commits on the
  branch than the series has patches, the remaining reviews no longer
  shift onto the wrong patches.

Changed
~~~~~~~
- ``review`` only reviews a series when at least one of its patches is in
  an active patchwork state (new, RFC, under-review, changes-requested or
  needs-review-ack); reviewing an inactive series fails with a message
  naming ``--any-state``, which overrides the check. ``review --scan``
  skips inactive series.
- A review now takes an exclusive lock on its series, so a second review
  of the same series is refused rather than corrupting its worktree and
  records.

0.0.11 - 2026-06-18
-------------------

Added
~~~~~
- A project logo, shown in the documentation and the README.

Fixed
~~~~~
- ``autolink`` finds a series whose cover-letter title changed in a
  later version: it searches patchwork by the current title rather than
  the title recorded when the version was created.

0.0.10 - 2026-06-15
-------------------

Fixed
~~~~~
- ``status --dest-branch`` (building a branch with the gathered review
  tags) works again on pygit2 1.16 and newer, where ``merge_trees()``
  no longer accepts a branch object.

0.0.9 - 2026-06-15
------------------

Fixed
~~~~~
- ``series status`` and ``gather`` now work on archived series, reading
  the patches from the archive tag rather than the deleted branch.

Changed
~~~~~~~
- Update the pinned dependencies to current releases and support the
  pygit2 1.15 API.
- Reorganise the documentation: it lives under ``doc/``, includes the
  changelog, can be built with ``make -C doc html``, and reads as a
  general patch tool rather than a U-Boot-specific one.

0.0.8 - 2026-06-08
------------------

Added
~~~~~
- ``patman review -c/--context`` passes extra notes to the review agent
  for a single run -- either a literal string or ``@path`` to read the
  text from a file.
- ``patman series changes <text>`` records a ``Series-changes`` (or,
  with ``-c``, ``Cover-changes``) bullet on the HEAD commit and amends
  it, instead of hand-editing the commit message.
- Documentation is now published at https://patman.readthedocs.io/.

Changed
~~~~~~~
- Follow-up reviews respect the "say it in v1" convention: later
  versions confirm whether earlier feedback was addressed rather than
  piling on fresh nits.
- A partial patch apply now warns and continues with whatever applied,
  instead of aborting and discarding the work.
- Patman is now distributed as the self-contained ``patch-manager``
  package, with ``u_boot_pylib`` vendored in, installable from PyPI
  with ``pip install patch-manager``.

Earlier releases
----------------

Releases 0.0.7 and earlier predate this changelog.
