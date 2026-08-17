"""Rate-limited, disk-cached NCBI/PMC client.

Every network read in this project goes through here so that (a) NCBI's
polite-use rate limits are honoured in one place, (b) re-runs are reproducible
offline from the cache, and (c) the licence of every full text is recorded at
the moment of retrieval rather than inferred later.

NCBI policy: 3 requests/second without an API key, 10/second with one.
Set NCBI_API_KEY to go faster.

All HTTP goes through `requests` on purpose: requests ships certifi, whereas a
python.org macOS build whose Install Certificates.command was never run has no
CA store for urllib and fails every TLS handshake.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.parse
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DATA = Path(__file__).resolve().parents[2] / "data"
CACHE = DATA / "cache"
TOOL = "rdcd"
# NCBI's E-utilities terms require a contact email on every request so they can
# reach you before rate-limiting you. Set RDCD_EMAIL; the placeholder below is
# deliberately not a real address, because a personal email committed to a public
# repository gets harvested.
EMAIL = os.environ.get("RDCD_EMAIL", "rdcd-user@example.invalid")
if EMAIL.endswith(".invalid"):
    warnings.warn(
        "RDCD_EMAIL is not set. NCBI asks for a contact address on every request; "
        "set RDCD_EMAIL=you@example.org before any sustained fetching.",
        stacklevel=2,
    )
API_KEY = os.environ.get("NCBI_API_KEY")

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
IDCONV = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"
OA_FCGI = "https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi"

# Licences under which we may redistribute full-text-derived *expression*
# (quoted evidence sentences). Anything else: facts + metadata + outbound link
# only. See docs/LICENSING.md.
REDISTRIBUTABLE = {"CC0", "CC BY", "CC BY-SA"}


class RateLimiter:
    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second
        self._last = 0.0

    def wait(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self.interval:
            time.sleep(self.interval - gap)
        self._last = time.monotonic()


_limiter = RateLimiter(9.0 if API_KEY else 2.8)

_session = requests.Session()
_session.headers["User-Agent"] = f"{TOOL} (+{EMAIL})"
_session.mount(
    "https://",
    HTTPAdapter(
        pool_connections=8,
        pool_maxsize=16,
        max_retries=Retry(
            total=4,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
        ),
    ),
)


def _cache_path(kind: str, key: str, ext: str) -> Path:
    h = hashlib.sha1(key.encode()).hexdigest()
    p = CACHE / kind / h[:2]
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{h}.{ext}"


def fetch(url: str, *, kind: str, key: str, ext: str = "txt", force: bool = False) -> str:
    """GET with disk cache. The cache key is explicit so callers control identity.

    Caching is what makes the numbers in this repo reproducible: a re-run reads
    disk, not the network, so results cannot drift because NCBI changed under us.
    """
    cp = _cache_path(kind, key, ext)
    if cp.exists() and not force:
        return cp.read_text(encoding="utf-8", errors="replace")
    _limiter.wait()
    r = _session.get(url, timeout=90)
    r.raise_for_status()
    cp.write_text(r.text, encoding="utf-8")
    return r.text


def cached(kind: str, key: str, ext: str = "txt") -> bool:
    return _cache_path(kind, key, ext).exists()


def _eutils_params(extra: dict) -> str:
    p = {"tool": TOOL, "email": EMAIL, **extra}
    if API_KEY:
        p["api_key"] = API_KEY
    return urllib.parse.urlencode(p)


# --------------------------------------------------------------------------
# PMID -> PMCID
# --------------------------------------------------------------------------
def pmid_to_pmcid(pmids: Iterable[str], batch: int = 190) -> dict[str, str | None]:
    """Map PMIDs to PMCIDs. None means the article is not deposited in PMC."""
    ids = [str(p).replace("PMID:", "").strip() for p in pmids]
    ids = [i for i in ids if i]
    out: dict[str, str | None] = {}
    for i in range(0, len(ids), batch):
        chunk = ids[i : i + batch]
        q = urllib.parse.urlencode(
            {"ids": ",".join(chunk), "format": "json", "tool": TOOL, "email": EMAIL}
        )
        body = fetch(f"{IDCONV}?{q}", kind="idconv", key="idconv:" + ",".join(chunk), ext="json")
        try:
            recs = json.loads(body).get("records", [])
        except json.JSONDecodeError:
            recs = []
        for r in recs:
            out[str(r.get("requested-id"))] = r.get("pmcid")
        for c in chunk:
            out.setdefault(c, None)
    return out


# --------------------------------------------------------------------------
# Open-access status + licence + retraction, straight from the OA service
# --------------------------------------------------------------------------
@dataclass
class OAStatus:
    pmcid: str
    in_oa_subset: bool
    license: str | None
    retracted: bool
    tgz_href: str | None
    citation: str | None

    @property
    def redistributable(self) -> bool:
        return bool(self.license and self.license.strip() in REDISTRIBUTABLE)

    def to_dict(self) -> dict:
        return {
            "pmcid": self.pmcid,
            "in_oa_subset": self.in_oa_subset,
            "license": self.license,
            "retracted": self.retracted,
            "redistributable": self.redistributable,
            "citation": self.citation,
        }


_OA_REC = re.compile(r"<record\b([^>]*)>(.*?)</record>", re.S)
_ATTR = re.compile(r'([\w-]+)="([^"]*)"')
_LINK = re.compile(r"<link\b([^>]*?)/?>")


def oa_status(pmcid: str, *, force: bool = False) -> OAStatus:
    pmcid = pmcid if str(pmcid).startswith("PMC") else f"PMC{pmcid}"
    body = fetch(
        f"{OA_FCGI}?{urllib.parse.urlencode({'id': pmcid})}",
        kind="oa",
        key=f"oa:{pmcid}",
        ext="xml",
        force=force,
    )
    m = _OA_REC.search(body)
    if not m:
        return OAStatus(pmcid, False, None, False, None, None)
    attrs = dict(_ATTR.findall(m.group(1)))
    tgz = None
    for lm in _LINK.finditer(m.group(2)):
        la = dict(_ATTR.findall(lm.group(1)))
        if la.get("format") == "tgz":
            tgz = la.get("href")
    return OAStatus(
        pmcid=pmcid,
        in_oa_subset=True,
        license=attrs.get("license"),
        retracted=(attrs.get("retracted", "no").lower() == "yes"),
        tgz_href=tgz,
        citation=attrs.get("citation"),
    )


# --------------------------------------------------------------------------
# Full text (JATS XML) and PubMed records
# --------------------------------------------------------------------------
def pmc_fulltext_xml(pmcid: str, *, force: bool = False) -> str:
    """JATS XML for a PMC article. PMC withholds body text for non-OA records."""
    num = str(pmcid).replace("PMC", "")
    url = f"{EUTILS}/efetch.fcgi?" + _eutils_params(
        {"db": "pmc", "id": num, "rettype": "xml", "retmode": "xml"}
    )
    return fetch(url, kind="pmcxml", key=f"pmcxml:{pmcid}", ext="xml", force=force)


def pubmed_records_xml(pmids: Iterable[str], batch: int = 180) -> Iterator[str]:
    ids = [str(p).replace("PMID:", "").strip() for p in pmids]
    for i in range(0, len(ids), batch):
        chunk = ids[i : i + batch]
        url = f"{EUTILS}/efetch.fcgi?" + _eutils_params(
            {"db": "pubmed", "id": ",".join(chunk), "retmode": "xml"}
        )
        yield fetch(url, kind="pubmed", key="pubmed:" + ",".join(chunk), ext="xml")


def esearch(
    term: str,
    *,
    db: str = "pubmed",
    retmax: int = 100000,
    mindate: str | None = None,
    maxdate: str | None = None,
) -> list[str]:
    extra: dict = {"db": db, "term": term, "retmax": retmax, "retmode": "json"}
    if mindate:
        extra |= {"mindate": mindate, "maxdate": maxdate or "3000", "datetype": "pdat"}
    url = f"{EUTILS}/esearch.fcgi?" + _eutils_params(extra)
    body = fetch(
        url,
        kind="esearch",
        key=f"esearch:{db}:{term}:{retmax}:{mindate}:{maxdate}",
        ext="json",
    )
    try:
        return json.loads(body)["esearchresult"].get("idlist", [])
    except (json.JSONDecodeError, KeyError):
        return []


def esearch_count(term: str, *, db: str = "pubmed") -> int:
    url = f"{EUTILS}/esearch.fcgi?" + _eutils_params(
        {"db": db, "term": term, "retmax": 0, "retmode": "json"}
    )
    body = fetch(url, kind="esearch", key=f"esearchcount:{db}:{term}", ext="json")
    try:
        return int(json.loads(body)["esearchresult"]["count"])
    except (json.JSONDecodeError, KeyError, ValueError):
        return 0
