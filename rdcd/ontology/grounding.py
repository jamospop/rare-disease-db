"""Ground free text to HPO / HGNC / MONDO, with negation.

This is the deliberately dumb baseline: dictionary matching plus a NegEx-style
negation scope. It exists for three reasons.

1. It runs with no API key, so `make eval` produces a real number on a laptop
   with no account and no spend. A benchmark nobody else can run is not a
   benchmark.
2. It sets the floor an LLM extractor has to clear. "Our LLM gets F1 0.62" is
   meaningless until you know a dictionary gets 0.4x.
3. Its failure modes are legible. When it is wrong you can see exactly which
   phrase fired, which is how the error ledger gets populated honestly.

Negation is not an optional refinement here: 59% of gold phenotype features are
explicitly-absent findings, so a grounder without negation is wrong about the
majority of the target.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cached_property

from .store import OntologyStore

# Phrases that are ordinary English and would fire constantly. HPO contains a
# surprising number of these as labels or exact synonyms.
COMMON_WORD_BLOCKLIST = {
    "all", "and", "arm", "at", "back", "bad", "bar", "big", "birth", "body",
    "bridge", "cause", "cell", "chain", "change", "chest", "child", "class",
    "colour", "color", "complex", "cord", "count", "cup", "cycle", "death",
    "deep", "delay", "difference", "disease", "disorder", "down", "duct", "ear",
    "early", "end", "event", "face", "fall", "family", "fat", "female", "few",
    "field", "fine", "first", "flat", "fluid", "foot", "form", "front", "full",
    "gap", "general", "group", "growth", "hair", "hand", "head", "heart", "high",
    "hip", "history", "hole", "increase", "index", "infection", "inflammation",
    "injury", "joint", "kidney", "knee", "large", "late", "leg", "length",
    "level", "life", "light", "limb", "line", "liver", "long", "loss", "low",
    "lung", "male", "mass", "mean", "measure", "middle", "mild", "mode", "more",
    "mouth", "muscle", "nail", "neck", "nerve", "new", "normal", "nose",
    "number", "old", "one", "onset", "organ", "pain", "pattern", "phenotype",
    "plate", "point", "position", "pressure", "problem", "process", "rate",
    "ratio", "reduced", "response", "rest", "result", "ring", "rise", "root",
    "round", "sample", "scale", "sensation", "series", "set", "severe", "shape",
    "sharp", "short", "side", "sign", "size", "skin", "sleep", "small", "soft",
    "space", "speech", "spine", "state", "stone", "stress", "study", "surface",
    "syndrome", "system", "tall", "test", "thick", "thin", "time", "tissue",
    "tone", "total", "type", "unit", "up", "value", "vessel", "view", "voice",
    "volume", "wall", "water", "weight", "white", "wide", "width", "young",
}

MIN_PHRASE_LEN = 4

# HPO's root branches. `phenotypicFeatures` in a phenopacket holds only
# descendants of Phenotypic abnormality - verified: 100.00% of the 90,549 gold
# phenotype terms in the eval set are under HP:0000118, and 0 are outside it.
# The other branches are real HPO terms that are simply not phenotypes:
#   HP:0000005 Mode of inheritance    ("Autosomal dominant inheritance")
#   HP:0012823 Clinical modifier      ("Left", "Bilateral", "Recurrent")
#   HP:0032443 Past medical history   ("Family history")
#   HP:0040279 Frequency, HP:0032223 Blood group, HP:0045088 Clinical relevance
# Grounding into them produced 31.9% guaranteed-false-positive assertions.
HPO_PHENOTYPIC_ABNORMALITY = "HP:0000118"

# NegEx-style triggers.
PRE_NEG = [
    "no evidence of", "no signs of", "no sign of", "no history of", "not have",
    "without evidence of", "with no", "negative for", "ruled out", "rule out",
    "free of", "absence of", "lack of", "lacked", "lacks", "denies", "denied",
    "without", "absent", "no", "not", "never", "neither", "nor", "excluded",
    "unremarkable for", "resolution of", "failed to show", "did not show",
    "did not have", "does not have", "was not", "were not",
]
POST_NEG = [
    "was absent", "were absent", "is absent", "are absent", "was not present",
    "were not present", "not present", "not seen", "not observed", "not detected",
    "not identified", "not found", "was normal", "were normal", "ruled out",
    "was excluded", "were excluded", "were negative", "was negative",
]
# Scope terminators: negation does not cross these.
TERMINATORS = [
    "but", "however", "although", "though", "except", "aside from", "apart from",
    "other than", "whereas", "while", "nevertheless", ";", "despite",
    # Sentence boundaries terminate negation scope. The grounder is normally
    # called per sentence, but a caller passing a paragraph must not have a
    # cue leak across a full stop into the next sentence.
    ".", "?", "!",
]

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9'\-]*|\d+(?:\.\d+)?")


@dataclass(slots=True)
class Match:
    term_id: str
    phrase: str
    start: int          # offsets relative to the string passed in
    end: int
    negated: bool = False
    negation_cue: str | None = None


class Grounder:
    """Longest-match dictionary grounding over an ontology's labels."""

    def __init__(
        self,
        store: OntologyStore,
        *,
        which: str = "hpo",
        max_ngram: int = 8,
        blocklist: set[str] | None = None,
        scopes: tuple[str, ...] = ("EXACT", "NARROW"),
        multiword_related: bool = True,
        root: str | None = HPO_PHENOTYPIC_ABNORMALITY,
    ):
        """
        scopes: synonym scopes to trust outright.
        multiword_related: also accept BROAD/RELATED synonyms when they contain a
            space. HPO scopes "Hearing loss" as RELATED to Hearing impairment,
            and dropping it loses one of the commonest phrases in the corpus;
            single-word BROAD/RELATED synonyms stay excluded because those are
            where the false positives live. Settled empirically, not by taste:
            see scripts/calibrate_grounder.py and docs/BENCHMARKS.md.
        root: keep only terms under this ontology root. Defaults to
            HP:0000118 (Phenotypic abnormality) for HPO, because that is the only
            branch phenopackets use for phenotypic features. Pass None to ground
            against the whole ontology.
        """
        self.store = store
        self.which = which
        self.max_ngram = max_ngram
        self.blocklist = COMMON_WORD_BLOCKLIST if blocklist is None else blocklist
        self.scopes = scopes
        self.multiword_related = multiword_related
        self.root = root if which == "hpo" else None

    @cached_property
    def phrases(self) -> dict[str, list[str]]:
        ont = self.store.hpo if self.which == "hpo" else self.store.mondo
        raw = ont.label_index(scopes=self.scopes)
        if self.multiword_related:
            loose = ont.label_index(scopes=("BROAD", "RELATED"))
            for phrase, ids in loose.items():
                if " " in phrase:
                    raw.setdefault(phrase, ids)
        allowed: frozenset[str] | None = None
        if self.root:
            allowed = ont.descendants(self.root)
        out: dict[str, list[str]] = {}
        for phrase, ids in raw.items():
            if allowed is not None:
                ids = [i for i in ids if ont.normalize(i) in allowed]
                if not ids:
                    continue
            if len(phrase) < MIN_PHRASE_LEN:
                continue
            if phrase in self.blocklist:
                continue
            # A phrase that is a single common word is unusable regardless of length
            if " " not in phrase and phrase.rstrip("s") in self.blocklist:
                continue
            out[phrase] = ids
        return out

    # ---- matching ---------------------------------------------------------
    def find(self, text: str) -> list[Match]:
        """Longest non-overlapping dictionary matches, with negation applied."""
        toks = [(m.group(0), m.start(), m.end()) for m in _WORD.finditer(text)]
        lowered = [t[0].lower() for t in toks]
        matches: list[Match] = []
        i = 0
        while i < len(toks):
            hit = None
            for n in range(min(self.max_ngram, len(toks) - i), 0, -1):
                cand = " ".join(lowered[i : i + n])
                ids = self.phrases.get(cand)
                if not ids:
                    # tolerate simple plurals
                    ids = self.phrases.get(cand.rstrip("s")) if cand.endswith("s") else None
                if ids:
                    hit = (n, cand, ids)
                    break
            if hit:
                n, cand, ids = hit
                start, end = toks[i][1], toks[i + n - 1][2]
                matches.append(Match(term_id=ids[0], phrase=text[start:end], start=start, end=end))
                i += n
            else:
                i += 1
        low = text.lower()
        for m in matches:
            cue = self._negation_cue(low, m.start, m.end)
            if cue:
                m.negated, m.negation_cue = True, cue
        return matches

    def _negation_cue(self, low: str, start: int, end: int, window: int = 60) -> str | None:
        """Look left then right for a trigger, stopping at scope terminators."""
        left = low[max(0, start - window) : start]
        for t in TERMINATORS:
            if (idx := left.rfind(t)) != -1:
                left = left[idx + len(t) :]
        for trig in PRE_NEG:
            if re.search(rf"(?:^|[^a-z]){re.escape(trig)}[^a-z]*$", left) or re.search(
                rf"(?:^|[^a-z]){re.escape(trig)}(?:[^a-z]|$)", left
            ):
                return trig
        right = low[end : end + window]
        for t in TERMINATORS:
            if (idx := right.find(t)) != -1:
                right = right[:idx]
        for trig in POST_NEG:
            if re.search(rf"(?:^|[^a-z]){re.escape(trig)}(?:[^a-z]|$)", right):
                return trig
        return None


