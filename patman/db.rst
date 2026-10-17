.. SPDX-License-Identifier: GPL-2.0+

Managing the database
=====================

Patman stores series tracking, review and workflow state in a SQLite
database (``.patman.db``) in the top-level git directory. The schema is
versioned and migrated automatically when patman starts. For what the
database records and the schema of each table, see
:doc:`The patman database </database>`.

Set the ``PATMAN_DB_DIR`` environment variable to use a database elsewhere
(``~`` is expanded). The variable names a *directory* in which patman looks
for ``.patman.db``, mirroring the default layout. This is useful if you keep
a single 'main' patman database in one repo but want to run, for example,
``patman review`` in another worktree or repo: the DB path is overridden but
git operations still run against the current repo. For instance::

    export PATMAN_DB_DIR=~/u-boot
    cd ~/some-other-tree
    patman review -s my-series

Reviews of other people's series can be kept in a separate database, so they
do not mix with your own series. Set ``review_db_dir`` in the ``[settings]``
section of ``~/.patman`` (or use ``--review-db-dir``) to the directory which
holds it::

    [settings]
    review_db_dir: ~/dev/u-boot-review

``patman review`` then stores its reviews there, while upstreams and their
patchwork servers are still read from the main database. ``patman serve``
shows the reviews from the same place.

To move existing reviews into the review database, or back again::

    patman database -n split    # show what would be moved
    patman database split       # move reviews to the review database
    patman database join        # move them back to the main database

Series can also be copied from another database, such as one from a
different machine, with ``patman database merge DIR`` (or the path of the
database file). Use ``--own`` or ``--reviews`` to copy just one kind of
series, and ``-R`` to copy into the review database. The other database is
not changed. Missing upstreams are added to the main database too.

Your own series and series you have reviewed are always kept apart: if a
series has the same name as one of the other kind, it is left alone and
reported. When a series is in both databases, its new versions are added,
but a version which is in both is left alone. When moving, a series which
cannot be moved completely (for example because it has a version which is
already in the other database) is left where it is, so nothing is lost.
Each database which is changed is first backed up to ``.patman.db.bak``
alongside it.

The connection is opened in WAL mode (``journal_mode=WAL``,
``synchronous=NORMAL``) with a 30-second busy timeout, which lets multiple
patman invocations share the database safely: concurrent readers do not block
each other, and a writer only briefly excludes other writers. This means it is
fine to run, for example, ``patman status`` in one terminal while a long
``patman review`` is running in another. WAL creates ``.patman.db-wal`` and
``.patman.db-shm`` sidecar files alongside the database; they are managed by
SQLite and removed on a clean close, so copy or sync all three together if you
back the database up by hand.
