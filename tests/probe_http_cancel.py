"""验证"请求还在跑时取消，之后还能立刻再发"。

以前的症状：发一个慢请求 → 中途 http_cancel() → 后台线程跑完把状态置成 DONE，
没人取 → 之后每一次 http_request() 都返回 -1（"上一个还没跑完"），直到重新部署。

本探针只用本机 127.0.0.1 上的临时 HTTP 服务，不联网。
"""
import ctypes
import http.server
import os
import sys
import tempfile
import threading
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RIME = os.environ.get("RIME_DLL", r"C:\Program Files\Rime\weasel-0.17.4\rime.dll")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DLL = os.environ.get("ACTIONS_DLL", os.path.join(_ROOT, "actions.dll"))

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, ok))
    print(("  PASS  " if ok else "  FAIL  ") + name + ("  " + detail if detail else ""))


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path.startswith("/slow"):
            time.sleep(3.0)
        body = b'{"ok":"' + self.path.encode() + b'"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


LUA = r"""
local A = assert(package.loadlib([[DLLPATH]], "luaopen_actions"))()
local port = tonumber(PORT)
local base = "http://127.0.0.1:" .. port
local out = {}

local function state() return (A.http_state()) end

out.slow_seq = tostring(A.http_request(base .. "/slow", "{}", "Content-Type: application/json"))
sleep(300)
out.running = state()

out.cancel = tostring(A.http_cancel())
out.after_cancel = state()

out.fast_seq = tostring(A.http_request(base .. "/fast", "{}", "Content-Type: application/json"))

local waited = 0
while waited < 8000 do
    if state() ~= "running" then break end
    sleep(100)
    waited = waited + 100
end
out.final_state = state()
local body, err = A.http_result()
out.body = body and body:sub(1, 60) or ("ERR:" .. tostring(err))
out.idle_after = state()

local f = assert(io.open([[OUTFILE]], "wb"))
for k, v in pairs(out) do f:write(k .. "=" .. tostring(v) .. "\n") end
f:close()
"""


def run():
    dll = ctypes.CDLL(RIME)
    dll.luaL_newstate.restype = ctypes.c_void_p
    dll.luaL_openlibs.argtypes = [ctypes.c_void_p]
    dll.luaL_loadstring.restype = ctypes.c_int
    dll.luaL_loadstring.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    dll.lua_pcallk.restype = ctypes.c_int
    dll.lua_pcallk.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                               ctypes.c_void_p, ctypes.c_void_p]
    dll.lua_tolstring.restype = ctypes.c_char_p
    dll.lua_tolstring.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_size_t)]
    dll.lua_close.argtypes = [ctypes.c_void_p]

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("local http server on 127.0.0.1:%d" % port)

    outpath = os.path.join(tempfile.gettempdir(), "probe_http_cancel.out")
    if os.path.exists(outpath):
        os.remove(outpath)

    prelude = "function sleep(ms) local t0 = os.clock() while (os.clock() - t0) * 1000 < ms do end end\n"
    src = (prelude + LUA
           .replace("DLLPATH", DLL.replace("\\", "\\\\"))
           .replace("OUTFILE", outpath.replace("\\", "\\\\"))
           .replace("PORT", str(port)))

    L = dll.luaL_newstate()
    dll.luaL_openlibs(L)
    rc = dll.luaL_loadstring(L, src.encode("utf-8"))
    if rc == 0:
        rc = dll.lua_pcallk(L, 0, 0, 0, None, None)
    if rc != 0:
        n = ctypes.c_size_t()
        p = dll.lua_tolstring(L, -1, ctypes.byref(n))
        print("LUA ERROR:", p.decode("utf-8", "replace") if p else "?")
        dll.lua_close(L)
        srv.shutdown()
        return 1
    dll.lua_close(L)
    srv.shutdown()

    got = {}
    with open(outpath, "r", encoding="utf-8") as f:
        for line in f:
            if "=" in line:
                k, v = line.rstrip("\n").split("=", 1)
                got[k] = v

    print("--- 结果")
    for k in ("slow_seq", "running", "cancel", "after_cancel", "fast_seq",
              "final_state", "body", "idle_after"):
        print("  %-12s %s" % (k, got.get(k)))
    print("--- 断言")

    check("慢请求发出（seq >= 1）", got.get("slow_seq", "").isdigit() and int(got["slow_seq"]) >= 1,
          "seq=" + got.get("slow_seq", ""))
    check("取消时状态是 running", got.get("running") == "running", got.get("running", ""))
    check("取消成功（cancel = 1）", got.get("cancel") == "1", got.get("cancel", ""))
    check("取消后立刻回到 idle", got.get("after_cancel") == "idle", got.get("after_cancel", ""))
    check("取消后能立刻再发（不再 -1）",
          got.get("fast_seq", "").isdigit() and int(got["fast_seq"]) >= 1,
          "seq=" + got.get("fast_seq", ""))
    check("新请求完成", got.get("final_state") == "done", got.get("final_state", ""))
    check("拿到的是新请求的结果", "/fast" in got.get("body", ""), got.get("body", ""))
    check("取完回到 idle", got.get("idle_after") == "idle", got.get("idle_after", ""))

    failed = [n for n, ok in CHECKS if not ok]
    print("\nCHECKS: %s" % ("ALL PASS" if not failed else "FAILED: " + ", ".join(failed)))
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(run())
