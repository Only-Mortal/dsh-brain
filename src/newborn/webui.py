"""极简 Web 监控台（第一版，用标准库，不引入框架）。

作用：打开浏览器就能看见大脑现在什么样——后端是哪个、事件进来多少、情绪数值、
最近说了什么、记住了什么、有没有没发出去的行动。

路由：
    GET  /            监控页（3 秒自动刷新）
    GET  /api/state   全量状态 JSON
    GET  /api/health  存活检查
    POST /api/ingest  {"text": "...", "actor": "u1", "conversation": "c1"}  手动喂一句

启动：
    python -m newborn.webui --db memory/newborn.sqlite3 --port 8765
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import db, kernel, memory as memory_mod
from .events import EventEnvelope
from .inference import describe, get_backend
from .reasoner import make_reasoner

DEFAULT_DB = Path(__file__).resolve().parents[2] / "memory" / "newborn.sqlite3"

PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<title>newborn · 大脑监控</title>
<style>
:root{--bg:#0f1216;--fg:#e6e9ee;--dim:#8b94a3;--card:#171b21;--line:#242a33;--ok:#57c98b;--warn:#e0b34a;--bad:#e06c6c}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.6 "Segoe UI","Microsoft YaHei",sans-serif}
header{display:flex;gap:16px;align-items:baseline;padding:16px 22px;border-bottom:1px solid var(--line)}
h1{font-size:16px;margin:0;font-weight:600}
.tag{font-size:12px;color:var(--warn);
border:1px solid var(--warn);border-radius:999px;padding:1px 8px}
.wrap{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px;padding:16px 22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card h2{font-size:13px;color:var(--dim);margin:0 0 10px;font-weight:600;letter-spacing:.5px}
.kv{display:flex;justify-content:space-between;gap:12px;padding:3px 0}
.kv span:first-child{color:var(--dim)}
.bar{height:8px;background:#20262e;border-radius:4px;overflow:hidden;margin:4px 0 10px}
.bar i{display:block;height:100%;background:linear-gradient(90deg,#3d7cf4,#57c98b)}
ul{list-style:none;margin:0;padding:0}
li{padding:6px 0;border-bottom:1px dashed #202630;font-size:13px}
li:last-child{border-bottom:0}
small{color:var(--dim)}
.mono{font-family:Consolas,monospace;font-size:12px;color:var(--dim)}
#err{color:var(--bad);padding:0 22px}
</style></head>
<body>
<header>
  <h1>newborn · 大脑监控</h1>
  <span class="tag" id="backend">后端加载中</span>
  <span class="mono" id="clock"></span>
</header>
<div id="err"></div>
<div class="wrap">
  <div class="card"><h2>运行状态</h2><div id="stats"></div></div>
  <div class="card"><h2>模拟情绪</h2><div id="affect"></div></div>
  <div class="card"><h2>最近事件</h2><ul id="events"></ul></div>
  <div class="card"><h2>正在记的事</h2><ul id="memories"></ul></div>
  <div class="card"><h2>待执行行动</h2><ul id="outbox"></ul></div>
  <div class="card"><h2>手动喂一句</h2>
    <input id="say" placeholder="说点什么，回车投递" style="width:100%;padding:8px;background:#0f1216;color:var(--fg);border:1px solid var(--line);border-radius:8px">
    <small id="sayhint"></small>
  </div>
</div>
<script>
function esc(s){return (s??'').toString().replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]))}
async function tick(){
  try{
    const r = await fetch('/api/state'); const d = await r.json();
    document.getElementById('err').textContent = d.error || '';
    const b = d.backend || {};
    document.getElementById('backend').textContent = '推理后端：' + (b.name||'?') + (b.model? ' · '+b.model : '') + (b.test_only? ' （测试用）':'');
    document.getElementById('clock').textContent = new Date().toLocaleString();
    const s = d.stats || {};
    document.getElementById('stats').innerHTML = Object.entries({
      '事件总数':s.events, '待处理/失败':s.pending_events, '活跃记忆':s.memories,
      '候选记忆':s.candidates, '待发行动':s.outbox_pending
    }).map(([k,v])=>`<div class="kv"><span>${k}</span><span>${v??'-'}</span></div>`).join('');
    const a = s.affect || {};
    document.getElementById('affect').innerHTML = Object.entries(a).map(([k,v])=>{
      const pct = Math.max(0,Math.min(100, (v+ (k==='valence'?1:0)) / (k==='valence'?2:1) *100));
      return `<div class="kv"><span>${k}</span><span>${v}</span></div><div class="bar"><i style="width:${pct}%"></i></div>`;
    }).join('');
    document.getElementById('events').innerHTML = (d.events||[]).map(e=>
      `<li><small>${new Date(e.received_at*1000).toLocaleTimeString()} · ${esc(e.process_state)}</small><br>${esc(e.text).slice(0,80)}</li>`).join('') || '<li><small>还没有事件</small></li>';
    document.getElementById('memories').innerHTML = (d.memories||[]).map(m=>
      `<li><small>${esc(m.lifecycle_state)} · ${esc(m.epistemic_status)} · ${esc(m.privacy_scope)}</small><br>${esc(m.content).slice(0,90)}</li>`).join('') || '<li><small>还没有记忆</small></li>';
    document.getElementById('outbox').innerHTML = (d.outbox||[]).map(o=>
      `<li><small>${esc(o.status)} · ${esc(o.kind)}</small><br>${esc(o.text).slice(0,90)}</li>`).join('') || '<li><small>没有待发行动</small></li>';
  }catch(e){ document.getElementById('err').textContent = '拉取失败：'+e; }
}
const say = document.getElementById('say');
say.addEventListener('keydown', async ev=>{
  if(ev.key!=='Enter'||!say.value.trim())return;
  const text = say.value.trim(); say.value='';
  document.getElementById('sayhint').textContent='投递中…';
  try{
    const r = await fetch('/api/ingest',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text})});
    const d = await r.json();
    document.getElementById('sayhint').textContent = d.status==='processed'
      ? ('已处理，注意力 '+d.attention+'；回复：'+(d.reply||'（无）'))
      : ('状态：'+d.status+' '+(d.error||''));
    tick();
  }catch(e){ document.getElementById('sayhint').textContent='投递失败：'+e; }
});
tick(); setInterval(tick, 3000);
</script>
</body></html>
"""


