.. SPDX-License-Identifier: GPL-2.0+

How patman is organised
=======================

This is a map of patman's source, for working on it. Each module also has a
docstring describing it, and :doc:`review-internals` goes into more detail on
the review code.

Running a command
-----------------

``patman`` runs ``patman/__main__.py``, which:

1. parses the command line with ``cmdline.parse_args()``. The defaults for
   each option come from the settings files, read by ``settings``, so that
   e.g. ``project`` in ``~/.patman`` changes the default for ``-p``
2. passes the arguments to ``control.do_patman()``, which calls the function
   for the command, e.g. ``do_series()`` for ``patman series``

The functions in ``control`` open the database and set up a connection to
patchwork where the command needs them, then call into the module which does
the work. ``patman test`` is handled in ``__main__.py`` itself.

The modules
-----------

Commands
~~~~~~~~

``cmdline``
    The argument parser, with an ``add_..._subparser()`` function for each
    command
``control``
    The function for each command, and showing the manual with ``-H``
``settings``
    Reading ``~/.patman`` and ``.patman`` files, and the email aliases

Patches and sending
~~~~~~~~~~~~~~~~~~~

``patchstream``
    Reads the commits of a branch from ``git log``, picks out the patman tags
    (``Series-to:``, ``Commit-notes:``, change logs and so on) and writes the
    patch files with the tags removed
``commit`` and ``series``
    A commit, with its tags, and a series of them, with its recipients and
    change logs
``checkpatch``
    Runs ``checkpatch.pl`` on the patches and reads what it reports
``get_maintainer``
    Finds who to copy on each patch
``send``
    ``patman send``: creates, checks and sends the patches, using the modules
    above
``relay``
    Sends patches through a web endpoint rather than by email
``project``
    Works out which project a tree holds, e.g. U-Boot or Linux

Patchwork
~~~~~~~~~

``patchwork``
    The ``Patchwork`` class, which reads from the patchwork REST API with
    aiohttp, so that many requests can be made at once. Tests give it a fake
    server with ``Patchwork.for_testing()``
``status``
    ``patman status``: collects the review tags and comments for each patch
    and adds the tags to the commits

The series database
~~~~~~~~~~~~~~~~~~~

``database``
    The ``Database`` class, which holds everything in ``.patman.db`` and
    migrates it to the latest schema (``LATEST``) when it is opened. See
    :doc:`database`
``cseries`` and ``cser_helper``
    ``patman series``: the ``Cseries`` class, with a method for each
    subcommand. ``CseriesHelper``, which it is based on, holds the code which
    the subcommands share
``workflow``
    The workflow records, such as when a series was sent and the todo list
``dbmerge``
    ``patman database``: copying and moving series between databases

``patman upstream`` and ``patman patchwork`` are also handled by ``Cseries``.

Review
~~~~~~

``review/``
    ``patman review``, split into modules for finding the series, applying
    it, the agents' prompts, the emails, Gmail drafts, replies and scanning
    for new versions. Its ``__init__.py`` lists the modules, and
    :doc:`review-internals` describes how the review works
``gmail``
    The Gmail API, used for the review drafts and for finding replies
``coverity``
    Runs Coverity on a series, for ``patman review --coverity``

The web server
~~~~~~~~~~~~~~

``server/``
    ``patman serve``:

    - ``app``: the ``Server`` class, with the aiohttp handlers for the JSON API
    - ``info``: the information which the page shows, read from the database
    - ``params``: checking requests and turning them into ``patman review``
      arguments
    - ``jobs``: running the jobs which the page starts, one at a time, and
      resuming them after a restart

    :doc:`serve-protocol` describes the API
``web/``
    The page itself: ``index.html``, ``app.js`` and ``app.css``, with no
    build step

Libraries
~~~~~~~~~

patman uses the U-Boot Python library (``u_boot_pylib``) for running
commands (``command``), git (``gitutil``), coloured output and capturing it
in tests (``terminal``) and messages at different levels (``tout``). It uses
pygit2 to read and change the git repo directly, and aiohttp for patchwork
and the web server.

To run Python again, e.g. for ``python -m patman`` in a child process, use
``interpreter.get_python()`` rather than ``sys.executable``. Python takes
``sys.executable`` from the name it is started with, which a wrapper may set
to its own: a Guix package of patman does this, so ``sys.executable`` is the
``patman`` script rather than Python.

The tests
---------

``patman test`` runs all the tests; give a name, e.g.
``patman test test_series_add``, to run just one. The tests live next to the
code:

``func_test``
    ``TestFunctional``: creating and sending patches, tags, aliases and
    ``patman status``
``test_checkpatch``
    ``TestPatch``: ``checkpatch.pl`` and reading its output
``test_cseries``
    ``TestCseries``: ``patman series``, e.g. adding, linking, archiving and
    renaming series
``test_status``
    ``TestSeriesStatus``: gathering the status of series and showing it
``test_upstream``
    ``TestUpstream``: ``patman upstream`` and ``patman patchwork``
``test_workflow``
    ``TestWorkflow``: ``patman workflow``
``test_database``
    ``TestDatabase``: ``patman database`` and migrating the schema
``test_review``
    ``TestReview``: ``patman review``, with the agent mocked
``test_send``
    ``TestSend``: sending, including through a relay
``test_server``
    ``TestWebServer``: the JSON API of ``patman serve``
``test_settings``
    ``TestSettings``: reading the settings files

``test_common`` holds what the tests share. ``TestCommon`` gives each test a
temporary directory, with ``make_git_tree()`` to create a git repo in it with
a few branches of commits, and keeps the tests away from the user's own
database. ``CseriesTester`` adds a series database, a fake patchwork server
and ``run_args()``, which runs a patman command as the user would and checks
its exit code.

Many of the ``patman series`` tests use a ``check_...()`` generator, which
sets things up and yields to the test, which then either calls the
``Cseries`` method or runs the command. This tests both ways of doing
something without writing the test twice.

No test talks to a real patchwork server, mail server or agent.

Adding a command
----------------

To add a command, e.g. ``patman thing``:

- add ``add_thing_subparser()`` to ``cmdline``, with help for each option,
  and call it from ``setup_parser()``. The command reference in
  ``doc/reference/`` is generated from this, so add a page there
- add ``do_thing()`` to ``control`` and call it from ``do_patman()``
- put the code itself in a module of its own, or in ``Cseries`` if it works
  on series in the database
- add tests in a test file of its own, based on ``CseriesTester`` if it
  needs a git tree and database, and add the test class to ``__main__.py``
- describe it in the manual (``patman/*.rst``, listed in
  ``control.HELP_FILES``) and, if it changes the schema, in
  :doc:`database`
- add an entry to ``CHANGELOG.rst``
