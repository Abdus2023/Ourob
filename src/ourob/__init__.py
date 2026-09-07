"""ourob -- an autonomous-engineering runtime that lives inside the repository it engineers.

The premise is narrow and testable: the source, the skills, the policies, the
state model, the verification gates and the bootstrap mechanism are all ordinary
files in this repository, and the runtime is able to edit them.  What makes that
safe rather than merely possible is that every edit passes through the same
policy set, is written to a tamper-evident journal, and is only *accepted* if the
verification suite is green afterwards.

Quick orientation::

    ourob.kernel          the loop
    ourob.skills          what the runtime can do (discoverable, self-extendable)
    ourob.policies        what it is allowed to do
    ourob.state           what it remembers (append-only, hash-chained)
    ourob.verify          how a change is judged
    ourob.bootstrap       how the runtime rebuilds itself from this directory
"""

from __future__ import annotations

__version__ = "0.1.0"

REPO_MARKER = "ourob.toml"

__all__ = ["__version__", "REPO_MARKER"]
