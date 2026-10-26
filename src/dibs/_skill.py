# SPDX-FileCopyrightText: Simon A. F. Lund <os@safl.dk>
#
# SPDX-License-Identifier: BSD-3-Clause

"""The agent skill, shared by ``dibs skill`` and the server's ``/agents``
page, so both say exactly the same thing."""

from __future__ import annotations

import re
from pathlib import Path

SKILL_PATH = Path(__file__).parent / "skill" / "SKILL.md"

# The YAML frontmatter Claude Code reads, then the REUSE comment.
_PREAMBLE = re.compile(r"\A---\n.*?\n---\n\s*<!--.*?-->\s*", re.DOTALL)


def skill_text() -> str:
    """The skill as Claude Code installs it."""
    return SKILL_PATH.read_text()


def skill_body() -> str:
    """The protocol alone, for readers that are not Claude Code."""
    return _PREAMBLE.sub("", skill_text(), count=1)
