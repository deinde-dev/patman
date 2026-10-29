.. SPDX-License-Identifier: GPL-2.0+

Command reference
=================

This lists every patman command and its options, taken from patman's own
argument parser, so it always matches the code. ``patman <command> -h``
shows the same for one command.

For how the commands fit together, see the :doc:`/guide/index`. Options can
also be set in ``~/.patman``; see :doc:`/configuration`.

The commands are:

.. patman-command::
   :commands:

.. toctree::
   :hidden:

   send
   status
   series
   patchwork
   upstream
   review
   serve
   maint
   database
   workflow

Options for all commands
------------------------

These come before the command, e.g. ``patman -v send``.

.. patman-command::
