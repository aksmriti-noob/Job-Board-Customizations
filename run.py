"""Daily job tracker. Fetches every configured company, classifies postings into lanes,
keeps history in jobs.sqlite, and writes a self-contained dashboard to docs/index.html.

  python run.py            # full run
  python run.py --sample   # offline demo with sample data (no network)
"""
import re, sys, json, hashlib, sqlite3, datetime as dt, concurrent.futures as cf
from pathlib import Path
import yaml, html, requests

# ---------------- sources ----------------
import re, html, datetime as dt, requests

UA = {"User-Agent": "Mozilla/5.0 (compatible; opsjobs-tracker/1.0)"}
T = 25

def _txt(h):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", h or ""))).strip()

def greenhouse(slug, terms):
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true", headers=UA, timeout=T)
    r.raise_for_status()
    return [{"title": j["title"], "location": j.get("location", {}).get("name", ""), "url": j["absolute_url"],
             "posted_at": (j.get("first_published") or j.get("updated_at") or "")[:10],
             "description": _txt(j.get("content", ""))} for j in r.json().get("jobs", [])]

def lever(slug, terms):
    r = requests.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", headers=UA, timeout=T)
    r.raise_for_status()
    return [{"title": j["text"], "location": j.get("categories", {}).get("location", ""), "url": j["hostedUrl"],
             "posted_at": dt.datetime.utcfromtimestamp(j["createdAt"] / 1000).date().isoformat() if j.get("createdAt") else "",
             "description": j.get("descriptionPlain", "")} for j in r.json()]

def ashby(slug, terms):
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", headers=UA, timeout=T)
    r.raise_for_status()
    return [{"title": j["title"], "location": (j.get("location") or "") + (" (Remote)" if j.get("isRemote") else ""),
             "url": j.get("jobUrl") or j.get("applyUrl", ""), "posted_at": (j.get("publishedAt") or "")[:10],
             "description": _txt(j.get("descriptionHtml", ""))} for j in r.json().get("jobs", [])]

def smartrecruiters(slug, terms):
    out, offset = [], 0
    while True:
        r = requests.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset={offset}", headers=UA, timeout=T)
        r.raise_for_status()
        d = r.json()
        for j in d.get("content", []):
            loc = j.get("location", {})
            out.append({"title": j["name"], "location": ", ".join(x for x in [loc.get("city"), loc.get("region"), loc.get("country")] if x)
                        + (" (Remote)" if loc.get("remote") else ""),
                        "url": f"https://jobs.smartrecruiters.com/{slug}/{j['id']}", "posted_at": (j.get("releasedDate") or "")[:10], "description": ""})
        offset += 100
        if offset >= d.get("totalFound", 0) or offset > 3000:
            break
    return out

def workday(slug, terms):
    host, site = slug.split("/", 1)
    site = site.split("/")[-1]                       # tolerate en-US/... prefixes
    tenant = host.split(".")[0]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    out, seen = [], set()
    for term in terms:                                # search per term instead of paging a 5,000-job board
        offset = 0
        while True:
            r = requests.post(api, json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": term},
                              headers={**UA, "Content-Type": "application/json", "Accept": "application/json"}, timeout=T)
            r.raise_for_status()
            d = r.json()
            for j in d.get("jobPostings", []):
                path = j.get("externalPath", "")
                if path in seen:
                    continue
                seen.add(path)
                out.append({"title": j["title"], "location": j.get("locationsText", ""),
                            "url": f"https://{host}/{site}{path}", "posted_at": "", "description": ""})
            offset += 20
            if offset >= d.get("total", 0) or offset >= 200:
                break
    return out

def amazon(slug, terms):
    out, seen = [], set()
    for term in terms:
        for offset in (0, 100):
            r = requests.get("https://www.amazon.jobs/en/search.json", headers=UA, timeout=T,
                             params={"base_query": term, "country": "USA", "result_limit": 100, "offset": offset, "sort": "recent"})
            r.raise_for_status()
            js = r.json().get("jobs", [])
            for j in js:
                if j["id_icims"] in seen:
                    continue
                seen.add(j["id_icims"])
                out.append({"title": j["title"], "location": j.get("normalized_location") or j.get("location", ""),
                            "url": "https://www.amazon.jobs" + j["job_path"], "posted_at": _amzdate(j.get("posted_date", "")),
                            "description": j.get("description", "") or j.get("basic_qualifications", "")})
            if len(js) < 100:
                break
    return out

