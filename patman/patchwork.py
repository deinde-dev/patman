# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2025 Simon Glass <sjg@chromium.org>
#
"""Provides a basic API for the patchwork server
"""

import asyncio
import base64
import re
import socket
from urllib.parse import quote_plus, urlparse
import urllib.request

import aiohttp
from collections import namedtuple

from u_boot_pylib import terminal
from u_boot_pylib import tout

from patman import pwcache
from patman import traffic

# Information passed to series_get_states()
# link (str): Patchwork link for series
# series_id (int): Series ID in database
# series_name (str): Series name
# version (int): Version number of series
# show_comments (bool): True to show comments
# show_cover_comments (bool): True to show cover-letter comments
STATE_REQ = namedtuple(
    'state_req',
    'link,series_id,series_name,version,show_comments,show_cover_comments')

# Responses from series_get_states()
# int: ser_ver ID number
# COVER: Cover-letter info
# list of Patch: Information on each patch in the series
# list of dict: patches, see get_series()['patches']
STATE_RESP = namedtuple('state_resp', 'svid,cover,patches,patch_list')

# Information about a cover-letter on patchwork
# id (int): Patchwork ID of cover letter
# state (str): Current state, e.g. 'accepted'
# num_comments (int): Number of comments
# name (str): Series name
# comments (list of dict): Comments
COVER = namedtuple('cover', 'id,num_comments,name,comments')

# Number of retries
RETRIES = 3

# HTTP status codes which suggest that a request may work if tried again
RETRY_STATUS = (502, 503, 504)

# Exception which aiohttp raises when a name does not resolve, if it has one.
# With aiodns installed, the underlying error is an OSError rather than a
# socket.gaierror, so this is the only way to tell
DNS_ERROR = getattr(aiohttp, 'ClientConnectorDNSError', ())

# Seconds to wait for a request to complete before trying again, which can be
# changed with the 'patchwork_timeout' setting
REQUEST_TIMEOUT = 30

# Seconds for which each kind of response from patchwork is used again from
# the cache, rather than read again (see pwcache)
#
# A patch's text does not change, but its state may
PATCH_MAX_AGE = 60 * 60
# A cover letter has no state
COVER_MAX_AGE = 24 * 60 * 60
# New comments may arrive at any time
COMMENTS_MAX_AGE = 5 * 60
# A series gains patches until patchwork has them all (received_all), then
# does not change, so is then kept however old it is
SERIES_MAX_AGE = 10 * 60
# Patchwork adds review tags to a series' mbox as they arrive
MBOX_MAX_AGE = 60 * 60
# The projects on a server rarely change
PROJECTS_MAX_AGE = 24 * 60 * 60

# Maximum number of requests to send to patchwork at once. Busy servers may
# block a client which opens a lot of connections, so keep this small
MAX_CONCURRENT = 4

# Patches which are part of a multi-patch series are shown with a prefix like
# [prefix, version, sequence], for example '[RFC, v2, 3/5]'. All but the last
# part is optional. This decodes the string into groups. For single patches
# the [] part is not present:
# Groups: (ignore, ignore, ignore, prefix, version, sequence, subject)
RE_PATCH = re.compile(r'(\[(((.*),)?(.*),)?(.*)\]\s)?(.*)$')

# This decodes the sequence string into a patch number and patch count
RE_SEQ = re.compile(r'(\d+)/(\d+)')


class Patch(dict):
    """Models a patch in patchwork

    This class records information obtained from patchwork

    Some of this information comes from the 'Patch' column:

        [RFC,v2,1/3] dm: Driver and uclass changes for tiny-dm

    This shows the prefix, version, seq, count and subject.

    The other properties come from other columns in the display.

    Properties:
        pid (str): ID of the patch (typically an integer)
        seq (int): Sequence number within series (1=first) parsed from sequence
            string
        count (int): Number of patches in series, parsed from sequence string
        raw_subject (str): Entire subject line, e.g.
            "[1/2,v2] efi_loader: Sort header file ordering"
        prefix (str): Prefix string or None (e.g. 'RFC')
        version (str): Version string or None (e.g. 'v2')
        raw_subject (str): Raw patch subject
        subject (str): Patch subject with [..] part removed (same as commit
            subject)
        data (dict or None): Patch data:
    """
    def __init__(self, pid, state=None, data=None, comments=None,
                 series_data=None):
        super().__init__()
        self.id = pid  # Use 'id' to match what the Rest API provides
        self.seq = None
        self.count = None
        self.prefix = None
        self.version = None
        self.raw_subject = None
        self.subject = None
        self.state = state
        self.data = data
        self.comments = comments
        self.series_data = series_data
        self.name = None

    # These make us more like a dictionary
    def __setattr__(self, name, value):
        self[name] = value

    def __getattr__(self, name):
        return self[name]

    def __hash__(self):
        return hash(frozenset(self.items()))

    def __str__(self):
        return self.raw_subject

    def parse_subject(self, raw_subject):
        """Parse the subject of a patch into its component parts

        See RE_PATCH for details. The parsed info is placed into seq, count,
        prefix, version, subject

        Args:
            raw_subject (str): Subject string to parse

        Raises:
            ValueError: the subject cannot be parsed
        """
        self.raw_subject = raw_subject.strip()
        mat = RE_PATCH.search(raw_subject.strip())
        if not mat:
            raise ValueError(f"Cannot parse subject '{raw_subject}'")
        self.prefix, self.version, seq_info, self.subject = mat.groups()[3:]
        mat_seq = RE_SEQ.match(seq_info) if seq_info else False
        if mat_seq is None:
            self.version = seq_info
            seq_info = None
        if self.version and not self.version.startswith('v'):
            self.prefix = self.version
            self.version = None
        if seq_info:
            if mat_seq:
                self.seq = int(mat_seq.group(1))
                self.count = int(mat_seq.group(2))
        else:
            self.seq = 1
            self.count = 1