MAX_BODY_BYTES = 8192
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
MASKED = "（私有内容已隐藏；需要在带令牌的接口里按实例申请）"


def is_loopback(host: str) -> bool:
    return (host or "").strip().lower() in LOOPBACK


class Handler(BaseHTTPRequestHandler):
    server_version = "newborn-webui/0.2"
    db_path: str = str(DEFAULT_DB)
    token: str | None = None
    allow_remote: bool = False

    # ---- 访问控制：默认只认回环；开了远程就必须带令牌
    def _authorized(self) -> bool:
        if self.token:
            got = self.headers.get("X-Auth-Token") or ""
            return got == self.token
        return is_loopback(self.client_address[0])

    def log_message(self, fmt: str, *args) -> None:  # 安静点
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # ---------------------------------------------------------------- helpers
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _conn(self) -> sqlite3.Connection:
        return db.connect(self.db_path)

    def _state(self, instance: str = "default") -> dict:
        conn = self._conn()
        try:
            events = conn.execute(
                "SELECT event_id, received_at, process_state, payload, privacy_scope FROM events"
                " WHERE instance_id = ? ORDER BY received_at DESC LIMIT 20", (instance,)).fetchall()
            memories = conn.execute(
                "SELECT memory_id, content, display_text, lifecycle_state, epistemic_status,"
                " privacy_scope, created_at FROM memories WHERE instance_id = ?"
                " ORDER BY created_at DESC LIMIT 20", (instance,)).fetchall()
            outbox = conn.execute(
                "SELECT o.action_id, o.kind, o.payload, o.status FROM outbox o"
                " JOIN events e ON e.event_id = o.event_id WHERE e.instance_id = ?"
                " ORDER BY o.created_at DESC LIMIT 20", (instance,)).fetchall()
            backend = get_backend()
            return {
                "backend": describe(backend),
                "stats": kernel.stats(conn, instance),
                "events": [{
                    "event_id": e["event_id"],
                    "received_at": e["received_at"],
                    "process_state": e["process_state"],
                    "text": (json.loads(e["payload"]) or {}).get("text", ""),
                } for e in events],
                "memories": [dict(m) for m in memories],
                "outbox": [{
                    "action_id": o["action_id"], "kind": o["kind"], "status": o["status"],
                    "text": (json.loads(o["payload"]) or {}).get("text", ""),
                } for o in outbox],
                "ts": time.time(),
            }
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            conn.close()

    # ---------------------------------------------------------------- routes
    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            self._json({"error": "unauthorized"}, 401)
            return
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/health":
            self._json({"ok": True, "ts": time.time()})
        elif path == "/api/state":
            self._json(self._state())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._json({"error": "unauthorized"}, 401)
            return
        path = urllib.parse.urlparse(self.path).path
        if path != "/api/ingest":
            self._json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                self._json({"status": "rejected", "problems": ["请求体过大"]}, 413)
                return
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8") or "{}")
            text = str(body.get("text", "")).strip()
            if not text:
                self._json({"status": "rejected", "problems": ["text 为空"]}, 400)
                return
            env = EventEnvelope(
                event_type="message.received",
                source="webui",
                payload={"text": text},
                instance_id=str(body.get("instance") or "default"),
                actor_id=str(body.get("actor") or "owner"),
                conversation_id=body.get("conversation") or "web",
                privacy_scope=str(body.get("privacy") or "conversation"),
            )
            conn = self._conn()
            try:
                res = kernel.process_event(conn, env, make_reasoner())
            finally:
                conn.close()
            self._json(res)
        except Exception as exc:
            self._json({"status": "error", "error": f"{type(exc).__name__}: {exc}"}, 500)


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8765,
          token: str | None = None) -> None:
    if not is_loopback(host) and not token:
        raise SystemExit("拒绝在非回环地址上无令牌启动：加 --token 或用 127.0.0.1")
    Handler.db_path = db_path
    Handler.token = token
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"newborn 监控台：http://{host}:{port}   数据库：{db_path}", flush=True)
    print(f"推理后端：{describe(get_backend())}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    p = argparse.ArgumentParser(description="newborn 监控台")
    p.add_argument("--db", default=os.environ.get("NEWBORN_DB") or str(DEFAULT_DB))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    a = p.parse_args(argv)
    serve(a.db, a.host, a.port, a.token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
