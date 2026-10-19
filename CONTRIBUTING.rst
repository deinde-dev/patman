Contributing to Patman
======================

Development setup
-----------------

Work in a virtual environment::

    python -m venv .venv
    . .venv/bin/activate
    pip install -r requirements.txt
    pip install -e .[test]

Running the tests
-----------------

::

    patman test                 # the whole suite
    patman test <name>          # a single test, e.g. test_series_send

The suite can also be run with ``pytest``.

The suite is self-contained: a pinned ``checkpatch.pl`` and its helper
files live in ``scripts/`` and ``u_boot_pylib`` is installed from PyPI as
``u-boot-pylib`` (https://github.com/deinde-dev/u-boot-pylib), so no
surrounding U-Boot source is needed. ``checkpatch.pl`` does need ``perl``
to be installed.

The tests which run ``checkpatch.pl`` use the copy in ``scripts/`` with the
options in ``.checkpatch.conf``, wherever the suite is run from. Since
neither is installed with patman, these tests are skipped when patman's
sources are not present, e.g. when running ``patman test`` from an
installed copy.

Building the documentation
--------------------------

::

    pip install -r doc/requirements.txt
    make -C doc html        # output in doc/_build/html

The manual lives in ``patman/``: the introduction in ``patman.rst``, then
one file for each chapter, such as ``sending.rst`` and ``tags.rst``.
``control.HELP_FILES`` lists them in order. ``patman -H`` shows them all, and
``doc/`` wraps them for Sphinx, so edit the manual there. To link to another
page, use a :doc: link with its own text, such as
``:doc:`the Tags chapter </tags>```, since ``patman -H`` shows just the text.

The command reference in ``doc/reference/`` is generated from the argument
parser by ``doc/_ext/patman_cmdref.py``, so give each new command and option
a help string. ``doc/guide/`` holds the step-by-step guide, with output from
real series.

The technical documentation is in ``doc/`` too. ``doc/internals.rst`` is a
map of the source and says how to add a command. ``doc/database.rst``
describes the database schema; update it along with any schema migration.
``doc/serve-protocol.rst`` describes the JSON API provided by
``patman serve``; update it along with any change to the API.
``doc/review-internals.rst`` describes how the review works.

``doc/demo/record.sh`` records the short video of patman in a terminal which
is shown at https://deinde.dev/patman/. Run it again after a change to the
output which the video shows.

``doc/demo/record_serve.py`` records a video of the ``patman serve`` page:
reviewing a series from the inbox, reading and changing a review and sending
the reviews. It runs ``doc/demo/serve_demo.py``, which serves made-up series
and fakes the jobs, so nothing is reviewed or sent for real; this can also be
run on its own to try the page. Recording needs Playwright, Google Chrome,
Xvfb and ffmpeg.

``doc/demo/screenshots.py`` takes the pictures of the page in the guide
(``doc/guide/images``) in the same way. Run it again after a change to the
page which they show, and check the pictures before committing them. It
needs Playwright and Google Chrome, and Pillow to make the pictures
smaller.

Building the package
--------------------

::

    python -m build
    twine check --strict dist/*

Building the snap
-----------------

``snap/snapcraft.yaml`` packages patman as a snap with classic confinement,
since patman works on your own git trees and runs your git, git send-email
and editor. Build it with::

    snapcraft pack

which uses LXD to build it in a clean Ubuntu container. Install the result
for testing with::

    sudo snap install --dangerous --classic patman_*.snap

The snap takes git, perl (for ``checkpatch.pl``) and Claude Code (for
``patman review``) from the host. It leaves out the copy of Claude Code which
the Claude Agent SDK bundles, and the Google API client's descriptions of
APIs other than Gmail's, which would make it considerably larger.

Continuous integration
----------------------

The *Tests* workflow runs the suite on Python 3.10-3.12 and builds and
checks the package on every push and pull request.

Making a release
----------------

Releases are published to PyPI automatically by the *Release* workflow
when a version tag is pushed. The flow is:

1. Update ``CHANGELOG.rst``: move the ``Unreleased`` entries under a new
   ``X.Y.Z - <date>`` heading.
2. Bump ``version`` in ``pyproject.toml``.
3. Commit the changes.
4. Tag it. The tag must be ``v`` followed by the exact version, for
   example::

       git tag v0.0.8
       git push origin v0.0.8

   A tag containing ``rc`` (for example ``v0.0.8rc1``) publishes to
   TestPyPI; a final tag publishes to the real PyPI. The workflow
   refuses to publish if the tag does not match the project version.

   You can also trigger the *Release* workflow manually (the
   ``workflow_dispatch`` option) to publish the current branch to
   TestPyPI for a dry run, without a tag.

Publishing uses PyPI Trusted Publishing (OIDC), so no API tokens are
stored. The PyPI and TestPyPI projects must each be configured to trust
this repository's ``release.yml`` workflow (environments ``pypi`` and
``testpypi``) before the first release.
