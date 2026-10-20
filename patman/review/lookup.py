# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2025 Canonical Ltd.
# Written by Simon Glass <simon.glass@canonical.com>
#

"""Finding and reading the series to review on patchwork

This looks up a series by its link, its title or one of its patches, reads
the series, its cover letter, the state of its patches and the comments it
has received, and works out the name under which a review is stored.
"""

import os
import tempfile

import aiohttp

from u_boot_pylib import tools
from u_boot_pylib import tout

from patman.review import common


def _format_comment(what, comment):
    """Format a patchwork comment for the agent to read

    Args:
        what (str): What the comment is on, e.g. 'cover letter'
        comment (dict): Comment from patchwork

    Returns:
        str: Comment, with a heading and its sender and date
    """
    sub = comment.get('submitter', {})
    return (f"=== Comment on {what} ===\n"
            f"From: {sub.get('name', '')} <{sub.get('email', '')}>\n"
            f"Date: {comment.get('date', '')}\n\n"
            f"{comment.get('content', '')}\n")


def write_comments_file(series_data, pwork):
    """Fetch and write existing patchwork comments to a temp file

    Args:
        series_data (dict): Series data from patchwork get_series()
        pwork: Patchwork instance

    Returns:
        str or None: Path to the comments file, or None if no comments
    """
    patches = series_data.get('patches', [])
    cover = series_data.get('cover_letter')

    async def _fetch_comments():
        all_comments = []
        async with aiohttp.ClientSession() as client:
            if cover:
                for comment in await pwork.get_cover_comments(
                        client, cover['id']):
                    all_comments.append(
                        _format_comment('cover letter', comment))

            for i, patch in enumerate(patches):
                for comment in await pwork.get_patch_comments(
                        client, str(patch['id'])):
                    all_comments.append(_format_comment(
                        f"patch {i + 1}: {patch.get('name', '')}", comment))

        return all_comments

    comments = common.run(_fetch_comments())

    if not comments:
        return None

    comments_path = os.path.join(tempfile.gettempdir(),
                                 'patman_review_comments.txt')
    tools.write_file(comments_path, '\n'.join(comments), binary=False)

    tout.notice(f'Found {len(comments)} existing comment(s)')
    return comments_path


def search_patch(pwork, title):
    """Search patchwork for a patch by title and return its series and index

    Queries the patchwork patches API by title, picks the most recent
    match, then looks up its series.

    Args:
        pwork (Patchwork): Configured patchwork instance
        title (str): Patch title text to search for

    Returns:
        tuple: (series_link, patch_seq)

    Raises:
        ValueError: if no matching patch is found
    """
    from urllib.parse import quote_plus

    results = common.pw_call(pwork.search_patches, quote_plus(title, safe=':'))

    if not results:
        raise ValueError(f"No patch found matching '{title}'")

    if len(results) > 1:
        tout.notice(f"Found {len(results)} matching patches:")
        for i, p in enumerate(results[:10]):
            tout.notice(f"  {i + 1}. [{p['id']}] {p['name']}")

    best = results[0]
    patch_id = best['id']
    tout.notice(f"Using: [{patch_id}] {best['name']}")
    return lookup_patch_series(pwork, patch_id)


def lookup_patch_series(pwork, patch_id):
    """Look up a patch on patchwork and return its series link and position

    Args:
        pwork (Patchwork): Configured patchwork instance
        patch_id (int): Patchwork patch ID

    Returns:
        tuple: (series_link, patch_seq) where series_link is the series
            ID as a string and patch_seq is the 1-based position

    Raises:
        ValueError: if the patch or its series cannot be found
    """
    data = common.pw_call(pwork.get_patch, patch_id)

    series_list = data.get('series', [])
    if not series_list:
        raise ValueError(f'Patch {patch_id} has no associated series')

    series_link = str(series_list[0]['id'])
    patch_name = data.get('name', '')
    tout.notice(f"Patch {patch_id}: '{patch_name}'")
    tout.notice(f"Series: {series_list[0].get('name', '')} "
                f"(link {series_link})")

    # Fetch the series to find the patch position
    series_data = fetch_series(pwork, series_link)[0]
    patches = series_data.get('patches', [])
    for i, patch in enumerate(patches):
        if patch.get('id') == patch_id:
            return series_link, i + 1
    return series_link, 1


