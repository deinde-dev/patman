# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
# pylint: disable=C0302
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

The newest series on the list are read from lore's Atom feed of the patches
and cover letters sent to it, for the inbox on the 'patman serve' page.

A review uses a lore list for an upstream which has one and no patchwork URL,
e.g. 'https://lore.kernel.org/devicetree-compiler', as do 'patman series
autolink' and 'patman series send', to find the series which you send to it

Lore has no patch states, so a patch is 'new' unless the upstream branch has
it: a maintainer who applies a patch from the list adds its Message-ID as a
trailer, either as 'Message-ID: <msgid>' or in a link to lore, so a patch
whose Message-ID is in the branch is 'accepted'
"""

import asyncio
import contextlib
import email.utils
import re
from types import SimpleNamespace
from urllib.parse import quote_plus, unquote, urlparse
import xml.etree.ElementTree as ET

from u_boot_pylib import command
from u_boot_pylib import gitutil
from u_boot_pylib import tout

from patman import lore
from patman import patchwork
from patman import pwcache

# Prefix of the ID of a patch or cover letter read from lore
ID_PREFIX = 'lore:'

# Where the lists are, on lore
LORE_BASE = 'https://lore.kernel.org'

# Path of the config of a list, which gives its name and addresses
LIST_CONFIG = '_/text/config/raw'

# The name of a list in its config, e.g. '[publicinbox "lkml"]'
RE_INBOX = re.compile(r'^\[publicinbox "([^"]+)"\]', re.M)

# An address of a list in its config, e.g.
# 'address = linux-kernel@vger.kernel.org'
RE_ADDRESS = re.compile(r'^\s*address\s*=\s*(\S+)', re.M)

# Search which gives the patches and cover letters sent to a list, leaving out
# the replies to them
FEED_QUERY = 's:PATCH AND NOT s:Re'

# Number of messages in each page of lore's feed
FEED_PAGE = 200

# How long to keep the results of a search, in seconds
SEARCH_KEEP = 10 * 60

# Prefix of the name of a series, e.g. '[v2] ' in '[v2] dm: Tidy up'
RE_PREFIX = re.compile(r'^\[[^]]*\] ')

# Hosts whose links name a message by its Message-ID, e.g. in the trailer
# 'Link: https://lore.kernel.org/r/<msgid>'
MSGID_HOSTS = ('lore.kernel.org', 'patch.msgid.link')

# Namespaces of the elements in lore's Atom feed
ATOM = '{http://www.w3.org/2005/Atom}'
THREAD = '{http://purl.org/syndication/thread/1.0}'


def trailer_msgid(value):
    """Get the Message-ID which a Message-ID: or Link: trailer gives

    Args:
        value (str): Value of the trailer, e.g. '<abc@example.com>' or
            'https://lore.kernel.org/r/abc@example.com'

    Returns:
        str or None: Message-ID without angle brackets, e.g.
            'abc@example.com', or None if the value does not give one
    """
    value = value.strip()
    if value.startswith('<'):
        msgid = lore.bare_msgid(value)
    else:
        url = urlparse(value)
        if url.hostname not in MSGID_HOSTS:
            return None
        parts = [unquote(part) for part in url.path.split('/') if part]
        # lore has the list (or 'r') first, e.g. '/devicetree-compiler/<msgid>/'
        if url.hostname == 'lore.kernel.org':
            parts = parts[1:]
        msgid = parts[0] if parts else ''
    return msgid if '@' in msgid else None


def applied_msgids(ref, git_dir=None):
    """Get the Message-IDs of the patches which a branch has applied

    Args:
        ref (str): Branch to look in, e.g. 'us/main'
        git_dir (str or None): Directory containing the git repo, or None
            for the current directory

    Returns:
        set of str: Message-IDs without angle brackets, from the Message-ID:
            and Link: trailers of the commits in the branch
    """
    if not gitutil.ref_exists(ref, git_dir):
        tout.info(f"No branch '{ref}', so no patches are applied")
        return set()
    result = command.run_one(
        'git', 'log', '--format=%(trailers:key=Message-ID,key=Link,'
        'valueonly)', ref, cwd=git_dir, capture=True, capture_stderr=True,
        raise_on_error=False)
    if result.return_code:
        tout.warning(f"Cannot read the patches applied to '{ref}': "
                     f'{result.stderr.strip()}')
        return set()
    return {msgid for line in result.stdout.splitlines()
            if (msgid := trailer_msgid(line))}


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
        tuple or None: As for parse_subject()
    """
    return parse_subject(str(msg.get('Subject', '')))


