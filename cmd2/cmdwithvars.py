# coding=utf-8
""" Variables support plugin """

import shlex

from .plugin import PostparsingData
from .cmd2 import Cmd

class VariableNotFound(Exception):
    pass

class ValueNotFound(Exception):
    pass

class CmdWithVars(Cmd):
    """
    Variables support for cmd2 shell

    Defines two new command:
    * let command declares and sets variable value
    * del command removes that variable

    To reference variable in command's args, use $varname syntax
    Example which works out of the box:
        let cmd quit
        help $cmd
        del cmd

    Variable prefix (by default shell-like $) can be changes by passing 'var_prefix' parameter to the initializer

    """
    def __init__(self, *args, var_prefix = '$', **kwargs):
        super(CmdWithVars, self).__init__(*args, **kwargs)
        self._var_prefix = var_prefix
        self._variables = {}
        self.register_postparsing_hook(self._expand_variables)

    def _expand_variables(self, params : PostparsingData) -> PostparsingData:
        ''' A hook used to substitute existing varialbles '''
        def expand(arg : str):
            if arg.startswith(self._var_prefix):
                if not arg in self._variables:
                    raise VariableNotFound()
                return self._variables[arg]
            else:
                return arg

        newinput = ' '.join([params.statement.command] + [expand(a) for a in params.statement.arg_list])
        params.statement = self.statement_parser.parse(newinput)

        return params

    def _varname(self, arg):
        return self._var_prefix + arg

    def do_let(self, arg):
        ''' Declare a variable and set its value: let <var-name> <var-value> '''
        args = shlex.split(arg)

        if len(args) != 2:
            raise ValueNotFound()

        self._variables[self._varname(args[0])] = args[1]

    def do_del(self, arg):
        ''' Remove a variable: del <var-name> '''
        var = self._varname(arg)
        if not var in self._variables:
            raise VariableNotFound()
        del self._variables[var]

