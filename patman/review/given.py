# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""The context which the review agent was given for each review

A review records the parts of its prompt which vary, such as the earlier
review of the patch and the reviewer's notes (see prompts.context_parts()).
A review made before patman recorded them may still have the transcript of
its agent session, which starts with the prompt, so the parts are found in
that by their headings.
"""

import glob
import json
import os
import re

# A session ID, as patman makes them (a UUID)
RE_SESSION = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-'
                        r'[0-9a-f]{12}$')

# The heading of each part of a prompt, with its title and the text which
# starts the instructions after it, if any
HEADINGS = (
    (re.compile(r'^WRITING STYLE:$'), 'Writing voice',
     'Match this voice when writing your comments:'),
    (re.compile(r'^SERIES OVERVIEW \(.*\):$|^SERIES \(\d+ patches?\):$'),
     'Series', "You can run 'git show"),
    (re.compile(r'^EXISTING COMMENTS:$'), 'Existing comments', 'CRITICAL:'),
    (re.compile(r'^\S.* CONVENTIONS \(check the patches against these\):$'),
     'Conventions', None),
    (re.compile(r'^SERIES CONTEXT \(cover letter\):$|^COVER LETTER:$'),
     'Cover letter', None),
    (re.compile(r'^PREVIOUS (COVER-LETTER )?REVIEW \(from (earlier '
                r'version|v\d+)\):$'), 'Earlier review', 'This is a FOLLOW-UP'),
    (re.compile(r'^UNCHANGED SINCE v\d+:$'), 'Unchanged', 'Do NOT raise'),
    (re.compile(r'^USER CONTEXT \(.*\):$'), 'Your notes', None),
    (re.compile(r'^COVERITY \(.*\):$'), 'Coverity', 'If any of these defects'),
)

# A line which starts the instructions, ending a part
RE_STOP = re.compile(r'^(TASK|IMPORTANT|OUTPUT FORMAT|Rules):$|'
                     r'^\d+\. Review the patch for:$')


def transcript_dir():
    """Get the directory holding the transcripts of the agent sessions

    Returns:
        str: Directory, e.g. '/home/fred/.claude/projects'
    """
    base = os.environ.get('CLAUDE_CONFIG_DIR') or os.path.expanduser(
        '~/.claude')
    return os.path.join(base, 'projects')


def transcript_prompt(session_id, base=None):
    """Get the prompt which started an agent session, from its transcript

    Args:
        session_id (str or None): Session ID
        base (str or None): Directory holding the transcripts, or None for
            the one Claude uses (see transcript_dir())

    Returns:
        str or None: Prompt, or None if the transcript is not there
    """
    if not session_id or not RE_SESSION.match(session_id):
        return None
    for fname in glob.glob(os.path.join(base or transcript_dir(), '*',
                                        f'{session_id}.jsonl')):
        try:
            with open(fname, encoding='utf-8') as inf:
                for line in inf:
                    entry = json.loads(line)
                    if entry.get('type') != 'user':
                        continue
                    content = entry.get('message', {}).get('content')
                    if isinstance(content, list):
                        content = ''.join(item.get('text', '')
                                          for item in content
                                          if isinstance(item, dict))
                    return content or None
        except (OSError, ValueError):
            continue
    return None


def parts_from_prompt(prompt):
    """Find the parts of a prompt which give the agent its context

    Args:
        prompt (str): Prompt, as from prompts.build_review_prompt() or
            build_cover_review_prompt()

    Returns:
        list of tuple: Each part, as (title, text), as from
            prompts.context_parts()
    """
    found = []
    inside = False
    for line in prompt.splitlines():
        heading = next(((title, cut) for regex, title, cut in HEADINGS
                        if regex.match(line.rstrip())), None)
        if heading:
            found.append((*heading, []))
            inside = True
        elif inside and RE_STOP.match(line.rstrip()):
            inside = False
        elif inside:
            found[-1][2].append(line)

    parts = []
    for title, cut, lines in found:
        text = '\n'.join(lines)
        if cut and cut in text:
            pos = text.index(cut)
            # The voice comes after its instruction, the others before
            text = text[pos + len(cut):] if title == 'Writing voice' else \
                text[:pos]
        text = text.strip('\n').rstrip()
        if text.strip():
            parts.append((title, text))
    return parts


def get_context(db, review_id, base=None):
    """Get the context which the agent was given for a review

    Args:
        db (Database): Review database
        review_id (int): Review record ID
        base (str or None): Directory holding the transcripts, or None for
            the one Claude uses

    Returns:
        tuple:
            str or None: Where the context is from: 'stored' if the review
                recorded it, 'transcript' if it is from the agent's session,
                or None if it is not known
            list of tuple: Each part, as (title, text)
    """
    parts = db.review_get_context(review_id)
    if parts is not None:
        return 'stored', parts
    prompt = transcript_prompt(db.review_get_session(review_id), base)
    if prompt:
        return 'transcript', parts_from_prompt(prompt)
    return None, []
