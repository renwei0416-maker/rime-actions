"""离线验证正式 dll 里 AI 那套函数是否真的能用（网络只连本机 127.0.0.1）。

安全边界（这个脚本不会碰外面的世界）：
- http_request / http_state / http_result / http_cancel：只打本机临时 HTTP 服务，
  而且服务是本脚本自己起的、只监听 127.0.0.1 的临时端口，不连外网。
- send_text：只测"参数非法 -> 0"的两条分支（空串 / 超长）。真打字会把字打进当前窗口，
  所以这里绝不调用带正文的 send_text；异步逐字打字由 tests 里的桩和 dry-run 覆盖。
- nudge：只发一次 F24（键盘上没这个键、没有任何软件会响应它），发完就等 200ms。
- clipboard_get：只读剪贴板，不写；读不到也算过（只报告拿到的是文本还是错误）。
"""
import ctypes as C
import hashlib
import http.server
import pathlib
import sys
import threading
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RIME = r"C:\Program Files\Rime\weasel-0.17.4\rime.dll"
DLLDIR = r"C:\Users\Administrator\Desktop\Ai_Work\DSH_harness\backspace-build"
DLL = pathlib.Path(DLLDIR) / "actions.dll"

OK_BODY = '{"choices":[{"message":{"content":"你好世界"}}]}'

seen = []


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8", "replace")
        seen.append({
            "path": self.path,
            "body": body,
            "auth": self.headers.get("Authorization") or "",
        })
        if self.path.rstrip("/").endswith("/fail"):
            raw = b"boom"
            self.send_response(500)
        else:
            raw = OK_BODY.encode("utf-8")
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]
BASE = "http://127.0.0.1:%d" % PORT

D = C.CDLL(RIME)
D.luaL_newstate.restype = C.c_void_p
D.luaL_loadstring.restype = C.c_int
D.luaL_loadstring.argtypes = [C.c_void_p, C.c_char_p]
D.lua_pcallk.restype = C.c_int
D.lua_pcallk.argtypes = [C.c_void_p, C.c_int, C.c_int, C.c_int, C.c_void_p, C.c_void_p]
D.lua_tolstring.restype = C.c_char_p
D.lua_tolstring.argtypes = [C.c_void_p, C.c_int, C.POINTER(C.c_size_t)]
D.lua_close.argtypes = [C.c_void_p]

L = C.c_void_p(D.luaL_newstate())
D.luaL_openlibs(L)


def lua(src):
    if D.luaL_loadstring(L, src.encode("utf-8")) != 0:
        return "<load error>"
    if D.lua_pcallk(L, 0, 1, 0, None, None) != 0:
        n = C.c_size_t()
        return "<pcall error: %s>" % D.lua_tolstring(L, -1, C.byref(n)).decode("utf-8", "replace")
    n = C.c_size_t()
    ptr = D.lua_tolstring(L, -1, C.byref(n))
    return ptr.decode("utf-8", "replace") if ptr else ""


print("dll:", DLL, DLL.stat().st_size, "bytes",
      hashlib.sha256(DLL.read_bytes()).hexdigest()[:16] + "…")
print("local http:", BASE)

fails = []


def check(name, got, want):
    ok = got == want
    if not ok:
        fails.append(name)
    print(("  PASS  " if ok else "  FAIL  ") + name + "  got=" + repr(got)
          + ("" if ok else "  want=" + repr(want)))


def contains(name, got, needle):
    ok = needle in got
    if not ok:
        fails.append(name)
    print(("  PASS  " if ok else "  FAIL  ") + name + "  got=" + repr(got[:120])
          + ("" if ok else "  want~" + repr(needle)))


lua('package.cpath = [[' + DLLDIR + r'\?.dll]] .. ";" .. package.cpath' "\n"
    'SK = require("actions")')

check("api", lua(
    'return table.concat({type(SK.http_request), type(SK.http_state),'
    ' type(SK.http_result), type(SK.http_cancel), type(SK.clipboard_get),'
    ' type(SK.nudge), type(SK.send_text)}, "/")'),
    "function/function/function/function/function/function/function")

# ---- 参数校验（不排队、不联网）----
check("http_request('') -> 0", lua('return SK.http_request("")'), "0")
check("http_request('ftp://…') -> 0",
      lua('return SK.http_request("ftp://127.0.0.1/x")'), "0")
check("send_text('') -> 0", lua('return SK.send_text("")'), "0")
check("send_text(20001 bytes) -> 0",
      lua('return SK.send_text(string.rep("a", 20001))'), "0")
check("open_url('') -> 0", lua('return SK.open_url("")'), "0")

# ---- 成功路径：POST 到本机服务，取回 JSON ----
body = '{"model":"probe-model","messages":[{"role":"user","content":"hi"}]}'
# 头之间必须是真正的 CRLF（WinHttpAddRequestHeaders 按 CRLF 切行）；
# 用 string.char(13,10) 拼，避免 Lua 长字符串里写 "\r\n" 变成字面反斜杠。
hdrs = "[[Content-Type: application/json]] .. string.char(13,10)"
hdrs += " .. [[Authorization: Bearer probe-key]]"
check("http_request ok -> seq 1", lua(
    'SK_SEQ = SK.http_request([[' + BASE + '/v1/chat/completions]], [[' + body + ']], '
    + hdrs + ')' "\n"
    'return tostring(SK_SEQ)'), "1")

state = ""
for _ in range(200):
    state = lua('local s = SK.http_state(); return s')
    if state in ("done", "failed"):
        break
    time.sleep(0.05)
check("state reaches done", state, "done")

res = lua('local b = SK.http_result();'
          ' return (b and (b:gsub("%s+", " ")) or "nil")')
contains("result has content 你好世界", res, "你好世界")
contains("result is the canned json", res, "choices")

check("state back to idle", lua('local s = SK.http_state(); return s'), "idle")
check("http_cancel() on idle -> 0", lua('return tostring(SK.http_cancel())'), "0")

check("server saw one POST", len(seen), 1)
if seen:
    contains("server got the body", seen[0]["body"], "probe-model")
    check("server got the auth header", seen[0]["auth"], "Bearer probe-key")
    check("server got the path", seen[0]["path"], "/v1/chat/completions")

# ---- 失败路径：HTTP 500 ----
check("http_request fail-path -> seq 2", lua(
    'return tostring(SK.http_request([[' + BASE + '/fail]], "{}", ""))'), "2")

state = ""
for _ in range(200):
    state = lua('local s = SK.http_state(); return s')
    if state in ("done", "failed"):
        break
    time.sleep(0.05)
check("state reaches failed", state, "failed")

err = lua('local b, e = SK.http_result();'
          ' return (b and ("UNEXPECTED:" .. tostring(b)) or tostring(e))')
contains("error mentions 500", err, "500")
check("state idle after failure", lua('local s = SK.http_state(); return s'), "idle")

# ---- 剪贴板（只读）与 F24 唤醒（无副作用）----
clip = lua('local t, e = SK.clipboard_get();'
           ' if t then return "text:" .. tostring(#t) end'
           ' return "err:" .. tostring(e)')
print("  INFO  clipboard_get ->", repr(clip))
ok_clip = clip.startswith("text:") or clip.startswith("err:")
check("clipboard_get returns text or an error", ok_clip, True)

check("nudge(-1, 0) -> 1", lua('return tostring(SK.nudge(-1, 0))'), "1")
time.sleep(0.2)

print()
if fails:
    print("CHECKS: ALL PASS" if not fails else "FAILED: " + ", ".join(fails))
    sys.exit(1)
print("CHECKS: ALL PASS")
