# coding=utf-8
"""Variable support for cmd2 shell"""

from . import cmd2

class CmdVars(cmd2.Cmd):
    def __init__(self, *args, **kwargs):
        super(CmdVars, self).__init__(*args, **kwargs)

