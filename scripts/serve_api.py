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
from rdcd.search.similar import CaseSimilarity

ROOT = Path(__file__).resolve().parents[1]
RELEASES = ROOT / "data" / "releases"


class Index:
    """In-memory index over one release. Small enough to hold; simple enough to trust."""

    def __init__(self, release: Path, corpus: Path | None = None):
        self.release = release
        self.corpus = corpus
        self.manifest = json.loads((release / "manifest.json").read_text())
        self.cases: dict[str, dict] = {}
        # Sets, not lists: a case asserting the same term both present and absent
        # would otherwise be counted twice, making the term list disagree with
        # the case query.
        self.by_hpo: dict[str, set[str]] = defaultdict(set)
        self.by_gene: dict[str, set[str]] = defaultdict(set)
        self.by_disease: dict[str, set[str]] = defaultdict(set)
        self.by_pmid: dict[str, set[str]] = defaultdict(set)
        sources = [(release / "cases.jsonl", "gold-derived")]
        if corpus and (corpus / "cases.jsonl").exists():
            sources.append((corpus / "cases.jsonl", "never-curated"))
        self.provenance_counts: Counter = Counter()
        lines = []
        for path, origin in sources:
            for ln in path.read_text().splitlines():
                if ln.strip():
                    lines.append((ln, origin))
        for line, origin in lines:
            rec = json.loads(line)
            rec["_origin"] = origin
            self.provenance_counts[origin] += 1
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
        # Similarity index over every case that has observed phenotypes.
        self.sim = CaseSimilarity(STORE)
        for cid, rec in self.cases.items():
            obs = [p["term"]["id"] for p in (rec.get("phenotypes") or [])
                   if not p.get("excluded")]
            if not obs:
                continue
            self.sim.add(cid, obs, {
                "diagnoses": [{"id": d["disease"]["id"], "label": d["disease"].get("label")}
                              for d in (rec.get("diagnoses") or [])],
                "genes": [v["gene"]["label"] for v in (rec.get("variants") or [])
                          if v.get("gene")],
                "source": {k: (rec.get("source") or {}).get(k)
                           for k in ("pmid", "pmcid", "license", "retracted", "title", "year")},
                "qa_flags": rec.get("qa_flags") or [],
                "origin": rec.get("_origin"),
            })

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
            "by_origin": dict(self.provenance_counts),
            "searchable_cases": len(self.sim.cases),
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


def corpus_dir() -> Path | None:
    d = ROOT / "data" / "corpus"
    return d if (d / "cases.jsonl").exists() else None


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
            if path == "/api/similar":
                terms = [t.strip() for t in (q.get("hpo", "")).split(",") if t.strip()]
                hits = INDEX.sim.search(terms, top=min(int(q.get("limit", 15)), 50))
                return self._send({
                    "query": [{"id": t, "label": STORE.hpo.label(t)} for t in terms],
                    "n_searchable_cases": len(INDEX.sim.cases),
                    "candidate_diagnoses": INDEX.sim.disease_tally(hits),
                    "similar_cases": [h.to_dict() for h in hits],
                    "caveat": (
                        "Ranked published case reports by phenotype overlap. NOT a "
                        "diagnostic device and not medical advice. Records were produced "
                        "by automated extraction (phenotype F1 0.56); read the cited "
                        "paper before relying on anything here, and take findings to a "
                        "clinician."
                    ),
                })
            if path == "/api/labels":
                ids = [i.strip() for i in q.get("ids", "").split(",") if i.strip()]
                return self._send({i: STORE.hpo.label(i) for i in ids if STORE.hpo.label(i)})
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


