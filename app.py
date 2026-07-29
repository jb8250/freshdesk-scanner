"""Flask app: photo -> target color -> Mohawk Blendal/Bronzing blend recipe."""
import os
from flask import Flask, request, render_template_string, jsonify, send_from_directory
from PIL import Image
import io, json, re, requests
from datetime import datetime, timezone, timedelta
from blend import nearest, solve_blend

app = Flask(__name__)

# Scanner keyword set — matches Chrome extension logic (word-boundary regex).
KEYWORDS = [
    "photo", "photos", "picture", "pictures",
    "pic", "pics", "video", "videos", "vid"
]
KEYWORD_RE = re.compile(r"\b(" + "|".join(KEYWORDS) + r")\b", re.IGNORECASE)

# Freshdesk queue scanner config
FRESHDESK_DOMAIN = "broadriverretail-help.freshdesk.com"
FRESHDESK_API_KEY = os.environ.get("FRESHDESK_API_KEY", "")

# If no env var, try the same config file pattern as the bot.
if not FRESHDESK_API_KEY:
    _cfg = os.path.expanduser("~/.config/furtouch/freshdesk_api_key")
    if os.path.exists(_cfg):
        with open(_cfg, "r") as fh:
            FRESHDESK_API_KEY = fh.read().strip()

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
os.makedirs(CACHE_DIR, exist_ok=True)
CACHE_FILE = os.path.join(CACHE_DIR, "queue_cache.json")
CACHE_TTL_SECONDS = 1800  # 30 minutes

# Only scan tickets updated in the last N days to avoid pulling all 8488.
UPDATED_SINCE_DAYS = 60  # ~2 months

def fd_auth():
    return (FRESHDESK_API_KEY, "X")

def keyword_filter_hits(text):
    return bool(KEYWORD_RE.search(text or ""))

# Known status values on this account (from live ticket data):
#   2 = Customer responded  (needs review)
#   5 = Closed               (exclude)
#   6 = Waiting on customer  (optional include)
SCAN_STATUSES = [2, 6]  # Customer responded + Waiting on customer


def paginate_tickets():
    """Fetch all tickets across pages from the list endpoint."""
    page = 1
    per_page = 100
    since = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    while True:
        url = f"https://{FRESHDESK_DOMAIN}/api/v2/tickets"
        params = {"page": page, "per_page": per_page, "updated_since": since}
        r = requests.get(url, auth=fd_auth(), params=params, timeout=30)
        if r.status_code == 429:
            retry = r.headers.get("Retry-After")
            wait = int(retry) if retry and retry.isdigit() else 5
            raise requests.exceptions.HTTPError(
                f"429 rate-limited by Freshdesk. Retry after {wait}s."
            )
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        yield from data
        if len(data) < per_page:
            break
        page += 1


def passes_filters(t):
    status = t.get("status")
    if status not in SCAN_STATUSES:
        return False
    subject = t.get("subject") or ""
    if not KEYWORD_RE.search(subject):
        return False
    # Untagged check: match extension behavior — only flag tickets with missing/empty tags.
    tags = t.get("tags") or []
    if tags:
        return False
    # Overdue check for customer-responded tickets (status 2) using resolution deadline only.
    if status == 2:
        due = t.get("due_by")
        if due:
            try:
                dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
                if dt >= datetime.now(timezone.utc):
                    return False  # not yet overdue
            except Exception:
                pass
    return True