class GeneGrounder:
    """Gene symbols are matched case-sensitively: 'MET' is a gene, 'met' is a verb."""

    AMBIGUOUS = {
        "MET", "SET", "CAT", "CAN", "MAX", "REST", "AGO", "ACHE", "AIR", "ARM",
        "BAD", "CAMP", "CAR", "CARS", "COPE", "GAN", "IMPACT", "MICE", "MIR",
        "NHS", "PIGS", "RAN", "SDS", "SHE", "SON", "SPARC", "STAR", "TANK",
        "TRIP", "WARS", "YES", "MARS", "LARS", "PET", "AS", "IS", "IT", "OR",
        "AND", "NOT", "ALL", "ANY", "APP", "AGE", "END", "FAT", "GAS", "HR",
        "ICE", "LAP", "MB", "NET", "OS", "PC", "PIP", "POP", "REG", "SI",
        "SIX", "TEC", "TH", "TIA", "TOP", "TRA", "TYR",
        # Disease and syndrome abbreviations that collide with gene aliases.
        # Found by the provenance audit: "LCA" (Leber congenital amaurosis)
        # resolved to GUCY2D and "KS" (Kabuki/Kallmann syndrome) to OXSM.
        "LCA", "KS", "CDH", "DD", "ID", "ASD", "VSD", "CHD", "IUGR", "SGA",
        "ADHD", "ASD1", "MR", "EEG", "MRI", "CT", "PCR", "WES", "WGS", "SNP",
        "CNV", "NGS", "CNS", "GI", "IQ", "BMI", "OFC", "SD", "CI", "OR", "RR",
    }
    _SYM = re.compile(r"\b[A-Z][A-Z0-9]{1,9}(?:-[A-Z0-9]{1,4})?\b")

    def __init__(self, store: OntologyStore):
        self.store = store

    def find(self, text: str) -> list[Match]:
        out: list[Match] = []
        for m in self._SYM.finditer(text):
            sym = m.group(0)
            if sym in self.AMBIGUOUS or len(sym) < 2:
                continue
            hid = self.store.gene_id(sym)
            if hid:
                out.append(Match(term_id=hid, phrase=sym, start=m.start(), end=m.end()))
        return out


# HGVS expressions are highly regular and worth matching exactly: they are the
# strongest single signal that a sentence is describing a causal variant.
HGVS_C = re.compile(r"\b(?:[NX][MR]_\d+(?:\.\d+)?:)?c\.[\d_+\-*]+(?:[ACGT]+>[ACGT]+|del[ACGT]*|dup[ACGT]*|ins[ACGT]+|delins[ACGT]+)", re.I)
HGVS_P = re.compile(r"\b(?:[NX]P_\d+(?:\.\d+)?:)?p\.\(?[A-Z][a-z]{2}\d+(?:[A-Z][a-z]{2}|Ter|\*|fs|del|dup)\)?")


def find_hgvs(text: str) -> list[tuple[str, str, int, int]]:
    out = []
    for kind, rx in (("hgvs.c", HGVS_C), ("hgvs.p", HGVS_P)):
        for m in rx.finditer(text):
            out.append((kind, m.group(0), m.start(), m.end()))
    return out
