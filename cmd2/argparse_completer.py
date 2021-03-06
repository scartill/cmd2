# coding=utf-8
# flake8: noqa C901
# NOTE: Ignoring flake8 cyclomatic complexity in this file
"""
This module defines the AutoCompleter class which provides argparse-based tab completion to cmd2 apps.
See the header of argparse_custom.py for instructions on how to use these features.
"""

import argparse
import inspect
import numbers
import shutil
import textwrap
from collections import deque
from typing import Dict, List, Optional, Union

from . import cmd2
from . import utils
from .ansi import ansi_aware_write, ansi_safe_wcswidth, style_error
from .argparse_custom import ATTR_CHOICES_CALLABLE, INFINITY, generate_range_error
from .argparse_custom import ATTR_SUPPRESS_TAB_HINT, ATTR_DESCRIPTIVE_COMPLETION_HEADER, ATTR_NARGS_RANGE
from .argparse_custom import ChoicesCallable, CompletionError, CompletionItem
from .rl_utils import rl_force_redisplay

# If no descriptive header is supplied, then this will be used instead
DEFAULT_DESCRIPTIVE_HEADER = 'Description'

# Name of the choice/completer function argument that, if present, will be passed a dictionary of
# command line tokens up through the token being completed mapped to their argparse destination name.
ARG_TOKENS = 'arg_tokens'


def _single_prefix_char(token: str, parser: argparse.ArgumentParser) -> bool:
    """Returns if a token is just a single flag prefix character"""
    return len(token) == 1 and token[0] in parser.prefix_chars


# noinspection PyProtectedMember
def _looks_like_flag(token: str, parser: argparse.ArgumentParser) -> bool:
    """
    Determine if a token looks like a flag. Unless an argument has nargs set to argparse.REMAINDER,
    then anything that looks like a flag can't be consumed as a value for it.
    Based on argparse._parse_optional().
    """
    # Flags have to be at least characters
    if len(token) < 2:
        return False

    # Flags have to start with a prefix character
    if not token[0] in parser.prefix_chars:
        return False

    # If it looks like a negative number, it is not a flag unless there are negative-number-like flags
    if parser._negative_number_matcher.match(token):
        if not parser._has_negative_number_optionals:
            return False

    # Flags can't have a space
    if ' ' in token:
        return False

    # Starts like a flag
    return True


