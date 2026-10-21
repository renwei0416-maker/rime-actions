"""验证 actions.dll 的 press_backspace_then_text：先删再打，顺序由后台线程保证。

用 dry-run 版 dll，不向 Windows 发任何按键。
"""
import ctypes as C
import os
import subprocess
import sys
import time
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = _ROOT
MINGW_BIN = r"C:\msys64\mingw64\bin"
WEASEL_DIR = r"C:\Program Files\Rime\weasel-0.17.4"
RIME = os.path.join(WEASEL_DIR, "rime.dll")
DRYDIR = os.path.join(BASE, "_testdata", "dryrun", "userdir", "lua", "wanxiang", "ziji")


def ensure_dry_run_dll():
    target = os.path.join(DRYDIR, "actions.dll")
    if os.path.exists(target):
        return
    os.makedirs(DRYDIR, exist_ok=True)
    env = dict(os.environ)
    env["PATH"] = MINGW_BIN + ";" + env["PATH"]
    cmd = [
        os.path.join(MINGW_BIN, "gcc.exe"), "-shared", "-fPIC", "-DACTIONS_DRY_RUN",
        "-o", target, "actions.c", "-Iinclude", "-L" + WEASEL_DIR,
        "-luser32", "-lrime", "-lshell32", "-lwinhttp", "-O2",
        "-Wl,--no-insert-timestamp",
    ]
    subprocess.run(cmd, cwd=BASE, env=env, check=True)


ensure_dry_run_dll()

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
    rc = D.luaL_loadstring(L, src.encode("utf-8"))
    if rc != 0:
        return "<load error %d>" % rc
    if D.lua_pcallk(L, 0, args, 0, None, None) != 0:
        n = C.c_size_t()
        return "<pcall error: %s>" % D.lua_tolstring(L, -1, C.byref(n)).decode("utf-8", "replace")
    if args == 0:
        return ""
    n = C.c_size_t()
    ptr = D.lua_tolstring(L, -1, C.byref(n))
    return ptr.decode("utf-8", "replace") if ptr else ""


print("load:", lua(
    'package.cpath = [[' + DRYDIR + r'\?.dll]] .. ";" .. package.cpath' "\n"
    'M = require("actions")' "\n"
    'return type(M.press_backspace_then_text)'))

CHECKS = []


def check(name, got, want):
    ok = got == want
    CHECKS.append(ok)
    print(("  OK   " if ok else "  FAIL ") + "%-22s -> %s" % (name, got))
    if not ok:
        print("         want: %s" % (want,))


# 3 个退格（每个按下+抬起 = 6 个事件）后打 "AB"（每个字符 2 个事件）
lua('M._dry_run_reset(); R = M.press_backspace_then_text(3, "AB")')
time.sleep(0.8)
check("ret", lua('return tostring(R)'), "1")
check("events", lua('return table.concat(M._dry_run_events(), ",")'),
      "08,08-up,08,08-up,08,08-up,u0041,u0041-up,u0042,u0042-up")

# count = 0：只打字
lua('M._dry_run_reset(); R = M.press_backspace_then_text(0, "hi")')
time.sleep(0.8)
check("no_delete", lua('return table.concat(M._dry_run_events(), ",")'),
      "u0068,u0068-up,u0069,u0069-up")

# 中文：按 UTF-16 逐单元打
lua('M._dry_run_reset(); R = M.press_backspace_then_text(1, "你")')
time.sleep(0.8)
check("cjk", lua('return table.concat(M._dry_run_events(), ",")'),
      "08,08-up,u4F60,u4F60-up")

# 参数错误一律 0，什么也不发
for args in ('(-1, "x")', '(3, "")', '(3, nil)'):
    lua('M._dry_run_reset(); R = M.press_backspace_then_text' + args)
    time.sleep(0.3)
    check("invalid " + args, lua('return tostring(R) .. "|" .. (#M._dry_run_events() > 0 and "sent" or "none")'),
          "0|none")

print()
print("CHECKS:", "ALL PASS" if all(CHECKS) else "FAILED")
sys.exit(0 if all(CHECKS) else 1)
