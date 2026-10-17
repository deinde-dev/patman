.. SPDX-License-Identifier: GPL-2.0+

Configuration
=============

During the first run patman creates a config file for you by taking the default
user name and email address from the global .gitconfig file.

If your project ships a git mail-alias file (U-Boot, for example,
provides 'doc/git-mailrc'), patman can use it to supply the email
aliases you need. To make this work, tell git where to find the file
by typing this once::

    git config sendemail.aliasesfile doc/git-mailrc

Config files
------------

Config is read from three files, each overriding the previous one on a
per-key basis (lowest to highest priority):

1. ``.patman-defaults`` at the root of the git repository. This is
   tracked in git and shipped by the project to provide sensible
   defaults (such as the patchwork URL or the get_maintainer.pl
   invocation). Because it is the lowest layer, anyone can override it,
   so it never takes control away from the user. In a tree whose
   ``.gitignore`` ignores dot-files (as U-Boot's does), force-add it
   with ``git add -f .patman-defaults``.
2. ``~/.patman``, the user's global config.
3. ``.patman`` at the root of the git repository, the user's per-repo
   override (not tracked).

The values of the project's ``.patman`` take precedence over those of the
global ``~/.patman``, which makes it possible to override the `project`
settings variable or anything else in a project-specific way. Command-line
arguments override all three. A project shipping ``.patman-defaults`` might
contain::

    [settings]
    patchwork_url: https://patchwork.ozlabs.org
    get_maintainer_script: scripts/get_maintainer.pl --norolestats

A user who sets ``patchwork_url`` in their own ``~/.patman`` (or passes
``-P``) overrides it; anything they leave unset falls back to these
defaults.

Aliases
-------

To add your own aliases, add an [alias] section to `~/.patman` like this::

    # patman alias file

    [alias]
    me: Simon Glass <sjg@chromium.org>

    u-boot: U-Boot Mailing List <u-boot@lists.denx.de>
    wolfgang: Wolfgang Denk <wd@denx.de>
    others: Mike Frysinger <vapier@gentoo.org>, Fred Bloggs <f.bloggs@napier.net>

Aliases are recursive.

If you want to avoid sending patches to email addresses that are picked up
by patman but are known to bounce you can add a [bounces] section to your
.patman file. Unlike the [alias] section these are simple key: value pairs
that are not recursive::

    [bounces]
    gonefishing: Fred Bloggs <f.bloggs@napier.net>

Settings
--------

If you want to change the defaults for patman's command-line arguments,
you can add a [settings] section to your .patman file.  This can be used
for any command line option by referring to the "dest" for the option in
patman.py.  For reference, the useful ones (at the moment) shown below
(all with the non-default setting)::

    [settings]
    ignore_errors: True
    process_tags: False
    verbose: True
    smtp_server: /path/to/sendmail
    patchwork_url: https://patchwork.ozlabs.org
    patchwork_timeout: 60

``patchwork_timeout`` is the number of seconds to wait for each request to
patchwork before trying again (default 30). A busy server can stall a
request, so patman retries a request which times out, has its connection
reset or refused, or gets a 502, 503 or 504 status, up to three times.

By default patman adds ``base-commit:`` and ``branch:`` trailers to the
generated patches (or the cover letter). These record the commit that the
series applies on top of. If you work on a local or downstream branch whose
base commit does not exist in the upstream tree, these trailers are
misleading, so you can suppress them with ``--no-base-commit`` on the command
line, or permanently via::

    [settings]
    insert_base_commit: False

If you want to adjust settings (or aliases) that affect just a single
project you can add a section that looks like [project_settings] or
[project_alias].  If you want to use tags for your linux work, you could do::

    [linux_settings]
    process_tags: True

Checkpatch and get_maintainer
-----------------------------

If the project provides a checkpatch.pl (in its 'scripts/' or 'tools/'
directory, as U-Boot and Linux do) patman will locate and use it.
Failing that you can put it into your path or ~/bin/checkpatch.pl

Projects in the U-Boot and Linux kernel style provide a
'scripts/get_maintainer.pl' that works out where to send patches. For
other projects, you may want to specify a different script to be run,
for example via a project-specific `.patman` file::

    # .patman configuration file at the root of some project

    [settings]
    get_maintainer_script: etc/teams.scm get-maintainer

The `get_maintainer_script` option corresponds to the
`--get-maintainer-script` argument of the `send` command.  It is
looked relatively to the root of the current git repository, as well
as on PATH.  It can also be provided arguments, as shown above.  The
contract is that the script should accept a patch file name and return
a list of email addresses, one per line, like `get_maintainer.pl`
does.