def _amzdate(s):
    try:
        return dt.datetime.strptime(s, "%B %d, %Y").date().isoformat()
    except Exception:
        return ""

def microsoft(slug, terms):
    out, seen = [], set()
    for term in terms:
        for pg in (1, 2, 3):
            r = requests.get("https://gcsservices.careers.microsoft.com/search/api/v1/search", headers=UA, timeout=T,
                             params={"q": term, "lc": "United States", "l": "en_us", "pg": pg, "pgSz": 20, "o": "Recent"})
            r.raise_for_status()
            js = r.json().get("operationResult", {}).get("result", {}).get("jobs", [])
            for j in js:
                if j["jobId"] in seen:
                    continue
                seen.add(j["jobId"])
                p = j.get("properties", {})
                out.append({"title": j["title"], "location": "; ".join(p.get("locations", [])) or p.get("primaryLocation", ""),
                            "url": f"https://jobs.careers.microsoft.com/global/en/job/{j['jobId']}",
                            "posted_at": (j.get("postingDate") or "")[:10], "description": _txt(p.get("description", ""))})
            if len(js) < 20:
                break
    return out

def google(slug, terms):
    out, seen = [], set()
    for term in terms:
        r = requests.get("https://careers.google.com/api/v3/search/", headers=UA, timeout=T,
                         params={"q": term, "location": "United States", "page": 1, "sort_by": "date"})
        r.raise_for_status()
        for j in r.json().get("jobs", []):
            if j["id"] in seen:
                continue
            seen.add(j["id"])
            out.append({"title": j["title"], "location": "; ".join(l.get("display", "") for l in j.get("locations", [])),
                        "url": j.get("apply_url") or f"https://www.google.com/about/careers/applications/jobs/results/{j['id'].split('/')[-1]}",
                        "posted_at": (j.get("publish_date") or "")[:10], "description": _txt(j.get("description", ""))})
    return out

def tesla(slug, terms):
    r = requests.get("https://www.tesla.com/cua-api/apps/careers/state", headers=UA, timeout=T)
    r.raise_for_status()
    d = r.json()
    locs = d.get("lookup", {}).get("locations", {})
    out = []
    for j in d.get("listings", []):
        loc = locs.get(str(j.get("l")), {}) if isinstance(locs, dict) else {}
        locname = loc.get("name", "") if isinstance(loc, dict) else str(loc)
        out.append({"title": j.get("t", ""), "location": locname,
                    "url": f"https://www.tesla.com/careers/search/job/{j.get('id')}", "posted_at": "", "description": ""})
    return out

FETCHERS = {f.__name__: f for f in (greenhouse, lever, ashby, smartrecruiters, workday, amazon, microsoft, google, tesla)}

# ---------------- tracker ----------------
ROOT = Path(__file__).parent
CFG = yaml.safe_load((ROOT / "config.yaml").read_text())
DB = ROOT / "jobs.sqlite"
DOCS = ROOT / "docs"
TODAY = dt.date.today().isoformat()

