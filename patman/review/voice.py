# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2025 Canonical Ltd.
# Written by Simon Glass <simon.glass@canonical.com>
#

"""Learning the reviewer's writing voice

patman can learn how the reviewer writes from their past reviews, either
from Gmail or from patchwork, and keeps a style guide in voice.md for the
review agents to follow. It also refines the guide by comparing drafts with
the reviews which were actually sent.
"""

from collections import namedtuple
import os
import re
import tempfile

import aiohttp

from u_boot_pylib import claude as claude_mod
from u_boot_pylib import gitutil
from u_boot_pylib import tools
from u_boot_pylib import tout

from patman import gmail
from patman.review import common


def do_learn_voice(args, pwork):
    """Build a voice profile from past reviews

    Args:
        args (Namespace): Command-line arguments
        pwork (Patchwork or None): Configured patchwork instance

    Returns:
        int: 0 on success, 1 on failure
    """

    source = args.learn_voice
    account = getattr(args, 'gmail_account', None)
    user_email = None
    list_email = None

    if pwork and pwork.proj_id:
        async def _get_list_email():
            projects = await pwork.get_projects()
            for proj in projects:
                if proj['id'] == pwork.proj_id:
                    return proj.get('list_email')
            return None

        list_email = common.run(_get_list_email())

    if args.reviewer:
        match = re.match(r'(.+?)\s*<(.+?)>', args.reviewer)
        if match:
            user_email = match.group(2).strip()
    if not user_email:
        user_email = gitutil.get_default_user_email()

    count = getattr(args, 'voice_count', 20)
    vp = VoiceParams(source, account, pwork, user_email, list_email, count)
    return 0 if learn_voice_sync(vp) else 1


VOICE_PATH = os.path.join(os.path.expanduser('~/.config/patman.d'),
                          'voice.md')


_VOICE_PROMPT = '''Analyse the following email reviews written by a single
person. These are code-review replies on the U-Boot mailing list.

Study the writing style carefully and produce a concise style guide that
captures this person's voice. Focus on:

- Tone (formal, casual, terse, friendly, etc.)
- Typical greeting and sign-off patterns
- How they quote code and structure comments
- Common phrases or idioms they use
- Level of detail in explanations
- How they phrase requests for changes vs suggestions
- How they express approval

Output ONLY the style guide in markdown, with no preamble, explanation,
or commentary before or after it. It should be usable as-is by an AI to
replicate this person's voice when writing reviews. Keep it under 50 lines.

=== EMAILS ===
{emails}
'''


VoiceParams = namedtuple('VoiceParams',
                         'source account pwork user_email list_email count')


async def _fetch_voice_gmail(account, list_email, user_email, max_results=20):
    """Fetch review emails from Gmail for voice learning

    Args:
        account (str or None): Gmail account to read from
        list_email (str): Mailing list email to filter by
        user_email (str): Reviewer's email, to skip own patch threads
        max_results (int): Maximum emails to fetch

    Returns:
        list of str: Review email bodies, or None on failure
    """

    if not gmail.check_available():
        return None

    tout.notice(f'Fetching sent review emails to {list_email}...')
    service = gmail.get_service(account)
    return gmail.fetch_sent_reviews(service, list_email, user_email,
                                    max_results)


async def _fetch_voice_patchwork(pwork, user_email, max_comments=20):
    """Fetch review comments from patchwork for voice learning

    Args:
        pwork (Patchwork): Configured patchwork instance
        user_email (str): Email to search for in comments
        max_comments (int): Number of comments to collect

    Returns:
        list of str: Comment bodies, or None on failure
    """

    if not pwork or not pwork.proj_id:
        tout.error('Patchwork project not configured')
        return None

    tout.notice(f'Fetching comments by {user_email} from patchwork...')
    async with aiohttp.ClientSession() as client:
        return await pwork.fetch_user_comments(client, user_email,
                                               max_comments)


