#!/usr/bin/env python3
"""Export the case corpus as ONE self-contained HTML file that searches offline.

The API and UI in `serve_api.py` require someone to clone a repository, download
300 MB of ontologies and run a server. That is a fine developer experience and a
useless one for the person this is supposed to help. This emits a single file with
the data and the scoring inline, so it works from a link, on a phone, with no
install and no network.

Everything is precomputed here so the browser only does arithmetic: term labels,
information content, and ancestor closures for exactly the terms that appear, plus
each case's phenotype ids as integer indices. Scoring in the page is the same
symmetric best-match information content the server uses, so results match.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rdcd.ontology.store import STORE

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "dist"


def load_cases() -> list[dict]:
    files = []
    rel = sorted((ROOT / "data" / "releases").iterdir()) if (ROOT / "data" / "releases").exists() else []
    if rel:
        files.append((rel[-1] / "cases.jsonl", "gold-derived"))
    corpus = ROOT / "data" / "corpus" / "cases.jsonl"
    if corpus.exists():
        files.append((corpus, "never-curated"))
    out = []
    for path, origin in files:
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                rec["_origin"] = origin
                out.append(rec)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-cases", type=int, default=6000)
    ap.add_argument("--min-phenotypes", type=int, default=3)
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)

    records = load_cases()
    print(f"loaded {len(records)} records")

    # Keep cases with enough signal to rank meaningfully.
    kept = []
    for r in records:
        obs = [p["term"]["id"] for p in (r.get("phenotypes") or []) if not p.get("excluded")]
        obs = [t for t in (STORE.hpo.normalize(x) for x in obs) if t]
        if len(obs) < args.min_phenotypes:
            continue
        kept.append((r, sorted(set(obs))))
        if len(kept) >= args.max_cases:
            break
    print(f"  {len(kept)} cases with >= {args.min_phenotypes} observed phenotypes")

    # Term vocabulary: only what is actually used, plus ancestors needed for scoring.
    used: set[str] = set()
    for _, obs in kept:
        used.update(obs)
    closure: set[str] = set()
    for t in used:
        closure |= STORE.hpo.ancestors(t)
    vocab = sorted(closure)
    idx = {t: i for i, t in enumerate(vocab)}
    print(f"  {len(used)} distinct phenotypes, {len(vocab)} terms incl. ancestors")

    labels = [STORE.hpo.label(t) or t for t in vocab]
    ic = [round(STORE.information_content(t), 3) for t in vocab]
    anc = [sorted(idx[a] for a in STORE.hpo.ancestors(t) if a in idx) for t in vocab]

    cases = []
    for r, obs in kept:
        src = r.get("source") or {}
        cases.append({
            "p": [idx[t] for t in obs if t in idx],
            "t": (src.get("title") or "")[:160],
            "m": src.get("pmid") or "",
            "y": src.get("year") or "",
            "l": src.get("license") or "",
            "d": [d["disease"].get("label") or d["disease"]["id"]
                  for d in (r.get("diagnoses") or [])][:3],
            "g": [v["gene"]["label"] for v in (r.get("variants") or []) if v.get("gene")][:3],
            "o": 1 if r.get("_origin") == "never-curated" else 0,
            "r": 1 if src.get("retracted") else 0,
        })

    # Searchable term list for the autocomplete: only terms cases actually have.
    searchable = sorted(
        ({"i": idx[t], "n": STORE.hpo.label(t) or t,
          "c": sum(1 for _, obs in kept if t in obs)} for t in used),
        key=lambda r: -r["c"],
    )

    genes: dict[str, list[int]] = {}
    for i, c in enumerate(cases):
        for g in c["g"]:
            genes.setdefault(g.upper(), []).append(i)
    print(f"  {len(genes)} distinct genes indexed")

    data = {"labels": labels, "ic": ic, "anc": anc, "cases": cases,
            "terms": searchable, "genes": genes}
    blob = json.dumps(data, separators=(",", ":"))
    print(f"  payload {len(blob)/1e6:.2f} MB raw, {len(gzip.compress(blob.encode()))/1e6:.2f} MB gzipped")

    html = TEMPLATE.replace("__DATA__", blob)
    out = OUT / "case-search.html"
    out.write_text(html)
    print(f"\nwrote {out}  ({out.stat().st_size/1e6:.2f} MB)")


TEMPLATE = r"""<title>Rare-Disease Case Search</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
/* Light palette is the complete set; dark redefines only tokens, in both the
   unstamped (system) and explicitly-stamped states. Nothing sets a colour outside
   these blocks, so the page never renders one theme's ink on the other's ground. */