UI = """<!doctype html><meta charset=utf-8><title>Rare-Disease Case Search</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{--bg:#fbfbf9;--panel:#fff;--fg:#1a1a18;--mut:#6f6f68;--line:#e4e3dd;
      --acc:#8a4b2a;--warn:#fdf1e7;--warnl:#eeceb4}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font:15.5px/1.6 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
header{padding:26px 24px 20px;border-bottom:1px solid var(--line);background:var(--panel)}
.wrap{max-width:940px;margin:0 auto}
h1{margin:0;font-size:19px;font-weight:650;letter-spacing:-.015em}
.sub{color:var(--mut);font-size:13.5px;margin-top:5px;max-width:62ch}
main{max-width:940px;margin:0 auto;padding:22px 24px 60px}
.notice{background:var(--warn);border:1px solid var(--warnl);border-radius:8px;
 padding:12px 14px;font-size:13.5px;margin-bottom:22px}
.notice b{color:var(--acc)}
label.fld{display:block;font-size:13px;color:var(--mut);margin-bottom:7px}
input{width:100%;padding:12px 14px;font:inherit;border:1px solid var(--line);
 border-radius:8px;background:var(--panel)}
input:focus{outline:2px solid var(--acc);outline-offset:-1px}
#chips{margin:12px 0 4px;min-height:30px}
.chip{display:inline-flex;align-items:center;gap:7px;padding:5px 10px;margin:0 7px 7px 0;
 background:var(--panel);border:1px solid var(--line);border-radius:16px;font-size:13.5px}
.chip button{border:0;background:none;color:var(--mut);cursor:pointer;font-size:15px;
 line-height:1;padding:0}
.chip button:hover{color:var(--acc)}
.sugg{position:relative}
.sugglist{position:absolute;z-index:9;left:0;right:0;background:var(--panel);
 border:1px solid var(--line);border-radius:8px;margin-top:4px;overflow:hidden;
 box-shadow:0 6px 20px rgba(0,0,0,.07)}
.sugglist div{padding:9px 13px;cursor:pointer;font-size:14px}
.sugglist div:hover{background:#f5f4f0}
.sugglist .ic{color:var(--mut);font-size:12px;float:right}
h2{font-size:14px;font-weight:640;margin:30px 0 4px;letter-spacing:.01em}
.h2note{color:var(--mut);font-size:12.5px;margin:0 0 12px}
.dx{background:var(--panel);border:1px solid var(--line);border-radius:9px;
 padding:11px 14px;margin-bottom:8px;display:flex;justify-content:space-between;gap:14px}
.dx b{font-weight:600}
.dx .n{color:var(--mut);font-size:12.5px;white-space:nowrap}
.case{background:var(--panel);border:1px solid var(--line);border-radius:9px;
 padding:14px 16px;margin-bottom:10px}
.case h3{margin:0 0 3px;font-size:14.5px;font-weight:600;line-height:1.35}
.case h3 a{color:var(--acc);text-decoration:none}
.case h3 a:hover{text-decoration:underline}
.meta{color:var(--mut);font-size:12.5px;margin-bottom:8px}
.shared{font-size:13px}
.shared span{display:inline-block;padding:2px 8px;margin:3px 5px 0 0;background:#f2f1ec;
 border-radius:4px}
.shared span.hi{background:#f6e7dc;color:var(--acc);font-weight:500}
.tag{display:inline-block;padding:1px 7px;border-radius:4px;font-size:11.5px;
 background:#eef2ee;color:#4a6b52;margin-left:6px}
.tag.new{background:#eaf0f6;color:#3c5a78}
.flag{background:var(--warn);color:var(--acc)}
.stats{display:flex;gap:20px;flex-wrap:wrap;color:var(--mut);font-size:12.5px;margin-top:14px}
.stats b{color:var(--fg);font-variant-numeric:tabular-nums}
.empty{color:var(--mut);font-size:14px;padding:10px 0}
footer{border-top:1px solid var(--line);margin-top:44px;padding-top:16px;
 color:var(--mut);font-size:12.5px}
footer a{color:var(--acc)}
</style>
<header><div class=wrap>
<h1>Rare-Disease Case Search</h1>
<div class=sub>Describe a patient's findings. This searches published case reports for
patients whose reported features overlap, and shows what those cases turned out to be.</div>
<div class=stats id=stats></div>
</div></header>
<main>
<div class=notice><b>Read this first.</b> This is a research tool, <b>not a diagnostic
device and not medical advice</b>. It ranks published case reports by phenotype overlap.
Records come from automated extraction with known error rates (phenotype F1 0.56;
explicitly-absent findings are largely missing). Always read the cited paper, and take
anything useful to a clinician.</div>

<label class=fld for=q>Add a clinical finding &mdash; type a few letters, then pick a term</label>
<div class=sugg><input id=q placeholder="e.g. seizure, microcephaly, hearing loss, short stature" autocomplete=off>
<div id=sugg></div></div>
<div id=chips></div>
<div id=out></div>
<footer>Data: extracted from open-access PubMed Central case reports, CC BY 4.0, with a
character offset into the source for every assertion. Code and method:
<a href="https://github.com/jamospop/rare-disease-db">github.com/jamospop/rare-disease-db</a>.
Scoring weights rare findings far above common ones (information content over the HPO DAG).</footer>
</main>
<script>
const $=s=>document.querySelector(s); const sel=new Map();
fetch('/api/stats').then(r=>r.json()).then(s=>{
  const o=s.by_origin||{};
  $('#stats').innerHTML=
    `<span>searchable cases <b>${(s.searchable_cases||0).toLocaleString()}</b></span>`+
    (o['never-curated']?`<span>never previously structured <b>${o['never-curated'].toLocaleString()}</b></span>`:'')+
    `<span>distinct findings <b>${s.distinct_hpo_terms.toLocaleString()}</b></span>`+
    `<span>genes <b>${s.distinct_genes.toLocaleString()}</b></span>`;});
let t;
$('#q').oninput=e=>{clearTimeout(t);const v=e.target.value;
  t=setTimeout(()=>suggest(v),160)};
function suggest(v){
  if(v.trim().length<2){$('#sugg').innerHTML='';return}
  fetch('/api/terms?q='+encodeURIComponent(v)).then(r=>r.json()).then(ts=>{
    $('#sugg').innerHTML=ts.length?'<div class=sugglist>'+ts.slice(0,8).map(t=>
      `<div onclick="add('${t.id}','${(t.label||'').replace(/'/g,"&#39;")}')">${t.label}`+
      `<span class=ic>${t.n_cases} cases</span></div>`).join('')+'</div>':'';});}
function add(id,label){ sel.set(id,label); $('#q').value=''; $('#sugg').innerHTML='';
  draw(); run(); }
function del(id){ sel.delete(id); draw(); run(); }
function draw(){ $('#chips').innerHTML=[...sel].map(([id,l])=>
  `<span class=chip>${l}<button onclick="del('${id}')" title="remove">&times;</button></span>`).join(''); }
function run(){
  if(!sel.size){$('#out').innerHTML='';return}
  fetch('/api/similar?limit=15&hpo='+[...sel.keys()].join(',')).then(r=>r.json()).then(d=>{
    const dx=d.candidate_diagnoses||[], cs=d.similar_cases||[];
    let h='';
    h+='<h2>What similar published cases turned out to be</h2>';
    h+='<p class=h2note>Diagnoses of the matching cases, weighted by how closely each matched. '
      +'A short list here means few published cases resemble this combination &mdash; which is '
      +'information, not an answer.</p>';
    h+= dx.length? dx.slice(0,8).map(e=>
      `<div class=dx><span><b>${e.label||e.id}</b></span>`+
      `<span class=n>${e.n_cases} case${e.n_cases>1?'s':''}</span></div>`).join('')
      : '<div class=empty>No diagnosed case in the corpus shares these findings.</div>';
    h+=`<h2>Matching published cases</h2>`;
    h+=`<p class=h2note>Highlighted findings are the rare, informative ones &mdash; those carry `
      +`the match. Open the paper before relying on any of it.</p>`;
    h+= cs.length? cs.map(c=>{
      const src=c.source||{}, pm=src.pmid;
      const title=src.title||('Case '+c.case_id);
      const link=pm?`<a href="https://pubmed.ncbi.nlm.nih.gov/${pm}/" target=_blank rel=noopener>${title}</a>`:title;
      const newTag=c.origin==='never-curated'?'<span class="tag new">not in any curated database</span>':'';
      const ret=src.retracted?'<span class="tag flag">RETRACTED</span>':'';
      return `<div class=case><h3>${link}${ret}</h3>
      <div class=meta>${pm?'PMID '+pm+' &middot; ':''}${src.year||''}${src.license?' &middot; '+src.license:''}
        &middot; ${c.n_shared} of ${c.n_case_phenotypes} recorded findings shared${newTag}</div>
      <div class=shared>${(c.shared_phenotypes||[]).map(p=>
        `<span class="${p.information_content>=3?'hi':''}">${p.label}</span>`).join('')}</div>
      ${(c.diagnoses||[]).length?`<div class=meta style="margin-top:8px">Reported diagnosis: `+
        c.diagnoses.map(x=>x.label||x.id).join(', ')+
        ((c.genes||[]).length?' &middot; gene '+c.genes.join(', '):'')+'</div>':''}
      </div>`}).join('')
      : '<div class=empty>No published case in the corpus shares these findings. That can mean '
        +'the combination is genuinely unreported, or simply that extraction missed it.</div>';
    $('#out').innerHTML=h;});}
// Shared links carry ids; resolve them to labels so a shared search is readable.
const pre=new URLSearchParams(location.search).get('hpo');
if(pre){const ids=pre.split(',').filter(Boolean);
  ids.forEach(id=>sel.set(id,id)); draw(); run();
  fetch('/api/labels?ids='+encodeURIComponent(ids.join(','))).then(r=>r.json())
    .then(m=>{ids.forEach(id=>{if(m[id])sel.set(id,m[id])});draw();});}
</script>"""


def main() -> None:
    global INDEX
    rel = latest_release()
    print(f"loading release {rel.name} ...")
    INDEX = Index(rel, corpus_dir())
    st = INDEX.stats()
    print(f"  {st['records']} records ({st.get('by_origin')}), "
          f"{st['distinct_hpo_terms']} HPO terms, {st['distinct_genes']} genes")
    print(f"  {st['searchable_cases']} cases searchable by phenotype similarity")
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    print(f"serving on http://localhost:{port}  (ctrl-c to stop)")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