QUEUE_HTML = """\
<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Freshdesk Review Queue</title>
<style>
 body{font-family:system-ui,Arial,sans-serif;max-width:960px;margin:auto;padding:16px;background:#f5f5f5;color:#222}
 h1{font-size:22px;margin:0 0 4px}
 .sub{color:#666;font-size:13px;margin-bottom:16px}
 .controls{margin-bottom:12px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
 .controls label{font-size:13px}
 .controls select,.controls button{font-size:13px;padding:5px 10px}
 table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08)}
 th,td{padding:9px 12px;border-bottom:1px solid #eee;text-align:left;font-size:14px}
 th{background:#fafafa;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:0.03em;color:#555}
 tr:hover td{background:#f9f6f0}
 a.tid{color:#1a73e8;text-decoration:none;font-weight:600} a.tid:hover{text-decoration:underline}
 .badge{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:700;text-transform:uppercase}
 .badge-overdue{background:#fde8e8;color:#c00}
 .badge-ok{background:#e8f5e9;color:#2e7d32}
 .badge-waiting{background:#fff3e0;color:#e65100}
 .empty{background:#fff;border-radius:8px;padding:24px;text-align:center;color:#888;box-shadow:0 1px 3px rgba(0,0,0,0.06)}
 .error{background:#fde8e8;color:#c00;border-radius:8px;padding:16px;margin-bottom:12px}
 .refresh{float:right;font-size:12px;color:#666}
 .meta{font-size:12px;color:#888}
</style></head><body>
<h1>Freshdesk Review Queue</h1>
<div class=sub>{{ total }} ticket{{ '' if total==1 else 's' }} matching your filters · <span class=refresh><a href=/queue style=color:#666>Refresh</a></span></div>

{% if error %}
<div class=error>{{ error }}</div>
{% endif %}

{% if tickets %}
<table>
<tr>
  <th>Ticket</th>
  <th>Subject</th>
  <th>Status</th>
  <th>Priority</th>
  <th>Due</th>
  <th>Created</th>
  <th>Tags</th>
  <th>Type</th>
</tr>
{% for t in tickets %}
<tr>
  <td><a class=tid href="{{ t.url }}" target=_blank rel=noopener>#{{ t.id }}</a></td>
  <td>{{ t.subject }}</td>
  <td>{{ t.status_label }}</td>
  <td>{{ t.priority_label }}</td>
  <td class=meta>{{ t.due_display | safe }}</td>
  <td class=meta>{{ t.created_display }}</td>
  <td>{% if t.tags %}{{ t.tags|join(', ') }}{% else %}<em style=color:#bbb>none</em>{% endif %}</td>
  <td>{{ t.type or '—' }}</td>
</tr>
{% endfor %}
</table>
{% else %}
<div class=empty>No tickets match the current filter.</div>
{% endif %}

<script>
setTimeout(function(){ location.reload(); }, 300000); // auto-refresh every 5 min
</script>
</body></html>
"""

def ticket_url(ticket_id):
    return f"https://{FRESHDESK_DOMAIN}/a/tickets/{ticket_id}"

