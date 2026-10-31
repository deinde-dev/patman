# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2025 Canonical Ltd.
# Written by Simon Glass <simon.glass@canonical.com>
#
# pylint: disable=C0415,R0913,R0914,R0917,W0718

"""Gmail API integration for creating draft review emails.

Handles OAuth2 authentication and creation of draft emails via the
Gmail API. Tokens are stored in ~/.config/patman/ for reuse across
sessions.
"""

import base64
import os
import re
from email.header import Header, decode_header
from email.mime.text import MIMEText
from email.utils import formataddr, getaddresses

from u_boot_pylib import tout

# Config paths - use patman.d to avoid conflict with existing ~/.config/patman
CONFIG_DIR = os.path.expanduser('~/.config/patman.d')
CLIENT_SECRET_PATH = os.path.join(CONFIG_DIR, 'client_secret.json')

# Gmail API scopes - compose drafts and read messages (for thread lookup)
SCOPES = ['https://www.googleapis.com/auth/gmail.compose',
          'https://www.googleapis.com/auth/gmail.readonly']


def _token_path(account=None):
    """Get the token path for a given account

    Args:
        account (str or None): Gmail account email, or None for default

    Returns:
        str: Path to the token file
    """
    if account:
        safe = account.replace('@', '_at_').replace('.', '_')
        return os.path.join(CONFIG_DIR, f'gmail_token_{safe}.json')
    return os.path.join(CONFIG_DIR, 'gmail_token.json')


def check_available():
    """Check if Gmail API dependencies are installed

    Returns:
        bool: True if google-api-python-client and google-auth-oauthlib
              are available
    """
    try:
        from googleapiclient import discovery  # pylint: disable=W0611
        from google_auth_oauthlib import flow  # pylint: disable=W0611
        from google.auth.transport import requests  # pylint: disable=W0611
    except ImportError:
        tout.error('Gmail API dependencies not available')
        tout.error('Install with: pip install google-api-python-client '
                   'google-auth-httplib2 google-auth-oauthlib')
        return False

    if not os.path.exists(CLIENT_SECRET_PATH):
        tout.error(f'Gmail client secret not found at {CLIENT_SECRET_PATH}')
        tout.error('Download OAuth client credentials from Google Cloud '
                   'Console and save them there')
        return False
    return True


