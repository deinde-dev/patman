# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Check the commits which a maintainer is about to push

Each project has its own rules for how patches are applied, such as which
trailers each commit ends with. The [maint] section of the settings gives
them, e.g. for dtc:

    [maint]
    upstream: git.kernel.org/pub/scm/utils/dtc/dtc.git main
    history: prefer-linear
    trailers_end: message-id signoff
    pr_trailers_end: link signoff
    signoff_own: no
    require: author-signoff
    issue_trailers: Closes Link
    github: dgibson/dtc
    gitlab: dgibson/dtc

'patman maint check' checks each commit in the queue, i.e. those on the
branch which are not upstream yet, against these rules, so that a mistake is
caught before the commits are pushed, however they were applied.
"""

from dataclasses import dataclass, field
import re
import subprocess
from urllib.parse import urlparse

from u_boot_pylib import command
from u_boot_pylib import gitutil

from patman import lorelist
from patman import settings

# How strict to be about merge commits: none, none unless allowed, or any
HISTORY = ('linear', 'prefer-linear', 'any')

# What a commit's trailers can be required to end with
END_KINDS = ('message-id', 'link', 'signoff')

# Rules which 'require' can give
REQUIRE = ('author-signoff',)

# What the thanks for an applied series reply to: its cover letter (or its
# first patch, if it has none), or each patch applied
THANKS_TO = ('cover', 'each')

# A link to an issue or a pull request on GitHub, or a merge request on
# GitLab, giving the repo, the kind and the number
RE_ISSUE = re.compile(
    r'^https?://(?P<host>github\.com|gitlab\.com)/(?P<repo>[^/]+/[^/]+?)'
    r'(?:/-)?/(?P<kind>issues|pull|merge_requests)/(?P<num>\d+)/?$')

# The keys of patman's own tags, which 'patman send' removes from the patches
# it sends, so that they never reach the list nor upstream
RE_PATMAN_TAG = re.compile(
    r'^((Series|Cover|Commit)-[a-z-]+|Patch-cc|Change-Id)$', re.IGNORECASE)

# Separators for the fields of each commit read by read_commits()
FIELD_SEP = '\x00'
COMMIT_SEP = '\x01'


@dataclass
class Rules:  # pylint: disable=R0902
    """A project's rules for the commits which a maintainer pushes

    Properties:
        upstream (str): Upstream branch which the queue goes on, e.g.
            'us/main'
        branch (str or None): Branch to check, e.g. 'main', or None if none
            is checked out
        push_branch (str): Branch which 'maint apply' applies patches to,
            e.g. 'main-push'
        signoff (str): Maintainer's sign-off, e.g. 'Simon Glass <sjg@x.org>'
        history (str): One of HISTORY
        trailers_end (list of str): Trailers which each patch from the list
            ends with, from END_KINDS
        pr_trailers_end (list of str): Trailers which the last commit of a
            pull or merge request ends with, from END_KINDS; the others end
            with the sign-off
        signoff_own (bool): True if your own patches end with your sign-off
            too; False if not, since you signed them off as their author
        require (list of str): Other rules, from REQUIRE
        issue_trailers (list of str): Trailers which may link to an issue,
            e.g. ['Closes', 'Link']
        repos (list of str): Repos whose issues and pull requests may be
            linked to, e.g. ['github.com/dgibson/dtc']
        test (list of str): Shell commands which test the queue, e.g.
            ['make check']
        ci_push (list of tuple): Where to push the queue to run CI on it:
            str: Remote, e.g. 'david'
            list of str: Refspecs, with {branch} for the branch, e.g.
                ['+{branch}:ci']
        publish (list of tuple): Where to push the queue to publish it, in
            the same form, e.g. [('us', ['{branch}:main'])]
        thanks_to (str): What the thanks reply to, one of THANKS_TO
    """
    upstream: str
    branch: str
    signoff: str
    push_branch: str = ''

    history: str = 'linear'
    trailers_end: list = field(default_factory=lambda: ['signoff'])
    pr_trailers_end: list = field(default_factory=lambda: ['signoff'])
    signoff_own: bool = True
    require: list = field(default_factory=list)
    issue_trailers: list = field(default_factory=lambda: ['Closes', 'Link'])
    repos: list = field(default_factory=list)
    test: list = field(default_factory=list)
    ci_push: list = field(default_factory=list)
    publish: list = field(default_factory=list)
    thanks_to: str = 'cover'


@dataclass
class Commit:
    """A commit in the queue

    Properties:
        hash (str): Commit hash
        parents (list of str): Hashes of its parents
        author (str): Author, e.g. 'Fred Bloggs <fred@x.org>'
        email (str): Author's email address
        subject (str): Subject
        trailers (list of tuple): (key, value) for each trailer, in order
    """
    hash: str
    parents: list
    author: str
    email: str
    subject: str
    trailers: list


def norm_url(url):
    """Normalise the URL of a git repo, so that remotes can be matched

    Args:
        url (str): URL, e.g. 'git://git.kernel.org/pub/scm/utils/dtc/dtc.git'
            or 'git@github.com:dgibson/dtc.git'

    Return:
        str: Host and path, e.g. 'git.kernel.org/pub/scm/utils/dtc/dtc' or
            'github.com/dgibson/dtc'
    """
    url = url.strip()
    if '://' in url:
        parsed = urlparse(url)
        path = f'{parsed.hostname or ""}{parsed.path}'
    else:
        # scp-like syntax, e.g. 'git@github.com:dgibson/dtc.git'
        path = url.split('@', 1)[-1].replace(':', '/', 1)
    return path.rstrip('/').removesuffix('.git').lower()


def find_remote(name, git_dir=None):
    """Find the remote which a rule names, by its name or its URL

    Args:
        name (str): Name of the remote, e.g. 'us', or its URL without the
            scheme, e.g. 'git.kernel.org/pub/scm/utils/dtc/dtc.git'
        git_dir (str or None): Directory of the git repo, or None for the
            current one

    Return:
        str or None: Name of the remote, or None if there is none
    """
    out = command.output('git', 'remote', '-v', cwd=git_dir)
    want = norm_url(name)
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        if parts[0] == name or norm_url(parts[1]) == want:
            return parts[0]
    return None


def _words(value):
    """Split a setting into its words, ignoring commas

    Args:
        value (str or None): Setting, e.g. 'message-id signoff'

    Return:
        list of str: Words, e.g. ['message-id', 'signoff']
    """
    return (value or '').replace(',', ' ').split()


def _items(value):
    """Split a setting into its items, which are separated by semicolons

    Args:
        value (str or None): Setting, e.g. 'make check; meson test -C build'

    Return:
        list of str: Items, e.g. ['make check', 'meson test -C build']
    """
    return [item.strip() for item in (value or '').split(';') if item.strip()]


def _pushes(name, value, example):
    """Read the places which a setting says to push to

    Args:
        name (str): Name of the setting, e.g. 'ci_push'
        value (str or None): Setting, e.g. 'david +{branch}:ci; gl {branch}:ci'
        example (str): Example of an entry, for the error

    Return:
        list of tuple: Each place:
            str: Remote, e.g. 'david'
            list of str: Refspecs, e.g. ['+{branch}:ci']

    Raises:
        ValueError: An entry is not a remote and its refspecs
    """
    pushes = []
    for item in _items(value):
        words = item.split()
        if len(words) < 2:
            raise ValueError(
                f"Each entry of the maint setting '{name}' needs a remote and "
                f"one or more refspecs, e.g. '{example}', not '{item}'")
        pushes.append((words[0], words[1:]))
    return pushes


def _check_words(name, words, allowed):
    """Check that each word of a setting is one which it can have

    Args:
        name (str): Name of the setting
        words (list of str): Its words
        allowed (tuple of str): Words it can have

    Raises:
        ValueError: A word is not allowed
    """
    for word in words:
        if word not in allowed:
            raise ValueError(f"Unknown value '{word}' in the maint setting "
                             f"'{name}': use {', '.join(allowed)}")


def fetch(remotes, git_dir=None):
    """Fetch from remotes, so that what they have is known

    Args:
        remotes (list of str): Remotes, e.g. ['us', 'david']
        git_dir (str or None): Directory of the git repo, or None for the
            current one

    Return:
        bool: True if each fetch worked; if not, the problem is shown
    """
    for remote in dict.fromkeys(remotes):
        result = subprocess.run(['git', 'fetch', '--quiet', remote],
                                cwd=git_dir, capture_output=True, text=True,
                                check=False)
        if result.returncode:
            print(f'Cannot fetch {remote}: {result.stderr.strip()}')
            return False
    return True


def current_branch(git_dir=None):
    """Get the branch which is checked out

    Args:
        git_dir (str or None): Directory of the git repo, or None for the
            current one

    Return:
        str or None: Name of the branch, or None if HEAD is detached
    """
    out = command.output_one_line('git', 'rev-parse', '--abbrev-ref', 'HEAD',
                                  cwd=git_dir)
    return None if out == 'HEAD' else out


def _default_branch(upstream, push_branch, git_dir):
    """Get the branch to check when none is given

    This is the one checked out, unless it has no commits of its own, e.g.
    main when it is level with the upstream, in which case it is the push
    branch, if there is one

    Args:
        upstream (str): Upstream branch, e.g. 'us/main'
        push_branch (str): Push branch, e.g. 'main-push'
        git_dir (str or None): Directory of the git repo, or None for the
            current one

    Return:
        str or None: Branch, or None if none is checked out and there is no
            push branch
    """
    branch = current_branch(git_dir)
    if branch and command.output('git', 'rev-list', '-1',
                                 f'{upstream}..{branch}', cwd=git_dir,
                                 raise_on_error=False).strip():
        return branch
    if gitutil.ref_exists(f'refs/heads/{push_branch}', git_dir):
        return push_branch
    return branch


def get_rules(values=None, git_dir=None, branch=None, queue=False):
    """Get the rules from the [maint] settings

    Args:
        values (dict or None): Settings, or None to use those read from the
            config (settings.maint)
        git_dir (str or None): Directory of the git repo, or None for the
            current one
        branch (str or None): Branch to check, or None for the default (see
            _default_branch())
        queue (bool): True if the default branch is the push branch, which
            holds the queue, e.g. to test, run CI on or publish it

    Return:
        Rules: The rules

    Raises:
        ValueError: The settings are missing or not valid
    """
    values = settings.maint if values is None else values
    if not values.get('upstream'):
        raise ValueError(
            "No upstream to check against: set 'upstream' in the [maint] "
            "section of the settings, e.g. 'upstream: us main'")
    words = values['upstream'].split()
    if len(words) != 2:
        raise ValueError(
            "The maint setting 'upstream' needs a remote (its name or URL) and "
            f"a branch, e.g. 'us main', not '{values['upstream']}'")
    remote = find_remote(words[0], git_dir)
    if not remote:
        raise ValueError(f"No remote matches '{words[0]}', given by the maint "
                         "setting 'upstream'")
    signoff = values.get('signoff')
    if not signoff:
        name = gitutil.get_default_user_name()
        email = gitutil.get_default_user_email()
        if not name or not email:
            raise ValueError("Set 'signoff' in the [maint] section, or your "
                             'name and email in git')
        signoff = f'{name} <{email}>'

    upstream = f'{remote}/{words[1]}'
    push_branch = values.get('push_branch') or f'{words[1]}-push'
    if not branch:
        branch = push_branch if queue else _default_branch(
            upstream, push_branch, git_dir)
    rules = Rules(upstream, branch, signoff, push_branch)
    if values.get('history'):
        _check_words('history', [values['history']], HISTORY)
        rules.history = values['history']
    for name in ('trailers_end', 'pr_trailers_end'):
        if values.get(name):
            setattr(rules, name, _words(values[name]))
            _check_words(name, getattr(rules, name), END_KINDS)
    if values.get('signoff_own'):
        rules.signoff_own = values['signoff_own'].lower() in ('yes', 'true',
                                                              '1', 'on')
    rules.require = _words(values.get('require'))
    _check_words('require', rules.require, REQUIRE)
    if values.get('issue_trailers'):
        rules.issue_trailers = _words(values['issue_trailers'])
    for host in ('github', 'gitlab'):
        rules.repos += [f'{host}.com/{repo}'.lower()
                        for repo in _words(values.get(host))]
    rules.test = _items(values.get('test'))
    rules.ci_push = _pushes('ci_push', values.get('ci_push'),
                            'david +{branch}:ci')
    rules.publish = _pushes('publish', values.get('publish'),
                            'us {branch}:main')
    if values.get('thanks_to'):
        _check_words('thanks_to', [values['thanks_to']], THANKS_TO)
        rules.thanks_to = values['thanks_to']
    return rules


def _parse_trailers(text):
    """Parse the trailers of a commit, as git gives them

    Args:
        text (str): Trailers, one on each line, e.g. 'Signed-off-by: Fred'

    Return:
        list of tuple: (key, value) for each trailer
    """
    out = []
    for line in text.splitlines():
        key, sep, value = line.partition(':')
        if sep:
            out.append((key.strip(), value.strip()))
    return out


def read_commits(upstream, branch, git_dir=None):
    """Read the commits in the queue, oldest first

    Args:
        upstream (str): Upstream branch, e.g. 'us/main'
        branch (str): Branch holding the queue, e.g. 'main'
        git_dir (str or None): Directory of the git repo, or None for the
            current one

    Return:
        list of Commit: Commits on the branch which are not upstream
    """
    return _log([f'{upstream}..{branch}'], git_dir)


def find_commits(branch, texts, git_dir=None):
    """Find the commits on a branch whose message has any of some text

    Args:
        branch (str): Branch to look on, e.g. 'us/main'
        texts (list of str): Text to look for, e.g. Message-IDs
        git_dir (str or None): Directory of the git repo, or None for the
            current one

    Return:
        list of Commit: Commits found, oldest first
    """
    if not texts:
        return []
    return _log(['--fixed-strings', *[f'--grep={text}' for text in texts],
                 branch], git_dir)


def _log(args, git_dir):
    """Read commits from 'git log'

    Args:
        args (list of str): Arguments which pick the commits
        git_dir (str or None): Directory of the git repo, or None for the
            current one

    Return:
        list of Commit: Commits, oldest first
    """
    # git writes the separators, which cannot be given in an argument
    fmt = '%x00'.join(['%H', '%P', '%an', '%ae', '%s',
                       '%(trailers:only,unfold)']) + '%x01'
    out = command.output('git', 'log', '--reverse', f'--format={fmt}', *args,
                         cwd=git_dir)
    commits = []
    for item in out.split(COMMIT_SEP):
        item = item.strip('\n')
        if not item:
            continue
        chash, parents, name, email, subject, trailers = item.split(FIELD_SEP)
        commits.append(Commit(chash, parents.split(), f'{name} <{email}>',
                              email, subject, _parse_trailers(trailers)))
    return commits


def email_of(ident):
    """Get the email address in an identity, in lower case

    Args:
        ident (str): Identity, e.g. 'Fred Bloggs <Fred@x.org>'

    Return:
        str: Email address, e.g. 'fred@x.org', or the identity itself if it
            has none
    """
    match = re.search(r'<([^>]*)>', ident)
    return (match.group(1) if match else ident).strip().lower()


def _link(value):
    """Parse a link to an issue or a pull or merge request

    Args:
        value (str): Value of a trailer

    Return:
        re.Match or None: Match of RE_ISSUE, or None if it is not one
    """
    return RE_ISSUE.match(value.strip())


def _pr_link(rules, cmt):
    """Get the link to the pull or merge request which a commit came from

    Args:
        rules (Rules): Rules to use
        cmt (Commit): Commit to check

    Return:
        str or None: Value of its Link: trailer, if it links to a pull or
            merge request in one of the project's repos
    """
    for key, value in cmt.trailers:
        match = key.lower() == 'link' and _link(value)
        if (match and match['kind'] != 'issues' and
                f"{match['host']}/{match['repo']}".lower() in rules.repos):
            return value
    return None


def _is_kind(rules, kind, trailer, from_pr):
    """Check if a trailer is of a kind which a commit must end with

    Args:
        rules (Rules): Rules to use
        kind (str): Kind, from END_KINDS
        trailer (tuple): (key, value)
        from_pr (bool): True if the commit is from a pull or merge request

    Return:
        bool: True if the trailer is of that kind
    """
    key, value = trailer
    key = key.lower()
    if kind == 'message-id':
        return key == 'message-id' and bool(lorelist.trailer_msgid(value))
    if kind == 'link':
        if from_pr:
            match = _link(value)
            return key == 'link' and bool(match) and match['kind'] != 'issues'
        return key == 'link' and bool(lorelist.trailer_msgid(value))
    return (key == 'signed-off-by' and
            email_of(value) == email_of(rules.signoff))


def _describe(kinds, rules):
    """Describe the trailers which a commit must end with

    Args:
        kinds (list of str): Kinds, from END_KINDS
        rules (Rules): Rules to use

    Return:
        str: Description, e.g. "Message-ID:, then 'Signed-off-by: Fred'"
    """
    names = {'message-id': 'Message-ID:', 'link': 'Link:',
             'signoff': f"'Signed-off-by: {rules.signoff}'"}
    return ', then '.join(names[kind] for kind in kinds)


def _check_end(rules, cmt, kinds, from_pr):
    """Check that a commit's trailers end with the ones which the rules give

    Args:
        rules (Rules): Rules to use
        cmt (Commit): Commit to check
        kinds (list of str): Kinds of trailer it must end with
        from_pr (bool): True if it is from a pull or merge request

    Return:
        list of str: Problems found
    """
    if not kinds:
        return []
    end = cmt.trailers[-len(kinds):]
    if len(end) == len(kinds) and all(
            _is_kind(rules, kind, trailer, from_pr)
            for kind, trailer in zip(kinds, end)):
        return []
    got = ', '.join(f'{key}:' for key, _ in end) or 'nothing'
    return [f'The trailers should end with {_describe(kinds, rules)}, but '
            f'end with {got}']


def _check_links(rules, cmt):
    """Check the links to issues in a commit's trailers

    Args:
        rules (Rules): Rules to use
        cmt (Commit): Commit to check

    Return:
        list of str: Problems found
    """
    problems = []
    allowed = {name.lower() for name in rules.issue_trailers}
    for key, value in cmt.trailers:
        match = _link(value)
        if not match or match['kind'] != 'issues':
            continue
        repo = f"{match['host']}/{match['repo']}".lower()
        if key.lower() not in allowed:
            problems.append(
                f'{key}: links to an issue; use '
                f"{' or '.join(f'{name}:' for name in rules.issue_trailers)}")
        elif rules.repos and repo not in rules.repos:
            problems.append(f'{key}: links to an issue in {repo}, which is '
                            'not one of the '
                            f"project's repos ({', '.join(rules.repos)})")
    return problems


def _is_own(rules, cmt):
    """Check if a commit is your own, i.e. you are its author

    Args:
        rules (Rules): Rules to use
        cmt (Commit): Commit to check

    Return:
        bool: True if its author has the email address of your sign-off
    """
    return email_of(cmt.email) == email_of(rules.signoff)


def check_commit(rules, cmt, from_pr=False, last_of_pr=False, direct=False):
    """Check a commit which is not a merge commit against the rules

    Args:
        rules (Rules): Rules to use
        cmt (Commit): Commit to check
        from_pr (bool): True if it is from a pull or merge request
        last_of_pr (bool): True if it is the last commit of one
        direct (bool): True if it is your own commit, not sent to the list

    Return:
        list of str: Problems found
    """
    problems = []
    tags = [key for key, _ in cmt.trailers if RE_PATMAN_TAG.match(key)]
    if tags:
        # Check the rest as if they were not there, to say what else is wrong
        problems.append(
            f"Has patman's own tags ({', '.join(f'{key}:' for key in tags)}), "
            "which 'patman send' removes but a push does not: push the "
            'patches as they were sent')
        cmt = Commit(cmt.hash, cmt.parents, cmt.author, cmt.email,
                     cmt.subject, [(key, value) for key, value in cmt.trailers
                                   if not RE_PATMAN_TAG.match(key)])
    own = _is_own(rules, cmt)
    if 'author-signoff' in rules.require and not any(
            key.lower() == 'signed-off-by' and
            email_of(value) == email_of(cmt.email)
            for key, value in cmt.trailers):
        problems.append(f'No Signed-off-by from the author, {cmt.author}')
    if direct:
        kinds = ['signoff']
    elif from_pr:
        kinds = rules.pr_trailers_end if last_of_pr else ['signoff']
    else:
        kinds = rules.trailers_end
        if sum(key.lower() == 'message-id' for key, _ in cmt.trailers) > 1:
            problems.append('There is more than one Message-ID:')
    if own and not rules.signoff_own and not direct:
        kinds = [kind for kind in kinds if kind != 'signoff']
    problems += _check_end(rules, cmt, kinds, from_pr)
    problems += _check_links(rules, cmt)
    return problems


def _from_list(cmt):
    """Check if a commit is a patch from the list, by its Message-ID

    Args:
        cmt (Commit): Commit to check

    Return:
        bool: True if it has a Message-ID: trailer, or a Link: to a message
    """
    return any(key.lower() in ('message-id', 'link') and
               lorelist.trailer_msgid(value) for key, value in cmt.trailers)


def check_commits(rules, commits, allow_merge=False):
    """Check the commits in the queue against the rules

    A commit which is not from the list, i.e. has no Message-ID, must be part
    of a pull or merge request: the commits before one which links to the
    request are taken to be part of it, back to the last patch from the list.
    The exception is your own commit, which you may apply without sending it
    to the list; it must end with your sign-off

    Args:
        rules (Rules): Rules to use
        commits (list of Commit): Commits in the queue, oldest first
        allow_merge (bool): True to allow merge commits when history is
            'prefer-linear'

    Return:
        list of tuple: For each commit, oldest first:
            Commit: The commit
            list of str: Problems found, if any
    """
    results = {}
    pending = []

    def orphans():
        for cmt in pending:
            results[cmt.hash] = [
                'No Message-ID:, and no Link: to a pull or merge request '
                'after it, so it is not clear where it is from']
        pending.clear()

    for cmt in commits:
        if len(cmt.parents) > 1:
            if rules.history == 'linear':
                results[cmt.hash] = ['Merge commit, but the history must be '
                                     'linear']
            elif rules.history == 'prefer-linear' and not allow_merge:
                results[cmt.hash] = ['Merge commit: use --allow-merge if it '
                                     'is meant']
            else:
                results[cmt.hash] = []
            continue
        if _pr_link(rules, cmt):
            for other in pending:
                results[other.hash] = check_commit(rules, other, True)
            pending.clear()
            results[cmt.hash] = check_commit(rules, cmt, True, True)
        elif _from_list(cmt):
            orphans()
            results[cmt.hash] = check_commit(rules, cmt)
        elif _is_own(rules, cmt):
            orphans()
            results[cmt.hash] = check_commit(rules, cmt, direct=True)
        else:
            pending.append(cmt)
    orphans()
    return [(cmt, results[cmt.hash]) for cmt in commits]


def _is_ancestor(commit, ref, git_dir):
    """Check if a commit is an ancestor of a ref, or is the ref

    Args:
        commit (str): Commit or branch
        ref (str): Branch
        git_dir (str or None): Directory of the git repo, or None for the
            current one

    Return:
        bool: True if it is
    """
    return not command.run_one('git', 'merge-base', '--is-ancestor', commit,
                               ref, cwd=git_dir,
                               raise_on_error=False).return_code


def do_check(rules, allow_merge=False, git_dir=None):
    """Check the queue against the rules and say what is wrong

    Args:
        rules (Rules): Rules to use
        allow_merge (bool): True to allow merge commits when history is
            'prefer-linear'
        git_dir (str or None): Directory of the git repo, or None for the
            current one

    Return:
        int: 0 if all is well, else 1, e.g. if the branch does not contain the
            upstream branch
    """
    if not rules.branch:
        raise ValueError('No branch is checked out: use -b to say which to '
                         'check')
    for ref in (rules.upstream, rules.branch):
        if not gitutil.ref_exists(ref, git_dir):
            raise ValueError(f"No branch '{ref}'")
    if (_is_ancestor(rules.branch, rules.upstream, git_dir) and
            not _is_ancestor(rules.upstream, rules.branch, git_dir)):
        # The merge is into the branch which is checked out
        cmd = f'git merge --ff-only {rules.upstream}'
        if current_branch(git_dir) != rules.branch:
            cmd = f'git checkout {rules.branch} && {cmd}'
        print(f'Nothing to check: {rules.branch} is behind {rules.upstream}; '
              f'to bring it up to date: {cmd}')
        return 0
    # A branch which has diverged cannot be pushed, but its own commits are
    # still worth checking
    diverged = not _is_ancestor(rules.upstream, rules.branch, git_dir)
    if diverged:
        print(f"'{rules.branch}' does not contain {rules.upstream}, so it "
              'cannot be pushed; to rebase it: git rebase '
              f'{rules.upstream} {rules.branch}')

    commits = read_commits(rules.upstream, rules.branch, git_dir)
    if not commits:
        print(f'Nothing to check: {rules.branch} has no commits which are '
              f'not in {rules.upstream}')
        return 0
    print(f'Checking {len(commits)} commit{"s" if len(commits) != 1 else ""} '
          f'in {rules.upstream}..{rules.branch}')
    bad = 0
    for cmt, problems in check_commits(rules, commits, allow_merge):
        print(f"  {'ok  ' if not problems else 'FAIL'} {cmt.hash[:12]} "
              f'{cmt.subject}')
        for problem in problems:
            print(f'       - {problem}')
        bad += bool(problems)
    if bad:
        print(f'{bad} of {len(commits)} with problems')
    return 1 if bad or diverged else 0
