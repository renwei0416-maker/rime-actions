"""端到端验证正式 dll 的 open_url：真的用默认浏览器打开一个网址。

验证办法：起一个本地 HTTP 服务，让 open_url 打开 http://127.0.0.1:<port>/probe-open-url，
然后看服务端日志里有没有这条 GET —— 有就说明浏览器确实被拉起来了。

用法：python probe_open_url_e2e.py http://127.0.0.1:8765/probe-open-url
"""
import ctypes as C
import pathlib
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RIME = r"C:\Program Files\Rime\weasel-0.17.4\rime.dll"
DLLDIR = r"C:\Users\Administrator\Desktop\Ai_Work\DSH_harness\backspace-build"
DLL = pathlib.Path(DLLDIR) / "actions.dll"
URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765/probe-open-url"

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


def lua(src, args=1):
    if D.luaL_loadstring(L, src.encode("utf-8")) != 0:
        return "<load error>"
    if D.lua_pcallk(L, 0, args, 0, None, None) != 0:
        n = C.c_size_t()
        return "<pcall error: %s>" % D.lua_tolstring(L, -1, C.byref(n)).decode("utf-8", "replace")
    if args == 0:
        return ""
    n = C.c_size_t()
    ptr = D.lua_tolstring(L, -1, C.byref(n))
    return ptr.decode("utf-8", "replace") if ptr else ""


lua('package.cpath = [[' + DLLDIR + r'\?.dll]] .. ";" .. package.cpath')
print("require:", lua('SK = require("actions") return type(SK.open_url)'))

ret = lua('return SK.open_url([[' + URL + ']])')
print("open_url ->", ret, "(1=已排队)")

# 后台线程有 FIRE_DELAY_MS=80ms 的延迟，等它真的去开
time.sleep(1.5)

# 再排一次，确认 150ms 硬保护之后的第二次也能过（可选信息）
ret2 = lua('return SK.open_url([[' + URL + ']])')
print("second open_url ->", ret2)

D.lua_close(L)
print("RESULT: OK" if ret == "1" else "RESULT: UNEXPECTED")
sys.exit(0 if ret == "1" else 1)
