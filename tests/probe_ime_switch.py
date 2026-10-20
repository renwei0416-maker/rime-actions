"""受控实验（不建窗口，只观测）：注入的 Ctrl+Shift+Left 会不会触发 Windows 的“切换输入法”热键？

每次都读“前台窗口所属线程”的键盘布局 HKL：
  A) 一次 SendInput 全塞进去（= 当前 dll 的注入方式）
  B) 分步 + 35ms 间隔（= 拟真手按）
A 变了而 B 没变 -> 改 dll 分步注入就能解决；
A、B 都变      -> 是系统热键（HKCU\\Keyboard Layout\\Toggle\\Layout Hotkey），得从系统设置下手。
每次变完都恢复原来的布局。
"""
import ctypes
import time
from ctypes import wintypes

u = ctypes.WinDLL("user32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class _INPUTunion(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 24)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTunion)]


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL, VK_SHIFT, VK_LEFT = 0x11, 0x10, 0x25
WM_INPUTLANGCHANGEREQUEST = 0x0050

u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                           ctypes.c_size_t, ctypes.c_size_t]
u.SendMessageW.restype = ctypes.c_ssize_t


def ev(vk, up=False):
    i = INPUT()
    i.type = INPUT_KEYBOARD
    i.u.ki.wVk = vk
    i.u.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
    return i


def send(events):
    arr = (INPUT * len(events))(*events)
    return u.SendInput(len(events), arr, ctypes.sizeof(INPUT))


def fg_hkl():
    hwnd = u.GetForegroundWindow()
    tid = u.GetWindowThreadProcessId(hwnd, None)
    return hwnd, tid, u.GetKeyboardLayout(tid)


def win_title(hwnd):
    n = u.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def restore(hwnd, hkl):
    u.PostMessageW(hwnd, WM_INPUTLANGCHANGEREQUEST, 0, hkl)
    time.sleep(0.35)


def run_case(tag, delayed):
    hwnd0, _, hkl0 = fg_hkl()
    if delayed:
        gap = 0.035
        for e in ([ev(VK_CONTROL)], [ev(VK_SHIFT)], [ev(VK_LEFT)],
                  [ev(VK_LEFT, True)], [ev(VK_SHIFT, True)], [ev(VK_CONTROL, True)]):
            send(e)
            time.sleep(gap)
    else:
        send([ev(VK_CONTROL), ev(VK_SHIFT), ev(VK_LEFT),
              ev(VK_LEFT, True), ev(VK_SHIFT, True), ev(VK_CONTROL, True)])
    time.sleep(0.4)
    _, _, hkl1 = fg_hkl()
    switched = hkl1 != hkl0
    print("%-34s HKL 0x%08X -> 0x%08X   %s" % (tag, hkl0, hkl1,
                                               "换了输入法!" if switched else "没变"))
    if switched:
        restore(hwnd0, hkl0)
        _, _, hkl2 = fg_hkl()
        print("%-34s 恢复后 0x%08X %s" % ("", hkl2, "OK" if hkl2 == hkl0 else "(还没恢复)"))
    return switched


def main():
    hwnd, tid, hkl = fg_hkl()
    print("前台窗口: %r  (thread %d)  HKL=0x%08X" % (win_title(hwnd), tid, hkl))
    print()
    a = run_case("A) 一次塞进去（当前 dll）", delayed=False)
    b = run_case("B) 分步 + 35ms 间隔", delayed=True)
    print()
    print("RESULT batch=%s delayed=%s" % (a, b))
    if a and b:
        print("=> 系统级热键在起作用，分步注入也绕不开")
    elif a and not b:
        print("=> 分步注入能绕开，改 dll 即可")
    else:
        print("=> 两种都没触发（可能前台程序自己吃掉了这个组合）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
