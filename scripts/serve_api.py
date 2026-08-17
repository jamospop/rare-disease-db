#!/usr/bin/env python3
"""Read-only API and reference UI over a dataset release.

Standard library only: no framework. This is deliberate: the API is a thin, stable
contract over the release files, and a downstream builder who *can* talk to users should
build a better front end on it. Making the data thick and the UI thin is the hedge for
never being able to user-test the UI.

Endpoints
  GET /api/health
  GET /api/stats
  GET /api/cases?hpo=HP:0001250&gene=STXBP1&disease=MONDO:0018975&pmid=...&tier=...&limit=50
  GET /api/cases/<id>
  GET /api/cases/<id>/phenopacket
  GET /api/search?q=seizure                      (HPO term lookup, then cases)
  GET /api/terms?q=seizure                       (ontology autocomplete)
  GET /                                          (reference search UI)

Usage: make api      then open http://localhost:8080
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rdcd.ontology.store import STORE

ROOT = Path(__file__).resolve().parents[1]
RELEASES = ROOT / "data" / "releases"


class Index:
    """In-memory index over one release. Small enough to hold; simple enough to trust."""

    def __init__(self, release: Path):
        self.release = release
        self.manifest = json.loads((release / "manifest.json").read_text())
        self.cases: dict[str, dict] = {}
        # Sets, not lists: a case asserting the same term both present and absent
        # would otherwise be counted twice, making the term list disagree with
        # the case query.
        self.by_hpo: dict[str, set[str]] = defaultdict(set)
        self.by_gene: dict[str, set[str]] = defaultdict(set)
        self.by_disease: dict[str, set[str]] = defaultdict(set)
        self.by_pmid: dict[str, set[str]] = defaultdict(set)
        for line in (release / "cases.jsonl").read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            cid = rec["id"]
            self.cases[cid] = rec
            for p in rec.get("phenotypes") or []:
                self.by_hpo[p["term"]["id"]].add(cid)
            for v in rec.get("variants") or []:
                if v.get("gene", {}).get("label"):
                    self.by_gene[v["gene"]["label"].upper()].add(cid)
            for d in rec.get("diagnoses") or []:
                self.by_disease[d["disease"]["id"]].add(cid)
            pmid = (rec.get("source") or {}).get("pmid")
            if pmid:
                self.by_pmid[str(pmid)].add(cid)

    def stats(self) -> dict:
        tiers = Counter((c.get("source") or {}).get("license") or "unstated"
                        for c in self.cases.values())
        flags = Counter(f for c in self.cases.values() for f in (c.get("qa_flags") or []))
        return {
            "release": self.manifest["version"],
            "schema_version": self.manifest["schema_version"],
            "extractor": self.manifest["extractor"],
            "records": len(self.cases),
            "distinct_hpo_terms": len(self.by_hpo),
            "distinct_genes": len(self.by_gene),
            "distinct_diseases": len(self.by_disease),
            "distinct_sources": len(self.by_pmid),
            "licences": dict(tiers.most_common()),
            "qa_flags": dict(flags.most_common(10)),
            "caveat": self.manifest["caveat"],
        }

    def query(self, *, hpo=None, gene=None, disease=None, pmid=None, tier=None,
              limit=50) -> list[dict]:
        sets = []
        if hpo:
            # Include descendants: a query for Seizure should match Focal seizure.
            wanted = set()
            for t in STORE.hpo.descendants(hpo):
                wanted.update(self.by_hpo.get(t, set()))
            sets.append(wanted)
        if gene:
            sets.append(set(self.by_gene.get(gene.upper(), set())))
        if disease:
            sets.append(set(self.by_disease.get(disease, set())))
        if pmid:
            sets.append(set(self.by_pmid.get(str(pmid).replace("PMID:", ""), set())))
        ids = set.intersection(*sets) if sets else set(self.cases)
        out = []
        for cid in sorted(ids):
            rec = self.cases[cid]
            if tier and ((rec.get("source") or {}).get("license") or "") != tier:
                continue
            out.append(self._summary(rec))
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _summary(rec: dict) -> dict:
        obs = [p for p in (rec.get("phenotypes") or []) if not p.get("excluded")]
        exc = [p for p in (rec.get("phenotypes") or []) if p.get("excluded")]
        src = rec.get("source") or {}
        return {
            "id": rec["id"],
            "source": {
                "pmid": src.get("pmid"), "pmcid": src.get("pmcid"),
                "license": src.get("license"), "retracted": src.get("retracted", False),
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{src.get('pmid')}/" if src.get("pmid") else None,
            },
            "n_observed": len(obs),
            "n_excluded": len(exc),
            "observed": [{"id": p["term"]["id"], "label": p["term"].get("label")} for p in obs[:8]],
            "genes": [v["gene"]["label"] for v in (rec.get("variants") or []) if v.get("gene")],
            "diagnoses": [
                {"id": d["disease"]["id"], "label": d["disease"].get("label")}
                for d in (rec.get("diagnoses") or [])
            ],
            "confidence": rec.get("confidence"),
            "qa_flags": rec.get("qa_flags") or [],
        }

    def terms(self, q: str, limit: int = 20) -> list[dict]:
        ql = q.strip().lower()
        if len(ql) < 2:
            return []
        out = []
        for term_id, cids in self.by_hpo.items():
            label = STORE.hpo.label(term_id) or ""
            if ql in label.lower():
                out.append({"id": term_id, "label": label, "n_cases": len(cids),
                            "information_content": round(STORE.information_content(term_id), 3)})
        out.sort(key=lambda r: (-r["n_cases"], r["label"]))
        return out[:limit]


INDEX: Index | None = None


def latest_release() -> Path:
    if not RELEASES.exists() or not any(RELEASES.iterdir()):
        raise SystemExit("No release found. Run: make release")
    return sorted(RELEASES.iterdir())[-1]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter default logging
        sys.stderr.write("  %s\n" % (fmt % args))

    def _send(self, obj, status=200, ctype="application/json"):
        body = (obj if isinstance(obj, bytes)
                else json.dumps(obj, indent=1).encode()) if ctype == "application/json" \
            else obj.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "html" in ctype else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")  # open read API
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = u.path.rstrip("/") or "/"
        assert INDEX is not None
        try:
            if path == "/":
                return self._send(UI, ctype="text/html")
            if path == "/api/health":
                return self._send({"status": "ok", "release": INDEX.manifest["version"]})
            if path == "/api/stats":
                return self._send(INDEX.stats())
            if path == "/api/terms":
                return self._send(INDEX.terms(q.get("q", "")))
            if path == "/api/search":
                terms = INDEX.terms(q.get("q", ""), limit=5)
                cases = INDEX.query(hpo=terms[0]["id"], limit=int(q.get("limit", 25))) if terms else []
                return self._send({"terms": terms, "cases": cases})
            if path == "/api/cases":
                return self._send(INDEX.query(
                    hpo=q.get("hpo"), gene=q.get("gene"), disease=q.get("disease"),
                    pmid=q.get("pmid"), tier=q.get("tier"),
                    limit=min(int(q.get("limit", 50)), 500)))
            if path.startswith("/api/cases/"):
                rest = path[len("/api/cases/"):]
                if rest.endswith("/phenopacket"):
                    cid = rest[: -len("/phenopacket")]
                    rec = INDEX.cases.get(cid)
                    if not rec:
                        return self._send({"error": "not found"}, 404)
                    from rdcd.schema import CaseRecord
                    return self._send(CaseRecord(**rec).to_phenopacket())
                rec = INDEX.cases.get(rest)
                return self._send(rec or {"error": "not found"}, 200 if rec else 404)
            return self._send({"error": "no such endpoint", "see": "/"}, 404)
        except Exception as e:  # noqa: BLE001
            return self._send({"error": type(e).__name__, "detail": str(e)}, 500)


UI = """<!doctype html><meta charset=utf-8><title>Rare-Disease Case Database</title>
<style>
:root{--bg:#fbfbfa;--fg:#1a1a18;--mut:#6b6b66;--line:#e3e3df;--acc:#8a4b2a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:15px/1.55 ui-sans-serif,-apple-system,Segoe UI,Roboto,sans-serif}
header{padding:22px 24px;border-bottom:1px solid var(--line)}
h1{margin:0;font-size:17px;font-weight:640;letter-spacing:-.01em}
.sub{color:var(--mut);font-size:13px;margin-top:4px}
main{max-width:960px;margin:0 auto;padding:24px}
input{width:100%;padding:11px 13px;font:inherit;border:1px solid var(--line);
  border-radius:7px;background:#fff}
input:focus{outline:2px solid var(--acc);outline-offset:-1px}
.hint{color:var(--mut);font-size:12.5px;margin:7px 0 20px}
.term{display:inline-block;padding:4px 9px;margin:0 6px 6px 0;border:1px solid var(--line);
  border-radius:14px;background:#fff;cursor:pointer;font-size:13px}
.term:hover{border-color:var(--acc);color:var(--acc)}
.card{background:#fff;border:1px solid var(--line);border-radius:9px;padding:14px 16px;margin-bottom:10px}
.card h3{margin:0 0 6px;font-size:14px;font-weight:600}
.card h3 a{color:var(--acc);text-decoration:none}
.meta{color:var(--mut);font-size:12.5px}
.chip{display:inline-block;padding:2px 7px;margin:3px 4px 0 0;background:#f2f2ef;
  border-radius:4px;font-size:12px}
.flag{background:#fdf0e8;color:var(--acc)}
.stats{display:flex;gap:22px;flex-wrap:wrap;color:var(--mut);font-size:12.5px;
  padding-bottom:16px;border-bottom:1px solid var(--line);margin-bottom:20px}
.stats b{color:var(--fg);font-variant-numeric:tabular-nums}
.warn{background:#fdf0e8;border:1px solid #f0d8c8;border-radius:7px;padding:11px 13px;
  font-size:13px;margin-bottom:20px}
</style>
<header><h1>Open Rare-Disease Case Database</h1>
<div class=sub>Reference UI. Everything here is also available on the JSON API - see <code>/api/stats</code>.</div></header>
<main>
<div class=warn><b>Research use only.</b> Records were produced by the dictionary baseline
(graded phenotype F1 0.56; absent-finding F1 0.11). Not for clinical decisions.</div>
<div class=stats id=stats></div>
<input id=q placeholder="Search a phenotype - try seizure, microcephaly, hearing loss" autofocus>
<div class=hint>Matching HPO terms appear first; picking one lists cases. Queries include
descendant terms, so "Seizure" also matches "Focal-onset seizure".</div>
<div id=terms></div><div id=out></div>
</main>
<script>
const $=s=>document.querySelector(s);
fetch('/api/stats').then(r=>r.json()).then(s=>{
  $('#stats').innerHTML=[['records',s.records],['HPO terms',s.distinct_hpo_terms],
    ['genes',s.distinct_genes],['diseases',s.distinct_diseases],['sources',s.distinct_sources]]
    .map(([k,v])=>`<span>${k} <b>${v.toLocaleString()}</b></span>`).join('')
    +`<span>release <b>${s.release}</b></span>`;});
let t;
$('#q').oninput=e=>{clearTimeout(t);const v=e.target.value;t=setTimeout(()=>terms(v),180)};
// ?q= prefill makes a search shareable as a link.
const pre=new URLSearchParams(location.search).get('q');
if(pre){$('#q').value=pre;terms(pre);}
function terms(v){ if(v.trim().length<2){$('#terms').innerHTML='';$('#out').innerHTML='';return}
  fetch('/api/terms?q='+encodeURIComponent(v)).then(r=>r.json()).then(ts=>{
    $('#terms').innerHTML=ts.length?ts.map(t=>
      `<span class=term onclick="cases('${t.id}')">${t.label} <span class=meta>${t.n_cases}</span></span>`).join('')
      :'<div class=meta>No indexed HPO term matches.</div>';
    if(ts.length)cases(ts[0].id);});}
function cases(id){ fetch('/api/cases?limit=25&hpo='+encodeURIComponent(id))
  .then(r=>r.json()).then(cs=>{
  $('#out').innerHTML=cs.length?cs.map(c=>`<div class=card>
    <h3>${c.source.pmid?`<a href="${c.source.url}" target=_blank rel=noopener>PMID ${c.source.pmid}</a>`:c.id}</h3>
    <div class=meta>${c.n_observed} present · ${c.n_excluded} explicitly absent
      ${c.source.license?'· '+c.source.license:''}${c.source.retracted?' · <b>RETRACTED</b>':''}
      ${c.confidence!=null?'· confidence '+c.confidence:''}</div>
    <div>${c.observed.map(p=>`<span class=chip>${p.label||p.id}</span>`).join('')}</div>
    <div>${c.genes.map(g=>`<span class=chip>${g}</span>`).join('')}
         ${c.diagnoses.map(d=>`<span class=chip>${d.label||d.id}</span>`).join('')}</div>
    <div>${c.qa_flags.slice(0,4).map(f=>`<span class="chip flag">${f}</span>`).join('')}</div>
  </div>`).join(''):'<div class=meta>No cases for that term.</div>';});}
</script>"""


def main() -> None:
    global INDEX
    rel = latest_release()
    print(f"loading release {rel.name} ...")
    INDEX = Index(rel)
    st = INDEX.stats()
    print(f"  {st['records']} records, {st['distinct_hpo_terms']} HPO terms, "
          f"{st['distinct_genes']} genes, {st['distinct_sources']} sources")
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    print(f"serving on http://localhost:{port}  (ctrl-c to stop)")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
