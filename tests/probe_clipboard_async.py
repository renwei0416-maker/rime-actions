"""离线验证正式 dll 的"后台读剪贴板"接口（clipboard_request / clipboard_take）。

只读剪贴板、只写本脚本自己设置的那段测试文字，不碰别的程序。
流程：先用 PowerShell 把已知文字放进剪贴板 → clipboard_request() → 轮询 clipboard_take()
      直到拿到文本 → 校验内容 → 再取一次应为空 → 清空剪贴板后应拿到错误文案。
"""
import ctypes as C
import hashlib
import os
import pathlib
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RIME = os.environ.get("RIME_DLL", r"C:\Program Files\Rime\weasel-0.17.4\rime.dll")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DLLDIR = os.environ.get("DLLDIR", _ROOT)
DLL = pathlib.Path(DLLDIR) / "actions.dll"

SAMPLE = "异步剪贴板测试ABC"

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


TAKE = ('local t, e = SK.clipboard_take()\n'
        'if t then return "T:" .. t end\n'
        'if e then return "E:" .. e end\n'
        'return "P"')


def take_poll(timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        r = lua(TAKE)
        if r != "P":
            return r
        time.sleep(0.05)
    return "P"


def set_clip(text):
    if text == "":
        ps = "Set-Clipboard -Value $null"
    else:
        ps = "Set-Clipboard -Value '" + text.replace("'", "''") + "'"
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


print("dll:", DLL, DLL.stat().st_size, "bytes",
      hashlib.sha256(DLL.read_bytes()).hexdigest()[:16] + "…")

fails = []


def check(name, got, want):
    ok = got == want
    if not ok:
        fails.append(name)
    print(("  PASS  " if ok else "  FAIL  ") + name + "  got=" + repr(got)
          + ("" if ok else "  want=" + repr(want)))


lua('package.cpath = [[' + DLLDIR + r'\?.dll]] .. ";" .. package.cpath' "\n"
    'SK = require("actions")')

check("api", lua(
    'return table.concat({type(SK.clipboard_request), type(SK.clipboard_take),'
    ' type(SK.clipboard_get)}, "/")'), "function/function/function")

# ---- 有文字：排队 -> 轮询 -> 拿到 ----
set_clip(SAMPLE)
check("request -> 1", lua('return SK.clipboard_request()'), "1")
check("take -> 文本", take_poll(), "T:" + SAMPLE)
check("take again -> 空", lua(TAKE), "P")

# ---- 清空剪贴板：排队后应拿到错误文案 ----
set_clip("")
check("request (empty) -> 1", lua('return SK.clipboard_request()'), "1")
r = take_poll()
ok = r.startswith("E:")
if not ok:
    fails.append("empty clipboard -> 错误文案")
print(("  PASS  " if ok else "  FAIL  ") + "empty clipboard -> 错误文案  got=" + repr(r))

# ---- 同步老接口仍在 ----
set_clip(SAMPLE)
check("clipboard_get -> 文本", lua('return tostring(SK.clipboard_get())'), SAMPLE)

set_clip("")
print()
if fails:
    print("CHECKS: FAILED ->", fails)
    sys.exit(1)
print("CHECKS: ALL PASS")