async def learn_voice(vp):
    """Analyse past reviews and create a voice profile

    Fetches review text from the chosen source, sends it to a Claude agent
    for style analysis, and saves the result as voice.md

    Args:
        vp (VoiceParams): Voice learning parameters

    Returns:
        bool: True if the voice profile was created successfully
    """
    if not claude_mod.check_available():
        return False

    if vp.source == 'patchwork':
        bodies = await _fetch_voice_patchwork(vp.pwork, vp.user_email,
                                              vp.count)
    else:
        if not vp.list_email:
            tout.error('No mailing list email known; use -U to specify '
                       'an upstream')
            return False
        if not vp.user_email:
            tout.error('No reviewer email known; use --reviewer')
            return False
        bodies = await _fetch_voice_gmail(vp.account, vp.list_email,
                                          vp.user_email, vp.count)

    if not bodies:
        tout.error(f'No review text found from {vp.source}')
        return False

    tout.notice(f'Found {len(bodies)} reviews, analysing style...')

    # Write reviews to a temp file so the agent can read them
    # (avoids exceeding prompt size limits with many reviews)
    reviews_path = os.path.join(tempfile.gettempdir(),
                                'patman_voice_reviews.txt')
    combined = '\n\n--- NEXT EMAIL ---\n\n'.join(bodies)
    tools.write_file(reviews_path, combined, binary=False)

    prompt = _VOICE_PROMPT.format(emails=f'(see file: {reviews_path})')
    options = common.agent_options(allowed_tools=['Read'],
        max_buffer_size=claude_mod.MAX_BUFFER_SIZE)

    success, result = await claude_mod.run_agent_collect(prompt, options)

    # Clean up temp file
    try:
        os.unlink(reviews_path)
    except OSError:
        pass

    if not success or not result.strip():
        tout.error('Failed to analyse writing style')
        return False

    os.makedirs(os.path.dirname(VOICE_PATH), exist_ok=True)
    tools.write_file(VOICE_PATH, result.strip() + '\n', binary=False)

    tout.notice(f'Voice profile saved to {VOICE_PATH}')
    return True


def learn_voice_sync(vp):
    """Synchronous wrapper for learn_voice()"""
    return common.run(learn_voice(vp))


def get_voice():
    """Load the voice profile if it exists

    Returns:
        str or None: Voice profile text, or None if not configured
    """
    if os.path.exists(VOICE_PATH):
        return tools.read_file(VOICE_PATH, binary=False).strip()
    return None


_REFINE_PROMPT = '''Compare the AI-generated draft review with the review
that was actually sent by the reviewer. The differences reveal the
reviewer's preferences.

Read the two files below, then analyse what changed and why. Focus on:
- Phrasing the reviewer preferred over the AI's version
- Content the reviewer added or removed
- Tone or style adjustments
- Structural changes (ordering, quoting style, etc.)

AI DRAFT:
(see file: {draft_path})

ACTUALLY SENT:
(see file: {sent_path})

CURRENT VOICE PROFILE:
(see file: {voice_path})

Now output ONLY an updated voice profile in markdown. Keep everything
from the current profile that is still accurate, and add or revise
entries based on the draft-vs-sent differences. Keep it under 60 lines.
Do not include any preamble or commentary.
'''


async def refine_voice(draft_body, sent_body):
    """Refine the voice profile by comparing a draft with what was sent

    Args:
        draft_body (str): The AI-generated review text
        sent_body (str): The review text actually sent by the user

    Returns:
        bool: True if the voice profile was updated
    """

    if not claude_mod.check_available():
        return False

    voice = get_voice()
    if not voice:
        tout.notice('No voice profile to refine; run --learn-voice first')
        return False

    # Write files for the agent to read
    draft_path = os.path.join(tempfile.gettempdir(), 'patman_voice_draft.txt')
    sent_path = os.path.join(tempfile.gettempdir(), 'patman_voice_sent.txt')
    tools.write_file(draft_path, draft_body, binary=False)
    tools.write_file(sent_path, sent_body, binary=False)

    prompt = _REFINE_PROMPT.format(draft_path=draft_path,
        sent_path=sent_path, voice_path=VOICE_PATH)
    options = common.agent_options(allowed_tools=['Read'],
        max_buffer_size=claude_mod.MAX_BUFFER_SIZE)

    tout.notice('Analysing draft vs sent differences...')
    success, result = await claude_mod.run_agent_collect(prompt, options)

    for path in (draft_path, sent_path):
        if os.path.exists(path):
            os.unlink(path)

    if not success or not result.strip():
        tout.error('Failed to refine voice profile')
        return False

    tools.write_file(VOICE_PATH, result.strip() + '\n', binary=False)

    tout.notice(f'Voice profile updated: {VOICE_PATH}')
    return True


def refine_voice_sync(draft_body, sent_body):
    """Synchronous wrapper for refine_voice()"""
    return common.run(refine_voice(draft_body, sent_body))