:root{
  --ground:#f6f8f8; --panel:#ffffff; --sunk:#eef2f2;
  --ink:#111619; --ink-2:#3d4a4f; --muted:#68777c;
  --line:#dde5e5; --line-2:#c8d5d5;
  --accent:#14666a; --accent-soft:#e2eded; --accent-ink:#0d4a4d;
  --caution:#9a5b12; --caution-bg:#fdf4e8; --caution-line:#eddcc2;
  --shadow:0 1px 2px rgba(16,32,34,.06), 0 8px 24px rgba(16,32,34,.05);
}
@media (prefers-color-scheme:dark){ :root:not([data-theme=light]){
  --ground:#0f1416; --panel:#161d20; --sunk:#1b2427;
  --ink:#e6edee; --ink-2:#b3c2c5; --muted:#84979b; --line:#263134; --line-2:#35454a;
  --accent:#5fb5b8; --accent-soft:#16302f; --accent-ink:#8fd0d2;
  --caution:#d9a05a; --caution-bg:#241c10; --caution-line:#43331d;
  --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.28);
}}
:root[data-theme=dark]{
  --ground:#0f1416; --panel:#161d20; --sunk:#1b2427;
  --ink:#e6edee; --ink-2:#b3c2c5; --muted:#84979b;
  --line:#263134; --line-2:#35454a;
  --accent:#5fb5b8; --accent-soft:#16302f; --accent-ink:#8fd0d2;
  --caution:#d9a05a; --caution-bg:#241c10; --caution-line:#43331d;
  --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.28);
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--ground);color:var(--ink);
 font:15.5px/1.62 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",sans-serif}
.serif{font-family:ui-serif,Georgia,"Iowan Old Style","Times New Roman",serif}
.mono,.num{font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
 font-variant-numeric:tabular-nums}
.wrap{max-width:880px;margin:0 auto;padding:0 20px}

header{background:var(--panel);border-bottom:1px solid var(--line);padding:30px 0 0}
h1{margin:0;font-size:26px;font-weight:600;letter-spacing:-.02em;text-wrap:balance}
.lede{color:var(--ink-2);font-size:14.5px;margin:8px 0 0;max-width:60ch}
.bar{display:flex;gap:26px;flex-wrap:wrap;padding:16px 0 14px;margin-top:18px;
 border-top:1px solid var(--line);color:var(--muted);font-size:12px;
 text-transform:uppercase;letter-spacing:.06em}
.bar b{display:block;font-size:17px;letter-spacing:0;color:var(--ink);text-transform:none;
 font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums}

main{padding:24px 0 64px}
.caution{background:var(--caution-bg);border:1px solid var(--caution-line);
 border-radius:4px;padding:13px 15px;font-size:13.5px;color:var(--ink-2);margin-bottom:26px}
.caution b{color:var(--caution)}

.query{background:var(--panel);border:1px solid var(--line);border-radius:5px;
 padding:16px 16px 12px;box-shadow:var(--shadow)}
label.fld{display:block;font-size:11.5px;text-transform:uppercase;letter-spacing:.07em;
 color:var(--muted);margin-bottom:8px}
.field{position:relative}
input{width:100%;padding:11px 13px;font:inherit;color:var(--ink);
 border:1px solid var(--line-2);border-radius:4px;background:var(--ground)}
input:focus{outline:2px solid var(--accent);outline-offset:1px;background:var(--panel)}
.sugglist{position:absolute;z-index:20;left:0;right:0;top:100%;margin-top:5px;
 background:var(--panel);border:1px solid var(--line-2);border-radius:4px;
 box-shadow:var(--shadow);max-height:300px;overflow-y:auto}
.sugglist button{display:flex;justify-content:space-between;gap:12px;width:100%;
 text-align:left;padding:9px 13px;border:0;background:none;color:var(--ink);
 font:inherit;cursor:pointer}
.sugglist button:hover,.sugglist button:focus{background:var(--sunk);outline:none}
.sugglist .kind{display:inline-block;min-width:34px;color:var(--accent);font-size:10.5px;
 text-transform:uppercase;letter-spacing:.06em;margin-right:8px}