class Review:
    """Represents a single review email collected in Patchwork

    Patches can attract multiple reviews. Each consists of an author/date and
    a variable number of 'snippets', which are groups of quoted and unquoted
    text.
    """
    def __init__(self, meta, snippets):
        """Create new Review object

        Args:
            meta (str): Text containing review author and date
            snippets (list): List of snippets in th review, each a list of text
                lines
        """
        self.meta = ' : '.join([line for line in meta.splitlines() if line])
        self.snippets = snippets


def get_proxy(url):
    """Get the proxy to use for a URL, if any

    This uses the usual environment variables, such as HTTPS_PROXY, so that
    patchwork can be reached through a tunnel when it cannot be reached
    directly. Hosts in NO_PROXY are reached directly. Unlike the 'trust_env'
    option of aiohttp, this does not read ~/.netrc

    Args:
        url (str): URL to be requested

    Returns:
        str: URL of the proxy, or None to connect directly
    """
    parsed = urlparse(url)
    proxy = urllib.request.getproxies().get(parsed.scheme)
    if proxy and not urllib.request.proxy_bypass(parsed.hostname):
        return proxy
    return None


class Patchwork:
    """Class to handle communication with patchwork
    """
    def __init__(self, url, show_progress=True, single_thread=False):
        """Set up a new patchwork handler

        Args:
            url (str): URL of patchwork server, e.g.
               'https://patchwork.ozlabs.org'
        """
        self.url = url.rstrip('/') if url else url
        self.fake_request = None
        self.proj_id = None
        self.link_name = None
        self.upstream = None
        self._show_progress = show_progress
        self.semaphore = asyncio.Semaphore(
            1 if single_thread else MAX_CONCURRENT)
        self.request_count = 0
        # Cache of responses, None to use the default one, or False for none
        self.cache = None
        # Number of responses taken from the cache rather than patchwork
        self.cache_hits = 0

    async def _request(self, client, subpath):
        """Call the patchwork API and return the result as JSON

        Args:
            client (aiohttp.ClientSession): Session to use
            subpath (str): URL subpath to use

        Returns:
            dict: Json result

        Raises:
            ValueError: the URL could not be read
        """
        # print('subpath', subpath)
        self.request_count += 1
        if self.fake_request:
            return self.fake_request(subpath)

        full_url = f'{self.url}/api/1.2/{subpath}'
        async with self.semaphore:
            # print('full_url', full_url)
            for i in range(RETRIES + 1):
                try:
                    traffic.add(self.url, requests=1)
                    async with client.get(
                            full_url, proxy=get_proxy(full_url),
                            timeout=aiohttp.ClientTimeout(
                                total=REQUEST_TIMEOUT)) as response:
                        traffic.add(self.url,
                                    nbytes=len(await response.read()))
                        if response.status in RETRY_STATUS and i < RETRIES:
                            # The server is overloaded, so try again later
                            await asyncio.sleep(i + 1)
                            continue
                        if response.status != 200:
                            raise ValueError(
                                f"Could not read URL '{full_url}' "
                                f'(status {response.status})')
                        result = await response.json()
                        # print('- done', full_url)
                        return result
                    break
                except aiohttp.ClientConnectorError as exc:
                    # A name which does not resolve will not start working,
                    # but a busy server may fail to accept a connection
                    if isinstance(exc, DNS_ERROR) or \
                            isinstance(exc.os_error, socket.gaierror) or \
                            i == RETRIES:
                        raise
                    await asyncio.sleep(i + 1)
                except asyncio.TimeoutError as exc:
                    # The server may stall when busy. Say which one, since
                    # the exception itself has no message
                    if i == RETRIES:
                        raise asyncio.TimeoutError(
                            f"Patchwork '{self.url}' did not respond within "
                            f'{REQUEST_TIMEOUT} seconds, after '
                            f'{RETRIES + 1} tries') from exc
                    await asyncio.sleep(i + 1)
                except (aiohttp.ServerDisconnectedError,
                        aiohttp.ClientOSError):
                    # The server may drop or reset connections when busy
                    if i == RETRIES:
                        raise
                    await asyncio.sleep(i + 1)

    def _get_cache(self):
        """Get the cache to use, opening the default one the first time

        Returns:
            pwcache.Cache or None: Cache, or None if there is none
        """
        if self.cache is None:
            self.cache = pwcache.get() or False
        return self.cache or None

    def _from_cache(self, subpath, max_age, keep=None):
        """Get a response from the cache, if there is a suitable one

        Args:
            subpath (str): Path of the request
            max_age (float or None): Oldest response to use, in seconds; 0 to
                use none (unless 'keep' says it is current), or None to use any
            keep (callable or None): Function which is passed a cached
                response and returns True if it is current however old it is,
                e.g. because it cannot change

        Returns:
            dict or list or str or None: Response, or None if there is none
        """
        cache = self._get_cache()
        if not cache or (max_age == 0 and not keep):
            return None
        data = cache.get(self.url, subpath, max_age, keep)
        if data is not None:
            self.cache_hits += 1
            cache.add_hit(self.url)
        return data

    def _to_cache(self, subpath, data):
        """Keep a response in the cache, if there is one

        Args:
            subpath (str): Path of the request
            data (dict or list or str): Response
        """
        cache = self._get_cache()
        if cache:
            cache.put(self.url, subpath, data)

    async def _cached_request(self, client, subpath, max_age, keep=None):
        """Call the patchwork API, using a recent enough response if cached

        Args:
            client (aiohttp.ClientSession): Session to use
            subpath (str): URL subpath to use
            max_age (float or None): Oldest cached response to use, in
                seconds; 0 to always read patchwork, still caching the result,
                or None to use any cached response
            keep (callable or None): Function which is passed a cached
                response and returns True if it is current however old it is

        Returns:
            dict: Json result

        Raises:
            ValueError: the URL could not be read
        """
        data = self._from_cache(subpath, max_age, keep)
        if data is None:
            data = await self._request(client, subpath)
            self._to_cache(subpath, data)
        return data

    def read_time(self, subpath):
        """Get when the cached copy of a response was read from patchwork

        Args:
            subpath (str): Path of the request, as from patch_path() etc.

        Returns:
            float or None: Time it was read, in seconds since the epoch, or
                None if there is no copy
        """
        cache = self._get_cache()
        return cache.fetched(self.url, subpath) if cache else None

    def read_age(self, subpath):
        """Get how long ago the cached copy of a response was read

        Args:
            subpath (str): Path of the request, as from patch_path() etc.

        Returns:
            float or None: Age in seconds, or None if there is no copy
        """
        cache = self._get_cache()
        return cache.age(self.url, subpath) if cache else None

    @staticmethod
    def patch_path(patch_id):
        """Get the request path for a patch, e.g. for read_time()"""
        return f'patches/{patch_id}/'

    @staticmethod
    def comments_path(kind, item_id):
        """Get the request path for the comments on a patch or cover letter

        Args:
            kind (str): 'patch' or 'cover'
            item_id (str or int): Patchwork ID
        """
        return f"{'patches' if kind == 'patch' else 'covers'}/{item_id}/comments/"

    @staticmethod
    def recent_series_path(project, page, per_page):
        """Get the request path for a page of the most recent series"""
        return (f'series/?project={project}&order=-date&page={page}'
                f'&per_page={per_page}')

    @staticmethod
    def set_timeout(secs):
        """Set the time to wait for each request before trying again

        Args:
            secs (float): Timeout in seconds
        """
        global REQUEST_TIMEOUT  # pylint: disable=W0603
        REQUEST_TIMEOUT = secs

    @staticmethod
    def for_testing(func):
        """Get an instance to use for testing

        Args:
            func (function): Function to call to handle requests. The function
                is passed a URL and is expected to return a dict with the
                resulting data

        Returns:
            Patchwork: testing instance
        """
        pwork = Patchwork(None, show_progress=False)
        pwork.fake_request = func
        pwork.url = 'https://my-fake-url'
        pwork.cache = False
        return pwork

    class _Stats:
        def __init__(self, parent):
            self.parent = parent
            self.request_count = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            self.request_count = self.parent.request_count

    def collect_stats(self):
        """Context manager to count requests across a range of patchwork calls

        Usage:
            pwork = Patchwork(...)
            with pwork.count_requests() as counter:
                pwork.something()
            print(f'{counter.count} requests')
        """
        self.request_count = 0
        return self._Stats(self)

    async def get_projects(self, max_age=PROJECTS_MAX_AGE):
        """Get a list of projects on the server

        Reads all the pages of projects, since some servers have more
        projects than fit on one page.

        Args:
            max_age (float or None): Oldest cached copy to use, in seconds; 0
                to read patchwork, or None to use any

        Returns:
            list of dict, one for each project
                'name' (str): Project name, e.g. 'U-Boot'
                'id' (int): Project ID, e.g. 9
                'link_name' (str): Project's link-name, e.g. 'uboot'
        """
        per_page = 100
        projects = []
        async with aiohttp.ClientSession() as client:
            page = 1
            while True:
                res = await self._cached_request(
                    client, f'projects/?page={page}&per_page={per_page}',
                    max_age)
                projects += res
                if len(res) < per_page:
                    break
                page += 1
        return projects

    async def query_series(self, client, desc):
        """Query series by name

        Args:
            client (aiohttp.ClientSession): Session to use
            desc: String to search for

        Return:
            list of series matches, each a dict, see get_series()
        """
        query = quote_plus(desc, safe=':')
        subpath = f'series/?project={self.proj_id}&q={query}'
        tout.info(f"Searching for '{desc}'")
        tout.debug(f'  GET {self.url}/api/1.2/{subpath}')
        return await self._request(client, subpath)

    async def _find_series(self, client, svid, ser_id, version, ser):
        """Find a series on the server

        Args:
            client (aiohttp.ClientSession): Session to use
            svid (int): ser_ver ID
            ser_id (int): series ID
            version (int): Version number to search for
            ser (Series): Contains description (cover-letter title)

        Returns:
            tuple:
                int: ser_ver ID (as passed in)
                int: series ID (as passed in)
                str: Series link, or None if not found
                list of dict, or None if found
                    each dict is the server result from a possible series
        """
        desc = ser.desc
        name_found = []

        # Do a series query on the description
        res = await self.query_series(client, desc)
        for pws in res:
            if pws['name'] == desc:
                if int(pws['version']) == version:
                    return svid, ser_id, pws['id'], None
                name_found.append(pws)

        # When there is no cover letter, patchwork uses the first patch as the
        # series name
        cmt = ser.commits[0]

        res = await self.query_series(client, cmt.subject)
        for pws in res:
            patch = Patch(0)
            patch.parse_subject(pws['name'])
            if patch.subject == cmt.subject:
                if int(pws['version']) == version:
                    return svid, ser_id, pws['id'], None
                name_found.append(pws)

        return svid, ser_id, None, name_found or res

    async def find_series(self, ser, version):
        """Find a series based on its description and version

        Args:
            ser (Series): Contains description (cover-letter title)
            version (int): Version number

        Return: tuple:
            tuple:
                str: Series ID, or None if not found
                list of dict, or None if found
                    each dict is the server result from a possible series
            int: number of server requests done
        """
        async with aiohttp.ClientSession() as client:
            # We don't know the svid and it isn't needed, so use -1
            _, _, link, options = await self._find_series(client, -1, -1,
                                                          version, ser)
        return link, options

    async def find_series_list(self, to_find):
        """Find the link for each series in a list

        Args:
            to_find (dict of svids to sync):
                key (int): ser_ver ID
                value (tuple):
                    int: Series ID
                    int: Series version
                    str: Series link
                    str: Series description

        Return: tuple:
            list of tuple, one for each item in to_find:
                int: ser_ver_ID
                int: series ID
                int: Series version
                str: Series link, or None if not found
                list of dict, or None if found
                    each dict is the server result from a possible series
            int: number of server requests done
        """
        self.request_count = 0
        async with aiohttp.ClientSession() as client:
            tasks = [asyncio.create_task(
                self._find_series(client, svid, ser_id, version, desc))
                for svid, (ser_id, version, link, desc) in to_find.items()]
            results = await asyncio.gather(*tasks)

        return results, self.request_count

    def project_set(self, project_id, link_name):
        """Set the project ID

        The patchwork server has multiple projects. This allows the ID and
        link_name of the relevant project to be selected

        This function is used for testing

        Args:
            project_id (int): Project ID to use, e.g. 6
            link_name (str): Name to use for project URL links, e.g. 'uboot'
        """
        self.proj_id = project_id
        self.link_name = link_name

    async def get_recent_series(self, client, project, page=1, per_page=50):
        """Read the most recent series in a project, newest first

        Args:
            client (aiohttp.ClientSession): Session to use
            project (str): Project link name, e.g. 'uboot'
            page (int): Page number, starting at 1
            per_page (int): Number of series in each page

        Returns:
            list of dict: Series information, as for get_series()
        """
        return await self._request(
            client, f'series/?project={project}&order=-date&page={page}'
            f'&per_page={per_page}')

    async def get_series(self, client, link, max_age=SERIES_MAX_AGE):
        """Read information about a series

        Args:
            client (aiohttp.ClientSession): Session to use
            link (str): Patchwork series ID
            max_age (float or None): Oldest cached copy to use, in seconds,
                unless patchwork has all its patches, since it then does not
                change; 0 to read patchwork unless so, or None to use any

        Returns: dict containing patchwork's series information
            id (int): series ID unique across patchwork instance, e.g. 3
            url (str): Full URL, e.g.
                'https://patchwork.ozlabs.org/api/1.2/series/3/'
            web_url (str): Full URL, e.g.
                'https://patchwork.ozlabs.org/project/uboot/list/?series=3
            project (dict): project information (id, url, name, link_name,
                list_id, list_email, etc.
            name (str): Series name, e.g. '[U-Boot] moveconfig: fix error'
            date (str): Date, e.g. '2017-08-27T08:00:51'
            submitter (dict): id, url, name, email, e.g.:
                "id": 6125,
                "url": "https://patchwork.ozlabs.org/api/1.2/people/6125/",
                "name": "Chris Packham",
                "email": "judge.packham@gmail.com"
            version (int): Version number
            total (int): Total number of patches based on subject
            received_total (int): Total patches received by patchwork
            received_all (bool): True if all patches were received
            mbox (str): URL of mailbox, e.g.
                'https://patchwork.ozlabs.org/series/3/mbox/'
            cover_letter (dict) or None, e.g.:
                "id": 806215,
                "url": "https://patchwork.ozlabs.org/api/1.2/covers/806215/",
                "web_url": "https://patchwork.ozlabs.org/project/uboot/cover/
                    20170827094411.8583-1-judge.packham@gmail.com/",
                "msgid": "<20170827094411.8583-1-judge.packham@gmail.com>",
                "list_archive_url": null,
                "date": "2017-08-27T09:44:07",
                "name": "[U-Boot,v2,0/4] usb: net: Migrate USB Ethernet",
                "mbox": "https://patchwork.ozlabs.org/project/uboot/cover/
                    20170827094411.8583-1-judge.packham@gmail.com/mbox/"
            patches (list of dict), each e.g.:
                "id": 806202,
                "url": "https://patchwork.ozlabs.org/api/1.2/patches/806202/",
                "web_url": "https://patchwork.ozlabs.org/project/uboot/patch/
                    20170827080051.816-1-judge.packham@gmail.com/",
                "msgid": "<20170827080051.816-1-judge.packham@gmail.com>",
                "list_archive_url": null,
                "date": "2017-08-27T08:00:51",
                "name": "[U-Boot] moveconfig: fix error message do_autoconf()",
                "mbox": "https://patchwork.ozlabs.org/project/uboot/patch/
                    20170827080051.816-1-judge.packham@gmail.com/mbox/"
        """
        # Once patchwork has all the patches, the series does not change
        return await self._cached_request(
            client, f'series/{link}/', max_age,
            keep=lambda data: data.get('received_all'))

    async def get_series_patch_states(self, client, link):
        """Get the patchwork state of each patch in a series

        Args:
            client (aiohttp.ClientSession): Session to use
            link (str): Patchwork series ID

        Returns:
            list of str: State slug for each patch, e.g. ['new', 'rfc']
        """
        res = await self._request(client, f'patches/?series={link}')
        return [patch.get('state') for patch in res]

    async def get_series_mbox(self, client, link, max_age=MBOX_MAX_AGE):
        """Download the raw mbox file for a series.

        The mbox URL lives directly under '/series/<link>/mbox/' rather
        than the JSON API, so this fetches the bytes directly rather
        than routing through self._request(). It is cached, base64-encoded,
        since the cache holds JSON

        Args:
            client (aiohttp.ClientSession): Session to use
            link (str): Patchwork series link/ID
            max_age (float or None): Oldest cached copy to use, in seconds; 0
                to read patchwork, or None to use any

        Returns:
            bytes: Raw mbox content

        Raises:
            ValueError: if the download fails
        """
        subpath = f'series/{link}/mbox/'
        kept = self._from_cache(subpath, max_age)
        if kept is not None:
            return base64.b64decode(kept)
        self.request_count += 1
        full_url = f'{self.url}/{subpath}'
        async with self.semaphore:
            traffic.add(self.url, requests=1)
            async with client.get(
                    full_url, proxy=get_proxy(full_url)) as response:
                traffic.add(self.url, nbytes=len(await response.read()))
                if response.status != 200:
                    raise ValueError(
                        f'Failed to download mbox: HTTP {response.status}')
                content = await response.read()
        self._to_cache(subpath, base64.b64encode(content).decode('ascii'))
        return content

    async def search_patches(self, client, query, project_id=None,
                             per_page=20):
        """Search patches by free-text query, most-recent first.

        Args:
            client (aiohttp.ClientSession): Session to use
            query (str): Text to match against patch titles
            project_id (int): Project ID to scope to, or None to use
                self.proj_id
            per_page (int): Maximum number of results to return

        Returns:
            list of dict: Patch records matching the query
        """
        if project_id is None:
            project_id = self.proj_id
        subpath = (f'patches/?project={project_id}&q={query}'
                   f'&order=-date&per_page={per_page}')
        return await self._request(client, subpath)

    async def get_patch(self, client, patch_id, max_age=PATCH_MAX_AGE):
        """Read information about a patch

        A copy kept from an earlier read is used if it is recent enough. The
        text of a patch does not change, but its state and delegate may, so
        pass max_age=0 when these must be current.

        Args:
            client (aiohttp.ClientSession): Session to use
            patch_id (str): Patchwork patch ID
            max_age (float or None): Oldest kept copy to use, in seconds; 0 to
                read patchwork, or None to use any kept copy

        Returns: dict containing patchwork's patch information
            "id": 185,
            "url": "https://patchwork.ozlabs.org/api/1.2/patches/185/",
            "web_url": "https://patchwork.ozlabs.org/project/cbe-oss-dev/patch/
                200809050416.27831.adetsch@br.ibm.com/",
            project (dict): project information (id, url, name, link_name,
                    list_id, list_email, etc.
            "msgid": "<200809050416.27831.adetsch@br.ibm.com>",
            "list_archive_url": null,
            "date": "2008-09-05T07:16:27",
            "name": "powerpc/spufs: Fix possible scheduling of a context",
            "commit_ref": "b2e601d14deb2083e2a537b47869ab3895d23a28",
            "pull_url": null,
            "state": "accepted",
            "archived": false,
            "hash": "bc1c0b80d7cff66c0d1e5f3f8f4d10eb36176f0d",
            "submitter": {
                "id": 93,
                "url": "https://patchwork.ozlabs.org/api/1.2/people/93/",
                "name": "Andre Detsch",
                "email": "adetsch@br.ibm.com"
            },
            "delegate": {
                "id": 1,
                "url": "https://patchwork.ozlabs.org/api/1.2/users/1/",
                "username": "jk",
                "first_name": "Jeremy",
                "last_name": "Kerr",
                "email": "jk@ozlabs.org"
            },
            "mbox": "https://patchwork.ozlabs.org/project/cbe-oss-dev/patch/
                200809050416.27831.adetsch@br.ibm.com/mbox/",
            "series": [],
            "comments": "https://patchwork.ozlabs.org/api/patches/185/
                comments/",
            "check": "pending",
            "checks": "https://patchwork.ozlabs.org/api/patches/185/checks/",
            "tags": {},
            "related": [],
            "headers": {...}
            "content": "We currently have a race when scheduling a context
                after we have found a runnable context in spusched_tick, the
                context may have been scheduled by spu_activate().

                This may result in a panic if we try to unschedule a context
                been freed in the meantime.

                This change exits spu_schedule() if the context has already
                scheduled, so we don't end up scheduling it twice.

                Signed-off-by: Andre Detsch <adetsch@br.ibm.com>",
            "diff": '''Index: spufs/arch/powerpc/platforms/cell/spufs/sched.c
                =======================================================
                --- spufs.orig/arch/powerpc/platforms/cell/spufs/sched.c
                +++ spufs/arch/powerpc/platforms/cell/spufs/sched.c
                @@ -727,7 +727,8 @@ static void spu_schedule(struct spu *spu
                 \t/* not a candidate for interruptible because it's called
                 \t   from the scheduler thread or from spu_deactivate */
                 \tmutex_lock(&ctx->state_mutex);
                -\t__spu_schedule(spu, ctx);
                +\tif (ctx->state == SPU_STATE_SAVED)
                +\t\t__spu_schedule(spu, ctx);
                 \tspu_release(ctx);
                 }
                '''
            "prefixes": ["3/3", ...]
        """
        return await self._cached_request(client, self.patch_path(patch_id),
                                          max_age)

    async def get_patch_comments(self, client, patch_id,
                                 max_age=COMMENTS_MAX_AGE):
        """Read comments about a patch

        Args:
            client (aiohttp.ClientSession): Session to use
            patch_id (str): Patchwork patch ID
            max_age (float or None): Oldest cached copy to use, in seconds; 0
                to read patchwork, or None to use any

        Returns: list of dict: list of comments:
            id (int): series ID unique across patchwork instance, e.g. 3331924
            web_url (str): Full URL, e.g.
                'https://patchwork.ozlabs.org/comment/3331924/'
            msgid (str): Message ID, e.g.
                '<d2526c98-8198-4b8b-ab10-20bda0151da1@gmx.de>'
            list_archive_url: (unknown?)
            date (str): Date, e.g. '2024-06-20T13:38:03'
            subject (str): email subject, e.g. 'Re: [PATCH 3/5] buildman:
                Support building within a Python venv'
            date (str): Date, e.g. '2017-08-27T08:00:51'
            submitter (dict): id, url, name, email, e.g.:
                "id": 61270,
                "url": "https://patchwork.ozlabs.org/api/people/61270/",
                "name": "Heinrich Schuchardt",
                "email": "xypron.glpk@gmx.de"
            content (str): Content of email, e.g. 'On 20.06.24 15:19,
                Simon Glass wrote:
                >...'
            headers: dict: email headers, see get_cover() for an example
        """
        return await self._cached_request(
            client, self.comments_path('patch', patch_id), max_age)

    async def get_cover(self, client, cover_id, max_age=COVER_MAX_AGE):
        """Read information about a cover letter

        Args:
            client (aiohttp.ClientSession): Session to use
            cover_id (int): Patchwork cover-letter ID
            max_age (float or None): Oldest cached copy to use, in seconds; 0
                to read patchwork, or None to use any

        Returns: dict containing patchwork's cover-letter information:
            id (int): cover-letter ID unique across patchwork instance
            url (str): Full URL, e.g. 'https://patchwork.ozlabs.org/api/1.2/covers/2054866/'
            web_url (str): Full URL, e.g. 'https://patchwork.ozlabs.org/project/uboot/cover/20250304130947.109799-1-sjg@chromium.org/'
            project (dict): project information (id, url, name, link_name,
                list_id, list_email, etc.
            msgid (str): Message ID, e.g. '20250304130947.109799-1-sjg@chromium.org>'
            list_archive_url (?)
            date (str): Date, e.g. '2017-08-27T08:00:51'
            name (str): Series name, e.g. '[U-Boot] moveconfig: fix error'
            submitter (dict): id, url, name, email, e.g.:
                "id": 6170,
                "url": "https://patchwork.ozlabs.org/api/1.2/people/6170/",
                "name": "Simon Glass",
                "email": "sjg@chromium.org"
            mbox (str): URL to mailbox, e.g. 'https://patchwork.ozlabs.org/project/uboot/cover/20250304130947.109799-1-sjg@chromium.org/mbox/'
            series (list of dict) each e.g.:
                "id": 446956,
                "url": "https://patchwork.ozlabs.org/api/1.2/series/446956/",
                "web_url": "https://patchwork.ozlabs.org/project/uboot/list/?series=446956",
                "date": "2025-03-04T13:09:37",
                "name": "binman: Check code-coverage requirements",
                "version": 1,
                "mbox": "https://patchwork.ozlabs.org/series/446956/mbox/"
            comments: Web URL to comments: 'https://patchwork.ozlabs.org/api/covers/2054866/comments/'
            headers (dict): The email headers, each a str, or a list of str
                for a header which appears more than once (e.g. 'Received').
                For example:
                "From": "Simon Glass <sjg@chromium.org>",
                "To": "U-Boot Mailing List <u-boot@lists.denx.de>",
                "Cc": "Simon Glass <sjg@chromium.org>, Tom Rini <trini@konsulko.com>",
                "Subject": "[PATCH 0/7] binman: Check code-coverage requirements",
                "Date": "Tue,  4 Mar 2025 06:09:37 -0700",
                "Message-ID": "<20250304130947.109799-1-sjg@chromium.org>",
                "List-Id": "U-Boot discussion <u-boot.lists.denx.de>"
            content (str): Email content, e.g. 'This series adds a cover-coverage check to CI for Binman. The iMX8 tests
are still not completed,...'
        """
        return await self._cached_request(client, f'covers/{cover_id}/',
                                          max_age)

    async def get_cover_comments(self, client, cover_id,
                                 max_age=COMMENTS_MAX_AGE):
        """Read comments about a cover letter

        Args:
            client (aiohttp.ClientSession): Session to use
            cover_id (str): Patchwork cover-letter ID
            max_age (float or None): Oldest cached copy to use, in seconds; 0
                to read patchwork, or None to use any

        Returns: list of dict: list of comments, each:
            id (int): series ID unique across patchwork instance, e.g. 3472068
            web_url (str): Full URL, e.g. 'https://patchwork.ozlabs.org/comment/3472068/'
            list_archive_url: (unknown?)

            project (dict): project information (id, url, name, link_name,
                list_id, list_email, etc.
            url (str): Full URL, e.g. 'https://patchwork.ozlabs.org/api/1.2/covers/2054866/'
            web_url (str): Full URL, e.g. 'https://patchwork.ozlabs.org/project/uboot/cover/20250304130947.109799-1-sjg@chromium.org/'
            project (dict): project information (id, url, name, link_name,
                list_id, list_email, etc.
            date (str): Date, e.g. '2025-03-04T13:16:15'
            subject (str): 'Re: [PATCH 0/7] binman: Check code-coverage requirements'
            submitter (dict): id, url, name, email, e.g.:
                "id": 6170,
                "url": "https://patchwork.ozlabs.org/api/people/6170/",
                "name": "Simon Glass",
                "email": "sjg@chromium.org"
            content (str): Email content, e.g. 'Hi,

On Tue, 4 Mar 2025 at 06:09, Simon Glass <sjg@chromium.org> wrote:
>
> This '...
            headers: dict: email headers, see get_cover() for an example
        """
        return await self._cached_request(
            client, self.comments_path('cover', cover_id), max_age)

    async def get_series_url(self, link):
        """Get the URL for a series

        Args:
            link (str): Patchwork series ID

        Returns:
            str: URL for the series page
        """
        return f'{self.url}/project/{self.link_name}/list/?series={link}&state=*&archive=both'

    async def _get_patch_status(self, client, patch_id):
        """Get the patch status

        Args:
            client (aiohttp.ClientSession): Session to use
            patch_id (int): Patch ID to look up in patchwork

        Return:
            PATCH: Patch information

        Requests:
            1 for patch, 1 for patch comments
        """
        # The state is wanted, so read it from patchwork
        data = await self.get_patch(client, patch_id, max_age=0)
        state = data['state']
        comment_data = await self.get_patch_comments(client, patch_id,
                                                     max_age=0)

        return Patch(patch_id, state, data, comment_data)

    async def get_series_cover(self, client, data):
        """Get the cover information (including comments)

        Args:
            client (aiohttp.ClientSession): Session to use
            data (dict): Return value from self.get_series()

        Returns:
            COVER object, or None if no cover letter
        """
        # Patchwork should always provide this, but use get() so that we don't
        # have to provide it in our fake patchwork _fake_patchwork_cser()
        cover = data.get('cover_letter')
        cover_id = None
        if cover:
            cover_id = cover['id']
            info = await self.get_cover_comments(client, cover_id, max_age=0)
            cover = COVER(cover_id, len(info), cover['name'], info)
        return cover

    async def fetch_user_comments(self, client, user_email, max_comments=20):
        """Fetch comments made by a user on recent patches

        Paginates through recent patches for the project, fetching
        comments on each until the target count is reached.

        Args:
            client (aiohttp.ClientSession): Session to use
            user_email (str): Email address to match
            max_comments (int): Number of comments to collect

        Returns:
            list of str: Comment body texts
        """
        comments = []
        page = 1
        per_page = 50
        patches_scanned = 0

        while len(comments) < max_comments:
            patches = await self._request(
                client, f'patches/?project={self.proj_id}&order=-date'
                f'&per_page={per_page}&page={page}')
            if not patches:
                break

            for patch in patches:
                if len(comments) >= max_comments:
                    break
                patches_scanned += 1
                tout.progress(
                    f'Scanned {patches_scanned} patches, '
                    f'found {len(comments)}/{max_comments} comments')
                patch_comments = await self._request(
                    client, f"patches/{patch['id']}/comments/")
                for comment in patch_comments:
                    submitter = comment.get('submitter', {})
                    if submitter.get('email') == user_email:
                        content = comment.get('content', '')
                        if content and '>' in content:
                            comments.append(content)
                            if len(comments) >= max_comments:
                                break

            page += 1
        tout.clear_progress()
        return comments

    async def series_get_state(self, client, link, read_comments,
                               read_cover_comments):
        """Sync the series information against patchwork, to find patch status

        Args:
            client (aiohttp.ClientSession): Session to use
            link (str): Patchwork series ID
            read_comments (bool): True to read the comments on the patches
            read_cover_comments (bool): True to read the comments on the cover
                letter

        Return: tuple:
            COVER object, or None if none or not read_cover_comments
            list of PATCH objects
        """
        # A complete series does not change, but one which is still arriving
        # is read again, to see its new patches
        data = await self.get_series(client, link, max_age=0)
        patch_list = list(data['patches'])

        count = len(patch_list)
        patches = []
        if read_comments:
            # Returns a list of Patch objects
            tasks = [self._get_patch_status(client, patch_list[i]['id'])
                     for i in range(count)]

            patch_status = await asyncio.gather(*tasks)
            for patch_data, status in zip(patch_list, patch_status):
                status.series_data = patch_data
                patches.append(status)
        else:
            for i in range(count):
                info = patch_list[i]
                pat = Patch(info['id'], series_data=info)
                pat.raw_subject = info['name']
                patches.append(pat)
        if self._show_progress:
            terminal.print_clear()

        if read_cover_comments:
            cover = await self.get_series_cover(client, data)
        else:
            cover = None

        return cover, patches
