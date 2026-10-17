# SPDX-License-Identifier: GPL-2.0+
"""Sphinx extension which documents patman's commands from its parser

This adds a 'patman-command' directive, which writes out the usage,
arguments and options of a command, taken from the argparse parser that
patman itself uses, so the reference cannot drift from the code::

    .. patman-command:: series

documents 'patman series' and each of its subcommands. With no argument
it documents the options which apply to all commands. With the
':commands:' option it lists the commands instead, each with its help.
"""

# This reads argparse's internals to walk the parser
# pylint: disable=protected-access

import argparse
import copy
import os
import pathlib
import sys

from docutils import nodes
from docutils.parsers.rst import Directive, directives
from docutils.statemachine import StringList
from sphinx.util import logging
from sphinx.util.nodes import nested_parse_with_titles

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

# The parser needs u_boot_pylib. Without it, build the rest of the docs and
# say how to get the reference
# pylint: disable=C0413
try:
    from patman import cmdline
    IMPORT_ERROR = None
except ImportError as exc:
    cmdline = None
    IMPORT_ERROR = exc

MISSING_HELP = ("The command reference needs u_boot_pylib to read patman's "
                "options ({}). Install it with 'pip install -r "
                "doc/requirements.txt', or set PYTHONPATH to a checkout of "
                'u-boot-pylib.')

logger = logging.getLogger(__name__)

# Defaults which depend on where the docs are built, and what to show instead
BUILD_DEFAULTS = {
    'project': 'detected from the git tree',
}

# Commands which are not documented in the reference
SKIP_COMMANDS = {'test'}

# Underline for each heading level below the page title
UNDERLINES = '-~^'


def get_parser():
    """Get patman's top-level parser, with 'patman' as its program name

    Returns:
        argparse.ArgumentParser: The parser
    """
    old = sys.argv[0]
    sys.argv[0] = 'patman'
    try:
        return cmdline.setup_parser()['main']
    finally:
        sys.argv[0] = old


def subcommands(parser):
    """Get the subcommands of a parser, without repeating their aliases

    Args:
        parser (argparse.ArgumentParser): Parser to look in

    Returns:
        list of tuple:
            str: Name of the subcommand
            list of str: Its aliases
            str: Its help text, or ''
            argparse.ArgumentParser: Its parser
    """
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            helps = {act.dest: act.help or ''
                     for act in action._choices_actions}
            by_parser = {}
            for name, sub in action.choices.items():
                if id(sub) in by_parser:
                    by_parser[id(sub)][1].append(name)
                else:
                    by_parser[id(sub)] = [name, [], sub]
            return [(name, aliases, helps.get(name, ''), sub)
                    for name, aliases, sub in by_parser.values()]
    return []


def expand_help(parser, action):
    """Get the help text for an action, with its default filled in

    Args:
        parser (argparse.ArgumentParser): Parser the action belongs to
        action (argparse.Action): Action to describe

    Returns:
        str: Help text
    """
    if not action.help:
        return ''
    if action.dest in BUILD_DEFAULTS:
        action = copy.copy(action)
        action.default = BUILD_DEFAULTS[action.dest]
    fmt = parser._get_formatter()
    return ' '.join(fmt._expand_help(action).split())


def term(action):
    """Get the term for an argument, e.g. '-c, --count COUNT'

    Args:
        action (argparse.Action): Action to describe

    Returns:
        str: The term, as RST
    """
    if not action.option_strings:
        return f'``{action.metavar or action.dest}``'
    metavar = ''
    if action.nargs != 0:
        metavar = ' ' + (action.metavar or action.dest.upper())
    return ', '.join(f'``{opt}{metavar}``' for opt in action.option_strings)


def describe(parser, out):
    """Write out the usage and arguments of a parser

    Args:
        parser (argparse.ArgumentParser): Parser to describe
        out (list of str): Lines of RST to add to
    """
    usage = parser.format_usage().replace('usage: ', '', 1).rstrip()
    out += ['::', '']
    out += ['    ' + line for line in usage.splitlines()]
    out.append('')
    for title, positional in (('Arguments', True), ('Options', False)):
        actions = [act for act in parser._actions
                   if bool(act.option_strings) != positional
                   and act.help != argparse.SUPPRESS
                   and not isinstance(act, (argparse._HelpAction,
                                            argparse._SubParsersAction))]
        if not actions:
            continue
        out += [f'.. rubric:: {title}', '']
        for act in actions:
            out.append(term(act))
            out.append('    ' + (expand_help(parser, act) or '(no help)'))
            out.append('')


def write_command(name, parser, depth, out):
    """Write out a subcommand, and any subcommands it has

    Args:
        name (str): Full name, e.g. 'patman series add'
        parser (argparse.ArgumentParser): Its parser
        depth (int): Heading level, 0 for the first level below the title
        out (list of str): Lines of RST to add to
    """
    describe(parser, out)
    for subname, aliases, text, sub in subcommands(parser):
        title = f'{name} {subname}'
        out += [title, UNDERLINES[depth] * len(title), '']
        if text:
            out += [text.rstrip('.') + '.', '']
        if aliases:
            out += ['Aliases: ' + ', '.join(f'``{al}``' for al in aliases), '']
        write_command(title, sub, depth + 1, out)


class PatmanCommand(Directive):
    """Document a patman command, or the global options if none is given"""
    optional_arguments = 1
    option_spec = {'commands': directives.flag}

    def run(self):
        if IMPORT_ERROR:
            if not getattr(PatmanCommand, 'warned', False):
                logger.warning(MISSING_HELP.format(IMPORT_ERROR))
                PatmanCommand.warned = True
            para = nodes.paragraph(
                text=MISSING_HELP.format(IMPORT_ERROR).replace(
                    'The command reference', 'This page'))
            return [nodes.warning('', para)]
        parser = get_parser()
        out = []
        if self.arguments:
            name = self.arguments[0]
            for subname, aliases, text, sub in subcommands(parser):
                if subname == name:
                    if aliases:
                        out += ['Aliases: ' +
                                ', '.join(f'``{al}``' for al in aliases), '']
                    write_command(f'patman {name}', sub, 0, out)
                    break
            else:
                raise self.error(f"Unknown patman command '{name}'")
        elif 'commands' in self.options:
            for subname, _, text, _ in subcommands(parser):
                if subname not in SKIP_COMMANDS:
                    out += [f':doc:`{subname}`', f'    {text}', '']
        else:
            describe(parser, out)

        node = nodes.container()
        nested_parse_with_titles(self.state,
                                 StringList(out, source=os.path.basename(
                                     __file__)), node)
        return node.children


def setup(app):
    """Set up the extension"""
    app.add_directive('patman-command', PatmanCommand)
    return {'version': '1.0', 'parallel_read_safe': True}
