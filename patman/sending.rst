.. SPDX-License-Identifier: GPL-2.0+

Sending a series
================

``patman send`` creates the patches from your branch, checks them and sends
them. :doc:`The guide </guide/index>` goes through this with a real series,
from the first version to the last, with the output at each step.

How to run it
-------------

First do a dry run:

.. code-block:: bash

    patman send -n

If it can't detect the upstream branch, try telling it how many patches
there are in your series

.. code-block:: bash

    patman -c5 send -n

This will create patch files in your current directory and tell you who
it is thinking of sending them to. Take a look at the patch files, and when
they look right, run it again without ``-n`` to send them.

Leaving out commits
    If the branch has commits which should not be sent, such as a
    work-in-progress commit at the top for testing, use ``-s`` to skip that
    many commits from the top, or ``-e`` to skip commits at the bottom, e.g.

    .. code-block:: bash

        patman -c5 -s1 send -n

    takes the five commits below the top one. Tags in a skipped commit are
    not seen, so keep them in a commit which is sent.

Fixing problems
    If the dry run reports checkpatch errors or warnings, fix the commit,
    e.g. with 'git rebase -i' and 'edit', then run the dry run again until it
    is clean. Use ``-i`` to send in spite of the errors, or ``--no-check`` to
    skip checkpatch.

Dropping commits
    When a commit is no longer needed, e.g. because the patch it depends on
    has been merged, drop it with 'git rebase -i' before sending the next
    version.

Where patches are sent
----------------------

Once the patches are created, patman sends them using git send-email. The
whole series is sent to the recipients in Series-to: and Series-cc.
You can Cc individual patches to other people with the Patch-cc: tag. Tags
in the subject are also picked up to Cc patches. For example, a commit like
this::

    commit 10212537b85ff9b6e09c82045127522c0f0db981
    Author: Mike Frysinger <vapier@gentoo.org>
    Date:    Mon Nov 7 23:18:44 2011 -0500

    x86: arm: add a git mailrc file for maintainers

    This should make sending out e-mails to the right people easier.

    Patch-cc: sandbox, mikef, ag
    Patch-cc: afleming

will create a patch which is copied to x86, arm, sandbox, mikef, ag and
afleming.

If you have a cover letter it will get sent to the union of the Patch-cc
lists of all of the other patches. If you want to sent it to additional
people you can add a tag::

    Cover-letter-cc: <list of addresses>

These people will get the cover letter even if they are not on the To/Cc
list for any of the patches.

In Linux and U-Boot patman also runs get_maintainer.pl on each patch and
copies it to the people it finds, unless you use -m to disable this.

Sending through a web relay
---------------------------

If you have no working outbound email (SMTP blocked, or a provider that
mangles patches), patman can post the series to a web submission
endpoint that sends the mail for you, instead of using ``git
send-email``. This is the same mechanism b4 uses, and works with the
kernel.org endpoint for kernel.org-hosted projects such as U-Boot.

Setting it up
~~~~~~~~~~~~~

The guide goes through the setup step by step, with the output to expect:
see 'Sending through the kernel.org web relay' in
:doc:`Setting up patman </guide/getting-started>`. In brief:

- give patatt, which signs the messages, a signing key, e.g.
  ``git config --global patatt.signingkey openpgp:<your-key-id>``
- set ``send_endpoint_web: https://lkml.kernel.org/_b4_submit`` in the
  ``[u-boot_settings]`` section of ``~/.patman``, or pass
  ``--send-endpoint-web <url>`` for a single run
- register your key and email address once, with
  ``patman send --web-auth-new`` and then
  ``patman send --web-auth-verify <challenge>``
- check it with ``patman send --reflect``, which has the endpoint mail the
  series back to you only

Notes
~~~~~

With the endpoint set, ``patman send`` builds an email per patch (with
the usual To/Cc), signs each with patatt and posts them. The endpoint
authenticates each submission by its patatt signature -- there is no
password or token stored.

Once the endpoint is configured it is used for every ``patman send`` in
that project. Pass ``--no-relay`` to send with ``git send-email`` for a
single run.

Threading (``--thread``) and ``--in-reply-to`` work with the relay just
as with ``git send-email``: the patches reply to the cover letter, which
can in turn reply to an earlier message.

Tips
----

#. Everything about a series, such as its version, who it goes to and its
   change logs, is kept in the commits. When you come back to a branch weeks
   later, there is nothing to remember.
#. Add the change log to a commit as you change it, rather than later when
   you cannot remember what changed. You can always go back and edit it.
#. To keep each version as sent, so you can compare them later, use a new
   branch for each version (as ``patman series inc`` does), or tag the
   branch before changing it, e.g. ``git tag sent/video-v2``.
#. You can change the patches in your editor before ``git send-email`` sends
   them, but be careful.
#. To run ``git send-email`` yourself, use ``-n``, which prints the command
   patman would have run.
#. Some mailing lists limit the size of a message, which binary content
   easily exceeds. Use ``--no-binary`` to leave it out of the patches, and
   give a link to a git repository in ``Commit-notes``, ``Series-notes`` or
   the cover letter so the maintainer can fetch the commit.
#. A patch which did not change in the latest version gets a note saying
   so. For example, a commit with::

        Series-version: 5
        Series-changes: 2
        - Some change

        Series-changes: 4
        - Another change

   has the change log::

        (no changes since v4)

        Changes in v4:
        - Another change

        Changes in v2:
        - Some change