def search_series(pwork, title, version=None):
    """Search patchwork for a series by cover-letter title

    Queries the patchwork API and returns the link for the best match.
    If multiple matches are found, shows them and picks the most recent,
    unless a specific version is requested.

    Args:
        pwork (Patchwork): Configured patchwork instance
        title (str): Title text to search for
        version (int or None): Specific version to select, or None for the
            most recent

    Returns:
        str: Patchwork series link/ID

    Raises:
        ValueError: if no matching series (or version) is found
    """
    if not pwork.proj_id:
        raise ValueError('Patchwork project not configured; use '
            "'patman patchwork set-project' or provide -l <link>")
    results = common.pw_call(pwork.query_series, title)

    if not results:
        raise ValueError(f"No series found matching '{title}'")

    if version is not None:
        matches = [r for r in results if r.get('version') == version]
        if not matches:
            avail = ', '.join(
                f"v{r.get('version')}"
                for r in sorted(results, key=lambda r: r.get('version', 0)))
            raise ValueError(
                f"No v{version} of '{title}' found (available: {avail})")
        # If several share the version, take the most recent by date
        best = max(matches, key=lambda r: r.get('date', ''))
        tout.notice(f"Using v{version}: {best['name']} (link {best['id']})")
        return str(best['id'])

    if len(results) == 1:
        match = results[0]
        tout.notice(f"Found: {match['name']} (v{match['version']}, "
                    f"link {match['id']})")
        return str(match['id'])

    # Multiple matches - show them and pick the most recent
    tout.notice(f"Found {len(results)} matching series:")
    for i, match in enumerate(results):
        tout.notice(f"  {i + 1}. [{match['id']}] {match['name']} "
                    f"(v{match['version']}, {match['date']})")

    best = max(results, key=lambda r: (r.get('version', 0), r.get('date', '')))
    tout.notice(f"Using most recent: {best['name']} (link {best['id']})")
    return str(best['id'])


def clean_series_name(name):
    """Strip the [U-Boot,v2,0/4] prefix from a series name

    Args:
        name (str): Raw series name from patchwork

    Returns:
        str: Cleaned name
    """
    if name.startswith('['):
        bracket_end = name.find(']')
        if bracket_end != -1:
            return name[bracket_end + 1:].strip()
    return name


def make_review_name(link, upstream=None):
    """Build a series name for a review branch

    Args:
        link (str): Patchwork series link/ID
        upstream (str or None): Upstream name

    Returns:
        str: Branch-style name, e.g. 'us-498633-review'
    """
    ups = upstream or 'pw'
    return f'{ups}-{link}-review'


def fetch_series(pwork, link):
    """Fetch and validate series metadata from patchwork

    If patchwork lacks some of the patches, they are taken from lore if it
    has them (see Patchwork.fill_from_lore()).

    Args:
        pwork (Patchwork): Configured patchwork instance
        link (str): Patchwork series link/ID

    Returns:
        tuple: (series_data, clean_name, version, patch_count)

    Raises:
        ValueError: if the series is incomplete, even with lore
    """
    series_data = common.pw_call(pwork.get_series, link)
    if not series_data.get('received_all', True):
        series_data = common.pw_call(pwork.fill_from_lore, series_data)
        for patch in series_data['patches']:
            if patch['id'] in series_data.get('lore_patches', []):
                tout.notice(f"Patchwork lacks {patch['name']}; using the "
                            'copy on lore')

    series_name = series_data.get('name', f'series-{link}')
    version = series_data.get('version', 1)
    patch_count = series_data.get('received_total', 0)
    if series_data.get('lore_patches'):
        patch_count = len(series_data['patches'])
    total = series_data.get('total', patch_count)

    clean_name = clean_series_name(series_name)

    tout.notice(f"Series: {clean_name}")
    tout.notice(f"Version: {version}, Patches: {patch_count}")

    if patch_count < total:
        raise ValueError('Incomplete series: patchwork received '
            f'{patch_count} of {total} patches')

    return series_data, clean_name, version, patch_count


def fetch_cover_content(pwork, series_data):
    """Fetch cover letter content from patchwork

    Args:
        pwork (Patchwork): Patchwork instance
        series_data (dict): Series data from patchwork

    Returns:
        str or None: Cover letter text
    """
    cover = series_data.get('cover_letter')
    if not cover:
        return None

    return common.pw_call(pwork.get_cover, cover['id']).get('content', '')


# Patchwork patch states worth reviewing: a series is reviewed only when at
# least one of its patches is in one of these states
ACTIVE_STATES = {'new', 'rfc', 'under-review', 'changes-requested',
                 'needs-review-ack'}


def get_patch_states(pwork, link):
    """Get the patchwork state of each patch in a series

    Args:
        pwork (Patchwork): Configured patchwork instance
        link (str): Patchwork series link/ID

    Returns:
        list of str: State slug for each patch
    """
    return common.pw_call(pwork.get_series_patch_states, link)