def fmt_due(due_str):
    if not due_str:
        return "—"
    try:
        dt = datetime.fromisoformat(due_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = dt - now
        days = int(delta.total_seconds() // 86400)
        hours = int((delta.total_seconds() % 86400) // 3600)
        if delta.total_seconds() < 0:
            return f"<span style='color:red;font-weight:bold'>{abs(days)}d {abs(hours)}h OVERDUE</span>"
        return f"{days}d {hours}h left"
    except Exception:
        return due_str

STATUS_LABELS = {2:"Customer responded", 3:"Pending", 4:"Resolved", 5:"Closed", 6:"Waiting on customer", 1:"Open"}
PRIORITY_LABELS = {1:"Low", 2:"Medium", 3:"High", 4:"Urgent"}

@app.route("/queue")
def queue():
    # Return API key warning if missing so user notices before blank page.
    if not FRESHDESK_API_KEY:
        return render_template_string(QUEUE_HTML, tickets=[], total=0,
                                      error="No Freshdesk API key found. Set FRESHDESK_API_KEY env var or write it to ~/.config/furtouch/freshdesk_api_key (chmod 600).")

    show_overdue = request.args.get("overdue", "1") != "0"
    include_waiting = request.args.get("waiting", "0") == "1"

    try:
        now_ts = datetime.now(timezone.utc).timestamp()
        cached = None
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, "r") as fh:
                    blob = json.load(fh)
                if now_ts - blob.get("fetched_at", 0) < CACHE_TTL_SECONDS:
                    cached = blob
            except Exception:
                pass

        if cached:
            raw = cached["tickets"]
            cache_age = int(now_ts - cached.get("fetched_at", now_ts))
        else:
            raw = list(paginate_tickets())
            raw = [t for t in raw if passes_filters(t)]
            with open(CACHE_FILE, "w") as fh:
                json.dump({"fetched_at": now_ts, "tickets": raw}, fh)
            cache_age = 0

        # apply waiting toggle at render time so no extra API calls
        if not include_waiting:
            raw = [t for t in raw if t.get("status") != 6]

    except requests.exceptions.HTTPError as e:
        return render_template_string(QUEUE_HTML, tickets=[], total=0,
                                      error=f"Freshdesk API error: {e.response.status_code} — check your API key and permissions.")
    except Exception as e:
        return render_template_string(QUEUE_HTML, tickets=[], total=0,
                                      error=f"Error fetching tickets: {e}")

    tickets_out = []
    for t in raw:
        sid = t.get("status")
        pid = t.get("priority", 0)
        created = t.get("created_at", "")
        due = t.get("due_by") or t.get("fr_due_by")
        tags = t.get("tags") or []

        # Derive a clean due/overdue display.
        due_display = fmt_due(due)

        tickets_out.append({
            "id": t["id"],
            "url": ticket_url(t["id"]),
            "subject": t.get("subject", ""),
            "status_label": STATUS_LABELS.get(sid, f"Status {sid}"),
            "priority_label": PRIORITY_LABELS.get(pid, f"P{pid}"),
            "due_display": due_display,
            "created_display": created[:10] if created else "—",
            "tags": tags if tags else [],
            "type": t.get("type"),
        })

    return render_template_string(QUEUE_HTML, tickets=tickets_out, total=len(tickets_out), error=None)


HTML = """
<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Mohawk Touch-Up Blend Finder</title>
<style>
 body{font-family:system-ui,Arial,sans-serif;max-width:900px;margin:auto;padding:16px;background:#faf7f2;color:#2a2018}
 h1{font-size:20px;margin:0 0 4px}
 .sub{color:#7a6a58;font-size:13px;margin-bottom:16px}
 .row{display:flex;gap:16px;flex-wrap:wrap}
 .card{background:#fff;border:1px solid #e6dccb;border-radius:10px;padding:14px;flex:1;min-width:260px}
 input[type=file]{width:100%}
 button{background:#7a4a1f;color:#fff;border:0;border-radius:8px;padding:9px 14px;cursor:pointer;font-size:14px}
 canvas{border:1px solid #ccc;border-radius:6px;max-width:100%;cursor:crosshair;touch-action:none}
 .sw{width:60px;height:60px;border-radius:8px;border:1px solid #999;display:inline-block;vertical-align:middle}
 table{width:100%;border-collapse:collapse;font-size:13px}
 td,th{padding:6px 8px;border-bottom:1px solid #eee;text-align:left}
 .chip{width:18px;height:18px;border-radius:4px;display:inline-block;vertical-align:middle;margin-right:6px;border:1px solid #999}
 .recipe{background:#fffdf8;border:1px solid #e6dccb;border-radius:10px;padding:12px;margin:10px 0}
 .bar{height:14px;border-radius:4px;background:linear-gradient(90deg,#ddd,#bbb);display:inline-block;vertical-align:middle}
 .muted{color:#7a6a58;font-size:12px}
 .best{border-color:#7a4a1f;box-shadow:0 0 0 2px #7a4a1f33}
</style></head>
<body>
<h1>Mohawk Touch-Up Blend Finder</h1>
<div class=sub>Upload a photo of the damaged area. Click the spot whose color you want to match, or let it auto-pick the dominant tone. It suggests Blendal/Bronzing powder mixes from your inventory.</div>
<div class=row>
  <div class=card>
    <input id=file type=file accept="image/*"><br><br>
    <button id=auto>Auto-pick dominant color</button>
    <p class=muted>Or click anywhere on the photo to sample that pixel.</p>
    <div style="margin-top:10px">
      Target: <span id=sw class=sw style="background:#ccc"></span>
      RGB <span id=rgbv>—</span>
    </div>
    <button id=find style="margin-top:10px">Find blend</button>
  </div>
  <div class=card>
    <canvas id=cv width=400></canvas>
    <div id=status class=muted></div>
  </div>
</div>
<div id=out></div>

<script>
const cv=document.getElementById('cv'),ctx=cv.getContext('2d');
const file=document.getElementById('file');
let imgEl=null, targetRGB=null;
file.onchange=()=>{
  const f=file.files[0]; if(!f)return;
  const url=URL.createObjectURL(f);
  imgEl=new Image(); imgEl.onload=()=>{
    const max=400; const sc=Math.min(max/imgEl.width,max/imgEl.height,1);
    cv.width=imgEl.width*sc; cv.height=imgEl.height*sc;
    ctx.drawImage(imgEl,0,0,cv.width,cv.height);
  };
  imgEl.src=url;
};
cv.onclick=(e)=>{
  const r=cv.getBoundingClientRect();
  const x=Math.floor((e.clientX-r.left)/r.width*cv.width);
  const y=Math.floor((e.clientY-r.top)/r.height*cv.height);
  const d=ctx.getImageData(x,y,1,1).data;
  setTarget([d[0],d[1],d[2]]);
};
document.getElementById('auto').onclick=()=>{
  if(!imgEl)return;
  const d=ctx.getImageData(0,0,cv.width,cv.height).data;
  let r=0,g=0,b=0,n=0;
  // dominant via simple average of non-near-white/black-ish pixels
  let rs=0,gs=0,bs=0,nn=0;
  for(let i=0;i<d.length;i+=4){
    const R=d[i],G=d[i+1],B=d[i+2];
    if(R>240&&G>240&&B>240)continue;
    rs+=R;gs+=G;bs+=B;nn++;
  }
  setTarget([Math.round(rs/nn),Math.round(gs/nn),Math.round(bs/nn)]);
};
function setTarget(rgb){
  targetRGB=rgb;
  document.getElementById('sw').style.background=`rgb(${rgb[0]},${rgb[1]},${rgb[2]})`;
  document.getElementById('rgbv').textContent=`${rgb[0]}, ${rgb[1]}, ${rgb[2]}`;
}
document.getElementById('find').onclick=async()=>{
  if(!targetRGB){alert('Pick a color first');return;}
  document.getElementById('status').textContent='Computing...';
  const res=await fetch('/api/blend',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({rgb:targetRGB})}).then(r=>r.json());
  render(res);
  document.getElementById('status').textContent='';
};
function chip(c){return `<span class=chip style="background:rgb(${c[0]},${c[1]},${c[2]})"></span>`;}
function render(res){
  let h='';
  h+=`<div class=card style="margin-top:14px"><b>Nearest single powders</b><table>`;
  h+=`<tr><th>Powder</th><th>Color</th><th>RGB</th><th>ΔE</th></tr>`;
  res.nearest.forEach(p=>{
    h+=`<tr><td>${chip(p.rgb)}${p.name}</td><td></td><td>${p.rgb.join(', ')}</td><td>${p.dE}</td></tr>`;
  });
  h+=`</table></div>`;
  h+=`<h3 style="margin-top:18px">Suggested blends</h3>`;
  res.recipes.forEach((r,i)=>{
    const cls=i===0?'recipe best':'recipe';
    let mix=r.powders.map(p=>`${p.name} <b>${p.weight_pct}%</b>`).join(' + ');
    h+=`<div class="${cls}"><div>${chip(r.result_rgb)} Target match — ΔE ${r.dE} (lower=better)</div>
      <div style="margin:6px 0"><b>Mix:</b> ${mix}</div>
      <div class=muted>${r.powders.map(p=>`${chip(p.rgb)}${p.name} (${p.conf})`).join(' ')}</div></div>`;
  });
  document.getElementById('out').innerHTML=h;
}
</script>
</body></html>
"""

@app.route("/")
def home():
    return render_template_string(HTML)

@app.route("/api/blend", methods=["POST"])
def api_blend():
    data = request.get_json()
    rgb = tuple(int(x) for x in data["rgb"])
    n = nearest(rgb, k=8)
    recs = solve_blend(rgb, max_powders=3)
    return jsonify({"target": list(rgb), "nearest": n, "recipes": recs})

def dominant_rgb(img):
    """Return average RGB of non-near-white/near-black pixels (the surface tone)."""
    img = img.convert("RGB")
    small = img.resize((200, 200))
    px = list(small.getdata())
    rs = gs = bs = nn = 0
    for (R, G, B) in px:
        if R > 240 and G > 240 and B > 240:
            continue
        if R < 12 and G < 12 and B < 12:
            continue
        rs += R; gs += G; bs += B; nn += 1
    if nn == 0:
        return (128, 128, 128)
    return (rs // nn, gs // nn, bs // nn)

@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return "no file", 400
    f = request.files["file"]
    img = Image.open(io.BytesIO(f.read()))
    rgb = dominant_rgb(img)
    return jsonify({"rgb": list(rgb)})

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5050"))
    if host == "0.0.0.0":
        raise SystemExit(
            "Refusing to bind to 0.0.0.0. Set HOST=127.0.0.1 or export PORT=5050."
        )
    app.run(host=host, port=port, debug=False)
