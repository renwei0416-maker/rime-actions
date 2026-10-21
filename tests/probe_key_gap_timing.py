# -*- coding: utf-8 -*-
"""Verify that press_keys' second argument (gap_ms) really paces the injection.

Owns a test window with a one-line EDIT (text "alpha beta gamma delta",
caret at end), forces it foreground, then asks the PRODUCTION dll to send
"left,left,left,left" -- four separate steps, so the caret walks left one
char at a time -- twice: gap 0 (all events in one SendInput batch) and
gap 250 (each event 250 ms apart).  It polls the selection while the keys
are being delivered and reports how long the walking took.

Expected: gap 0 finishes in well under 200 ms, gap 250 needs about
(8 events + 3 step gaps) * 250 ms ~= 2 s.  ASCII output.
"""
import ctypes as C
import time

import probe_inject_variants as P
import os
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RIME = r"C:\Program Files\Rime\weasel-0.17.4\rime.dll"
DLL = os.path.join(_ROOT, "actions.dll")

D = C.CDLL(RIME)
D.luaL_newstate.restype = C.c_void_p
D.luaL_loadstring.restype = C.c_int
D.luaL_loadstring.argtypes = [C.c_void_p, C.c_char_p]
D.lua_pcallk.restype = C.c_int
D.lua_pcallk.argtypes = [C.c_void_p, C.c_int, C.c_int, C.c_int, C.c_void_p, C.c_void_p]
D.lua_tolstring.restype = C.c_char_p
D.lua_tolstring.argtypes = [C.c_void_p, C.c_int, C.POINTER(C.c_size_t)]

L = C.c_void_p(D.luaL_newstate())
D.luaL_openlibs(L)


def lua(src):
    if D.luaL_loadstring(L, src.encode("utf-8")) != 0:
        return "<load error>"
    if D.lua_pcallk(L, 0, 1, 0, None, None) != 0:
        return "<pcall error>"
    n = C.c_size_t()
    p = D.lua_tolstring(L, -1, C.byref(n))
    return p.decode("utf-8", "replace") if p else ""


print("dll:", lua(
    'local f = package.loadlib([[' + DLL + ']], "luaopen_actions")\n'
    'SK = f()\n'
    'return type(SK.press_keys)'))

P.make_window()
print("TEXT = %r (%d chars, caret at end)" % (P.TEXT, len(P.TEXT)))
print("foreground ok = %s" % (P.u.GetForegroundWindow() == P.hwnd))
print()

SPEC = "left,left,left,left"
fails = []

for gap in (0, 250):
    P.clear_pending()
    P.set_text(P.TEXT)
    P.pump(300)

    start = time.time()
    ret = lua('return SK.press_keys([[' + SPEC + ']], %d)' % gap)
    deadline = start + 6.0
    last = None
    moved_at = []
    while time.time() < deadline:
        P.pump(10)
        s, e = P.get_sel()
        if last is None or s != last:
            moved_at.append((round(time.time() - start, 2), s))
            last = s
        if s == 18:      # 4 steps * 1 char left
            break
    elapsed = time.time() - start
    s, e = P.get_sel()

    # gap 0: should be done in a blink; gap 250: each char must arrive >= ~200 ms apart
    if gap == 0:
        ok = ret == "1" and s == 18 and elapsed < 0.5
    else:
        ok = ret == "1" and s == 18 and elapsed > 1.0
    if not ok:
        fails.append((gap, ret, s, round(elapsed, 2)))

    print("%-4s ret=%s final caret=%d elapsed=%.2fs steps=%s" % (
        "OK" if ok else "FAIL", ret, s, elapsed, moved_at))

print()
print("RESULT:", "ALL PASS" if not fails else "FAILED -> %s" % fails)
