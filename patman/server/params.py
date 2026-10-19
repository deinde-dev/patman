# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Check the parameters of requests and turn them into patman arguments
"""

import re


class BadRequest(ValueError):
    """A request is not valid, e.g. a missing field or a bad value"""


# Patch selection for 'review -i', e.g. '3' or '1,3-5'
RE_INDEX = re.compile(r'^\d+(-\d+)?(,\d+(-\d+)?)*$')

# Patchwork series or patch in a URL, e.g. '.../series/123/', '?series=123'
RE_SERIES_URL = re.compile(r'series[=/](\d+)')
RE_PATCH_URL = re.compile(r'/patch/(\d+)')


def get_int(data, key):
    """Get an optional positive integer from a request

    Args:
        data (dict): Request data
        key (str): Key to read

    Return:
        int or None: Value, or None if missing

    Raises:
        ValueError: The value is not a positive integer
    """
    val = data.get(key)
    if val in (None, ''):
        return None
    if isinstance(val, bool) or not str(val).isdigit() or not int(val):
        raise BadRequest(f"Invalid {key} '{val}'")
    return int(val)


def parts_text(seqs):
    """Describe which parts of a review a job acts on

    Args:
        seqs (list of int): Patches (0 for the cover letter), or empty for all

    Return:
        str: Description, e.g. 'patch 2', 'the cover letter' or 'reviews'
    """
    if len(seqs) != 1:
        return 'reviews'
    return f'patch {seqs[0]}' if seqs[0] else 'the cover letter'


def parse_review_req(data, upstreams):
    """Work out the 'patman review' arguments for a review request

    The request is a dict with these keys, all optional except that one of
    'target' and 'title' is needed:
        target (str): Patchwork series ID, or the URL of a series or patch
        title (str): Search for a series by its cover-letter title
        version (int): Version to review, with 'title'
        patch (int): Patchwork patch ID, to review just that patch
        whole (bool): With a patch, review its whole series
        index (str): Review only these patches, e.g. '1,3-5'
        ups (str): Upstream whose patchwork server to use
        drafts (bool): Create Gmail drafts for the reviews
        force (bool): Review again, even if already reviewed
        context (str): Notes for the review agent, e.g. 'this is an RFC'

    Args:
        data (dict): Request
        upstreams (list of str): Names of the known upstreams

    Return:
        tuple:
            list of str: Arguments for 'patman review'
            str: Description of the review, for display

    Raises:
        ValueError: The request is not valid
    """
    target = str(data.get('target') or '').strip()
    title = str(data.get('title') or '').strip()
    patch = get_int(data, 'patch')
    args = []
    if target:
        mpatch = RE_PATCH_URL.search(target)
        mseries = RE_SERIES_URL.search(target)
        if target.isdigit():
            args += ['-s', target]
            desc = f'series {target}'
        elif mpatch:
            patch = int(mpatch.group(1))
        elif mseries:
            args += ['-s', mseries.group(1)]
            desc = f'series {mseries.group(1)}'
        else:
            raise BadRequest(f"Cannot find a series or patch in '{target}'")
    if patch:
        args += ['-p', str(patch)]
        desc = f'patch {patch}'
        if data.get('whole'):
            args.append('-w')
            desc = f'series of patch {patch}'
    if title:
        args += ['-S', title]
        desc = f"'{title}'"
        version = get_int(data, 'version')
        if version:
            args += ['-V', str(version)]
    if not args:
        raise BadRequest('Please give a series, patch or title to review')
    if len([1 for opt in ('-s', '-p', '-S') if opt in args]) != 1:
        raise BadRequest('Please give only one of a series, patch or title')
    index = str(data.get('index') or '').replace(' ', '')
    if index:
        if not RE_INDEX.match(index):
            raise BadRequest(f"Invalid patch selection '{index}'")
        args += ['-i', index]
    ups = data.get('ups')
    if ups:
        if ups not in upstreams:
            raise BadRequest(f"Unknown upstream '{ups}'")
        args += ['-U', ups]
    if data.get('drafts'):
        args.append('-d')
    if data.get('force'):
        args.append('-f')
    context = data.get('context')
    if context is not None and not isinstance(context, str):
        raise BadRequest('Invalid context')
    if (context or '').strip():
        args += ['-c', context.strip()]
    return args, f'Review {desc}'