.chip.gene{font-family:ui-monospace,Menlo,monospace;font-size:12.5px}
.sugglist .n{color:var(--muted);font-size:12px;
 font-family:ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums}
#chips{display:flex;flex-wrap:wrap;gap:7px;margin-top:12px}
#chips:empty{display:none}
.chip{display:inline-flex;align-items:center;gap:8px;padding:5px 8px 5px 11px;
 background:var(--accent-soft);border:1px solid var(--line);border-radius:3px;
 font-size:13.5px;color:var(--accent-ink)}
.chip button{border:0;background:none;color:var(--muted);cursor:pointer;
 font-size:16px;line-height:1;padding:0 2px}
.chip button:hover,.chip button:focus{color:var(--caution);outline:none}

h2{font-family:ui-serif,Georgia,serif;font-size:17px;font-weight:600;
 margin:34px 0 3px;letter-spacing:-.01em}
.note{color:var(--muted);font-size:12.5px;margin:0 0 14px;max-width:66ch}

.tally{display:flex;flex-direction:column;gap:1px;background:var(--line);
 border:1px solid var(--line);border-radius:5px;overflow:hidden}
.tally div{display:flex;justify-content:space-between;align-items:baseline;gap:14px;
 background:var(--panel);padding:11px 14px}
.tally .nm{font-weight:550;flex:1;min-width:0}
.tally .wtwrap{width:58px;flex:none;display:flex;justify-content:flex-end}
.tally .ct{color:var(--muted);font-size:12px;white-space:nowrap;width:56px;
 text-align:right;flex:none;
 font-family:ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums}
.tally .wt{height:3px;background:var(--accent);border-radius:2px;align-self:center;
 min-width:3px;opacity:.75}

.cases{display:flex;flex-direction:column;gap:10px}
.case{background:var(--panel);border:1px solid var(--line);border-radius:5px;padding:14px 16px}
.case h3{margin:0;font-size:15px;font-weight:600;line-height:1.4;text-wrap:balance}
.case h3 a{color:var(--accent-ink);text-decoration:none}
.case h3 a:hover,.case h3 a:focus{text-decoration:underline}
.rec{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:7px 0 10px;
 color:var(--muted);font-size:11.5px;
 font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums}
.tag{padding:1px 6px;border:1px solid var(--line-2);border-radius:3px;letter-spacing:.02em}
.tag.new{color:var(--accent-ink);border-color:var(--accent);background:var(--accent-soft)}
.tag.ret{color:var(--caution);border-color:var(--caution-line);background:var(--caution-bg)}
.finds{display:flex;flex-wrap:wrap;gap:5px}
.finds span{padding:3px 8px;background:var(--sunk);border:1px solid transparent;
 border-radius:3px;font-size:13px;color:var(--ink-2)}
.finds span.key{background:var(--accent-soft);border-color:var(--accent);
 color:var(--accent-ink);font-weight:550}
.dxline{margin-top:10px;padding-top:9px;border-top:1px solid var(--line);
 font-size:13px;color:var(--ink-2)}
.dxline b{font-weight:600;color:var(--ink)}
.empty{color:var(--muted);font-size:14px;padding:14px;background:var(--panel);
 border:1px dashed var(--line-2);border-radius:5px}
footer{border-top:1px solid var(--line);margin-top:48px;padding:18px 0 0;
 color:var(--muted);font-size:12.5px}
footer a{color:var(--accent-ink)}
@media (prefers-reduced-motion:no-preference){
 .case,.tally div{transition:border-color .12s ease}}
@media (max-width:560px){h1{font-size:22px}.bar{gap:18px}}
</style>
<header><div class=wrap>
<h1 class=serif>Rare-Disease Case Search</h1>
<p class=lede>Describe a patient's findings. This searches published case reports for
patients whose reported features overlap, and shows what those cases turned out to be.
It runs entirely in your browser &mdash; nothing you type is sent anywhere.</p>
<div class=bar id=stats></div>
</div></header>
<main><div class=wrap>
<p class=caution><b>Read this first.</b> A research tool &mdash; <b>not a diagnostic device
and not medical advice</b>. It ranks published case reports by how much their recorded
findings overlap with yours. Records come from automated extraction with known error rates
(phenotype F1 0.56), and findings a paper explicitly ruled out are mostly missing, so a
finding not listed here never means it was excluded. Read the cited paper, and take
anything useful to a clinician.</p>

