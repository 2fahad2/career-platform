"""What code is ACTUALLY running, as opposed to what was committed.

This exists because of a failure that cost four days without anyone noticing.
Thirty-two commits were written, gated, committed and pushed — the phone-shape
fix that made zero-touch activation possible for a real customer, the move of
delivery from dawn to 11:00, the UTILITY template that costs less than half a
marketing one, the outcome question, the renewal path — and the container
serving both webhooks was still running the image built four days earlier.
Every one of those fixes was true in the repository and absent from the running
system. The watchtower showed green throughout, because everything it probed
(the worker, the timers, the backup) really was healthy: the worker and the
nightly engine run from the host checkout and so were current, while the API
image is baked at build time and was not. Nothing compared the two.

«Committed» is not «deployed», and on a one-operator product there is no
release process that would notice the gap. So the gap is measured instead: a
content hash of the Python source of this package, computed the same way on
both sides. The API reports its own, the watchtower computes the repository's,
and a mismatch is a red line on the health screen naming the two.

Deliberately a content hash and not a git revision: a revision has to be
injected at build time, which is one more step to forget — and forgetting a
step is exactly the failure being fixed. Content compares itself.
"""

from __future__ import annotations

import hashlib
import pathlib

#: Short enough to read off a phone screen, long enough that two different
#: trees will not collide in the lifetime of this product.
_DIGEST_CHARS = 12


def source_fingerprint(root: pathlib.Path | None = None) -> str:
    """A stable hash of every ``.py`` file in the ``career`` package.

    Same tree → same string, on any machine, in any order, with or without
    ``__pycache__`` present. Deleting a file changes it, because the path is
    hashed alongside the bytes.
    """
    base = root or pathlib.Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(base.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        digest.update(str(path.relative_to(base)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:_DIGEST_CHARS]
