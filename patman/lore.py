# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Read mailing-list threads from lore.kernel.org

Patchwork has no way to find out which patches have new comments, so a sync
reads the comments on each patch, one request at a time, which is a lot of
requests for a busy server such as patchwork.ozlabs.org. Every comment is
also an email, and lore.kernel.org archives many lists, serving a whole
thread, with every reply, in one request. So a sync can read the thread of a
series from lore and share the replies out among its patches, in the form
patchwork gives them.

Lore does not always have every reply which patchwork has, so by default
('hint' mode) the comments still come from patchwork: lore only says which
patches have new replies, so that patchwork is asked for those, and for any
whose comments have not been read from patchwork for a while. In 'use' mode
the comments come from lore itself, falling back to patchwork if lore lacks
the thread, and 'off' does not use lore.

Lore's 'all' archive finds a message by its Message-ID on any list which lore
carries, so no setting is needed. A thread which lore does not have is
remembered for a while (see MISSING_MAX_AGE), so that lore is not asked for it
on every sync.
"""

import asyncio
import email
import email.policy
import email.utils
import gzip
import re
from datetime import timezone

import aiohttp

from patman import pwcache
from patman import traffic

# The archive of all the lists which lore carries
LORE_URL = 'https://lore.kernel.org/all'

# Seconds to wait for a thread, and the number of times to try again
REQUEST_TIMEOUT = 30
RETRIES = 1

# Maximum number of requests to send to lore at once
MAX_CONCURRENT = 2

# Seconds for which a thread which lore does not have is not asked for again
MISSING_MAX_AGE = 24 * 60 * 60

# How to use lore, as set by --lore or the 'lore' setting: one of MODES
MODES = ('hint', 'use', 'off')
MODE = 'hint'  # pylint: disable=C0103

# Seconds after which the comments on a patch are read from patchwork again in
# 'hint' mode, even if lore shows no new replies, in case lore lacks some
RECHECK = 7 * 24 * 60 * 60

# The subject of a patch, rather than a reply to one, e.g. '[PATCH v2 1/3] dm:'
RE_PATCH_SUBJECT = re.compile(r'\s*\[[^\]]*PATCH')


def set_mode(mode):
    """Set how to use lore

    Args:
        mode (str): One of MODES: 'hint' to use lore to see which patches
            have new replies, 'use' to read the comments from lore, or 'off'

    Raises:
        ValueError: The mode is not known
    """
    global MODE  # pylint: disable=W0603
    if mode not in MODES:
        raise ValueError(f"Unknown lore mode '{mode}': use one of "
                         f"{', '.join(MODES)}")
    MODE = mode


def on_list(comment, list_id):
    """Check whether a reply was sent to a list

    Args:
        comment (dict): Reply, as from to_comment()
        list_id (str or None): ID of the list, e.g.
            'u-boot.lists.u-boot-project.org', or None if not known

    Returns:
        bool: True if the reply was sent to the list, or there is no way to
            tell
    """
    lid = comment.get('headers', {}).get('List-Id', '')
    return not lid or not list_id or list_id in lid


def new_replies(replies, known, list_id):
    """Find the replies on lore which are not among those already known

    Args:
        replies (list of dict): Replies from lore, as from to_comment()
        known (list of dict): Comments already read, e.g. from patchwork
        list_id (str or None): ID of the project's list; replies sent to
            other lists are left out, since patchwork does not have them

    Returns:
        list of dict: Replies which are new
    """
    have = {bare_msgid(cmt.get('msgid') or '') for cmt in known}
    return [rep for rep in replies
            if bare_msgid(rep['msgid']) not in have and on_list(rep, list_id)]


def bare_msgid(msgid):
    """Get a Message-ID without its angle brackets

    Args:
        msgid (str): Message-ID, e.g. '<abc@example.com>'

    Returns:
        str: e.g. 'abc@example.com'
    """
    return msgid.strip().strip('<>')


class Lore:  # pylint: disable=R0903
    """Reads threads from lore

    Args:
        url (str): URL of the lore archive
    """
    def __init__(self, url=LORE_URL):
        self.url = url.rstrip('/')
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        # Number of threads read from lore
        self.request_count = 0

    async def get_thread(self, client, msgid):
        """Read the thread holding a message

        Args:
            client (aiohttp.ClientSession): Session to use
            msgid (str): Message-ID of a message in the thread, e.g. the
                cover letter, with or without angle brackets

        Returns:
            list of email.message.EmailMessage or None: Messages in the
                thread, or None if lore does not have it or cannot be read
        """
        msgid = bare_msgid(msgid)
        missing = f'missing/{msgid}'
        cache = pwcache.get()
        if cache and cache.get(self.url, missing, MISSING_MAX_AGE):
            return None
        content = await self._read(client, f'{msgid}/t.mbox.gz')
        if content is None:
            return None
        if content is False:
            if cache:
                cache.put(self.url, missing, True)
            return None
        try:
            return parse_mbox(gzip.decompress(content))
        except (OSError, EOFError):
            return None

    async def _read(self, client, subpath):
        """Read a file from lore

        Args:
            client (aiohttp.ClientSession): Session to use
            subpath (str): Path within the archive

        Returns:
            bytes or None or False: Content, None if it cannot be read just
                now, or False if lore does not have it
        """
        full_url = f'{self.url}/{subpath}'
        async with self.semaphore:
            for i in range(RETRIES + 1):
                self.request_count += 1
                traffic.add(self.url, requests=1, kind='thread')
                try:
                    async with client.get(
                            full_url, timeout=aiohttp.ClientTimeout(
                                total=REQUEST_TIMEOUT)) as response:
                        content = await response.read()
                        traffic.add(self.url, nbytes=len(content),
                                    kind='thread')
                        if response.status == 200:
                            return content
                        if response.status == 404:
                            return False
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    pass
                if i < RETRIES:
                    await asyncio.sleep(i + 1)
        return None


def parse_mbox(data):
    """Split an mbox file into its messages

    Args:
        data (bytes): Content of the mbox file

    Returns:
        list of email.message.EmailMessage: Messages
    """
    messages = []
    for part in re.split(rb'^From [^\n]*\n', data, flags=re.M):
        if part.strip():
            msg = email.message_from_bytes(part, policy=email.policy.default)
            # Keep the message as sent, for writing it to an mbox again
            msg.raw = part
            messages.append(msg)
    return messages


# The parts of the prefix of a patch's subject, e.g. '[PATCH v3 2/5]'
RE_SUBJECT = re.compile(r'\s*\[([^\]]*)\]\s*(.*)', re.S)
RE_SEQ = re.compile(r'\b(\d+)/(\d+)\b')
RE_VERSION = re.compile(r'\bv(\d+)\b', re.I)


def patch_info(msg):
    """Get the place of a patch in its series, from its subject

    Args:
        msg (email.message.EmailMessage): Message

    Returns:
        tuple or None: None if the message is not a patch in a series, else:
            int: Version, 1 if none is given
            int: Number of the patch, starting at 1
            int: Number of patches in the series
            str: Name as patchwork gives it, e.g. '[v3,2/5] dm: Tidy up'
    """
    subject = ' '.join(str(msg.get('Subject', '')).split())
    match = RE_SUBJECT.match(subject)
    if not match or 'PATCH' not in match.group(1).upper():
        return None
    seq = RE_SEQ.search(match.group(1))
    if not seq or int(seq.group(1)) == 0:
        return None
    version = RE_VERSION.search(match.group(1))
    parts = [part for part in match.group(1).split()
             if part.upper() != 'PATCH']
    return (int(version.group(1)) if version else 1, int(seq.group(1)),
            int(seq.group(2)), f"[{','.join(parts)}] {match.group(2)}")


def to_patch(msg, patch_id):
    """Turn a message holding a patch into a patch, as patchwork gives them

    Args:
        msg (email.message.EmailMessage): Message
        patch_id (str): ID to give it, e.g. 'lore:<msgid>'

    Returns:
        dict: Patch, with 'id', 'msgid', 'name', 'date', 'submitter',
            'content' (the commit message), 'diff', 'headers', 'state' and
            'web_url'
    """
    cmt = to_comment(msg)
    body = cmt['content']
    pos = body.find('\ndiff --git ')
    info = patch_info(msg)
    return {
        'id': patch_id, 'msgid': cmt['msgid'], 'date': cmt['date'],
        'name': info[3] if info else cmt['subject'],
        'submitter': cmt['submitter'],
        'content': body if pos < 0 else body[:pos + 1],
        'diff': '' if pos < 0 else body[pos + 1:],
        'headers': cmt['headers'], 'state': 'new',
        'web_url': cmt['web_url'], 'project': {},
    }


def _get_body(msg):
    """Get the plain-text body of a message

    Args:
        msg (email.message.EmailMessage): Message

    Returns:
        str: Body, or '' if it has none
    """
    part = msg.get_body(preferencelist=('plain',))
    if part is None:
        return ''
    try:
        return part.get_content()
    except (LookupError, ValueError):
        payload = part.get_payload(decode=True) or b''
        return payload.decode('utf-8', errors='replace')


def to_comment(msg):
    """Turn a message into a comment, as patchwork gives them

    Only the fields which a sync uses are filled in with care. There is no
    patchwork ID, so this is None.

    Args:
        msg (email.message.EmailMessage): Message

    Returns:
        dict: Comment, with 'id', 'msgid', 'date', 'subject', 'submitter'
            (with 'name' and 'email'), 'content', 'headers' and 'web_url'
    """
    name, addr = email.utils.parseaddr(str(msg.get('From', '')))
    date = ''
    if msg.get('Date'):
        try:
            when = email.utils.parsedate_to_datetime(str(msg['Date']))
            if when.tzinfo:
                when = when.astimezone(timezone.utc).replace(tzinfo=None)
            date = when.isoformat()
        except (TypeError, ValueError):
            pass
    msgid = bare_msgid(str(msg.get('Message-ID', '')))
    return {
        'id': None,
        'msgid': f'<{msgid}>',
        'date': date,
        'subject': str(msg.get('Subject', '')),
        'submitter': {'name': name or addr, 'email': addr},
        'content': _get_body(msg),
        'headers': {key: str(val) for key, val in msg.items()},
        'web_url': f'{LORE_URL}/{msgid}/',
    }


def comments_for(messages, msgids):
    """Share the replies in a thread out among the messages they reply to

    A reply belongs to the first of 'msgids' found by following its
    In-Reply-To back through the thread, or failing that, the last one in its
    References, as patchwork does. Messages which are themselves patches, e.g.
    a new version of the series sent in reply to the old one, are not
    replies.

    Args:
        messages (list of email.message.EmailMessage): Messages in the thread
        msgids (list of str): Message-IDs to find the replies to, e.g. those
            of the cover letter and patches

    Returns:
        dict: key (str): Message-ID from 'msgids', without angle brackets
            value (list of dict): Replies to it, as from to_comment(), in the
                order they appear in the thread
    """
    wanted = {bare_msgid(mid) for mid in msgids}
    parent = {}
    for msg in messages:
        mid = bare_msgid(str(msg.get('Message-ID', '')))
        reply_to = msg.get('In-Reply-To')
        if mid and reply_to:
            parent[mid] = bare_msgid(str(reply_to).split()[0])

    def find(msg):
        mid = bare_msgid(str(msg.get('Message-ID', '')))
        seen = {mid}
        up = parent.get(mid)
        while up and up not in seen:
            if up in wanted:
                return up
            seen.add(up)
            up = parent.get(up)
        refs = str(msg.get('References', '')).split()
        for ref in reversed(refs):
            if bare_msgid(ref) in wanted:
                return bare_msgid(ref)
        return None

    out = {mid: [] for mid in wanted}
    for msg in messages:
        mid = bare_msgid(str(msg.get('Message-ID', '')))
        subject = str(msg.get('Subject', ''))
        if mid in wanted or RE_PATCH_SUBJECT.match(subject):
            continue
        target = find(msg)
        if target:
            out[target].append(to_comment(msg))
    return out