<div class=query>
 <label class=fld for=q>Findings or a gene &mdash; type a few letters, then choose</label>
 <div class=field>
  <input id=q placeholder="seizure, microcephaly, hearing loss, KMT2B&hellip;"
   autocomplete=off role=combobox aria-expanded=false aria-controls=sugg>
  <div id=sugg></div>
 </div>
 <div id=chips></div>
</div>
<div id=out></div>

<footer>Open data (CC BY 4.0) extracted from open-access PubMed Central case reports, each
assertion carrying a character offset into its source. Method, measured error rates and code:
<a href="https://github.com/jamospop/rare-disease-db">github.com/jamospop/rare-disease-db</a>.
Matching weights rare findings far above common ones, so a shared unusual feature counts for
much more than a shared common one.</footer>
</div></main>
<script>
const D=__DATA__;
const A=D.anc.map(a=>new Set(a));
const $=s=>document.querySelector(s), sel=new Map(), pairs=new Map();
let gene=null;
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
$('#stats').innerHTML=[
 ['Cases searchable',D.cases.length],
 ['Never previously structured',D.cases.filter(c=>c.o).length],
 ['Distinct findings',D.terms.length]
].map(([k,v])=>`<span>${k}<b>${v.toLocaleString()}</b></span>`).join('');

function pairIC(a,b){const k=a<b?a*100000+b:b*100000+a;let v=pairs.get(k);
 if(v!==undefined)return v;v=0;const sa=A[a];
 for(const x of A[b])if(sa.has(x)&&D.ic[x]>v)v=D.ic[x];
 pairs.set(k,v);return v;}

let t;
$('#q').oninput=e=>{clearTimeout(t);const v=e.target.value;t=setTimeout(()=>suggest(v),120)};
$('#q').onkeydown=e=>{if(e.key==='Escape'){$('#sugg').innerHTML='';
 $('#q').setAttribute('aria-expanded','false');}};
function suggest(v){const s=v.trim().toLowerCase();
 if(s.length<2){$('#sugg').innerHTML='';$('#q').setAttribute('aria-expanded','false');return}
 const hits=D.terms.filter(x=>x.n.toLowerCase().includes(s)).slice(0,8);
 const gs=Object.keys(D.genes).filter(g=>g.toLowerCase().startsWith(s))
   .sort((a,b)=>D.genes[b].length-D.genes[a].length).slice(0,4);
 const rows=gs.map(g=>`<button type=button role=option onclick="addGene('${g}')">`+
   `<span><span class=kind>gene</span>${esc(g)}</span>`+
   `<span class=n>${D.genes[g].length}</span></button>`).join('')
  +hits.map(x=>`<button type=button role=option onclick="add(${x.i})">${esc(x.n)}`+
   `<span class=n>${x.c}</span></button>`).join('');
 $('#q').setAttribute('aria-expanded',rows?'true':'false');
 $('#sugg').innerHTML=rows?'<div class=sugglist id=sugglist role=listbox>'+rows+'</div>':'';}
function addGene(g){gene=(gene===g?null:g);$('#q').value='';$('#sugg').innerHTML='';
 $('#q').setAttribute('aria-expanded','false');draw();run();$('#q').focus();}
function add(i){sel.set(i,D.labels[i]);$('#q').value='';$('#sugg').innerHTML='';
 $('#q').setAttribute('aria-expanded','false');draw();run();$('#q').focus();}
function del(i){sel.delete(i);draw();run();}
function draw(){$('#chips').innerHTML=
 (gene?`<span class="chip gene">gene ${esc(gene)}<button type=button onclick="addGene('${gene}')"`+
   ` aria-label="Remove gene filter">&times;</button></span>`:'')+
 [...sel].map(([i,l])=>
 `<span class=chip>${esc(l)}<button type=button onclick="del(${i})"`+
 ` aria-label="Remove ${esc(l)}">&times;</button></span>`).join('');}

