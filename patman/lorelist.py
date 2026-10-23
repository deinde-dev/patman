# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Read series from a mailing list on lore, for a project without patchwork

Some projects, such as dtc, have a mailing list but no patchwork project. Lore
archives the list, so a series can be read from its thread there instead: the
cover letter (or the first patch, if there is no cover letter) names the
series, the patches are the messages in the thread with the same version and
count, and the other messages are the comments.

LoreList stands in for Patchwork, giving the series, patches, cover letter,
comments and mbox in the form patchwork gives them, so that a review can use
either. A series is known by the Message-ID of its cover letter or first
patch, and each patch and cover letter has an ID of 'lore:<msgid>'.

An upstream uses a lore list when its patchwork URL is the list on lore, e.g.
'https://lore.kernel.org/devicetree-compiler'
"""

import email.utils
from urllib.parse import unquote, urlparse

from patman import lore

# Prefix of the ID of a patch or cover letter read from lore
ID_PREFIX = 'lore:'


def parse_link(link):
    """Get the Message-ID which a series is known by, from what the user gives

    Args:
        link (str): Message-ID, with or without angle brackets, or the URL of
            the message on lore, e.g.
            'https://lore.kernel.org/devicetree-compiler/<msgid>/T/'

    Returns:
        str: Message-ID, without angle brackets
    """
    link = link.strip()
    if '://' in link:
        parts = urlparse(link).path.strip('/').split('/')
        if len(parts) >= 2:
            link = unquote(parts[1])
    return lore.bare_msgid(link)


def msgid_of(msg):
    """Get the Message-ID of a message

    Args:
        msg (email.message.EmailMessage): Message

    Returns:
        str: Message-ID, without angle brackets
    """
    return lore.bare_msgid(str(msg.get('Message-ID', '')))


def subject_info(msg):
    """Get the place of a patch or cover letter in its series, from its subject

    Unlike lore.patch_info(), this handles a cover letter and a patch on its
    own, with no count in its subject.

    Args:
        msg (email.message.EmailMessage): Message

    Returns:
        tuple or None: None if the message is not a patch or cover letter,
                else:
            int: Version, 1 if none is given
            int: Number of the patch, 0 for a cover letter, or 1 for a patch
                on its own
            int: Number of patches in the series
            str: Name as patchwork gives it, e.g. '[v3,2/5] dm: Tidy up', or
                just the title if the prefix only says 'PATCH'
    """
    subject = ' '.join(str(msg.get('Subject', '')).split())
    match = lore.RE_SUBJECT.match(subject)
    if not match or 'PATCH' not in match.group(1).upper():
        return None
    seq = lore.RE_SEQ.search(match.group(1))
    version = lore.RE_VERSION.search(match.group(1))
    parts = [part for part in match.group(1).split()
             if part.upper() != 'PATCH']
    name = match.group(2)
    if parts:
        name = f"[{','.join(parts)}] {name}"
    return (int(version.group(1)) if version else 1,
            int(seq.group(1)) if seq else 1,
            int(seq.group(2)) if seq else 1, name)


def _find_patches(messages, root, info):
    """Find the patches of a series in its thread

    Args:
        messages (list of email.message.EmailMessage): Messages in the thread
        root (email.message.EmailMessage): Cover letter or first patch
        info (tuple): Place of the root in its series, from subject_info()

    Returns:
        dict: key (int): Number of the patch, starting at 1
            value (email.message.EmailMessage): Patch
    """
    version, seq, total, _ = info
    link = msgid_of(root)
    found = {1: root} if seq == 1 else {}
    for msg in messages:
        refs = str(msg.get('References', '')).split()
        refs.append(str(msg.get('In-Reply-To', '')))
        if link not in {lore.bare_msgid(ref) for ref in refs}:
            continue
        minfo = subject_info(msg)
        if minfo and minfo[0] == version and minfo[2] == total and \
                minfo[1] >= 1 and \
                not str(msg.get('Subject', '')).lower().startswith('re:'):
            found.setdefault(minfo[1], msg)
    return found


class LoreList:
    """Reads series from a mailing list on lore, standing in for Patchwork

    Only what a review needs is provided. Lore has no patch states, so every
    patch is 'new'.

    Args:
        url (str): URL of the list on lore, e.g.
            'https://lore.kernel.org/devicetree-compiler'
    """
    def __init__(self, url):
        self.url = url.rstrip('/')
        self.lore = lore.Lore(self.url)
        # Name of the list, e.g. 'devicetree-compiler'
        self.link_name = self.url.rsplit('/', 1)[-1]
        # There is no patchwork project, but searching needs one to be set
        self.proj_id = self.link_name
        self.upstream = None
        # Messages of each thread read, keyed by the Message-ID of each
        # message in it
        self._threads = {}

    def project_set(self, project_id, link_name):
        """Set the project, which a list on lore does not need

        Args:
            project_id (int): Patchwork project ID
            link_name (str): Project link name
        """

    async def _thread(self, client, msgid):
        """Read the thread holding a message

        Args:
            client (aiohttp.ClientSession): Session to use
            msgid (str): Message-ID, without angle brackets

        Returns:
            list of email.message.EmailMessage: Messages in the thread

        Raises:
            ValueError: Lore does not have the message
        """
        if msgid not in self._threads:
            # A series may be given before lore has it, so do not rely on
            # lore having lacked it before
            messages = await self.lore.get_thread(client, msgid, recheck=True)
            for msg in messages or []:
                self._threads[msgid_of(msg)] = messages
            if msgid not in self._threads:
                raise ValueError(f"Cannot read message '{msgid}' from "
                                 f'{self.url}')
        return self._threads[msgid]

    async def _message(self, client, msgid):
        """Read a message

        Args:
            client (aiohttp.ClientSession): Session to use
            msgid (str): Message-ID, without angle brackets

        Returns:
            email.message.EmailMessage: Message
        """
        messages = await self._thread(client, msgid)
        return next(msg for msg in messages if msgid_of(msg) == msgid)

    def _project(self, msg):
        """Get the project of a series, as patchwork gives it

        Args:
            msg (email.message.EmailMessage): Cover letter or first patch

        Returns:
            dict: Project, with 'name', 'link_name', 'list_id' and
                'list_email', the address among those the message was sent
                to which is the list's, or None if there is none
        """
        list_id = lore.bare_msgid(str(msg.get('List-Id', '')).split()[-1]
                                  if msg.get('List-Id') else '')
        addrs = email.utils.getaddresses(
            [str(msg.get(field, '')) for field in ('To', 'Cc')])
        list_email = next((addr for _, addr in addrs
                           if addr.split('@')[0].lower() ==
                           self.link_name.lower()), None)
        return {'name': self.link_name, 'link_name': self.link_name,
                'list_id': list_id or None, 'list_email': list_email}

    def _entry(self, msg):
        """Get a patch or cover letter as a series lists it

        Args:
            msg (email.message.EmailMessage): Message

        Returns:
            dict: With 'id', 'msgid', 'name', 'date' and 'web_url'
        """
        mid = msgid_of(msg)
        cmt = lore.to_comment(msg)
        return {'id': f'{ID_PREFIX}{mid}', 'msgid': cmt['msgid'],
                'name': subject_info(msg)[3], 'date': cmt['date'],
                'web_url': f'{self.url}/{mid}/'}

    async def get_series(self, client, link, max_age=None):
        """Read a series from its thread on lore

        The patches are the messages which are in reply to the cover letter
        (or the first patch), directly or not, and have the same version and
        count, so that a new version sent in reply to an old one is not
        mixed in.

        Args:
            client (aiohttp.ClientSession): Session to use
            link (str): Message-ID of the cover letter, or of the first patch
                if there is no cover letter
            max_age (float or None): Not used

        Returns:
            dict: Series, as Patchwork.get_series() gives it, with 'id' set to
                the Message-ID

        Raises:
            ValueError: The message is not a cover letter or first patch
        """
        del max_age
        link = parse_link(link)
        root = await self._message(client, link)
        info = subject_info(root)
        if not info:
            raise ValueError(f"Message '{link}' is not a patch or cover "
                             'letter')
        version, seq, total, name = info
        if seq > 1:
            raise ValueError(
                f"Message '{link}' is patch {seq} of {total}; give the cover "
                'letter or the first patch')

        found = _find_patches(await self._thread(client, link), root, info)
        cmt = lore.to_comment(root)
        patches = [self._entry(found[num]) for num in sorted(found)]
        return {
            'id': link, 'name': name, 'version': version, 'total': total,
            'received_total': len(patches),
            'received_all': len(patches) >= total,
            'date': cmt['date'], 'submitter': cmt['submitter'],
            'web_url': f'{self.url}/{link}/',
            'mbox': f'{self.url}/{link}/t.mbox.gz',
            'project': self._project(root),
            'cover_letter': self._entry(root) if seq == 0 else None,
            'patches': patches,
        }

    async def get_series_patch_states(self, client, link):
        """Get the state of each patch in a series

        Lore has no states, so each patch is 'new'

        Args:
            client (aiohttp.ClientSession): Session to use
            link (str): Message-ID of the series

        Returns:
            list of str: 'new' for each patch
        """
        data = await self.get_series(client, link)
        return ['new'] * len(data['patches'])

    async def get_series_mbox(self, client, link, max_age=None):
        """Get an mbox holding the patches of a series, in order

        Args:
            client (aiohttp.ClientSession): Session to use
            link (str): Message-ID of the series
            max_age (float or None): Not used

        Returns:
            bytes: Content of the mbox
        """
        del max_age
        data = await self.get_series(client, link)
        out = []
        for patch in data['patches']:
            msg = await self._message(client, lore.bare_msgid(patch['msgid']))
            out.append(b'From mboxrd@z Thu Jan  1 00:00:00 1970\n' +
                       msg.raw.rstrip(b'\n') + b'\n\n')
        return b''.join(out)

    @staticmethod
    def lore_mbox(_data):
        """Get an mbox of patches which patchwork lacks, of which there are none

        Args:
            _data (dict): Series

        Returns:
            None: Always, so that get_series_mbox() is used
        """
        return None

    @staticmethod
    async def fill_from_lore(_client, data):
        """Fill in the patches which patchwork lacks, of which there are none

        Args:
            _client (aiohttp.ClientSession): Session to use
            data (dict): Series

        Returns:
            dict: The series, as given
        """
        return data

    async def get_patch(self, client, patch_id, max_age=None, since=None):
        """Read a patch

        Args:
            client (aiohttp.ClientSession): Session to use
            patch_id (str): ID of the patch, 'lore:<msgid>'
            max_age (float or None): Not used
            since (float or None): Not used

        Returns:
            dict: Patch, as lore.to_patch() gives it
        """
        del max_age, since
        msgid = str(patch_id).removeprefix(ID_PREFIX)
        return lore.to_patch(await self._message(client, msgid),
                             str(patch_id))

    async def get_cover(self, client, cover_id, max_age=None):
        """Read a cover letter

        Args:
            client (aiohttp.ClientSession): Session to use
            cover_id (str): ID of the cover letter, 'lore:<msgid>'
            max_age (float or None): Not used

        Returns:
            dict: Cover letter, with 'id', 'name', 'msgid', 'date',
                'submitter', 'content', 'headers' and 'web_url'
        """
        del max_age
        msg = await self._message(client,
                                  str(cover_id).removeprefix(ID_PREFIX))
        cover = lore.to_comment(msg)
        cover.update(self._entry(msg))
        cover['id'] = str(cover_id)
        return cover

    async def _comments(self, client, item_id):
        """Read the replies to a patch or cover letter

        Replies go to the patch or cover letter which they reply to, directly
        or not, so each patch and cover letter in the thread is considered,
        to keep a reply to a patch from going to the cover letter

        Args:
            client (aiohttp.ClientSession): Session to use
            item_id (str): ID of the patch or cover letter, 'lore:<msgid>'

        Returns:
            list of dict: Replies, as lore.to_comment() gives them
        """
        msgid = str(item_id).removeprefix(ID_PREFIX)
        messages = await self._thread(client, msgid)
        items = [msgid_of(msg) for msg in messages
                 if subject_info(msg) and
                 not str(msg.get('Subject', '')).lower().startswith('re:')]
        return lore.comments_for(messages, items + [msgid])[msgid]

    async def get_patch_comments(self, client, patch_id, max_age=None):
        """Read the replies to a patch

        Args:
            client (aiohttp.ClientSession): Session to use
            patch_id (str): ID of the patch, 'lore:<msgid>'
            max_age (float or None): Not used

        Returns:
            list of dict: Replies, as lore.to_comment() gives them
        """
        del max_age
        return await self._comments(client, patch_id)

    async def get_cover_comments(self, client, cover_id, max_age=None):
        """Read the replies to a cover letter

        Args:
            client (aiohttp.ClientSession): Session to use
            cover_id (str): ID of the cover letter, 'lore:<msgid>'
            max_age (float or None): Not used

        Returns:
            list of dict: Replies, as lore.to_comment() gives them
        """
        del max_age
        return await self._comments(client, cover_id)

    async def query_series(self, client, desc):
        """Search for a series by title, which is not supported yet

        Args:
            client (aiohttp.ClientSession): Session to use
            desc (str): Title to search for

        Raises:
            ValueError: Always
        """
        del client, desc
        raise ValueError(f'Cannot search {self.url} for a series by title; '
                         'give the Message-ID of its cover letter with -s')

    async def search_patches(self, client, query, project_id=None,
                             per_page=20):
        """Search for a patch by title, which is not supported yet

        Args:
            client (aiohttp.ClientSession): Session to use
            query (str): Title to search for
            project_id (int or None): Not used
            per_page (int): Not used

        Raises:
            ValueError: Always
        """
        del client, query, project_id, per_page
        raise ValueError(f'Cannot search {self.url} for a patch by title; '
                         'give the Message-ID of the series with -s')