# noinspection PyProtectedMember
class AutoCompleter(object):
    """Automatic command line tab completion based on argparse parameters"""

    class _ArgumentState(object):
        """Keeps state of an argument being parsed"""

        def __init__(self, arg_action: argparse.Action) -> None:
            self.action = arg_action
            self.min = None
            self.max = None
            self.count = 0
            self.is_remainder = (self.action.nargs == argparse.REMAINDER)

            # Check if nargs is a range
            nargs_range = getattr(self.action, ATTR_NARGS_RANGE, None)
            if nargs_range is not None:
                self.min = nargs_range[0]
                self.max = nargs_range[1]

            # Otherwise check against argparse types
            elif self.action.nargs is None:
                self.min = 1
                self.max = 1
            elif self.action.nargs == argparse.OPTIONAL:
                self.min = 0
                self.max = 1
            elif self.action.nargs == argparse.ZERO_OR_MORE or self.action.nargs == argparse.REMAINDER:
                self.min = 0
                self.max = INFINITY
            elif self.action.nargs == argparse.ONE_OR_MORE:
                self.min = 1
                self.max = INFINITY
            else:
                self.min = self.action.nargs
                self.max = self.action.nargs

    def __init__(self, parser: argparse.ArgumentParser, cmd2_app: cmd2.Cmd, *,
                 parent_tokens: Optional[Dict[str, List[str]]] = None) -> None:
        """
        Create an AutoCompleter

        :param parser: ArgumentParser instance
        :param cmd2_app: reference to the Cmd2 application that owns this AutoCompleter
        :param parent_tokens: optional dictionary mapping parent parsers' arg names to their tokens
                              this is only used by AutoCompleter when recursing on subcommand parsers
                              Defaults to None
        """
        self._parser = parser
        self._cmd2_app = cmd2_app

        if parent_tokens is None:
            parent_tokens = dict()
        self._parent_tokens = parent_tokens

        self._flags = []                      # all flags in this command
        self._flag_to_action = {}             # maps flags to the argparse action object
        self._positional_actions = []         # actions for positional arguments (by position index)
        self._subcommand_action = None        # this will be set if self._parser has subcommands

        # Start digging through the argparse structures.
        # _actions is the top level container of parameter definitions
        for action in self._parser._actions:
            # if the parameter is flag based, it will have option_strings
            if action.option_strings:
                # record each option flag
                for option in action.option_strings:
                    self._flags.append(option)
                    self._flag_to_action[option] = action

            # Otherwise this is a positional parameter
            else:
                self._positional_actions.append(action)
                # Check if this action defines subcommands
                if isinstance(action, argparse._SubParsersAction):
                    self._subcommand_action = action

    def complete_command(self, tokens: List[str], text: str, line: str, begidx: int, endidx: int) -> List[str]:
        """Complete the command using the argparse metadata and provided argument dictionary"""
        if not tokens:
            return []

        # Positionals args that are left to parse
        remaining_positionals = deque(self._positional_actions)

        # This gets set to True when flags will no longer be processed as argparse flags
        # That can happen when -- is used or an argument with nargs=argparse.REMAINDER is used
        skip_remaining_flags = False

        # _ArgumentState of the current positional
        pos_arg_state = None

        # _ArgumentState of the current flag
        flag_arg_state = None

        # Non-reusable flags that we've parsed
        matched_flags = []

        # Keeps track of arguments we've seen and any tokens they consumed
        consumed_arg_values = dict()  # dict(arg_name -> List[tokens])

        # Completed mutually exclusive groups
        completed_mutex_groups = dict()  # dict(argparse._MutuallyExclusiveGroup -> Action which completed group)

        def consume_argument(arg_state: AutoCompleter._ArgumentState) -> None:
            """Consuming token as an argument"""
            arg_state.count += 1
            consumed_arg_values.setdefault(arg_state.action.dest, [])
            consumed_arg_values[arg_state.action.dest].append(token)

        def update_mutex_groups(arg_action: argparse.Action) -> bool:
            """
            Check if an argument belongs to a mutually exclusive group and either mark that group
            as complete or print an error if the group has already been completed
            :param arg_action: the action of the argument
            :return: False if the group has already been completed and there is a conflict, otherwise True
            """
            # Check if this action is in a mutually exclusive group
            for group in self._parser._mutually_exclusive_groups:
                if arg_action in group._group_actions:

                    # Check if the group this action belongs to has already been completed
                    if group in completed_mutex_groups:

                        # If this is the action that completed the group, then there is no error
                        # since it's allowed to appear on the command line more than once.
                        completer_action = completed_mutex_groups[group]
                        if arg_action == completer_action:
                            return True

                        error = style_error("\nError: argument {}: not allowed with argument {}\n".
                                            format(argparse._get_action_name(arg_action),
                                                   argparse._get_action_name(completer_action)))
                        self._print_message(error)
                        return False

                    # Mark that this action completed the group
                    completed_mutex_groups[group] = arg_action

                    # Don't tab complete any of the other args in the group
                    for group_action in group._group_actions:
                        if group_action == arg_action:
                            continue
                        elif group_action in self._flag_to_action.values():
                            matched_flags.extend(group_action.option_strings)
                        elif group_action in remaining_positionals:
                            remaining_positionals.remove(group_action)

                    # Arg can only be in one group, so we are done
                    break

            return True

        #############################################################################################
        # Parse all but the last token
        #############################################################################################
        for token_index, token in enumerate(tokens[1:-1], start=1):

            # If we're in a positional REMAINDER arg, force all future tokens to go to that
            if pos_arg_state is not None and pos_arg_state.is_remainder:
                consume_argument(pos_arg_state)
                continue

            # If we're in a flag REMAINDER arg, force all future tokens to go to that until a double dash is hit
            elif flag_arg_state is not None and flag_arg_state.is_remainder:
                if token == '--':
                    flag_arg_state = None
                else:
                    consume_argument(flag_arg_state)
                continue

            # Handle '--' which tells argparse all remaining arguments are non-flags
            elif token == '--' and not skip_remaining_flags:
                # Check if there is an unfinished flag
                if flag_arg_state is not None and flag_arg_state.count < flag_arg_state.min:
                    self._print_unfinished_flag_error(flag_arg_state)
                    return []

                # Otherwise end the current flag
                else:
                    flag_arg_state = None
                    skip_remaining_flags = True
                    continue

            # Check the format of the current token to see if it can be an argument's value
            if _looks_like_flag(token, self._parser) and not skip_remaining_flags:

                # Check if there is an unfinished flag
                if flag_arg_state is not None and flag_arg_state.count < flag_arg_state.min:
                    self._print_unfinished_flag_error(flag_arg_state)
                    return []

                # Reset flag arg state but not positional tracking because flags can be
                # interspersed anywhere between positionals
                flag_arg_state = None
                action = None

                # Does the token match a known flag?
                if token in self._flag_to_action:
                    action = self._flag_to_action[token]
                elif self._parser.allow_abbrev:
                    candidates_flags = [flag for flag in self._flag_to_action if flag.startswith(token)]
                    if len(candidates_flags) == 1:
                        action = self._flag_to_action[candidates_flags[0]]

                if action is not None:
                    if not update_mutex_groups(action):
                        return []

                    if isinstance(action, (argparse._AppendAction,
                                           argparse._AppendConstAction,
                                           argparse._CountAction)):
                        # Flags with action set to append, append_const, and count can be reused
                        # Therefore don't erase any tokens already consumed for this flag
                        consumed_arg_values.setdefault(action.dest, [])
                    else:
                        # This flag is not reusable, so mark that we've seen it
                        matched_flags.extend(action.option_strings)

                        # It's possible we already have consumed values for this flag if it was used
                        # earlier in the command line. Reset them now for this use of it.
                        consumed_arg_values[action.dest] = []

                    new_arg_state = AutoCompleter._ArgumentState(action)

                    # Keep track of this flag if it can receive arguments
                    if new_arg_state.max > 0:
                        flag_arg_state = new_arg_state
                        skip_remaining_flags = flag_arg_state.is_remainder

            # Check if we are consuming a flag
            elif flag_arg_state is not None:
                consume_argument(flag_arg_state)

                # Check if we have finished with this flag
                if flag_arg_state.count >= flag_arg_state.max:
                    flag_arg_state = None

            # Otherwise treat as a positional argument
            else:
                # If we aren't current tracking a positional, then get the next positional arg to handle this token
                if pos_arg_state is None:
                    # Make sure we are still have positional arguments to parse
                    if remaining_positionals:
                        action = remaining_positionals.popleft()

                        # Are we at a subcommand? If so, forward to the matching completer
                        if action == self._subcommand_action:
                            if token in self._subcommand_action.choices:
                                # Merge self._parent_tokens and consumed_arg_values
                                parent_tokens = {**self._parent_tokens, **consumed_arg_values}

                                # Include the subcommand name if its destination was set
                                if action.dest != argparse.SUPPRESS:
                                    parent_tokens[action.dest] = [token]

                                completer = AutoCompleter(self._subcommand_action.choices[token], self._cmd2_app,
                                                          parent_tokens=parent_tokens)
                                return completer.complete_command(tokens[token_index:], text, line, begidx, endidx)
                            else:
                                # Invalid subcommand entered, so no way to complete remaining tokens
                                return []

                        # Otherwise keep track of the argument
                        else:
                            pos_arg_state = AutoCompleter._ArgumentState(action)

                # Check if we have a positional to consume this token
                if pos_arg_state is not None:
                    # No need to check for an error since we remove a completed group's positional from
                    # remaining_positionals which means this action can't belong to a completed mutex group
                    update_mutex_groups(pos_arg_state.action)

                    consume_argument(pos_arg_state)

                    # No more flags are allowed if this is a REMAINDER argument
                    if pos_arg_state.is_remainder:
                        skip_remaining_flags = True

                    # Check if we have finished with this positional
                    elif pos_arg_state.count >= pos_arg_state.max:
                        pos_arg_state = None

                        # Check if the next positional has nargs set to argparse.REMAINDER.
                        # At this point argparse allows no more flags to be processed.
                        if remaining_positionals and remaining_positionals[0].nargs == argparse.REMAINDER:
                            skip_remaining_flags = True

        #############################################################################################
        # We have parsed all but the last token and have enough information to complete it
        #############################################################################################

        # Check if we are completing a flag name. This check ignores strings with a length of one, like '-'.
        # This is because that could be the start of a negative number which may be a valid completion for
        # the current argument. We will handle the completion of flags that start with only one prefix
        # character (-f) at the end.
        if _looks_like_flag(text, self._parser) and not skip_remaining_flags:
            if flag_arg_state is not None and flag_arg_state.count < flag_arg_state.min:
                self._print_unfinished_flag_error(flag_arg_state)
                return []

            return self._complete_flags(text, line, begidx, endidx, matched_flags)

        completion_results = []

        # Check if we are completing a flag's argument
        if flag_arg_state is not None:
            try:
                completion_results = self._complete_for_arg(flag_arg_state.action, text, line,
                                                            begidx, endidx, consumed_arg_values)
            except CompletionError as ex:
                self._print_completion_error(flag_arg_state.action, ex)
                return []

            # If we have results, then return them
            if completion_results:
                return completion_results

            # Otherwise, print a hint if the flag isn't finished or text isn't possibly the start of a flag
            elif flag_arg_state.count < flag_arg_state.min or \
                    not _single_prefix_char(text, self._parser) or skip_remaining_flags:
                self._print_arg_hint(flag_arg_state.action)
                return []

        # Otherwise check if we have a positional to complete
        elif pos_arg_state is not None or remaining_positionals:

            # If we aren't current tracking a positional, then get the next positional arg to handle this token
            if pos_arg_state is None:
                action = remaining_positionals.popleft()
                pos_arg_state = AutoCompleter._ArgumentState(action)

            try:
                completion_results = self._complete_for_arg(pos_arg_state.action, text, line,
                                                            begidx, endidx, consumed_arg_values)
            except CompletionError as ex:
                self._print_completion_error(pos_arg_state.action, ex)
                return []

            # If we have results, then return them
            if completion_results:
                return completion_results

            # Otherwise, print a hint if text isn't possibly the start of a flag
            elif not _single_prefix_char(text, self._parser) or skip_remaining_flags:
                self._print_arg_hint(pos_arg_state.action)
                return []

        # Handle case in which text is a single flag prefix character that
        # didn't complete against any argument values.
        if _single_prefix_char(text, self._parser) and not skip_remaining_flags:
            return self._complete_flags(text, line, begidx, endidx, matched_flags)

        return completion_results

    def _complete_flags(self, text: str, line: str, begidx: int, endidx: int, matched_flags: List[str]) -> List[str]:
        """Tab completion routine for a parsers unused flags"""

        # Build a list of flags that can be tab completed
        match_against = []

        for flag in self._flags:
            # Make sure this flag hasn't already been used
            if flag not in matched_flags:
                # Make sure this flag isn't considered hidden
                action = self._flag_to_action[flag]
                if action.help != argparse.SUPPRESS:
                    match_against.append(flag)

        return utils.basic_complete(text, line, begidx, endidx, match_against)

    def _format_completions(self, action, completions: List[Union[str, CompletionItem]]) -> List[str]:
        # Check if the results are CompletionItems and that there aren't too many to display
        if 1 < len(completions) <= self._cmd2_app.max_completion_items and \
                isinstance(completions[0], CompletionItem):

            # If the user has not already sorted the CompletionItems, then sort them before appending the descriptions
            if not self._cmd2_app.matches_sorted:
                completions.sort(key=self._cmd2_app.default_sort_key)
                self._cmd2_app.matches_sorted = True

            token_width = ansi_safe_wcswidth(action.dest)
            completions_with_desc = []

            for item in completions:
                item_width = ansi_safe_wcswidth(item)
                if item_width > token_width:
                    token_width = item_width

            term_size = shutil.get_terminal_size()
            fill_width = int(term_size.columns * .6) - (token_width + 2)
            for item in completions:
                entry = '{: <{token_width}}{: <{fill_width}}'.format(item, item.description,
                                                                     token_width=token_width + 2,
                                                                     fill_width=fill_width)
                completions_with_desc.append(entry)

            desc_header = getattr(action, ATTR_DESCRIPTIVE_COMPLETION_HEADER, None)
            if desc_header is None:
                desc_header = DEFAULT_DESCRIPTIVE_HEADER
            header = '\n{: <{token_width}}{}'.format(action.dest.upper(), desc_header, token_width=token_width + 2)

            self._cmd2_app.completion_header = header
            self._cmd2_app.display_matches = completions_with_desc

        return completions

    def complete_subcommand_help(self, tokens: List[str], text: str, line: str, begidx: int, endidx: int) -> List[str]:
        """
        Supports cmd2's help command in the completion of subcommand names
        :param tokens: command line tokens
        :param text: the string prefix we are attempting to match (all matches must begin with it)
        :param line: the current input line with leading whitespace removed
        :param begidx: the beginning index of the prefix text
        :param endidx: the ending index of the prefix text
        :return: List of subcommand completions
        """
        # If our parser has subcommands, we must examine the tokens and check if they are subcommands
        # If so, we will let the subcommand's parser handle the rest of the tokens via another AutoCompleter.
        if self._subcommand_action is not None:
            for token_index, token in enumerate(tokens[1:], start=1):
                if token in self._subcommand_action.choices:
                    completer = AutoCompleter(self._subcommand_action.choices[token], self._cmd2_app)
                    return completer.complete_subcommand_help(tokens[token_index:], text, line, begidx, endidx)
                elif token_index == len(tokens) - 1:
                    # Since this is the last token, we will attempt to complete it
                    return utils.basic_complete(text, line, begidx, endidx, self._subcommand_action.choices)
                else:
                    break
        return []

    def format_help(self, tokens: List[str]) -> str:
        """
        Supports cmd2's help command in the retrieval of help text
        :param tokens: command line tokens
        :return: help text of the command being queried
        """
        # If our parser has subcommands, we must examine the tokens and check if they are subcommands
        # If so, we will let the subcommand's parser handle the rest of the tokens via another AutoCompleter.
        if self._subcommand_action is not None:
            for token_index, token in enumerate(tokens[1:], start=1):
                if token in self._subcommand_action.choices:
                    completer = AutoCompleter(self._subcommand_action.choices[token], self._cmd2_app)
                    return completer.format_help(tokens[token_index:])
                else:
                    break
        return self._parser.format_help()

    def _complete_for_arg(self, arg_action: argparse.Action,
                          text: str, line: str, begidx: int, endidx: int,
                          consumed_arg_values: Dict[str, List[str]]) -> List[str]:
        """
        Tab completion routine for an argparse argument
        :return: list of completions
        :raises CompletionError if the completer or choices function this calls raises one
        """
        # Check if the arg provides choices to the user
        if arg_action.choices is not None:
            arg_choices = arg_action.choices
        else:
            arg_choices = getattr(arg_action, ATTR_CHOICES_CALLABLE, None)

        if arg_choices is None:
            return []

        # If we are going to call a completer/choices function, then set up the common arguments
        args = []
        kwargs = {}
        if isinstance(arg_choices, ChoicesCallable):
            if arg_choices.is_method:
                args.append(self._cmd2_app)

            # Check if arg_choices.to_call expects arg_tokens
            to_call_params = inspect.signature(arg_choices.to_call).parameters
            if ARG_TOKENS in to_call_params:
                # Merge self._parent_tokens and consumed_arg_values
                arg_tokens = {**self._parent_tokens, **consumed_arg_values}

                # Include the token being completed
                arg_tokens.setdefault(arg_action.dest, [])
                arg_tokens[arg_action.dest].append(text)

                # Add the namespace to the keyword arguments for the function we are calling
                kwargs[ARG_TOKENS] = arg_tokens

        # Check if the argument uses a specific tab completion function to provide its choices
        if isinstance(arg_choices, ChoicesCallable) and arg_choices.is_completer:
            args.extend([text, line, begidx, endidx])
            results = arg_choices.to_call(*args, **kwargs)

        # Otherwise use basic_complete on the choices
        else:
            # Check if the choices come from a function
            if isinstance(arg_choices, ChoicesCallable) and not arg_choices.is_completer:
                arg_choices = arg_choices.to_call(*args, **kwargs)

            # Since arg_choices can be any iterable type, convert to a list
            arg_choices = list(arg_choices)

            # If these choices are numbers, and have not yet been sorted, then sort them now
            if not self._cmd2_app.matches_sorted and all(isinstance(x, numbers.Number) for x in arg_choices):
                arg_choices.sort()
                self._cmd2_app.matches_sorted = True

            # Since choices can be various types like int, we must convert them to strings
            for index, choice in enumerate(arg_choices):
                if not isinstance(choice, str):
                    arg_choices[index] = str(choice)

            # Filter out arguments we already used
            used_values = consumed_arg_values.get(arg_action.dest, [])
            arg_choices = [choice for choice in arg_choices if choice not in used_values]

            # Do tab completion on the choices
            results = utils.basic_complete(text, line, begidx, endidx, arg_choices)

        return self._format_completions(arg_action, results)

    @staticmethod
    def _print_message(msg: str) -> None:
        """Print a message instead of tab completions and redraw the prompt and input line"""
        import sys
        ansi_aware_write(sys.stdout, msg + '\n')
        rl_force_redisplay()

    def _print_arg_hint(self, arg_action: argparse.Action) -> None:
        """
        Print argument hint to the terminal when tab completion results in no results
        :param arg_action: action being tab completed
        """
        # Check if hinting is disabled
        suppress_hint = getattr(arg_action, ATTR_SUPPRESS_TAB_HINT, False)
        if suppress_hint or arg_action.help == argparse.SUPPRESS:
            return

        # Use the parser's help formatter to print just this action's help text
        formatter = self._parser._get_formatter()
        formatter.start_section("Hint")
        formatter.add_argument(arg_action)
        formatter.end_section()
        out_str = formatter.format_help()
        self._print_message('\n' + out_str)

    def _print_unfinished_flag_error(self, flag_arg_state: _ArgumentState) -> None:
        """
        Print an error during tab completion when the user has not finished the current flag
        :param flag_arg_state: information about the unfinished flag action
        """
        error = "\nError: argument {}: {} ({} entered)\n".\
            format(argparse._get_action_name(flag_arg_state.action),
                   generate_range_error(flag_arg_state.min, flag_arg_state.max),
                   flag_arg_state.count)
        self._print_message(style_error('{}'.format(error)))

    def _print_completion_error(self, arg_action: argparse.Action, completion_error: CompletionError) -> None:
        """
        Print a CompletionError to the user
        :param arg_action: action being tab completed
        :param completion_error: error that occurred
        """
        # Indent all lines of completion_error
        indented_error = textwrap.indent(str(completion_error), '  ')

        error = ("\nError tab completing {}:\n"
                 "{}\n".format(argparse._get_action_name(arg_action), indented_error))
        self._print_message(style_error('{}'.format(error)))
