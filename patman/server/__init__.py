# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Serve series status and reviews over HTTP for viewing on a phone

'patman serve' runs a small web server in the git tree. It provides a JSON
API over the patman database plus a mobile-friendly page which uses it.
Comments on patches and cover letters are not stored in the database, so
these are read live from patchwork when asked for.

The server can optionally keep the database up to date by running
'patman series gather-all -G' (and optionally 'patman review --sync')
periodically in a child process. The -G flag matters: without it,
gather-all writes review tags into the local branches, which must never
happen behind the user's back.

The JSON API is described in doc/serve-protocol.rst

Jobs (syncs and reviews) run one at a time, in the order they are started.

This package's modules are:

  app: the web server, with the page and its JSON API
  params: checking requests and turning them into patman arguments
  info: the information which the page shows, from the database
  jobs: running the jobs which the server starts, one at a time

Each module is imported here, so code outside the package can use, for
example, server.app.is_loopback() after 'from patman import server'
"""

from patman.server import app
from patman.server import info
from patman.server import jobs
from patman.server import params
from patman.server.app import Server, serve
