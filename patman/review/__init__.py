# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2025 Canonical Ltd.
# Written by Simon Glass <simon.glass@canonical.com>
#

"""AI-powered patch review using Claude

This package provides 'patman review' and 'patman series review'. Its
modules are:

  core: the main flow, which finds, applies, reviews and stores a series
  common: the review context and helpers which all the modules use
  lookup: finding and reading the series on patchwork
  apply: applying the series to a local branch
  prompts: the prompts given to the agents
  emails: parsing the agents' reviews and formatting them as emails
  drafts: Gmail drafts of the reviews, and syncing what was sent
  replies: replies to reviews, and responding to them
  stored: sending and revising reviews which are already stored
  scan: scanning for and reviewing new versions of a series
  voice: learning the reviewer's writing voice

Each module is imported here, so code outside the package can use, for
example, review.drafts.create_drafts() after 'from patman import review'
"""

from patman.review import apply
from patman.review import common
from patman.review import core
from patman.review import drafts
from patman.review import emails
from patman.review import lookup
from patman.review import prompts
from patman.review import replies
from patman.review import scan
from patman.review import stored
from patman.review import voice
from patman.review.core import do_review, review_series