def parse_subject(subject):
    """Get the place of a patch or cover letter in its series, from a subject

    Args:
        subject (str): Subject, e.g. '[PATCH v3 2/5] dm: Tidy up'

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
    subject = ' '.join(subject.split())
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


def _series_name(subject):
    """Get the name of a series, from the subject of a patch or cover letter

    Args:
        subject (str): Subject, e.g. '[PATCH v3 2/5] dm: Tidy up'

    Returns:
        str: Name, as patchwork gives it, e.g. '[v3] dm: Tidy up'
    """
    match = lore.RE_SUBJECT.match(' '.join(subject.split()))
    parts = [part for part in match.group(1).split()
             if part.upper() != 'PATCH' and not lore.RE_SEQ.match(part)]
    return f"[{','.join(parts)}] {match.group(2)}" if parts else match.group(2)


def parse_feed(data):
    """Get the patches and cover letters in a page of lore's Atom feed

    Args:
        data (bytes): Feed

    Returns:
        list of dict: One for each patch or cover letter, with:
            msgid (str): Message-ID, without angle brackets
            parent (str or None): Message-ID of the message which it replies
                to, if any
            subject (str): Subject
            info (tuple): Its place in its series (see parse_subject())
            date (str): When it was sent, e.g. '2026-10-20T15:37:51'
            submitter (dict): Who sent it, with 'name' and 'email'
    """
    out = []
    for entry in ET.fromstring(data).iter(f'{ATOM}entry'):
        subject = entry.findtext(f'{ATOM}title') or ''
        info = parse_subject(subject)
        link = entry.find(f'{ATOM}link')
        if not info or link is None:
            continue
        reply = entry.find(f'{THREAD}in-reply-to')
        out.append({
            'msgid': parse_link(link.get('href', '')),
            'parent': (parse_link(reply.get('href', ''))
                       if reply is not None else None),
            'subject': subject, 'info': info,
            'date': (entry.findtext(f'{ATOM}updated') or '').rstrip('Z'),
            'submitter': {
                'name': entry.findtext(f'{ATOM}author/{ATOM}name'),
                'email': entry.findtext(f'{ATOM}author/{ATOM}email')},
        })
    return out


# A term of a query for lore, e.g. 'dfn:scripts/make_fit.py' for the patches
# changing that file, or 'f:fred' for those from Fred. A subject often has a
# colon too, e.g. 'arm64: dts: Add a board', so only these prefixes count
RE_LORE_TERM = re.compile(
    r'(?:^|[\s(])(?:s|d|dt|rt|b|bs|nq|f|t|c|a|tc|l|m|dfn|dfa|dfb|dfhh|'
    r'dfctx|dfpre|dfpost|dfblob|patchid):\S')


def parse_list_config(data):
    """Get the name and addresses of a list, from its config on lore

    Args:
        data (bytes): Config, from LIST_CONFIG within the list

    Returns:
        tuple:
            str or None: Name of the list on lore, e.g. 'lkml'
            list of str: Addresses of the list, in lower case, e.g.
                ['linux-kernel@vger.kernel.org']
    """
    text = data.decode('utf-8', 'replace')
    match = RE_INBOX.search(text)
    return (match.group(1) if match else None,
            [addr.lower() for addr in RE_ADDRESS.findall(text)])


async def find_list(client, address, base=LORE_BASE):
    """Find the list on lore which has an address

    A list is usually named for the part of its address before the '@', e.g.
    'devicetree-compiler' for devicetree-compiler@vger.kernel.org, though lore
    may know it by another name, e.g. 'lkml' for linux-kernel@vger.kernel.org.
    The list's config gives its name and addresses, so another list with that
    name is not taken for it.

    Args:
        client (aiohttp.ClientSession): Session to use
        address (str): Address, e.g.
            'Devicetree Compiler <devicetree-compiler@vger.kernel.org>'
        base (str): Where the lists are

    Returns:
        str or None: URL of the list, e.g.
            'https://lore.kernel.org/devicetree-compiler', or None if lore does
            not have it
    """
    addr = email.utils.parseaddr(address)[1].lower()
    local = addr.partition('@')[0]
    if '@' not in addr or not local:
        return None
    data = await lore.Lore(f'{base}/{local}').read(client, LIST_CONFIG,
                                                   kind='search')
    if not data:
        return None
    name, addrs = parse_list_config(data)
    return f'{base}/{name}' if name and addr in addrs else None


def search_query(text):
    """Get the lore query which searches for the series matching some text

    Each word must be in the subject of a patch or cover letter. Text which
    has a term of a lore query (see RE_LORE_TERM) is taken to be a query for
    lore already, e.g. 'dfn:scripts/make_fit.py', which finds the patches
    changing that file

    Args:
        text (str): Text to search for, e.g. 'make_fit compress'

    Returns:
        str: Query, e.g.
            's:"make_fit" AND s:"compress" AND s:PATCH AND NOT s:Re'

    Raises:
        ValueError: There is no text
    """
    text = ' '.join(text.split())
    if RE_LORE_TERM.search(text):
        query = f'({text})'
    else:
        words = [word.replace('"', '') for word in text.split()]
        query = ' AND '.join(f's:"{word}"' for word in words if word)
    if not query.strip('()'):
        raise ValueError('Please give some text to search for')
    return f'{query} AND {FEED_QUERY}'


def _series_link(entry, by_msgid):
    """Find the Message-ID which a series in the feed is known by

    This is the cover letter, or the first patch if there is no cover letter,
    found by following the replies back, as long as each is in the same
    version of the series. The cover letter may be too old to be in the page
    of the feed, but a patch of a series of several replies to it.

    Args:
        entry (dict): Patch or cover letter, as from parse_feed()
        by_msgid (dict): Each patch and cover letter in the page of the feed,
            keyed by its Message-ID

    Returns:
        str: Message-ID of the series
    """
    version, _, total, _ = entry['info']
    seen = {entry['msgid']}
    while entry['parent'] in by_msgid and entry['parent'] not in seen:
        parent = by_msgid[entry['parent']]
        if (parent['info'][0], parent['info'][2]) != (version, total):
            break
        seen.add(parent['msgid'])
        entry = parent
    if entry['info'][1] and total > 1 and entry['parent'] and \
            entry['parent'] not in by_msgid:
        return entry['parent']
    return entry['msgid']


def feed_series(entries, url):
    """Gather the patches and cover letters in the feed into series

    Args:
        entries (list of dict): Patches and cover letters, from parse_feed()
        url (str): URL of the list on lore

    Returns:
        list of dict: Series, newest first, as Patchwork.get_recent_series()
            gives them
    """
    by_msgid = {ent['msgid']: ent for ent in entries}
    found = {}
    for ent in entries:
        found.setdefault(_series_link(ent, by_msgid), []).append(ent)
    out = []
    for link, ents in found.items():
        ents.sort(key=lambda ent: ent['info'][1])
        first = ents[0]
        cover = first if first['info'][1] == 0 else None
        patches = {}
        for ent in ents:
            if ent['info'][1]:
                patches.setdefault(ent['info'][1], ent)
        version, _, total, _ = first['info']
        out.append({
            'id': link, 'name': _series_name(first['subject']),
            'version': version, 'total': total,
            'received_total': len(patches),
            'received_all': len(patches) >= total,
            'date': min(ent['date'] for ent in ents),
            'submitter': first['submitter'],
            'web_url': f'{url}/{link}/',
            'mbox': f'{url}/{link}/t.mbox.gz',
            'project': {},
            'cover_letter': cover and {
                'id': f'{ID_PREFIX}{link}',
                'name': cover['info'][3]},
            'patches': [{'id': f"{ID_PREFIX}{ent['msgid']}",
                         'name': ent['info'][3],
                         'msgid': f"<{ent['msgid']}>"}
                        for _, ent in sorted(patches.items())],
        })
    out.sort(key=lambda ser: ser['date'], reverse=True)
    return out


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


class LoreList(patchwork.SeriesFinder):  # pylint: disable=R0902,R0904
    """Reads series from a mailing list on lore, standing in for Patchwork

    Only what a review and autolinking need is provided. Lore has no patch
    states, so a patch is 'new', or 'accepted' if the upstream branch set
    with use_branch() has its Message-ID.

    Args:
        url (str): URL of the list on lore, e.g.
            'https://lore.kernel.org/devicetree-compiler'
    """
    # Where the series are, for messages
    SOURCE = 'lore'

    def __init__(self, url):
        self.url = url.rstrip('/')
        self.lore = lore.Lore(self.url)
        # Name of the list, e.g. 'devicetree-compiler'
        self.link_name = self.url.rsplit('/', 1)[-1]
        # There is no patchwork project, but searching needs one to be set
        self.proj_id = self.link_name
        self.upstream = None
        # Branch which says which patches are applied, and its repo
        self.applied_ref = None
        self.git_dir = None
        # Message-IDs of the patches in applied_ref, once read
        self._applied = None
        # Number of searches made, for find_series_list()
        self.request_count = 0
        # Messages of each thread read, keyed by the Message-ID of each
        # message in it
        self._threads = {}

    def use_branch(self, ref, git_dir=None):
        """Set the branch which says which patches are applied

        Args:
            ref (str): Branch, e.g. 'us/main'
            git_dir (str or None): Directory containing the git repo, or
                None for the current directory
        """
        self.applied_ref = ref
        self.git_dir = git_dir
        self._applied = None

    def patch_state(self, patch_id):
        """Get the state of a patch, from the branch set with use_branch()

        The branch is read once, the first time a state is needed

        Args:
            patch_id (str): ID of the patch, e.g. 'lore:abc@example.com'

        Returns:
            str: 'accepted' if the branch has the patch, else 'new'
        """
        if self._applied is None:
            self._applied = (applied_msgids(self.applied_ref, self.git_dir)
                             if self.applied_ref else set())
        msgid = str(patch_id).removeprefix(ID_PREFIX)
        return 'accepted' if msgid in self._applied else 'new'

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

    @staticmethod
    def recent_series_path(_project, page, _per_page):
        """Get the path of a page of the feed of the newest series

        Lore gives FEED_PAGE messages in each page, however many series
        they make

        Args:
            _project (str): Project link name (not used)
            page (int): Page number, starting at 1
            _per_page (int): Number of series in each page (not used)

        Returns:
            str: Path within the list, e.g. for read_time()
        """
        path = f'?q={quote_plus(FEED_QUERY)}&x=A'
        if page > 1:
            path += f'&o={(page - 1) * FEED_PAGE}'
        return path

    @staticmethod
    def patch_path(patch_id):
        """Get the path of a patch, e.g. for read_time()

        Args:
            patch_id (str): ID of the patch, 'lore:<msgid>'

        Returns:
            str: Path within the list
        """
        return f'{str(patch_id).removeprefix(ID_PREFIX)}/'

    def read_time(self, subpath):
        """Get when the cached copy of a page of the feed was read

        Args:
            subpath (str): Path, as from recent_series_path()

        Returns:
            float or None: Time it was read, in seconds since the epoch, or
                None if there is no copy
        """
        cache = pwcache.get()
        return cache.fetched(self.url, subpath) if cache else None

    def read_age(self, subpath):
        """Get how long ago the cached copy of a page of the feed was read

        Args:
            subpath (str): Path, as from recent_series_path()

        Returns:
            float or None: Age in seconds, or None if there is no copy
        """
        cache = pwcache.get()
        return cache.age(self.url, subpath) if cache else None

    # pylint: disable-next=R0913
    async def get_recent_series(self, client, project, page=1, per_page=50,
                                max_age=0):
        """Read the newest series sent to the list, newest first

        Args:
            client (aiohttp.ClientSession): Session to use
            project (str): Project link name (not used)
            page (int): Page number, starting at 1
            per_page (int): Number of series in each page (not used)
            max_age (float or None): Oldest cached copy to use, in seconds; 0
                (the default) to read lore, or None to use any

        Returns:
            list of dict: Series, as Patchwork.get_recent_series() gives them

        Raises:
            ValueError: The feed cannot be read
        """
        subpath = self.recent_series_path(project, page, per_page)
        cache = pwcache.get()
        data = (cache.get(self.url, subpath, max_age)
                if cache and max_age != 0 else None)
        if data is None:
            content = await self.lore.read(client, subpath, kind='feed')
            if not content:
                raise ValueError(f'Cannot read the newest series from '
                                 f'{self.url}')
            data = await asyncio.gather(
                *[self.complete_series(client, ser)
                  for ser in feed_series(parse_feed(content), self.url)])
            if cache:
                cache.put(self.url, subpath, data)
        return data

    @staticmethod
    def _search_path(text):
        """Get the path of a search, e.g. for read_time()

        Args:
            text (str): Text to search for (see search_query())

        Returns:
            str: Path within the list
        """
        return f'?q={quote_plus(search_query(text))}&x=A'

    async def search_series(self, client, project, text, max_age=SEARCH_KEEP):
        """Search the list for series whose subjects match some text

        Only the newest FEED_PAGE patches and cover letters which match are
        read. The series are not completed from their threads (see
        complete_series()), so some may lack patches, but each has its name
        and number of patches

        Args:
            client (aiohttp.ClientSession): Session to use
            project (str): Project link name (not used)
            text (str): Text to search for (see search_query())
            max_age (float or None): Oldest cached copy to use, in seconds; 0
                to read lore, or None to use any

        Returns:
            list of dict: Series, newest first, as from get_recent_series()

        Raises:
            ValueError: There is no text, or lore cannot be searched
        """
        del project
        subpath = self._search_path(text)
        cache = pwcache.get()
        data = (cache.get(self.url, subpath, max_age)
                if cache and max_age != 0 else None)
        if data is None:
            content = await self.lore.read(client, subpath, kind='search')
            if content is None:
                raise ValueError(f'Cannot search {self.url}')
            # Lore says that nothing matches with a 404
            data = (feed_series(parse_feed(content), self.url) if content
                    else [])
            if cache:
                cache.put(self.url, subpath, data)
        return data

    async def complete_series(self, client, ser):
        """Fill in a series which is only partly in a page of the feed

        A series may lack its cover letter, or some patches, because they
        are in another page of the feed, or were missed by lore's search. Its
        thread then gives the whole series.

        Args:
            client (aiohttp.ClientSession): Session to use
            ser (dict): Series, as from feed_series()

        Returns:
            dict: The series, filled in from its thread if needed
        """
        own = any(pat['id'] == f"{ID_PREFIX}{ser['id']}"
                  for pat in ser['patches'])
        if ser['received_all'] and (ser['cover_letter'] or own):
            return ser
        try:
            full = await self.get_series(client, ser['id'])
        except ValueError:
            return ser
        return {**ser, **{key: full[key] for key in (
            'name', 'version', 'total', 'received_total', 'received_all',
            'date', 'submitter', 'cover_letter', 'patches')}}

    @staticmethod
    async def follow_events(_client, _project=None):
        """Follow the changes to patches, which lore does not record

        Returns:
            None: Always, since events are not followed
        """
        return None

    def get_states(self, patch_ids):
        """Get the states of patches, from the branch set with use_branch()

        Args:
            patch_ids (list of str): Patch IDs, e.g. ['lore:abc@example.com']

        Returns:
            dict: key (str): Patch ID, for each patch which is applied
                value (str): 'accepted'
        """
        if not self.applied_ref:
            return {}
        return {pid: 'accepted' for pid in patch_ids
                if self.patch_state(pid) == 'accepted'}

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
            max_age (float or None): 0 to read the thread from lore again,
                e.g. for patches which have arrived since; otherwise the copy
                read already is used

        Returns:
            dict: Series, as Patchwork.get_series() gives it, with 'id' set to
                the Message-ID

        Raises:
            ValueError: The message is not a cover letter or first patch
        """
        link = parse_link(link)
        if max_age == 0:
            self._threads.pop(link, None)
        root = await self._message(client, link)
        info = subject_info(root)
        if not info:
            raise ValueError(f"Message '{link}' is not a patch or cover "
                             'letter')
        version, seq, total, _ = info
        if seq > 1:
            raise ValueError(
                f"Message '{link}' is patch {seq} of {total}; give the cover "
                'letter or the first patch')

        found = _find_patches(await self._thread(client, link), root, info)
        cmt = lore.to_comment(root)
        patches = [self._entry(found[num]) for num in sorted(found)]
        return {
            'id': link, 'name': _series_name(str(root.get('Subject', ''))),
            'version': version, 'total': total,
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

        Lore has no states, so each patch is 'new', or 'accepted' if it is
        applied (see patch_state())

        Args:
            client (aiohttp.ClientSession): Session to use
            link (str): Message-ID of the series

        Returns:
            list of str: State of each patch
        """
        data = await self.get_series(client, link)
        return [self.patch_state(pat['id']) for pat in data['patches']]

    @contextlib.contextmanager
    def collect_stats(self):
        """Count the requests to lore across a range of calls

        Yields:
            SimpleNamespace: With request_count set to the number of requests
                once the range is done
        """
        stats = SimpleNamespace(request_count=0)
        start = self.lore.request_count
        try:
            yield stats
        finally:
            stats.request_count = self.lore.request_count - start

    async def get_series_url(self, link):
        """Get the URL of a series on the list

        Args:
            link (str): Message-ID of the series

        Returns:
            str: URL of the thread
        """
        return f'{self.url}/{link}/'

    async def series_get_state(self, client, link, read_comments,
                               read_cover_comments):
        """Read the patches of a series and their replies, for its status

        Lore has no states, so each patch is 'new', or 'accepted' if it is
        applied (see patch_state()). The thread is read from lore again, to
        see the patches and replies which have arrived since

        Args:
            client (aiohttp.ClientSession): Session to use
            link (str): Message-ID of the series
            read_comments (bool): True to read the replies to the patches
            read_cover_comments (bool): True to read the replies to the cover
                letter

        Returns:
            tuple:
                COVER or None: Cover letter, or None if there is none or
                    read_cover_comments is False
                list of patchwork.Patch: Patches
        """
        data = await self.get_series(client, link, max_age=0)
        patches = []
        for info in data['patches']:
            if read_comments:
                patch = await self.get_patch(client, info['id'])
                comments = await self.get_patch_comments(client, info['id'])
                pat = patchwork.Patch(info['id'],
                                      self.patch_state(info['id']), patch,
                                      comments, info)
            else:
                pat = patchwork.Patch(info['id'],
                                      self.patch_state(info['id']),
                                      series_data=info)
                pat.raw_subject = info['name']
            patches.append(pat)

        cover = None
        letter = data['cover_letter']
        if read_cover_comments and letter:
            comments = await self.get_cover_comments(client, letter['id'])
            cover = patchwork.COVER(letter['id'], len(comments),
                                    letter['name'], comments)
        return cover, patches

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
            dict: Patch, as lore.to_patch() gives it, named as in its series
                and linking to it on the list
        """
        del max_age, since
        msgid = str(patch_id).removeprefix(ID_PREFIX)
        msg = await self._message(client, msgid)
        patch = lore.to_patch(msg, str(patch_id))
        patch['web_url'] = f'{self.url}/{msgid}/'
        info = subject_info(msg)
        if info:
            patch['name'] = info[3]
        return patch

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
        """Search for the series whose cover letter or first patch has a title

        Lore is always searched, not the cache, since this is used to find a
        series just sent

        Args:
            client (aiohttp.ClientSession): Session to use
            desc (str): Title to search for, e.g. 'dm: Tidy up'

        Returns:
            list of dict: Series, newest first, as from get_recent_series(),
                except that the name is just the title, without the prefix,
                as patchwork gives it, e.g. 'dm: Tidy up'
        """
        tout.info(f"Searching for '{desc}'")
        self.request_count += 1
        found = await self.search_series(client, None, desc, max_age=0)
        return [{**ser, 'name': RE_PREFIX.sub('', ser['name'])}
                for ser in found]

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
