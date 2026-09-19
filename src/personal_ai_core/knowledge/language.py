"""Language filtering policy for retrieval.

One module, one rule, applied identically by both index arms -- because the
audited source got this wrong in a way that only showed up as missing results.

**The verified legacy defect.** In the audited `search_brain()`, passing a
`language` or `chunk_type` filter always returned an empty list. Not fewer
results: zero. A caller asking for Arabic content got nothing back and no
error, which reads exactly like "there is no Arabic content".

Two lessons are encoded here:

  1. The policy lives in one place, not duplicated into each index, so the
     two arms cannot drift apart.
  2. Chunks whose language is undetermined stay *reachable*. A strict equality
     filter would make every unlabelled chunk invisible the moment a caller
     names a language -- the same user-visible symptom as the legacy bug, from
     a different cause. Undetermined means unknown, not "not this one".
"""
from __future__ import annotations

from ..core.domain import UNDETERMINED_LANGUAGE


def language_matches(chunk_language: str, requested: str | None) -> bool:
    """Whether a chunk may be returned for a request in `requested`.

    No request, or a request for undetermined, means no filtering at all.
    Otherwise a chunk matches when it declares the requested language or
    declares none.
    """
    if requested is None or requested == UNDETERMINED_LANGUAGE:
        return True
    if chunk_language == UNDETERMINED_LANGUAGE:
        return True
    return chunk_language == requested
