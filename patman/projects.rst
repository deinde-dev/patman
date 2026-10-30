.. SPDX-License-Identifier: GPL-2.0+

Projects
========

Patman works with any project which takes patches by email. These notes give
the setup for the projects it is most used with, with example settings. The
other chapters cover each step in full.

Settings go in ``~/.patman``, or in a ``.patman`` at the top of the tree for
that tree alone (see :doc:`the Configuration chapter </configuration>`). In
``~/.patman``, a ``[<project>_settings]`` or ``[<project>_alias]`` section
applies only to that project, e.g. ``[linux_settings]``.

U-Boot
------

Patman's defaults suit U-Boot, so there is little to set up (see
:doc:`the guide </guide/index>`, which uses U-Boot throughout):

- It finds ``scripts/checkpatch.pl`` to check the patches and
  ``scripts/get_maintainer.pl`` to find who to copy.
- U-Boot's aliases are in ``doc/git-mailrc``. Run
  ``git config sendemail.aliasesfile doc/git-mailrc`` once in each clone, so
  that ``Series-to: u-boot`` sends to the mailing list.
- The tags at the start of each subject, such as ``dm:``, are looked up as
  aliases and copied. Use ``--no-tags`` to stop this.
- The series are on https://patchwork.ozlabs.org, in the project 'U-Boot'::

    patman upstream add us https://git.u-boot-project.org/u-boot/u-boot \
        U-Boot -p https://patchwork.ozlabs.org -t u-boot

- Reviews use U-Boot's conventions by default, so no ``[review]`` section is
  needed.

Example settings in ``~/.patman``:

.. code-block:: ini

    [alias]
    me: Your Name <you@example.com>
    heinrich: Heinrich Schuchardt <xypron.glpk@gmx.de>

    [u-boot_settings]
    # Send through the kernel.org web relay rather than git send-email
    send_endpoint_web: https://lkml.kernel.org/_b4_submit

    [settings]
    # Keep the series you review apart from your own
    review_db_dir: ~/dev/u-boot-review
    signoff: Regards,\nYour Name

Linux
-----

Patman detects a Linux tree from its ``kernel/`` directory and changes
some defaults to suit it:

- It runs Linux's ``scripts/checkpatch.pl`` without ``--no-tree``, so that it
  checks against the files in the tree, and ``scripts/get_maintainer.pl``,
  which finds the lists and maintainers to copy for each patch.
- The tags at the start of each subject, such as ``arm64:``, are not looked
  up as aliases. To look them up, set ``process_tags: True`` in the
  ``[linux_settings]`` section, with an alias for each tag.
- Each subsystem has its own project on https://patchwork.kernel.org, given
  by its name there::

    patman upstream add next \
        https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git \
        -p https://patchwork.kernel.org 'Rockchip SoC list'

- The kernel.org web relay can send the patches if your mail server does not
  suit ``git send-email`` (see :doc:`the Sending chapter </sending>`).
- For reviews, set ``project: Linux`` in the ``[review]`` section, with a
  file of the conventions to check (see :ref:`review_project`). For
  ``patman serve``, an upstream with the whole of lore and ``--no-feed`` lists
  just the series you add (see :ref:`adding a series <serve_search>`).

Example settings in ``~/.patman``:

.. code-block:: ini

    [linux_alias]
    # For use in Series-to: and Series-cc: tags
    rockchip: linux-rockchip@lists.infradead.org

    [linux_settings]
    # Send through the kernel.org web relay rather than git send-email
    send_endpoint_web: https://lkml.kernel.org/_b4_submit

and in the tree's ``.patman``:

.. code-block:: ini

    [settings]
    review_db_dir: ~/dev/linux-review

    [review]
    project: Linux
    conventions: .patman-review.md

dtc
---

The Devicetree Compiler has a mailing list,
``devicetree-compiler@vger.kernel.org``, but no patchwork project, so patman
reads its series from https://lore.kernel.org/devicetree-compiler:

- Add an alias for the list to ``~/.patman`` and use ``Series-to: dtc``.
- dtc has no ``checkpatch.pl``, so put one in ``~/bin``, e.g. a copy of
  Linux's, or turn the check off with ``--no-check`` or with
  ``check_patch: False`` in the ``[settings]`` of the tree's ``.patman``.
  There is no ``get_maintainer.pl`` either, so patman copies just the people
  in the tags.
- ``patman series send`` and ``autolink`` find each version on lore, from the
  list which the series is sent to, so the upstream needs no URL (see
  :ref:`series_lore`). To review others' series, give the upstream the list
  on lore (see :ref:`review_lore`)::

    patman upstream add dtc https://github.com/dgibson/dtc \
        -l https://lore.kernel.org/devicetree-compiler

- Lore has no patch states, but dtc's maintainers add the Message-ID of each
  patch they apply as a trailer, so ``gather`` and ``gather-all`` mark a
  patch 'accepted' once it is in the upstream's main branch, and
  ``series progress`` shows it (see :ref:`series_lore`). Fetch the upstream
  first.
- For reviews, set ``project: dtc`` in the ``[review]`` section. Run
  ``patman serve`` in the dtc tree to list the newest series sent to the list
  (see :ref:`a list on lore <serve_lore>`).
- A maintainer can apply series from the list by dtc's rules with
  ``patman maint apply``, check the commits to be pushed with
  ``patman maint check``, test them with ``patman maint test``, run CI on
  them with ``patman maint ci`` and push them with ``patman maint publish``.
  The chapter has dtc's ``[maint]`` section as its example (see
  :ref:`maint`).

Patman does not detect a dtc tree, so there is no ``[dtc_settings]``
section; put dtc's settings in the tree's ``.patman``. Example settings in
``~/.patman``:

.. code-block:: ini

    [alias]
    dtc: Devicetree Compiler <devicetree-compiler@vger.kernel.org>
    david: David Gibson <david@gibson.dropbear.id.au>

and in the tree's ``.patman``:

.. code-block:: ini

    [settings]
    # dtc has no checkpatch.pl
    check_patch: False
    review_db_dir: ~/dev/dtc-review

    [review]
    project: dtc
    conventions: .patman-review.md

dtc could instead ship ``check_patch: False`` and the ``[review]`` section in
a tracked ``.patman-defaults``, with its conventions file beside it, so that
everyone working in the tree gets them; ``review_db_dir`` is your own choice,
so it stays in your ``.patman``. Patman reads ``.patman-defaults`` first, so
``~/.patman`` and the tree's ``.patman`` can still override each setting (see
:doc:`the Configuration chapter </configuration>`).
