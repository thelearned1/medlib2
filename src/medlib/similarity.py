"""Tag similarity: which unrelated tags mark much the same files."""
from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Dict, List, Set, Tuple

from .models import SimilarTags

if TYPE_CHECKING:
    from .library import Library


def _wilson_lower(successes: int, n: int, z: float = 1.0) -> float:
    """Lower bound of the Wilson score interval for ``successes / n``: the
    proportion, discounted for how little evidence backs it."""
    if n == 0:
        return 0.0
    p = successes / n
    z2 = z * z
    centre = p + z2 / (2 * n)
    margin = z * ((p * (1 - p) + z2 / (4 * n)) / n) ** 0.5
    return max(0.0, (centre - margin) / (1 + z2 / n))


# Every tag paired with itself and each tag above it, along every parent.
_ANCESTORS = """
WITH RECURSIVE anc(tag_id, ancestor) AS (
    SELECT id, id FROM tags
    UNION
    SELECT anc.tag_id, p.parent_id FROM anc JOIN tag_parents p ON p.tag_id = anc.ancestor
)
SELECT tag_id, ancestor FROM anc
"""


def similar_tags(
    lib: "Library", limit: int = 20, min_score: float = 0.0, min_shared: int = 1
) -> List[SimilarTags]:
    """Rank pairs of tags by how alike their file sets are: candidates for
    ``tag merge``. A tag's files include its descendants'.

    Overlap that the hierarchy itself creates is not evidence of anything and
    is left out: ancestor/descendant pairs are skipped, and a file that two
    tags share only because it carries a common subtag of both ('shrek' under
    both 'ogre' and 'dreamworks') is ignored for that pair.

    The raw measure is the Jaccard index, shared / (files with either tag),
    which is symmetric: 5 'shrek' files that are all among 100 'cat' files
    score 0.05. It is then discounted for sample size with a Wilson lower
    bound, so 5-of-5 outranks 1-of-1."""
    ancestors: Dict[int, Set[int]] = {}
    for tag_id, ancestor in lib.db.execute(_ANCESTORS):
        ancestors.setdefault(tag_id, set()).add(ancestor)
    direct_tags: Dict[int, Set[int]] = {}
    for file_id, tag_id in lib.db.execute("SELECT file_id, tag_id FROM tag_files"):
        direct_tags.setdefault(file_id, set()).add(tag_id)

    counts: Counter = Counter()  # files per tag, subtags included
    both: Counter = Counter()  # files carrying both tags of a pair
    shared: Counter = Counter()  # ... not merely through a common subtag
    for direct in direct_tags.values():
        reach = sorted(set().union(*(ancestors[t] for t in direct)))
        counts.update(reach)
        for i, a in enumerate(reach):
            for b in reach[i + 1 :]:
                if a in ancestors[b] or b in ancestors[a]:
                    continue
                both[a, b] += 1
                if not any(a in ancestors[t] and b in ancestors[t] for t in direct):
                    shared[a, b] += 1

    scored: List[Tuple[float, int, int, int, int]] = []
    for (a, b), n in shared.items():
        if n < min_shared:
            continue
        # files with either tag, minus the ones ignored for this pair
        union = counts[a] + counts[b] - 2 * both[a, b] + n
        score = _wilson_lower(n, union)
        if score >= min_score:
            scored.append((score, n, a, b, union))
    scored.sort(key=lambda r: (-r[0], -r[1], r[2], r[3]))

    return [
        SimilarTags(
            a=lib.tag_show(a),
            b=lib.tag_show(b),
            count_a=counts[a],
            count_b=counts[b],
            shared=n,
            jaccard=n / union,
            score=score,
        )
        for score, n, a, b, union in scored[:limit]
    ]