function card(h){const c=h.c;
 const label=esc(c.t||('PMID '+c.m));
 const link=c.m?`<a href="https://pubmed.ncbi.nlm.nih.gov/${esc(c.m)}/" target=_blank`+
  ` rel=noopener>${label}</a>`:label;
 return `<article class=case><h3>${link}</h3>
  <div class=rec>${c.m?'<span>PMID '+esc(c.m)+'</span>':''}${c.y?'<span>'+esc(c.y)+'</span>':''}
   ${c.l?'<span>'+esc(c.l)+'</span>':''}
   ${h.shared.length?'<span>'+h.shared.length+'/'+c.p.length+' findings shared</span>'
     :'<span>'+c.p.length+' findings recorded</span>'}
   ${c.o?'<span class="tag new">not in any curated database</span>':''}
   ${c.r?'<span class="tag ret">retracted</span>':''}</div>
  ${h.shared.length?'<div class=finds>'+h.shared.map(([t,v])=>
    `<span class="${v>=3?'key':''}">${esc(D.labels[t])}</span>`).join('')+'</div>':''}
  ${c.d.length?`<p class=dxline>Reported diagnosis: <b>${esc(c.d.join(', '))}</b>`+
    (c.g.length?' &middot; gene '+esc(c.g.join(', ')):'')+'</p>':''}
  </article>`;}

function run(){
 if(!sel.size&&!gene){$('#out').innerHTML='';return}
 const allow=gene?new Set(D.genes[gene]||[]):null;
 if(!sel.size&&gene){  // gene-only lookup: list its cases, no ranking to do
  const rows=[...allow].map(i=>({c:D.cases[i],score:0,shared:[]}));
  $('#out').innerHTML='<h2>Published cases reporting '+esc(gene)+'</h2>'+
   '<p class=note>All cases in this corpus with a variant recorded in this gene. '+
   'Add findings above to rank them by how closely they match a patient.</p>'+
   (rows.length?'<div class=cases>'+rows.map(card).join('')+'</div>'
     :'<p class=empty>No case here reports this gene.</p>');
  return;}
 const q=[...sel.keys()],hits=[];
 for(const [ci,c] of D.cases.entries()){
  if(allow&&!allow.has(ci))continue;
  if(!c.p.length)continue;
  let fwd=0;for(const x of q){let b=0;for(const t of c.p){const v=pairIC(x,t);if(v>b)b=v;}fwd+=b;}
  let rev=0;for(const t of c.p){let b=0;for(const x of q){const v=pairIC(t,x);if(v>b)b=v;}rev+=b;}
  const score=(fwd/q.length+rev/c.p.length)/2;
  if(score<=0)continue;
  const shared=[];
  for(const t of c.p){let b=0;for(const x of q){const v=pairIC(t,x);if(v>b)b=v;}
   if(b>=1.5)shared.push([t,b]);}
  shared.sort((a,b)=>b[1]-a[1]);
  hits.push({c,score,shared:shared.slice(0,10)});}
 hits.sort((a,b)=>b.score-a.score);
 const top=hits.slice(0,15);
 const tally={};
 for(const h of top)for(const d of h.c.d){(tally[d]=tally[d]||{n:0,s:0});
  tally[d].n++;tally[d].s+=h.score;}
 const dx=Object.entries(tally).sort((a,b)=>b[1].s-a[1].s).slice(0,8);
 const maxS=dx.length?dx[0][1].s:1;
 let out='<h2>What similar cases turned out to be</h2>'+
  '<p class=note>Diagnoses of the matching cases, weighted by how closely each matched. '+
  'A short list means few published cases resemble this combination &mdash; that is '+
  'information, not an answer.</p>';
 out+=dx.length?'<div class=tally>'+dx.map(([k,v])=>
   `<div><span class=nm>${esc(k)}</span>`+
   `<span class=wtwrap><span class=wt style="width:${Math.max(4,Math.round(52*v.s/maxS))}px"></span></span>`+
   `<span class=ct>${v.n} case${v.n>1?'s':''}</span></div>`).join('')+'</div>'
  :'<p class=empty>No diagnosed case shares these findings.</p>';
 out+='<h2>Matching published cases</h2><p class=note>Highlighted findings are the rare, '+
  'informative ones &mdash; those are what carry the match. Open the paper before relying '+
  'on any of it.</p>';
 out+=top.length?'<div class=cases>'+top.map(card).join('')+'</div>'
  :'<p class=empty>No published case here shares these findings. That may mean the '+
   'combination is genuinely unreported, or simply that extraction missed it.</p>';
 $('#out').innerHTML=out;}
</script>"""


if __name__ == "__main__":
    main()