def _get_credentials(account=None):
    """Get or refresh OAuth2 credentials

    Loads existing token if available and valid. If expired, refreshes
    using the refresh token. If no valid token exists, initiates the
    OAuth2 consent flow.

    Args:
        account (str or None): Gmail account email (e.g.
            'user@gmail.com'), or None for default token

    Returns:
        google.oauth2.credentials.Credentials: Valid credentials

    Raises:
        FileNotFoundError: if client_secret.json is missing
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token_path = _token_path(account)
    creds = None

    # Load existing token
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    # Refresh or get new credentials
    if not creds or not creds.valid:
        if creds and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CLIENT_SECRET_PATH):
                raise FileNotFoundError(
                    f'Gmail client secret not found at {CLIENT_SECRET_PATH}')
            app_flow = InstalledAppFlow.from_client_secrets_file(
                CLIENT_SECRET_PATH, SCOPES)
            if account:
                tout.notice(f'Please authenticate as {account}')
            creds = app_flow.run_local_server(port=0)

        # Save the token for next time
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(token_path, 'w', encoding='utf-8') as fh:
            import json
            token_data = {
                'token': creds.token,
                'refresh_token': creds.refresh_token,
                'token_uri': creds.token_uri,
                'client_id': creds.client_id,
                'client_secret': creds.client_secret,
                'scopes': list(creds.scopes or []),
            }
            json.dump(token_data, fh)

    return creds


def get_service(account=None):
    """Get an authenticated Gmail API service object

    Args:
        account (str or None): Gmail account email, or None for default

    Returns:
        googleapiclient.discovery.Resource: Gmail API service
    """
    from googleapiclient.discovery import build

    creds = _get_credentials(account)
    return build('gmail', 'v1', credentials=creds)


def fetch_sent_reviews(service, list_email, user_email,
                       max_results=20):
    """Fetch sent emails that are reviews of other people's patches

    Paginates through Gmail search results to collect review emails.
    Filters out replies to the user's own patches by checking whether
    the first message in the thread was sent by someone else.

    Args:
        service (googleapiclient.discovery.Resource): Gmail API service
        list_email (str): Mailing list address to filter by
        user_email (str): The reviewer's own email, used to skip
            threads initiated by the reviewer
        max_results (int): Maximum number of emails to collect

    Returns:
        list of str: Email body texts
    """
    query = f'from:me to:{list_email} subject:(Re: PATCH)'
    bodies = []
    page_token = None
    # Cache thread originator lookups
    thread_from_cache = {}

    while len(bodies) < max_results:
        kwargs = {'userId': 'me', 'q': query, 'maxResults': 100}
        if page_token:
            kwargs['pageToken'] = page_token
        results = service.users().messages().list(**kwargs).execute()
        messages = results.get('messages', [])
        if not messages:
            break

        for msg_info in messages:
            if len(bodies) >= max_results:
                break
            tout.progress(
                f'Fetched {len(bodies)}/{max_results} review emails')

            msg = service.users().messages().get(
                userId='me', id=msg_info['id'], format='full').execute()

            # Check if the thread was started by someone else
            thread_id = msg.get('threadId')
            if thread_id not in thread_from_cache:
                thread = service.users().threads().get(
                    userId='me', id=thread_id,
                    format='metadata',
                    metadataHeaders=['From']).execute()
                first_msg = thread.get('messages', [{}])[0]
                hdrs = first_msg.get('payload', {}).get('headers', [])
                from_val = ''
                for h in hdrs:
                    if h['name'] == 'From':
                        from_val = h['value']
                        break
                thread_from_cache[thread_id] = from_val

            # Skip if this thread was started by us
            if user_email in thread_from_cache.get(thread_id, ''):
                continue

            payload = msg.get('payload', {})
            body = _extract_body(payload)
            if body and '>' in body:
                bodies.append(body)

        page_token = results.get('nextPageToken')
        if not page_token:
            break

    tout.clear_progress()
    return bodies


def _extract_body(payload):
    """Extract plain text body from a Gmail message payload

    Args:
        payload (dict): Gmail message payload

    Returns:
        str or None: Decoded body text
    """
    if payload.get('mimeType') == 'text/plain':
        data = payload.get('body', {}).get('data', '')
        if data:
            return base64.urlsafe_b64decode(data).decode('utf-8', 'replace')

    for part in payload.get('parts', []):
        result = _extract_body(part)
        if result:
            return result
    return None


def _find_thread(service, msgid):
    """Find the Gmail thread containing a message with the given Message-ID

    Args:
        service (googleapiclient.discovery.Resource): Gmail API service
        msgid (str): Message-ID header value (e.g. '<abc@example.com>')

    Returns:
        str or None: Gmail thread ID if found
    """
    # Strip angle brackets — Gmail search expects bare message ID
    bare_id = msgid.strip('<>')
    try:
        results = service.users().messages().list(
            userId='me', q=f'rfc822msgid:{bare_id}').execute()
        messages = results.get('messages', [])
        if messages:
            return messages[0].get('threadId')
    except Exception as exc:
        tout.warning(f'Failed to search Gmail for thread: {exc}')
    return None


def create_draft(service, to, subject, body,
                 in_reply_to=None, references=None, cc=None,
                 sender=None):
    """Create a Gmail draft email

    If in_reply_to is set, searches for the original message in Gmail
    and attaches the draft to that thread so it appears as a reply.

    If sender is provided and differs from the Gmail account's address,
    a From header is set so the email is sent on behalf of the right
    identity.

    Args:
        service (googleapiclient.discovery.Resource): Gmail API service
        to (str): Recipient email address
        subject (str): Email subject line
        body (str): Plain text email body
        in_reply_to (str or None): Message-ID to reply to
        references (str or None): References header value
        cc (str or None): CC addresses
        sender (str or None): Sender identity, e.g. 'Name <email>'.
            If set, added as the From header.

    Returns:
        dict: Gmail API response with draft ID and message info

    Raises:
        googleapiclient.errors.HttpError: on API failure
    """
    msg = MIMEText(body)
    if sender:
        msg['from'] = sender
    msg['to'] = to
    msg['subject'] = (subject if subject.isascii() else
                      Header(subject, 'utf-8'))
    if cc:
        msg['cc'] = cc
    if in_reply_to:
        msg['In-Reply-To'] = in_reply_to
        msg['References'] = references or in_reply_to

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    draft_body = {'message': {'raw': raw}}

    # Find the existing thread in Gmail so the draft appears as a reply
    if in_reply_to:
        thread_id = _find_thread(service, in_reply_to)
        if thread_id:
            draft_body['message']['threadId'] = thread_id
            tout.notice(f'  Found thread: {thread_id}')
        else:
            tout.notice(f'  Thread not found for {in_reply_to}'
                        ' - draft will be standalone')

    draft = service.users().drafts().create(
        userId='me', body=draft_body).execute()
    return draft


def delete_draft(service, draft_id):
    """Delete a Gmail draft

    A draft that no longer exists (already sent or deleted by hand) is
    ignored rather than raising.

    Args:
        service (googleapiclient.discovery.Resource): Gmail API service
        draft_id (str): ID of the draft to delete
    """
    from googleapiclient.errors import HttpError
    try:
        service.users().drafts().delete(userId='me', id=draft_id).execute()
    except HttpError as exc:
        if exc.resp.status != 404:
            raise


def draft_exists(service, draft_id):
    """Check whether a Gmail draft still exists

    Args:
        service (googleapiclient.discovery.Resource): Gmail API service
        draft_id (str): ID of the draft

    Gmail may still give a draft after it is sent, as its message, which
    is then labelled SENT rather than DRAFT.

    Returns:
        bool: True if it exists, False if it was sent or deleted
    """
    from googleapiclient.errors import HttpError
    try:
        draft = service.users().drafts().get(userId='me', id=draft_id,
                                             format='minimal').execute()
    except HttpError as exc:
        if exc.resp.status != 404:
            raise
        return False
    return 'DRAFT' in draft.get('message', {}).get('labelIds', [])


class AlreadySent(Exception):
    """A draft was sent already, e.g. from Gmail itself

    Properties:
        body (str): Text of the message which was sent
        msg_id (str): Gmail message ID
        thread_id (str): Gmail thread ID
    """
    def __init__(self, body, msg_id, thread_id):
        super().__init__(f'Message {msg_id} was sent already')
        self.body = body
        self.msg_id = msg_id
        self.thread_id = thread_id


def send_draft(service, draft_id):
    """Send a Gmail draft

    The text is read first, since it may have been changed in Gmail after
    the draft was created.

    Args:
        service (googleapiclient.discovery.Resource): Gmail API service
        draft_id (str): ID of the draft to send

    Returns:
        tuple or None: (body, Gmail message ID, thread ID) of the message
            sent, or None if the draft no longer exists

    Raises:
        AlreadySent: The draft was sent already, e.g. from Gmail, so it is
            no longer a draft
    """
    from googleapiclient.errors import HttpError
    try:
        draft = service.users().drafts().get(
            userId='me', id=draft_id, format='full').execute()
    except HttpError as exc:
        if exc.resp.status == 404:
            return None
        raise
    body = _extract_body(draft.get('message', {}).get('payload', {})) or ''
    try:
        msg = service.users().drafts().send(
            userId='me', body={'id': draft_id}).execute()
    except HttpError as exc:
        sent = _sent_message(service, draft, exc)
        if not sent:
            raise
        raise AlreadySent(_extract_body(sent.get('payload', {})) or body,
                          sent.get('id'), sent.get('threadId')) from exc
    return body, msg.get('id'), msg.get('threadId')


def _sent_message(service, draft, exc):
    """Find the message of a draft which Gmail cannot send, if it was sent

    Gmail says that a message is 'not a draft' when the draft was sent
    already, e.g. from Gmail itself.

    Args:
        service (googleapiclient.discovery.Resource): Gmail API service
        draft (dict): Draft, as read before sending it
        exc (HttpError): Error which Gmail gave when sending it

    Returns:
        dict or None: Message, if it was sent, else None
    """
    from googleapiclient.errors import HttpError
    msg_id = draft.get('message', {}).get('id')
    if exc.resp.status != 400 or 'not a draft' not in str(exc) or not msg_id:
        return None
    try:
        msg = service.users().messages().get(userId='me', id=msg_id,
                                             format='full').execute()
    except HttpError:
        return None
    return msg if 'SENT' in msg.get('labelIds', []) else None


def _decode_hdr(val):
    """Decode and unfold an email header value

    Headers from patchwork may be folded and may contain RFC 2047 encoded
    words, sometimes with the bogus charset 'unknown-8bit' (produced when
    the original email has raw 8-bit characters in a header). Such values
    are rejected by Gmail, so decode them to plain text, treating
    'unknown-8bit' as UTF-8

    Args:
        val (str or list of str): Header value, or list of values if the
            header appears more than once

    Returns:
        str: Decoded, unfolded header value
    """
    if isinstance(val, list):
        return ', '.join(_decode_hdr(v) for v in val)
    val = ' '.join(val.split())
    out = []
    for part, charset in decode_header(val):
        if isinstance(part, bytes):
            if not charset or charset == 'unknown-8bit':
                charset = 'utf-8'
            part = part.decode(charset, errors='replace')
        out.append(part)
    return ''.join(out)


def format_addrs(vals):
    """Parse and re-encode a list of address-header values

    Args:
        vals (list of str): Header values, each with one or more addresses

    Returns:
        str: Comma-separated addresses, deduplicated, with any non-ASCII
            names encoded suitably for an email header
    """
    seen = set()
    addrs = []
    for name, addr in getaddresses([_decode_hdr(v) for v in vals if v]):
        if not addr or addr.lower() in seen:
            continue
        seen.add(addr.lower())
        addrs.append(formataddr((name, addr), charset='utf-8'))
    return ', '.join(addrs)


def _build_cc(headers, list_email):
    """Build a CC list from the original patch headers

    Combines the original To, Cc headers and mailing list address,
    deduplicating entries.

    Args:
        headers (dict): Email headers from patchwork get_patch()
        list_email (str or None): Mailing list email address

    Returns:
        str: Comma-separated CC addresses
    """
    vals = [headers.get(field, '') for field in ('To', 'Cc')]
    if list_email:
        vals.append(list_email)
    return format_addrs(vals)


def _get_msgid(patch_data, hdrs):
    """Get the Message-ID from patch data or headers

    Args:
        patch_data (dict): Patch or cover letter data from patchwork
        hdrs (dict): Email headers from patchwork get_patch()

    Returns:
        str or None: Message-ID
    """
    return (patch_data.get('msgid') or
            hdrs.get('Message-Id') or
            hdrs.get('Message-ID'))


def _envelope(patch_data, hdrs, fallback_addr, list_email, refs=None):
    """Work out the headers for a review sent as a reply to a patch

    Args:
        patch_data (dict): Patch or cover letter data from patchwork
        hdrs (dict): Email headers of the patch or cover letter
        fallback_addr (str): Address to reply to if there is no Reply-To
        list_email (str or None): Mailing list email address
        refs (str or None): References header value. If None, uses
            the Message-ID as the reference (for cover letters).

    Returns:
        dict: Headers for the reply:
            subject (str): Subject
            in_reply_to (str or None): Message-ID of the patch
            references (str or None): References
            to (str): To addresses
            cc (str): Cc addresses
    """
    subject = _decode_hdr(hdrs.get('Subject', patch_data.get('name', '')))
    if not subject.startswith('Re: '):
        subject = f"Re: {subject}"
    msgid = _get_msgid(patch_data, hdrs)
    return {
        'subject': subject,
        'in_reply_to': msgid,
        'references': msgid if refs is None else refs,
        'to': format_addrs([hdrs.get('Reply-To') or fallback_addr]),
        'cc': _build_cc(hdrs, list_email),
    }


def review_envelopes(series_data, patch_headers, indexes):
    """Work out the headers for reviews sent as replies to their patches

    Each review is threaded as a reply to the patch (or cover letter) it is
    about, and goes to its sender, the people it was sent to and the
    mailing list. The result can be stored, so that the review can be
    drafted again without asking patchwork for the patch's headers.

    Args:
        series_data (dict): Series data from patchwork get_series(),
            containing 'patches', 'cover_letter', 'submitter', 'project'
        patch_headers (dict): Map of patch index to headers dict from
            patchwork get_patch(); 0 is the cover letter
        indexes (iterable of int): Patch indexes to work out headers for,
            with 0 for the cover letter and 1..N for the patches

    Returns:
        dict: Map of patch index to the headers, see _envelope()
    """
    fallback_addr = series_data.get('submitter', {}).get('email', '')
    list_email = series_data.get('project', {}).get('list_email')
    cover = series_data.get('cover_letter')
    patches = series_data.get('patches', [])
    cover_msgid = (_get_msgid(cover, patch_headers.get(0, {})) if cover
                   else None)
    wanted = set(indexes)

    envelopes = {}
    if cover and 0 in wanted:
        envelopes[0] = _envelope(cover, patch_headers.get(0, {}),
                                 fallback_addr, list_email)
    for i, patch in enumerate(patches):
        idx = i + 1
        if idx not in wanted:
            continue
        hdrs = patch_headers.get(idx, {})
        msgid = _get_msgid(patch, hdrs)
        refs = f'{cover_msgid} {msgid}' if cover_msgid else msgid
        envelopes[idx] = _envelope(patch, hdrs, fallback_addr, list_email,
                                   refs)
    return envelopes


def create_drafts_from_envelopes(envelopes, review_bodies, dry_run=False,
                                 account=None, sender=None):
    """Create Gmail drafts of reviews, given the headers for each

    Args:
        envelopes (dict): Map of patch index to the headers, see
            review_envelopes()
        review_bodies (dict): Map of patch index to review body text
        dry_run (bool): If True, print what would be created
            without calling the API
        account (str or None): Gmail account email to use
        sender (str or None): Reviewer identity, e.g.
            'Name <email>'. If this differs from the Gmail
            account, it is set as the From header.

    Returns:
        dict: Map of patch index to Gmail draft ID (empty for
            dry run)
    """
    service = None
    if not dry_run:
        if not check_available():
            return {}
        service = get_service(account)

    draft_ids = {}
    for idx in sorted(envelopes):
        if idx not in review_bodies:
            continue
        env = envelopes[idx]
        if dry_run:
            tout.notice(f"Would create draft: {env['subject']}")
            tout.notice(f"  To: {env['to']}, Cc: {env['cc']}")
            continue
        draft = create_draft(service, env['to'], env['subject'],
                             review_bodies[idx],
                             in_reply_to=env['in_reply_to'],
                             references=env['references'], cc=env['cc'],
                             sender=sender)
        tout.notice(f"Created draft: {env['subject']}")
        draft_ids[idx] = draft['id']
    return draft_ids


def create_review_drafts(series_data, review_bodies, patch_headers=None,
                         dry_run=False, account=None, sender=None,
                         envelopes=None):
    """Create Gmail drafts for a reviewed series

    Creates one draft per patch (and optionally the cover letter),
    each threaded as a reply to the original email.

    Args:
        series_data (dict): Series data from patchwork
            get_series(), containing 'patches', 'cover_letter',
            'submitter', 'project'
        review_bodies (dict): Map of patch index to review body
            text. Key 0 is the cover letter, 1..N are patches.
        patch_headers (dict or None): Map of patch index to
            headers dict from patchwork get_patch()
        dry_run (bool): If True, print what would be created
            without calling the API
        account (str or None): Gmail account email to use
        sender (str or None): Reviewer identity, e.g.
            'Name <email>'. If this differs from the Gmail
            account, it is set as the From header.
        envelopes (dict or None): If given, this is updated with the
            headers for each review, see review_envelopes(), so that they
            can be stored

    Returns:
        dict: Map of patch index to Gmail draft ID (empty for
            dry run)
    """
    envs = review_envelopes(series_data, patch_headers or {}, review_bodies)
    if envelopes is not None:
        envelopes.update(envs)
    return create_drafts_from_envelopes(envs, review_bodies, dry_run,
                                        account, sender)


def fetch_thread_replies(service, thread_id, after_msg_id):
    """Fetch the replies to one of our messages in a thread

    Only messages which reply to ours, directly or further down the
    conversation, are included. Messages we sent are left out.

    Args:
        service (googleapiclient.discovery.Resource): Gmail API service
        thread_id (str): Gmail thread ID
        after_msg_id (str): Gmail message ID of our sent review

    Returns:
        list of dict: Replies, each with:
            from, to, cc, subject, date, body: from the reply
            message_id (str): Message-ID header of the reply
            references (str): References header of the reply
            msg_id (str): Gmail message ID of the reply
            thread_id (str): Gmail thread ID
            answer (str or None): Text of a message we sent in response to
                the reply, if any
    """
    try:
        thread = service.users().threads().get(
            userId='me', id=thread_id, format='full').execute()
    except Exception:
        return []

    messages = thread.get('messages', [])

    def headers_of(msg):
        return {hdr['name'].lower(): hdr['value']
                for hdr in msg.get('payload', {}).get('headers', [])}

    # Find our message, and the Message-ID which replies refer to
    our_idx = -1
    for i, msg in enumerate(messages):
        if msg['id'] == after_msg_id:
            our_idx = i
            break
    if our_idx < 0:
        return []
    ours = headers_of(messages[our_idx]).get('message-id', '')

    # Our later messages, which may answer the replies
    answers = []
    for msg in messages[our_idx + 1:]:
        if 'SENT' in msg.get('labelIds', []):
            hdrs = headers_of(msg)
            answers.append((f"{hdrs.get('references', '')} "
                            f"{hdrs.get('in-reply-to', '')}", msg))

    replies = []
    for msg in messages[our_idx + 1:]:
        if 'SENT' in msg.get('labelIds', []):
            continue
        hdrs = headers_of(msg)
        refs = hdrs.get('references', '')
        if ours and ours not in f"{refs} {hdrs.get('in-reply-to', '')}":
            continue
        body = _extract_body(msg.get('payload', {}))
        msgid = hdrs.get('message-id', '')
        answer = next((_extract_body(ans.get('payload', {}))
                       for refs, ans in answers if msgid and msgid in refs),
                      None)
        if body:
            replies.append({
                'from': hdrs.get('from', ''),
                'to': hdrs.get('to', ''),
                'cc': hdrs.get('cc', ''),
                'subject': hdrs.get('subject', ''),
                'date': hdrs.get('date', ''),
                'body': body,
                'message_id': hdrs.get('message-id', ''),
                'references': refs,
                'msg_id': msg['id'],
                'thread_id': thread_id,
                'answer': answer,
            })
    return replies


def subject_key(subject):
    """Split a subject into its words, version and patch number

    The words are taken after any 'Re:' and bracketed prefixes, from which the
    version and patch number are taken, e.g. '[PATCH v2 03/15]' or
    '[v2,3/15]'

    Args:
        subject (str): Subject, e.g. 'Re: [PATCH v2 2/5] video: Add it'

    Returns:
        tuple:
            str: Normalised subject, e.g. 'video add it'
            int: Version, 1 if not given
            str or None: Patch number, e.g. '2/5', if given
    """
    subject = subject or ''
    version, index = 1, None
    while True:
        match = re.match(r'\s*(re:|\[([^]]*)\])\s*', subject, re.I)
        if not match:
            break
        for tok in re.split(r'[\s,]+', match.group(2) or ''):
            ver = re.fullmatch(r'v(\d+)', tok, re.I)
            num = re.fullmatch(r'(\d+)/(\d+)', tok)
            if ver:
                version = int(ver.group(1))
            elif num:
                index = f'{int(num.group(1))}/{int(num.group(2))}'
        subject = subject[match.end():]
    return _norm(subject), version, index


def same_subject(first, second):
    """Check whether two subjects are about the same patch

    Args:
        first (str): First subject
        second (str): Second subject

    Returns:
        bool: True if they have the same words and version, and the same
            patch number if both have one
    """
    words1, ver1, num1 = subject_key(first)
    words2, ver2, num2 = subject_key(second)
    return (words1 == words2 and ver1 == ver2 and
            (not num1 or not num2 or num1 == num2))


def norm_subject(subject):
    """Reduce an email or patch subject to its words, for comparing

    Args:
        subject (str): Subject, e.g. 'Re: [PATCH v2 2/5] video: Add it'

    Returns:
        str: Normalised subject, e.g. 'video add it'
    """
    return subject_key(subject)[0]


def _norm(text):
    """Reduce text to lower-case words separated by single spaces"""
    return ' '.join(re.findall(r"[\w']+", (text or '').lower()))


# A message we sent with the same subject as a draft, but not its text, is
# only taken as our response to the same patch within this many days
SUBJECT_DAYS = 14

# Lines which appear in many emails, so do not identify a message
RE_COMMON_LINE = re.compile(
    r"^(>|(hi|hello|dear|regards|thanks|cheers)\b|on .* wrote:$|"
    r"[\w-]+-by:)", re.I)


def _key_lines(body):
    """Get the lines of a message which are not quoted, normalised

    Args:
        body (str): Message text

    Returns:
        list of str: Lines, with quoted and empty lines left out
    """
    return [_norm(line) for line in (body or '').splitlines()
            if line.strip() and not line.startswith('>') and _norm(line)]


def search_phrase(body):
    """Pick a distinctive phrase from a message, for searching

    Quoted text, greetings, sign-offs and tags are left out, since they
    appear in many messages.

    Args:
        body (str): Message text

    Returns:
        str or None: Up to eight words from the longest remaining line, or
            None if there is no suitable line
    """
    best = ''
    for line in (body or '').splitlines():
        line = line.strip()
        if line and not RE_COMMON_LINE.match(line) and len(line) > len(best):
            best = line
    words = _norm(best).split()
    return ' '.join(words[:8]) if len(words) >= 4 else None


def _pick_sent(msgs, item, phrase):
    """Pick the message we sent in response to what a draft replies to

    Our messages with the same subject (including the version and patch
    number), sent after the draft was created, are considered. One
    containing the draft's distinctive phrase is preferred; otherwise the
    earliest, if sent within SUBJECT_DAYS, is taken, since we often write our
    own reply rather than sending the draft. Without a subject, only a message
    containing the phrase (or every line, for a draft with no phrase) will
    do.

    Args:
        msgs (list of dict): Gmail messages, in full format, oldest first
        item: Draft being looked for (see sync_drafts())
        phrase (str or None): Distinctive phrase from the draft

    Returns:
        tuple or None: (message, text) if found
    """
    earliest = None
    for msg in msgs:
        if 'SENT' not in msg.get('labelIds', []):
            continue
        if item.since and int(msg.get('internalDate', 0)) / 1000 < item.since:
            continue
        payload = msg.get('payload', {})
        hdrs = {hdr['name'].lower(): hdr['value']
                for hdr in payload.get('headers', [])}
        if item.subject and not same_subject(hdrs.get('subject'),
                                             item.subject):
            continue
        text = _extract_body(payload)
        if not text:
            continue
        sent = _norm(text)
        if phrase and phrase in sent:
            return msg, text
        if not phrase:
            lines = _key_lines(item.body)
            if lines and all(line in sent for line in lines):
                return msg, text
        if (item.subject and not earliest and
                (not item.since or int(msg.get('internalDate', 0)) / 1000 <
                 item.since + SUBJECT_DAYS * 86400)):
            earliest = msg, text
    return earliest


def _find_sent(service, item, thread_id):
    """Find the message we sent in response to what a draft replies to

    If the draft's thread is known, our messages there are checked.
    Otherwise the sent folder is searched, from the date the draft was
    created, for a distinctive phrase from it and for its subject.

    Args:
        service (googleapiclient.discovery.Resource): Gmail API service
        item: Draft being looked for (see sync_drafts())
        thread_id (str or None): Gmail thread of the draft, if known

    Returns:
        tuple or None: (body, Gmail message ID, thread ID) if found
    """
    phrase = search_phrase(item.body)
    if thread_id:
        thread = service.users().threads().get(
            userId='me', id=thread_id, format='full').execute()
        msgs = thread.get('messages', [])
    else:
        ids = []
        for term in (phrase, norm_subject(item.subject)):
            if not term:
                continue
            query = 'in:sent'
            if item.after:
                query += f' after:{item.after}'
            res = service.users().messages().list(
                userId='me', q=f'{query} "{term}"', maxResults=10).execute()
            ids += [info['id'] for info in res.get('messages', [])
                    if info['id'] not in ids]
        msgs = sorted((service.users().messages().get(
                           userId='me', id=msg_id, format='full').execute()
                       for msg_id in ids),
                      key=lambda msg: int(msg.get('internalDate', 0)))
    found = _pick_sent(msgs, item, phrase)
    if not found:
        return None
    msg, text = found
    return text, msg['id'], msg.get('threadId')


def sync_drafts(service, items):
    """Check whether drafts have been sent or deleted

    The thread of each draft which still exists is noted, so that the
    message we send can be found there later. A draft whose message is no
    longer a draft was sent, though Gmail may still give it. Our message may be the draft
    itself or one we wrote instead, so it is looked for (see _find_sent())
    even while the draft exists, if the item asks for this. A draft which
    has gone, with no message from us, was deleted without being sent.

    Args:
        service (googleapiclient.discovery.Resource): Gmail API service
        items (list): Drafts to check, each with these attributes:
            idnum (int): ID of the record holding the draft
            draft_id (str): Gmail draft ID
            body (str): Text of the draft
            thread_id (str or None): Gmail thread of the draft, if known
            subject (str or None): Subject of the email being replied to,
                if known; our message must have the same subject
            after (str or None): Date the draft was created, as YYYY/MM/DD
            since (float or None): Time the draft was created, in seconds
                since the epoch; our message must be later than this
            check_sent (bool): True to look for our message even if the
                draft still exists

    Returns:
        tuple:
            dict: Drafts which were sent, or answered with a message we
                wrote: key (int): idnum, value: tuple (body, Gmail message
                ID, thread ID)
            list of int: idnum of each draft deleted without being sent
            dict: Threads of drafts which still exist, where newly found:
                key (int): idnum, value (str): Gmail thread ID
    """
    sent = {}
    deleted = []
    threads = {}

    for item in items:
        if not item.draft_id:
            continue
        thread_id = item.thread_id
        exists = False
        try:
            draft = service.users().drafts().get(
                userId='me', id=item.draft_id, format='minimal').execute()
            # Gmail may still give a draft which was sent, as its message
            exists = 'DRAFT' in draft.get('message', {}).get('labelIds', [])
            found_id = draft.get('message', {}).get('threadId')
            if found_id and found_id != thread_id:
                threads[item.idnum] = found_id
                thread_id = found_id
        except Exception:  # pylint: disable=W0718
            pass
        if exists and not item.check_sent:
            continue

        try:
            found = _find_sent(service, item, thread_id)
        except Exception as exc:  # pylint: disable=W0718
            tout.warning(f'Cannot check whether draft {item.draft_id} was '
                         f'sent: {exc}')
            continue
        if found:
            sent[item.idnum] = found
        elif not exists:
            deleted.append(item.idnum)

    return sent, deleted, threads
