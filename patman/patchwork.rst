.. SPDX-License-Identifier: GPL-2.0+

Patchwork integration
=====================

If you point patman to your series on patchwork it can show you what new
reviews have appeared since you sent your series.

To set this up, add a Series-links tag to one of the commits in your series
(see the 'Tags' chapter).

Then you can type:

.. code-block:: bash

    patman status

and patman will show you each patch and what review tags have been collected,
for example::

    ...
     21 x86: mtrr: Update the command to use the new mtrr
        Reviewed-by: Wolfgang Wallner <wolfgang.wallner@br-automation.com>
      + Reviewed-by: Bin Meng <bmeng.cn@gmail.com>
     22 x86: mtrr: Restructure so command execution is in
        Reviewed-by: Wolfgang Wallner <wolfgang.wallner@br-automation.com>
      + Reviewed-by: Bin Meng <bmeng.cn@gmail.com>
    ...

This shows that patch 21 and 22 were sent out with one review but have since
attracted another review each. If the series needs changes, you can update
these commits with the new review tag before sending the next version of the
series.

To automatically pull into these tags into a new branch, use the -d option:

.. code-block:: bash

    patman status -d mtrr4

This will create a new 'mtrr4' branch which is the same as your current branch
but has the new review tags in it. The tags are added in alphabetic order and
are placed immediately after any existing ack/review/test/fixes tags, or at the
end. The new branch does not track an upstream branch, so set one with
'git branch --set-upstream-to' before sending from it.

There is also a -c option to list the comments received for each patch, and a
-C option to list those on the cover letter.

For a worked example using real series, see ``doc/guide/status-and-tags.rst``.

If you have several series in flight, 'patman series' keeps track of them for
you in a database: each version, its patchwork link and the state of each
patch. It can find the links, gather the review tags, start each new version
and archive a series once merged. The 'Series Management' chapter describes
this.
