# SPDX-License-Identifier: GPL-2.0+
#
# Copyright 2026 Simon Glass <sjg@chromium.org>
#
"""Explain how the rules for suggesting series score one series

'patman review --explain SERIES' runs every rule on the series, including
those which --suggest would not reach, e.g. after a 'skip' rule matches, and
shows what each test saw, to help in writing the rules.
"""

from datetime import datetime, timezone

import aiohttp

from u_boot_pylib import tout

from patman import lorelist
from patman import settings
from patman.review import common
from patman.review import lookup
from patman.review import suggest
from patman.review import suggest_rules
from patman.review import suggest_tests


def _verdict(sug, rules, results, score):
    """Say whether the rules suggest the series

    Args:
        sug (suggest.Suggester): Suggester holding the rules
        rules (list of Rule): Rules run, in order
        results (list of Result or None): What each rule's test found, None
            if it could not be run
        score (int): Score from the rules which add to it

    Returns:
        str: The verdict
    """
    for rule, res in zip(rules, results):
        if not res:
            continue
        matched = res.matched != rule.negate
        if rule.score == 'skip' and matched:
            return f"Not suggested: rule '{rule.name}' skips it"
        if rule.score == 'need' and not matched:
            return f"Not suggested: rule '{rule.name}' is needed"
    if score < sug.rules.min_score:
        return (f'Not suggested: score {score} is below min_score '
                f'{sug.rules.min_score}')
    return (f'Score {score}: suggested if among the best {sug.limit} recent '
            'series')


async def _facts(sug, pws):
    """Find what every test needs to know about a series

    Args:
        sug (suggest.Suggester): Suggester holding the rules
        pws (dict): Series, as from patchwork or lore

    Returns:
        tuple:
            bool: True if you have reviewed this version
            list of tuple: What each kind of test knows:
                dict: The tests, e.g. suggest_tests.FREE_TESTS
                Info, PatchInfo, Extra or None: What they know, or None if
                    it could not be found out
            list of str: Problems found, e.g. the patches cannot be read
    """
    done, info = sug.free_info(pws)
    problems = []
    patches = extra = None
    async with aiohttp.ClientSession() as client:
        if sug.needs_patches():
            try:
                patches = await sug.patch_info(client, pws)
            except suggest.PW_ERRORS as exc:
                problems.append(f'Cannot read the patches: {exc}')
        try:
            extra = await sug.extra(client, pws, patches)
        except suggest.PW_ERRORS as exc:
            problems.append(f'Cannot check it: {exc}')
    return done, [(suggest_tests.FREE_TESTS, info),
                  (suggest_tests.PATCH_TESTS, patches),
                  (suggest_tests.EXTRA_TESTS, extra)], problems


def _run(rule, facts):
    """Run the test of a rule

    Args:
        rule (Rule): Rule to run
        facts (list of tuple): What each kind of test knows, as from
            _facts()

    Returns:
        Result or None: What the test found, or None if what it needs to
            know could not be found out
    """
    tests, known = next((tests, known) for tests, known in facts
                        if rule.test in tests)
    return tests[rule.test](rule.args, known) if known is not None else None


def _table(rules, results):
    """Show what each rule found, as a table

    Args:
        rules (list of Rule): Rules run, in order
        results (list of Result or None): What each rule's test found, None
            if it could not be run

    Returns:
        tuple:
            list of str: Lines of the table
            int: Score from the rules which add to it
    """
    rows = [('Rule', 'Test', 'Match', 'Score', 'Seen')]
    score = 0
    for rule, res in zip(rules, results):
        if res is None:
            rows.append((rule.name, suggest_rules.rule_text(rule), '?',
                         suggest_rules.score_text(rule), 'not known'))
            continue
        matched = res.matched != rule.negate
        if matched and isinstance(rule.score, int):
            score += rule.score
        rows.append((rule.name, suggest_rules.rule_text(rule),
                     'yes' if matched else 'no',
                     suggest_rules.score_text(rule), res.seen))
    widths = [max(len(row[col]) for row in rows) for col in range(4)]
    return ['  ' + '  '.join(f'{text:{width}}'
                             for text, width in zip(row, widths)) +
            f'  {row[4]}' for row in rows], score


def explain(sug, pws, url):
    """Show how the rules score a series

    Args:
        sug (suggest.Suggester): Suggester holding the rules
        pws (dict): Series, as from patchwork or lore
        url (str): Link to the series

    Returns:
        list of str: Lines to show
    """
    lines = [suggest.series_title(pws), f'  {url}']
    done, facts, problems = common.run(_facts(sug, pws))
    rules = sug.rules.rules
    results = [_run(rule, facts) for rule in rules]
    table, score = _table(rules, results)
    lines += table + problems
    if lookup.is_own_series(pws, sug.emails, sug.own_db):
        lines.append('Not suggested: it is your own series')
    elif done:
        lines.append('Not suggested: you have reviewed this version')
    else:
        lines.append(_verdict(sug, rules, results, score))
    return lines


# pylint: disable-next=R0913
def do_explain(args, pwork, cser, own_emails, own_db, link):
    """Show how the rules for suggesting series score one series

    Args:
        args (Namespace): Command-line arguments (for limit)
        pwork (Patchwork or LoreList): Where the series comes from
        cser (Cseries): Open cseries instance with the reviews
        own_emails (set of str): Your email addresses, in lower case
        own_db (Database): Database holding your own series
        link (str): Series link

    Returns:
        int: 0 on success, 1 if there are no rules

    Raises:
        ValueError: A rule or setting is not valid, or the series cannot be
            read
    """
    rules = suggest_rules.get_rules()
    if not rules.rules:
        tout.error(suggest_rules.show_rules(rules,
                                            settings.review_project())[0])
        return 1
    for warning in suggest_rules.check_rules(
            rules, isinstance(pwork, lorelist.LoreList)):
        tout.warning(warning)
    pws = common.pw_call(pwork.get_series, link)
    sug = suggest.Suggester(rules, pwork, cser, own_emails, own_db,
                            datetime.now(timezone.utc),
                            getattr(args, 'limit', None) or rules.limit,
                            suggest.upstream_ref(args, cser, rules))
    url = common.run(pwork.get_series_url(link))
    for line in explain(sug, pws, url):
        print(line)
    return 0
