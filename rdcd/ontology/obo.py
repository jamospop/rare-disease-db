"""A small, fast OBO reader.

We parse OBO rather than the JSON releases because hp.obo is 11 MB against
23 MB for hp.json, and mondo.obo 51 MB - and the fields we need (id, name,
is_a, xref, synonym, obsolescence) are all present in the flat format. Parsed
ontologies are cached as pickles so repeat runs load in milliseconds.
"""
from __future__ import annotations

import pickle
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parents[2] / "data"
ONT = DATA / "ontologies"


@dataclass(slots=True)
class Term:
    id: str
    name: str | None = None
    parents: tuple[str, ...] = ()
    alt_ids: tuple[str, ...] = ()
    xrefs: tuple[str, ...] = ()
    xref_mods: tuple[str, ...] = ()  # parallel to xrefs; raw {...} modifier text
    synonyms: tuple[str, ...] = ()
    synonym_scopes: tuple[str, ...] = ()  # parallel to synonyms: EXACT/BROAD/NARROW/RELATED
    obsolete: bool = False
    replaced_by: str | None = None
    definition: str | None = None


_SYN = re.compile(r'^synonym:\s+"((?:[^"\\]|\\.)*)"\s*(EXACT|BROAD|NARROW|RELATED)?')
_TRAILING_MOD = re.compile(r"\s*\{[^{}]*\}\s*$")


def _split_value(raw: str) -> tuple[str, str]:
    """Return (clean value, modifier text) for an OBO tag value.

    OBO permits both a trailing "! human comment" and a trailing
    "{qualifier=...}" block. MONDO puts the mapping predicate in that block
    (e.g. MONDO:equivalentTo), so we keep it rather than throw it away.
    """
    v = raw.split(" ! ")[0].strip()
    mod = ""
    if m := _TRAILING_MOD.search(v):
        mod = m.group(0).strip()
        v = _TRAILING_MOD.sub("", v).strip()
    return v, mod
_DEF = re.compile(r'^def:\s+"((?:[^"\\]|\\.)*)"')


class Ontology:
    """Terms plus DAG queries. Read-only after construction."""

    def __init__(self, terms: dict[str, Term], name: str):
        self.terms = terms
        self.name = name
        self._alias: dict[str, str] = {}
        for t in terms.values():
            for a in t.alt_ids:
                self._alias[a] = t.id
            if t.obsolete and t.replaced_by:
                self._alias[t.id] = t.replaced_by
        self._children: dict[str, list[str]] = {}
        for t in terms.values():
            for p in t.parents:
                self._children.setdefault(p, []).append(t.id)

    # ---- identity ---------------------------------------------------------
    def normalize(self, term_id: str) -> str | None:
        """Resolve alt_ids and obsolete->replaced_by. None if unknown."""
        if not term_id:
            return None
        t = term_id.strip()
        seen = set()
        while t in self._alias and t not in seen:
            seen.add(t)
            t = self._alias[t]
        return t if t in self.terms else None

    def label(self, term_id: str) -> str | None:
        t = self.normalize(term_id)
        return self.terms[t].name if t else None

    def __contains__(self, term_id: str) -> bool:
        return self.normalize(term_id) is not None

    def __len__(self) -> int:
        return len(self.terms)

    # ---- DAG --------------------------------------------------------------
    @lru_cache(maxsize=200_000)
    def ancestors(self, term_id: str, include_self: bool = True) -> frozenset[str]:
        t = self.normalize(term_id)
        if not t:
            return frozenset()
        out: set[str] = {t} if include_self else set()
        stack = list(self.terms[t].parents)
        while stack:
            p = stack.pop()
            if p in out or p not in self.terms:
                continue
            out.add(p)
            stack.extend(self.terms[p].parents)
        return frozenset(out)

    def children(self, term_id: str) -> list[str]:
        t = self.normalize(term_id)
        return list(self._children.get(t, [])) if t else []

    @lru_cache(maxsize=100_000)
    def descendants(self, term_id: str, include_self: bool = True) -> frozenset[str]:
        t = self.normalize(term_id)
        if not t:
            return frozenset()
        out: set[str] = {t} if include_self else set()
        stack = list(self._children.get(t, []))
        while stack:
            c = stack.pop()
            if c in out:
                continue
            out.add(c)
            stack.extend(self._children.get(c, []))
        return frozenset(out)

    def is_ancestor_of(self, anc: str, desc: str) -> bool:
        a = self.normalize(anc)
        return bool(a and a in self.ancestors(desc))

    def path_distance(self, a: str, b: str, max_hops: int = 6) -> int | None:
        """Hops between two terms through their common ancestors, or None."""
        na, nb = self.normalize(a), self.normalize(b)
        if not na or not nb:
            return None
        if na == nb:
            return 0
        da = self._depths_from(na, max_hops)
        db = self._depths_from(nb, max_hops)
        common = set(da) & set(db)
        return min((da[c] + db[c] for c in common), default=None)

    def _depths_from(self, term_id: str, max_hops: int) -> dict[str, int]:
        depths = {term_id: 0}
        frontier = [term_id]
        for d in range(1, max_hops + 1):
            nxt = []
            for t in frontier:
                for p in self.terms[t].parents if t in self.terms else ():
                    if p not in depths and p in self.terms:
                        depths[p] = d
                        nxt.append(p)
            frontier = nxt
            if not frontier:
                break
        return depths

    def xref_index(
        self, prefixes: tuple[str, ...], *, predicate: str | None = None
    ) -> dict[str, list[str]]:
        """Map external CURIE -> our live term ids, e.g. OMIM:162200 -> [MONDO:...].

        Obsolete terms are followed to their replacement rather than skipped:
        OMIM ids frequently hang off a term that MONDO later merged, and
        dropping those silently loses real mappings.

        `predicate` filters on the OBO modifier block, e.g. "MONDO:equivalentTo"
        to keep only exact equivalences and exclude broader/narrower matches.
        """
        idx: dict[str, list[str]] = {}
        for t in self.terms.values():
            for x, mod in zip(t.xrefs, t.xref_mods or ("",) * len(t.xrefs)):
                if not x.startswith(prefixes):
                    continue
                if predicate and predicate not in mod:
                    continue
                live = self.normalize(t.id)
                if live and live not in idx.setdefault(x, []):
                    idx[x].append(live)
        return idx

    def label_index(
        self,
        include_synonyms: bool = True,
        scopes: tuple[str, ...] = ("EXACT", "NARROW"),
    ) -> dict[str, list[str]]:
        """Lowercased name/synonym -> term ids, for text grounding.

        BROAD and RELATED synonyms are excluded by default: they are the main
        source of dictionary false positives (a BROAD synonym of a specific term
        is, by definition, not that term).
        """
        idx: dict[str, list[str]] = {}
        for t in self.terms.values():
            if t.obsolete:
                continue
            names = [t.name] if t.name else []
            if include_synonyms:
                names += [
                    syn
                    for syn, sc in zip(t.synonyms, t.synonym_scopes or ("RELATED",) * len(t.synonyms))
                    if sc in scopes
                ]
            for n in names:
                k = n.strip().lower()
                if k and t.id not in idx.setdefault(k, []):
                    idx[k].append(t.id)
        return idx


