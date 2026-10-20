.. image:: https://raw.githubusercontent.com/deinde-dev/patman/master/logo/patman-logo-primary.png
   :alt: Patman
   :width: 400px

Patman patch manager
====================

.. image:: https://github.com/deinde-dev/patman/actions/workflows/test.yml/badge.svg
   :target: https://github.com/deinde-dev/patman/actions/workflows/test.yml
   :alt: Test status

.. image:: https://img.shields.io/pypi/v/patch-manager.svg
   :target: https://pypi.org/project/patch-manager/
   :alt: PyPI version

.. image:: https://readthedocs.org/projects/patman/badge/?version=latest
   :target: https://patman.readthedocs.io/en/latest/
   :alt: Documentation status

.. image:: https://img.shields.io/pypi/pyversions/patch-manager.svg
   :target: https://pypi.org/project/patch-manager/
   :alt: Supported Python versions

Patman is a command-line tool that automates the boring parts of preparing
and sending patches by email, in the style used by the U-Boot and Linux
kernel projects. It:

- creates patches directly from a git branch;
- cleans them up by removing unwanted tags and inserting a cover letter;
- runs checkpatch.pl and the project's checks before anything is sent;
- looks up the maintainers to Cc, and emails the series out;
- tracks series in a local database and integrates with Patchwork to gather
  review tags and follow up on comments.

Installation
------------

Install the latest release from PyPI::

    pip install patch-manager

Optional features are available as extras::

    pip install patch-manager[review]   # patch review using a Claude agent
    pip install patch-manager[gmail]    # Gmail drafts of reviews and replies
    pip install patch-manager[all]      # everything

To try changes before they are released, install the latest code from
GitHub instead, with any extras inside the brackets::

    pip install "patch-manager[all] @ git+https://github.com/deinde-dev/patman.git"

The version stays the same between releases, so to pick up newer changes
later, add ``--force-reinstall --no-deps``, which reinstalls patman without
reinstalling the packages it uses::

    pip install --force-reinstall --no-deps \
        "patch-manager @ git+https://github.com/deinde-dev/patman.git"

Some distributions, such as recent Debian and Ubuntu, do not let pip install
into the system's Python, failing with 'externally-managed-environment'. There,
install patman with pipx, which gives it its own environment::

    pipx install "patch-manager[all]"         # the latest release
    pipx install "patch-manager[all] @ git+https://github.com/deinde-dev/patman.git"

To update it, use ``pipx upgrade patch-manager`` for a release, or
``pipx reinstall patch-manager`` for the latest code from GitHub.

The ``patman`` command is then on your path. It is also installed as
``patch-manager``, which is the only name the Debian package provides, since
Debian already has an unrelated ``patman`` command.

Quick start
-----------

From a git branch containing the commits you want to send::

    patman              # create, check and email the series
    patman -n           # dry run: create and check, but do not send

See ``patman --full-help`` for the complete manual, or the documentation
linked below.

Documentation
-------------

The documentation is on Read the Docs, at https://patman.readthedocs.io/
Start with the guide, which goes through using patman step by step with a
real U-Boot series:

    https://patman.readthedocs.io/en/latest/guide/index.html

There is a short introduction, with a demo, at https://deinde.dev/patman/

Development
-----------

Run the test suite from a checkout::

    pip install -r requirements.txt
    pip install -e .
    patman test

The suite is self-contained: a pinned ``checkpatch.pl`` and its helper files
live in ``scripts/``, and ``u_boot_pylib`` is installed from PyPI as
``u-boot-pylib``, so no surrounding U-Boot source is required.

Build the documentation locally with::

    pip install -r doc/requirements.txt
    make -C doc html

The rendered HTML is written to ``doc/_build/html``.

License
-------

GPL-2.0-or-later. See the ``LICENSE`` file for the full text.
