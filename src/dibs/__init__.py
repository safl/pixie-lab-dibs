# SPDX-FileCopyrightText: Simon A. F. Lund <os@safl.dk>
#
# SPDX-License-Identifier: BSD-3-Clause

"""dibs: call dibs on shared lab machines.

People, ai-agents and automation-runners share lab machines. dibs keeps
a list of the things they share, fed from sources such as a config file
or a pixie server, and lets each of them see who is using a thing,
claim it, and release it. A claim is a note, not a lock: it tells
everyone who is on a machine, and it never stops anyone from logging
in.
"""

__version__ = "0.2.1"
