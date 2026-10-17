.. SPDX-License-Identifier: GPL-2.0+

How patch review works
======================

This describes what ``patman review`` does with a series once it has found
it on patchwork. :doc:`The Assisted patch review chapter </review>` describes
how to use it.

Reviewing each patch
--------------------

For each patch, the agent:

1. Studies all patches in the series to understand the overall design
2. Re-reads the specific patch in detail
3. Examines surrounding source code for context
4. Checks existing comments from other reviewers on Patchwork and
   avoids repeating points already made
5. Produces a structured review (GREETING/COMMENT/VERDICT format)

After all patches are reviewed, a refinement agent makes a second pass
over the drafts to tighten the language, remove cross-patch duplicates
and check voice consistency. Approved reviews without comments are
excluded from refinement to preserve their quoted commit messages.

If the agent has nothing to say about a patch -- no comments and not an
approval -- no review is produced for it, rather than an empty
greeting-only reply.

Comments on the commit message are placed before comments on the code,
matching the usual reply order.

A mechanical cleanup step also runs to remove backticks and fix function
quoting style (e.g. ``malloc()`` not ```malloc```).

Only one review of a given series runs at a time. If a review for the
same series is already in progress -- from another patman invocation or
a scan -- the second is refused rather than corrupting the shared
review worktree and database records.

Applying the series
-------------------

Before checking out the review branch, patman stashes any uncommitted
changes -- including untracked files -- so build artefacts on the
current branch don't leak into the review. The original branch and
stash are restored at the end of the run, including on failure.

If the apply agent applies some but not all of the patches -- for
example one depends on a prerequisite series that is not yet upstream
-- patman warns (``Only N of M patches applied to <branch>; reviewing
what is there``) and reviews the patches that did apply. Only when
nothing applies at all does it fail and roll back the new version's
database row.

Each review is matched to its patchwork patch by subject, not by
position, so a patch that failed to apply does not shift the remaining
reviews onto the wrong patches.

Review states
-------------

Each review goes through these states:

- **new**: Review drafted, not yet sent
- **draft**: Gmail draft created
- **sent**: Draft was sent; body updated with actual sent content
- **deleted**: Draft was deleted without sending
- **replied**: Author or another reviewer has replied to our review
