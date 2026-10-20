.. SPDX-License-Identifier: GPL-2.0+

Tags
====

patman reads tags from your commit messages to know where to send the series,
what its cover letter says, its version, its change logs and so on. It
removes them from the patches it sends. For example, the top commit of a
series might end::

    Series-to: u-boot
    Series-cc: heinrich
    Series-version: 2
    Cover-letter:
    binman: Tidy up some missing bintool docs
    Some of the bintools are missing documentation. This series adds the
    missing pieces to bring things up to scratch.
    END

Most tags can only appear once in the series, and they usually go in the top
commit, where ``git commit --amend`` reaches them. Change logs go in the
commit they describe. Tags are case sensitive: ``series-to:`` is not
recognised.

The tags are:

Series-to: email / alias
    Email address / alias to send patch series to (you can add this
    multiple times)

Series-cc: email / alias, ...
    Email address / alias to Cc patch series to (you can add this
    multiple times)

Series-version: n
    Sets the version number of this patch series

Series-prefix: prefix
    Sets the subject prefix. Normally empty but it can be RFC for
    RFC patches, or RESEND if you are being ignored. The patch subject
    is like [RFC PATCH] or [RESEND PATCH].
    In the meantime, git format.subjectprefix option will be added as
    well. If your format.subjectprefix is set to InternalProject, then
    the patch shows like: [InternalProject][RFC/RESEND PATCH]

Series-postfix: postfix
    Sets the subject "postfix". Normally empty, but can be the name of a
    tree such as net or net-next if that needs to be specified. The patch
    subject is like [PATCH net] or [PATCH net-next].

Series-name: name
    Sets the name of the series. You don't need to have a name, and
    patman does not yet use it, but it is convenient to put the branch
    name here to help you keep track of multiple upstreaming efforts.

Series-links: [id | version:id]...
    Set the ID of the series in patchwork. You can set this after you send
    out the series and look in patchwork for the resulting series. The
    URL you want is the one for the series itself, not any particular patch.
    E.g. for http://patchwork.ozlabs.org/project/uboot/list/?series=187331
    the series ID is 187331. This property can have a list of series IDs,
    one for each version of the series, e.g.

    ::

       Series-links: 1:187331 2:188434 189372

    Patman always uses the one without a version, since it assumes this is
    the latest one. When this tag is provided, patman can compare your local
    branch against patchwork to see what new reviews your series has
    collected ('patman status').

Series-patchwork-url: url
    This allows specifying the Patchwork URL for a branch. This overrides
    both the setting files ("patchwork_url") and the command-line argument.
    The URL should include the protocol and web site, with no trailing slash,
    for example 'https://patchwork.ozlabs.org/project'

Cover-letter:
    Sets the cover letter contents for the series. The first line
    will become the subject of the cover letter::

        Cover-letter:
        video: Speed up the console
        This series makes the video console faster, by drawing whole
        lines at a time.
        END

Cover-letter-cc: email / alias
    Additional email addresses / aliases to send cover letter to (you
    can add this multiple times)

Series-notes:
    Sets some notes for the patch series, which you don't want in
    the commit messages, but do want to send, The notes are joined
    together and put after the cover letter. Can appear multiple
    times::

        Series-notes:
        This depends on the 'cyclic' series, which is not applied yet.
        END

Commit-notes:
    Similar, but for a single commit (patch). These notes will appear
    immediately below the ``---`` cut in the patch file::

        Commit-notes:
        This has only been tried on sandbox so far.
        END

Signed-off-by: Their Name <email>
    A sign-off is added automatically to your patches (this is
    probably a bug). If you put this tag in your patches, it will
    override the default signoff that patman automatically adds.
    Multiple duplicate signoffs will be removed.

Tested-by / Reviewed-by / Acked-by
    These indicate that someone has tested/reviewed/acked your patch.
    When you get this reply on the mailing list, you can add this
    tag to the relevant commit and the script will include it when
    you send out the next version. If 'Tested-by:' is set to
    yourself, it will be removed. No one will believe you.

    Example::

        Tested-by: Their Name <fred@bloggs.com>
        Reviewed-by: Their Name <email>
        Acked-by: Their Name <email>

Series-changes: n
    This can appear in any commit. It lists the changes for a
    particular version n of that commit. The change list is
    created based on this information. Each commit gets its own
    change list and also the whole thing is repeated in the cover
    letter (where duplicate change lines are merged if
    Series-process-log includes uniq).

    By adding your change lists into your commits it is easier to
    keep track of what happened. When you amend a commit, remember
    to update the log there and then, knowing that the script will
    do the rest.

    Example::

        Series-changes: n
        - Guinea pig moved into its cage
        - Other changes ending with a blank line
        <blank line>

Commit-changes: n
    This tag is like Series-changes, except changes in this changelog will
    only appear in the changelog of the commit this tag is in. This is
    useful when you want to add notes which may not make sense in the cover
    letter. For example, you can have short changes such as "New" or
    "Lint".

    Example::

        Commit-changes: n
        - This line will not appear in the cover-letter changelog
        <blank line>

Cover-changes: n
    This tag is like Series-changes, except changes in this changelog will
    only appear in the cover-letter changelog. This is useful to summarize
    changes made with Commit-changes, or to add additional context to
    changes.

    Example::

        Cover-changes: n
        - This line will only appear in the cover letter
        <blank line>

Commit-added-in: n
    Add a change noting the version this commit was added in. This is
    equivalent to::

        Commit-changes: n
        - New

        Cover-changes: n
        - <commit subject>

    It is a convenient shorthand for suppressing the '(no changes in vN)'
    message.

Patch-cc / Commit-cc: Their Name <email>
    This copies a single patch to another email address. Note that the
    Cc: used by git send-email is ignored by patman, but will be
    interpreted by git send-email if you use it.

Series-process-log: sort, uniq
    This tells patman to sort and/or uniq the change logs. Changes may be
    multiple lines long, as long as each subsequent line of a change begins
    with a whitespace character. For example,

    Example::

        - This change
          continues onto the next line
        - But this change is separate

    Use 'sort' to sort the entries, and 'uniq' to include only
    unique entries. If omitted, no change log processing is done.
    Separate each tag with a comma.

Change-Id:
    This tag is used to generate the Message-Id of the emails that
    will be sent. When you keep the Change-Id the same you are
    asserting that this is a slightly different version (but logically
    the same patch) as other patches that have been sent out with the
    same Change-Id. The Change-Id tag line is removed from outgoing
    patches, unless the `keep_change_id` settings is set to `True`.

Various other tags are silently removed, like these Chrome OS and
Gerrit tags::

    BUG=...
    TEST=...
    Review URL:
    Reviewed-on:
    Commit-xxxx: (except Commit-notes)

``Co-developed-by:`` and ``Co-Authored-By:`` trailers with a ``noreply@``
address, as added by coding assistants, are removed too, unless the
``keep_assist_tags`` setting is ``True`` or ``--keep-assist-tags`` is given.
See :ref:`keeping these tags <keep_assist_tags>`.