STATES = {s: s for s in "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()}
STATE_NAMES = {"alabama":"AL","alaska":"AK","arizona":"AZ","arkansas":"AR","california":"CA","colorado":"CO","connecticut":"CT","delaware":"DE","florida":"FL","georgia":"GA","hawaii":"HI","idaho":"ID","illinois":"IL","indiana":"IN","iowa":"IA","kansas":"KS","kentucky":"KY","louisiana":"LA","maine":"ME","maryland":"MD","massachusetts":"MA","michigan":"MI","minnesota":"MN","mississippi":"MS","missouri":"MO","montana":"MT","nebraska":"NE","nevada":"NV","new hampshire":"NH","new jersey":"NJ","new mexico":"NM","new york":"NY","north carolina":"NC","north dakota":"ND","ohio":"OH","oklahoma":"OK","oregon":"OR","pennsylvania":"PA","rhode island":"RI","south carolina":"SC","south dakota":"SD","tennessee":"TN","texas":"TX","utah":"UT","vermont":"VT","virginia":"VA","washington":"WA","west virginia":"WV","wisconsin":"WI","wyoming":"WY"}
NON_US = ["india","canada","ireland","united kingdom","germany","singapore","australia","japan","china","brazil","mexico","france","netherlands","poland","israel","taiwan","korea","spain","italy","sweden","dublin","london","bangalore","bengaluru","hyderabad","toronto","vancouver","sydney","tokyo","berlin","amsterdam","paris","tel aviv","costa rica","philippines","malaysia","luxembourg","denmark","finland","norway","switzerland","belgium","austria","czech","romania","hungary","portugal"]

def lane_for(title, desc=""):
    t = title.lower()
    for lane, rules in CFG["lanes"].items():
        if any(x in t for x in rules.get("exclude", [])):
            continue
        if not any(x in t for x in rules["include"]):
            continue
        if rules.get("require_any") and not any(x in t for x in rules["require_any"]):
            continue
        return lane
    return None

def parse_location(loc):
    l = loc or ""
    ll = l.lower()
    remote = "remote" in ll
    state = ""
    for m in re.finditer(r"\b([A-Z]{2})\b", l):
        if m.group(1) in STATES:
            state = m.group(1); break
    if not state:
        for name, ab in STATE_NAMES.items():
            if name in ll:
                state = ab; break
    us = bool(state) or "united states" in ll or ll.strip() in ("us", "usa") or (remote and not any(x in ll for x in NON_US))
    if any(x in ll for x in NON_US) and not state:
        us = False
    return remote, state, us

def seniority(title):
    t = title.lower()
    if re.search(r"\b(director|head of|vp|vice president)\b", t): return "Director+"
    if re.search(r"\b(principal|staff|lead)\b", t): return "Principal"
    if re.search(r"\b(senior|sr\.?)\b", t) or re.search(r"\b(iii|iv|3|4)\b", t): return "Senior"
    if re.search(r"\b(ii|2)\b", t): return "Mid"
    return "Unspecified"

def salary(text):
    if not text: return None, None
    m = re.search(r"(?:\$|USD\s?)?\s?(\d{2,3}(?:,\d{3})?(?:\.\d+)?)\s?(k)?(?:\s?USD)?\s*(?:-|–|—|to)\s*(?:\$|USD\s?)?\s?(\d{2,3}(?:,\d{3})?(?:\.\d+)?)\s?(k)?(?:\s?USD)?", text, re.I)
    if m and not re.search(r"\$|usd|salary|pay|compensation", text[max(0,m.start()-80):m.end()+20], re.I): m = None
    if not m: return None, None
    def num(v, k):
        v = float(v.replace(",", ""))
        if k or v < 1000: v *= 1000
        return int(v)
    lo, hi = num(m.group(1), m.group(2)), num(m.group(3), m.group(4))
    if 30000 <= lo <= 900000 and lo <= hi <= 1500000:
        return lo, hi
    return None, None

def init(db):
    db.execute("""CREATE TABLE IF NOT EXISTS jobs(
      id TEXT PRIMARY KEY, company TEXT, title TEXT, lane TEXT, location TEXT, state TEXT, remote INTEGER,
      url TEXT, posted_at TEXT, salary_min INTEGER, salary_max INTEGER, seniority TEXT,
      first_seen TEXT, last_seen TEXT, active INTEGER)""")

def fetch_all():
    terms = CFG.get("search_terms", [])
    health, results = [], []
    def one(c):
        f = FETCHERS.get(c["ats"])
        t0 = dt.datetime.now()
        try:
            rows = f(c.get("slug"), terms)
            return c, rows, "ok", ""
        except Exception as e:
            return c, [], "error", f"{type(e).__name__}: {str(e)[:120]}"
    with cf.ThreadPoolExecutor(8) as ex:
        for c, rows, status, err in ex.map(one, CFG["companies"]):
            for r in rows: r["company"] = c["name"]
            results += rows
            health.append({"company": c["name"], "ats": c["ats"], "fetched": len(rows), "status": status, "note": err})
    return results, health

def sample():
    s = json.loads((ROOT / "sample_jobs.json").read_text())
    return s, [{"company": c, "ats": "sample", "fetched": sum(1 for j in s if j["company"] == c), "status": "ok", "note": ""} for c in sorted({j["company"] for j in s})]

DASHBOARD = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ops Jobs Tracker</title>
<style>
:root{--ink:#0B0F19;--muted:#5B6472;--line:#E4E7EC;--soft:#F5F6F8;--blue:#2563EB;--green:#16A34A;--amber:#D97706}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:Inter,system-ui,-apple-system,sans-serif;color:var(--ink);background:#fff;line-height:1.45;font-size:14px}
a{color:var(--ink)}
.top{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:14px 20px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.top h1{font-size:18px;letter-spacing:-.02em}
.top .sub{color:var(--muted);font-size:13px}
.layout{display:grid;grid-template-columns:260px 1fr;min-height:calc(100vh - 60px)}
aside{border-right:1px solid var(--line);padding:16px;background:var(--soft);position:sticky;top:0;height:100vh;overflow:auto}
aside h3{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:16px 0 8px}
aside h3:first-child{margin-top:0}
aside label{display:flex;align-items:center;gap:8px;padding:4px 0;cursor:pointer}
aside label span.n{margin-left:auto;color:var(--muted);font-size:12px}
input[type=text],input[type=number],select{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:8px;font:inherit;background:#fff}
main{padding:16px 20px}
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
.pill{border:1px solid var(--line);background:#fff;border-radius:999px;padding:6px 12px;font:500 13px Inter,system-ui,sans-serif;cursor:pointer;color:var(--muted)}
.pill[aria-pressed=true]{background:var(--ink);color:#fff;border-color:var(--ink)}
.btn{border:1px solid var(--line);background:#fff;border-radius:8px;padding:6px 10px;font:500 13px Inter,system-ui,sans-serif;cursor:pointer}
.btn:hover{border-color:var(--ink)}
.count{color:var(--muted);margin-left:auto;font-size:13px}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-size:12px;color:var(--muted);font-weight:600;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap;cursor:pointer;user-select:none}
td{padding:10px;border-bottom:1px solid var(--line);vertical-align:top}
tr.hidden{display:none}
tr.applied td{opacity:.55}
td .t{font-weight:600}
td .t a{text-decoration:none}
td .t a:hover{text-decoration:underline}
td .m{color:var(--muted);font-size:12px;margin-top:2px}
.tag{display:inline-block;font-size:11px;font-weight:600;padding:2px 7px;border-radius:6px;background:var(--soft);margin-right:4px}
.tag.pgm{background:#DBEAFE;color:#1E40AF}.tag.ie{background:#FEF3C7;color:#92400E}.tag.dc{background:#DCFCE7;color:#166534}
.new{color:var(--green);font-weight:600;font-size:12px}
.closed{color:var(--amber);font-size:12px;font-weight:600}
.act{display:flex;gap:4px;white-space:nowrap}
.act button{border:1px solid var(--line);background:#fff;border-radius:6px;padding:4px 7px;cursor:pointer;font-size:12px}
.act button.on{background:var(--ink);color:#fff;border-color:var(--ink)}
details{margin-top:20px;font-size:13px}
details summary{cursor:pointer;color:var(--muted)}
details table td,details table th{padding:5px 8px;font-size:12px}
.ok{color:var(--green)}.err{color:#DC2626}
.toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:var(--ink);color:#fff;padding:10px 16px;border-radius:10px;font-size:13px;opacity:0;transition:.3s;pointer-events:none}
.toast.show{opacity:1}
@media(max-width:860px){.layout{grid-template-columns:1fr}aside{position:static;height:auto;border-right:0;border-bottom:1px solid var(--line)}.hide-m{display:none}}
</style>
</head>
<body>
<div class="top">
  <div><h1>Ops Jobs Tracker</h1><div class="sub" id="sub"></div></div>
  <div style="display:flex;gap:8px"><button class="btn" onclick="exportCSV()">Export CSV</button><button class="btn" onclick="markSeen()">Mark all as seen</button></div>
</div>
<div class="layout">
<aside>
  <h3>Search</h3><input type="text" id="q" placeholder="title, company, city…" oninput="render()">
  <h3>Companies</h3>
  <div style="display:flex;gap:6px;margin-bottom:6px"><button class="btn" onclick="setAll('co',true)">All</button><button class="btn" onclick="setAll('co',false)">None</button></div>
  <div id="cos"></div>
  <h3>State</h3><select id="state" onchange="render()"><option value="">Any</option></select>
  <h3>Seniority</h3><div id="sen"></div>
  <h3>Minimum salary</h3><input type="number" id="minsal" placeholder="e.g. 150000" oninput="render()">
  <h3>Show</h3>
  <label><input type="checkbox" id="showClosed" onchange="render()"> Recently closed</label>
  <label><input type="checkbox" id="showHidden" onchange="render()"> Hidden roles</label>
  <label><input type="checkbox" id="onlyStar" onchange="render()"> Starred only</label>
</aside>
<main>
  <div class="bar" id="lanes"></div>
  <div class="bar">
    <button class="pill" data-days="1" aria-pressed="false" onclick="setDays(1,this)">Today</button>
    <button class="pill" data-days="7" aria-pressed="true" onclick="setDays(7,this)">7 days</button>
    <button class="pill" data-days="30" aria-pressed="false" onclick="setDays(30,this)">30 days</button>
    <button class="pill" data-days="9999" aria-pressed="false" onclick="setDays(9999,this)">All</button>
    <button class="pill" id="remotePill" aria-pressed="false" onclick="toggleRemote(this)">Remote only</button>
    <span class="count" id="count"></span>
  </div>
  <table><thead><tr><th onclick="sortBy('title')">Role</th><th onclick="sortBy('company')">Company</th><th class="hide-m" onclick="sortBy('location')">Location</th><th class="hide-m" onclick="sortBy('salary_max')">Comp</th><th onclick="sortBy('first_seen')">Posted</th><th></th></tr></thead>
  <tbody id="rows"></tbody></table>
  <details><summary>Source health · which career pages answered today</summary><table id="health"></table></details>
</main>
</div>
<div class="toast" id="toast"></div>
<script>
const DATA = __DATA__;
const J = DATA.jobs, LANES = DATA.lanes;
const store = { get:(k,d)=>{try{return JSON.parse(localStorage.getItem('oj:'+k))??d}catch(e){return d}}, set:(k,v)=>localStorage.setItem('oj:'+k,JSON.stringify(v)) };
let star = new Set(store.get('star',[])), applied = new Set(store.get('applied',[])), hidden = new Set(store.get('hidden',[]));
let lastSeen = store.get('lastSeen','1970-01-01'), days = 7, remote = false, lane = 'all', sortKey='first_seen', sortDir=-1;
document.getElementById('sub').textContent = `Updated ${DATA.generated.replace('T',' ').slice(0,16)} UTC · ${DATA.stats.active} open roles across ${new Set(J.map(j=>j.company)).size} companies · ${DATA.stats.new_today} new today`;

// filters
const cos = [...new Set(J.map(j=>j.company))].sort();
document.getElementById('cos').innerHTML = cos.map(c=>`<label><input type="checkbox" class="co" value="${c}" checked onchange="render()"> ${c}<span class="n">${J.filter(j=>j.company===c&&j.active).length}</span></label>`).join('');
const sens = ['Director+','Principal','Senior','Mid','Unspecified'];
document.getElementById('sen').innerHTML = sens.map(s=>`<label><input type="checkbox" class="sen" value="${s}" checked onchange="render()"> ${s}</label>`).join('');
const states=[...new Set(J.map(j=>j.state).filter(Boolean))].sort();
document.getElementById('state').innerHTML += states.map(s=>`<option>${s}</option>`).join('');
document.getElementById('lanes').innerHTML = `<button class="pill" aria-pressed="true" onclick="setLane('all',this)">All lanes</button>` + Object.entries(LANES).map(([k,v])=>`<button class="pill" aria-pressed="false" onclick="setLane('${k}',this)">${v}</button>`).join('');
function setAll(cls,v){document.querySelectorAll('.'+cls).forEach(x=>x.checked=v);render()}
function setLane(l,el){lane=l;[...el.parentNode.children].forEach(x=>x.setAttribute('aria-pressed','false'));el.setAttribute('aria-pressed','true');render()}
function setDays(d,el){days=d;[...el.parentNode.querySelectorAll('[data-days]')].forEach(x=>x.setAttribute('aria-pressed','false'));el.setAttribute('aria-pressed','true');render()}
function toggleRemote(el){remote=!remote;el.setAttribute('aria-pressed',remote);render()}
function sortBy(k){sortDir = sortKey===k ? -sortDir : (k==='first_seen'||k==='salary_max' ? -1 : 1); sortKey=k; render()}

function ago(d){const n=Math.round((Date.now()-Date.parse(d))/864e5);return n<=0?'Today':n===1?'1 day ago':n+' days ago'}
function comp(j){return j.salary_min?`$${Math.round(j.salary_min/1000)}k–$${Math.round(j.salary_max/1000)}k`:'<span style="color:#9CA3AF">—</span>'}

function render(){
  const q=document.getElementById('q').value.toLowerCase();
  const coSel=new Set([...document.querySelectorAll('.co:checked')].map(x=>x.value));
  const senSel=new Set([...document.querySelectorAll('.sen:checked')].map(x=>x.value));
  const st=document.getElementById('state').value, minsal=+document.getElementById('minsal').value||0;
  const showClosed=document.getElementById('showClosed').checked, showHidden=document.getElementById('showHidden').checked, onlyStar=document.getElementById('onlyStar').checked;
  const cutoff=Date.now()-days*864e5;
  let list=J.filter(j=>(showClosed||j.active)&&coSel.has(j.company)&&senSel.has(j.seniority)&&(lane==='all'||j.lane===lane)
    &&(!remote||j.remote)&&(!st||j.state===st)&&(!minsal||(j.salary_max||0)>=minsal)&&Date.parse(j.first_seen)>=cutoff
    &&(showHidden||!hidden.has(j.id))&&(!onlyStar||star.has(j.id))
    &&(!q||(j.title+' '+j.company+' '+j.location).toLowerCase().includes(q)));
  list.sort((a,b)=>{const x=a[sortKey]??'',y=b[sortKey]??'';return (x>y?1:x<y?-1:0)*sortDir});
  document.getElementById('count').textContent=`${list.length} roles`;
  document.getElementById('rows').innerHTML=list.map(j=>`<tr class="${applied.has(j.id)?'applied':''}">
    <td><div class="t"><a href="${j.url}" target="_blank" rel="noopener">${j.title}</a></div><div class="m"><span class="tag ${j.lane}">${LANES[j.lane]}</span>${j.seniority!=='Unspecified'?`<span class="tag">${j.seniority}</span>`:''}${j.remote?'<span class="tag">Remote</span>':''}</div></td>
    <td>${j.company}</td><td class="hide-m">${j.location||''}</td><td class="hide-m">${comp(j)}</td>
    <td>${j.active?(j.first_seen>lastSeen?`<span class="new">New</span> · `:'')+ago(j.first_seen):`<span class="closed">Closed</span> · ${ago(j.last_seen)}`}</td>
    <td class="act"><button class="${star.has(j.id)?'on':''}" title="Star" onclick="tog('star','${j.id}')">★</button><button class="${applied.has(j.id)?'on':''}" title="Applied" onclick="tog('applied','${j.id}')">✓</button><button title="Hide" onclick="tog('hidden','${j.id}')">✕</button><button title="Copy resume + cover letter prompt and open Gemini" onclick="prep('${j.id}')">Prep</button></td></tr>`).join('')
    || '<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:30px">Nothing matches. Widen the date range or company list.</td></tr>';
  document.getElementById('health').innerHTML='<tr><th>Company</th><th>Source</th><th>Status</th><th>Fetched</th><th>Matched</th><th>Note</th></tr>'+DATA.health.map(h=>`<tr><td>${h.company}</td><td>${h.ats}</td><td class="${h.status==='ok'?'ok':'err'}">${h.status}</td><td>${h.fetched}</td><td>${h.matched}</td><td>${h.note||''}</td></tr>`).join('');
}
function tog(k,id){const s={star,applied,hidden}[k];s.has(id)?s.delete(id):s.add(id);store.set(k,[...s]);render()}
function markSeen(){lastSeen=new Date().toISOString().slice(0,10);store.set('lastSeen',lastSeen);render();toast('Marked as seen. New roles from tomorrow will be flagged.')}
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2500)}
function exportCSV(){const rows=[...document.querySelectorAll('#rows tr')].length;const q=J.filter(j=>j.active);const csv=[['Title','Company','Lane','Location','State','Remote','Salary min','Salary max','Seniority','First seen','URL'].join(',')].concat(q.map(j=>[j.title,j.company,LANES[j.lane],j.location,j.state,j.remote?'yes':'',j.salary_min||'',j.salary_max||'',j.seniority,j.first_seen,j.url].map(v=>`"${String(v).replace(/"/g,'""')}"`).join(','))).join('\n');const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));a.download='ops-jobs.csv';a.click()}
async function prep(id){const j=J.find(x=>x.id===id);const p=`I am applying for the ${j.title} role at ${j.company} (${j.location}). Posting: ${j.url}

Read the job description at that link, or from the text I paste if the link does not open. I am attaching my current resume. Please:
1. Tailor my resume to this ${LANES[j.lane].toLowerCase()} role in one page. Keep every fact true; reframe, never invent. List the keywords from the posting that now appear and the ones I genuinely lack.
2. Write a one page cover letter in a natural human voice. No em-dashes, no lists introduced by colons, no very short sentences, no buzzwords.
3. Give an honest match score out of 10 with the two biggest gaps and how to address them in an interview.`;
  try{await navigator.clipboard.writeText(p);toast('Prompt copied. Paste into Gemini and attach your resume.')}catch(e){toast('Copy blocked by browser; select the text manually.')}
  window.open('https://gemini.google.com/app','_blank')}
render();
</script>
</body>
</html>
"""

def main():
    raw, health = fetch_all()
    db = sqlite3.connect(DB); init(db)
    kept, new = 0, 0
    for h in health: h["matched"] = 0
    hmap = {h["company"]: h for h in health}
    for r in raw:
        lane = lane_for(r["title"], r.get("description", ""))
        if not lane: continue
        remote, state, us = parse_location(r.get("location", ""))
        if CFG.get("us_only") and not us: continue
        lo, hi = salary(r.get("description", ""))
        jid = hashlib.sha1(r["url"].encode()).hexdigest()[:16]
        hmap[r["company"]]["matched"] += 1; kept += 1
        if db.execute("SELECT 1 FROM jobs WHERE id=?", (jid,)).fetchone():
            db.execute("UPDATE jobs SET last_seen=?, active=1, salary_min=COALESCE(?,salary_min), salary_max=COALESCE(?,salary_max), posted_at=CASE WHEN posted_at='' THEN ? ELSE posted_at END WHERE id=?",
                       (TODAY, lo, hi, r.get("posted_at", ""), jid))
        else:
            new += 1
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                       (jid, r["company"], r["title"].strip(), lane, r.get("location", ""), state, int(remote), r["url"],
                        r.get("posted_at") or TODAY, lo, hi, seniority(r["title"]), TODAY, TODAY))
    # anything not seen today by a source that succeeded is closed
    ok_cos = [h["company"] for h in health if h["status"] == "ok"]
    q = ",".join("?" * len(ok_cos))
    if ok_cos:
        db.execute(f"UPDATE jobs SET active=0 WHERE last_seen<? AND company IN ({q})", (TODAY, *ok_cos))
    db.commit()
    cutoff = (dt.date.today() - dt.timedelta(days=CFG.get("days_to_keep_closed", 14))).isoformat()
    cols = ["id","company","title","lane","location","state","remote","url","posted_at","salary_min","salary_max","seniority","first_seen","last_seen","active"]
    rows = [dict(zip(cols, r)) for r in db.execute(f"SELECT {','.join(cols)} FROM jobs WHERE active=1 OR last_seen>=? ORDER BY first_seen DESC, company", (cutoff,))]
    data = {"generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"), "lanes": {k: v["label"] for k, v in CFG["lanes"].items()},
            "jobs": rows, "health": health, "stats": {"matched": kept, "new_today": new, "active": sum(r["active"] for r in rows)}}
    DOCS.mkdir(exist_ok=True); (DOCS / "data").mkdir(exist_ok=True)
    (DOCS / "data" / "jobs.json").write_text(json.dumps(data, indent=1))
    tpl = DASHBOARD
    (DOCS / "index.html").write_text(tpl.replace("__DATA__", json.dumps(data).replace("</", "<\\/")))
    print(f"fetched {len(raw)} · matched {kept} · new today {new} · active {data['stats']['active']}")
    for h in health:
        print(f"  {h['company']:<16} {h['ats']:<15} {h['status']:<6} fetched {h['fetched']:<5} matched {h['matched']:<4} {h['note']}")

if __name__ == "__main__":
    main()