def parse_obo(path: Path, *, id_prefix: str | None = None) -> Ontology:
    terms: dict[str, Term] = {}
    cur: dict | None = None

    def flush() -> None:
        if not cur or "id" not in cur:
            return
        tid = cur["id"]
        if id_prefix and not tid.startswith(id_prefix):
            return
        terms[tid] = Term(
            id=tid,
            name=cur.get("name"),
            parents=tuple(cur.get("is_a", ())),
            alt_ids=tuple(cur.get("alt_id", ())),
            xrefs=tuple(x for x, _ in cur.get("xref", ())),
            xref_mods=tuple(m for _, m in cur.get("xref", ())),
            synonyms=tuple(n for n, _ in cur.get("synonym", ())),
            synonym_scopes=tuple(sc for _, sc in cur.get("synonym", ())),
            obsolete=cur.get("is_obsolete", False),
            replaced_by=cur.get("replaced_by"),
            definition=cur.get("def"),
        )

    with path.open(encoding="utf-8", errors="replace") as fh:
        in_term = False
        for line in fh:
            line = line.rstrip("\n")
            if line.startswith("["):
                flush()
                in_term = line.strip() == "[Term]"
                cur = {} if in_term else None
                continue
            if not in_term or cur is None or not line or line.startswith("!"):
                continue
            if line.startswith("id: "):
                cur["id"] = line[4:].strip()
            elif line.startswith("name: "):
                cur["name"] = line[6:].strip()
            elif line.startswith("is_a: "):
                cur.setdefault("is_a", []).append(_split_value(line[6:])[0])
            elif line.startswith("alt_id: "):
                cur.setdefault("alt_id", []).append(_split_value(line[8:])[0])
            elif line.startswith("xref: "):
                cur.setdefault("xref", []).append(_split_value(line[6:]))
            elif line.startswith("synonym: "):
                if m := _SYN.match(line):
                    cur.setdefault("synonym", []).append((m.group(1), m.group(2) or "RELATED"))
            elif line.startswith("def: "):
                if m := _DEF.match(line):
                    cur["def"] = m.group(1)
            elif line.startswith("is_obsolete: "):
                cur["is_obsolete"] = line[13:].strip() == "true"
            elif line.startswith("replaced_by: "):
                cur["replaced_by"] = _split_value(line[13:])[0]
        flush()
    return Ontology(terms, name=path.stem)


def load_cached(path: Path, *, id_prefix: str | None = None) -> Ontology:
    """Parse once, then reuse a pickle keyed on the source file's mtime+size."""
    stat = path.stat()
    cache = ONT / f".{path.stem}.v3.{int(stat.st_mtime)}.{stat.st_size}.pkl"
    if cache.exists():
        try:
            with cache.open("rb") as fh:
                terms, name = pickle.load(fh)
            return Ontology(terms, name)
        except Exception:  # noqa: BLE001 - stale/corrupt cache, reparse
            pass
    ont = parse_obo(path, id_prefix=id_prefix)
    for old in ONT.glob(f".{path.stem}.*.pkl"):
        old.unlink(missing_ok=True)
    with cache.open("wb") as fh:
        pickle.dump((ont.terms, ont.name), fh, protocol=pickle.HIGHEST_PROTOCOL)
    return ont
