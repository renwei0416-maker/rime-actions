/*
 * rime-actions / 上屏动作 —— Windows 专用 Lua C 模块（x64）。
 *
 * 对外提供十二个函数：
 *   press_backspace(count)             删除 count 个字符（连续退格）
 *   press_keys(spec[, gap_ms])         发一串按键组合；gap_ms 为相邻按键事件之间的间隔
 *   open_url(url)                      用默认浏览器打开网址（仅 http/https）
 *   send_text(utf8)                    把文本逐字打到光标处（不经过剪贴板）
 *   clipboard_get()                    同步读剪贴板里的文本
 *   clipboard_request()                让后台线程去读剪贴板（按键回调里用这个）
 *   clipboard_take()                   取回 clipboard_request 的结果
 *   http_request(url, body, headers)   异步 HTTP(S) POST，立即返回序号
 *   http_state()                       查询请求状态与序号
 *   http_result()                      取回响应体或错误
 *   http_cancel()                      丢弃进行中/已完成的请求
 *   nudge(delay_ms[, vk])              延时敲一个空键（默认 F24），用于唤醒输入法
 *
 * press_keys 的 spec 语法（大小写不敏感，多余空格会被忽略）：
 *   "ctrl+z"                  发一次 Ctrl+Z
 *   "shift+home"              发一次 Shift+Home
 *   "ctrl+shift+k"            发一次 Ctrl+Shift+K
 *   "ctrl+backspace"          发一次 Ctrl+Backspace（删前一个词）
 *   "shift+home,shift+left"   逗号分隔 = 依次发多步；每步的按键全部抬起后才发下一步
 *
 * 按键节奏（gap_ms，默认 30，范围 0..500）：每发出一个按键事件后先等这么久再发
 * 下一个，组合键内部（修饰键→主键→抬起）与多步之间都按这个间隔。部分程序对
 * 瞬时到达的按键串反应不过来（只执行第一步、或修饰键像是没按住），调大该值可解决。
 *
 * 可用的键名：单字母 a-z、单数字 0-9，以及
 *   backspace tab return enter escape esc space pageup pagedown end home
 *   left up right down insert delete del capslock
 *   f1..f12 numpad0..numpad9
 *   minus equal comma period slash semicolon quote grave
 *   bracketleft bracketright backslash
 * 可用的修饰键：ctrl / control、shift、alt、win / super / cmd
 *
 * 线程模型：按键注入、HTTP 请求、文本发送都在内部后台线程完成，调用立即返回。
 * 同一时刻只允许一个同类动作执行，过密的请求直接忽略。按键注入必须异步：在输入法
 * 的按键回调里直接调用 SendInput 会让注入的按键重入输入法自身的键盘钩子，连续触发
 * 时可能破坏引擎状态（崩溃在 rime.dll 内部，0xc0000005）。
 *
 * 返回值：
 *   press_backspace —— 本次被接受的退格数；0 表示被忽略。
 *   press_keys      —— 1 已排队；2 被忽略（上一个还在发 / 距上次太近）；0 参数非法。
 *   open_url        —— 1 已排队；2 被忽略；0 参数非法（空串、过长、非 http/https）。
 *                      地址里的中文需由调用方先做 UTF-8 percent-encode。
 *   send_text       —— 1 已排队；2 上一段文字还在打；0 参数非法（空串或超过 20000 字节）。
 *   clipboard_get   —— 文本 + nil；失败时 nil + 错误说明。
 *   clipboard_request —— 1 已排队去读；2 上一次还没取走；0 起不了后台线程。
 *   clipboard_take  —— 读好了：文本 + nil；失败：nil + 错误说明；还没好：nil + nil。
 *                      取走即清空，第二次调用会回到"还没好"。
 *   http_request    —— 正数 = 本次请求序号；-1 = 上一个请求还没回来；
 *                      0 = 参数非法（URL 协议/长度、body 或 headers 超长）。
 *   http_state      —— 状态字符串（running / done / failed / idle）+ 序号。
 *   http_result     —— done 时返回 响应体 + nil；failed 时 nil + 错误说明；
 *                      还在 running 时返回 nil + nil。
 *   http_cancel     —— 1 丢弃了一个已完成的响应；0 没有可丢弃的响应。
 *   nudge           —— 1 已排队；0 参数非法。
 *
 * 编译：Lua 头文件与导入库要和目标小狼毫配套，架构（x64）与 ABI 必须一致；
 * 链接需要 user32 / shell32 / winhttp / rime。
 */

#include <lua.h>
#include <lauxlib.h>
#include <windows.h>
#include <shellapi.h>
#include <winhttp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>

#define EXPORT __declspec(dllexport)

/* 防呆上限：挡住 Lua 那边传过来的异常大数字。
 * 真正的"单次最多删多少字"由 Lua 侧的 actions/max_backspaces 决定，
 * 这里只要不小于 actions.lua 里 LIMITS.max_backspaces（当前 10000）就行。 */
#define MAX_BACKSPACES 10000

/* ---------------- 异步注入参数 ---------------- */
#define FIRE_DELAY_MS   80  /* 收到请求后先等这么久，让本次按键先处理完 */
#define BATCH_SIZE       8  /* 每次 SendInput 发几个退格 */
#define BATCH_GAP_MS     5  /* 批与批之间歇多久 */
#define MIN_INTERVAL_MS 150 /* 距上次注入不足这个时间的新请求直接丢弃 */
#define WORKER_POLL_MS   5  /* 后台线程轮询待发槽位的间隔 */
#define WORKER_EXIT_WAIT_MS 2000 /* DLL 卸载时最多等后台线程多久 */
#define STEP_GAP_MS     30  /* 默认按键间隔：相邻两个按键事件之间歇多久 */
#define MAX_KEY_GAP_MS 500  /* 按键间隔的防呆上限（毫秒） */

#define MAX_SPEC_LEN   255  /* press_keys 的 spec 最长字符数 */
#define MAX_CHORD_EVENTS 12 /* 单步组合键最多产生的事件数（4 修饰 + 键按下抬起 + 4 修饰释放）*/
#define MAX_URL_LEN   2048  /* open_url 的网址最长字符数（percent-encode 之后）*/

/* 待发槽位、忙标志、上次注入时刻：跨线程访问，用原子操作读写 */
static volatile LONG      g_pending_kind  = 0;  /* 0=空闲 1=退格 2=组合键 3=打开网址 4=读剪贴板 5=删完再打字 */
static volatile LONG      g_pending_count = 0;  /* kind==1/5 时要发几个退格 */
static char               g_pending_spec[MAX_SPEC_LEN + 1]; /* kind==2 时的按键串 */
static char               g_pending_url[MAX_URL_LEN + 1];   /* kind==3 时的网址 */
static volatile LONG      g_pending_gap   = STEP_GAP_MS;     /* kind==2 时相邻按键的间隔（毫秒） */
static char              *g_pending_text  = NULL;            /* kind==5 时要打的字（malloc，worker 负责 free） */
static volatile LONG      g_busy       = 0;
static volatile LONG      g_text_busy  = 0;   /* 一段文字正在打：同一时刻只允许一段 */
static volatile ULONGLONG g_last_start = 0;
static PVOID              g_worker     = NULL;
static volatile LONG      g_stop       = 0;   /* DLL 卸载时置 1，让后台线程自己收摊 */

/* 除 worker 之外的临时线程（HTTP / 打字 / 唤醒）也登记在这里：卸载时一起等，
 * 免得模块已经卸掉了、线程还在跑我们的代码。 */
#define MAX_EXTRA_THREADS 8
static PVOID g_extra_threads[MAX_EXTRA_THREADS];

/* 离线自测专用：用 -DACTIONS_DRY_RUN 另编一份"只记不发"的 dll 来验证
 * 异步流程与按键序列，正式 dll 不含任何测试钩子（下面这些 #ifdef 都不会编进去）。 */
#ifdef ACTIONS_DRY_RUN
static volatile LONG g_dry_run_sent   = 0;
#define DRY_RUN_MAX_EVENTS 512
static char          g_dry_run_log[DRY_RUN_MAX_EVENTS][20];
static volatile LONG g_dry_run_events = 0;
/* 打开的网址也只记录、不真开 */
static volatile LONG g_dry_run_url_opens = 0;
static char          g_dry_run_url_text[MAX_URL_LEN + 1];
/* 最后一次 press_keys 用的按键间隔，供自测断言（dry-run 不真的等） */
static volatile LONG g_dry_run_gap = STEP_GAP_MS;
#endif

/* 登记一条临时线程的句柄；槽位不够就直接关句柄（线程照跑，只是不参与卸载等待）。
 * 登记前顺手回收已经结束的线程句柄，所以短命的线程不会占满槽位。 */
static void track_thread(HANDLE th)
{
    int i;

    if (!th)
        return;

    for (i = 0; i < MAX_EXTRA_THREADS; i++) {
        PVOID cur = g_extra_threads[i];

        if (cur) {
            if (WaitForSingleObject((HANDLE)cur, 0) == WAIT_OBJECT_0
                && InterlockedCompareExchangePointer(&g_extra_threads[i], th, cur) == cur) {
                CloseHandle((HANDLE)cur);
                return;
            }
            continue;
        }
        if (InterlockedCompareExchangePointer(&g_extra_threads[i], th, NULL) == NULL)
            return;
    }
    CloseHandle(th);
}

/* 卸载时等登记过的临时线程收尾；总预算不超过 WORKER_EXIT_WAIT_MS。 */
static void shutdown_extra_threads(void)
{
    ULONGLONG deadline = GetTickCount64() + WORKER_EXIT_WAIT_MS;
    int i;

    for (i = 0; i < MAX_EXTRA_THREADS; i++) {
        HANDLE th = (HANDLE)InterlockedExchangePointer(&g_extra_threads[i], NULL);
        ULONGLONG now;
        DWORD wait;

        if (!th)
            continue;
        now = GetTickCount64();
        wait = now < deadline ? (DWORD)(deadline - now) : 0;
        WaitForSingleObject(th, wait);
        CloseHandle(th);
    }
}

/* 把一批按键发出去，返回成功发出的按键个数 */
static UINT send_batch(INPUT *events, UINT total)
{
#ifdef ACTIONS_DRY_RUN
    UINT i;
    InterlockedExchangeAdd(&g_dry_run_sent, (LONG)total);
    for (i = 0; i < total; i++) {
        LONG idx = InterlockedIncrement(&g_dry_run_events) - 1;
        if (idx >= 0 && idx < DRY_RUN_MAX_EVENTS) {
            /* 事件文本：带扫描码的写成 "2As" / "47sx"（s = 扫描码写法，x = 扩展键），
             * 只填 wVk 的（退格那条路）仍写成 "08"；后缀 "-up" 表示抬起。 */
            if (events[i].ki.dwFlags & KEYEVENTF_SCANCODE)
                sprintf(g_dry_run_log[idx], "%02X%s%s",
                        (unsigned)events[i].ki.wScan,
                        (events[i].ki.dwFlags & KEYEVENTF_EXTENDEDKEY) ? "sx" : "s",
                        (events[i].ki.dwFlags & KEYEVENTF_KEYUP) ? "-up" : "");
            else if (events[i].ki.dwFlags & KEYEVENTF_UNICODE)
                sprintf(g_dry_run_log[idx], "u%04X%s",
                        (unsigned)events[i].ki.wScan,
                        (events[i].ki.dwFlags & KEYEVENTF_KEYUP) ? "-up" : "");
            else
                sprintf(g_dry_run_log[idx], "%02X%s",
                        (unsigned)events[i].ki.wVk,
                        (events[i].ki.dwFlags & KEYEVENTF_KEYUP) ? "-up" : "");
        }
    }
    return total;
#else
    return SendInput(total, events, sizeof(INPUT));
#endif
}

/* 按指定节奏发一批按键：每发一个事件等 gap 毫秒再发下一个。
 * gap <= 0 时退回"一次塞进去"的老写法（最快，但有些程序反应不过来）。 */
static UINT send_events_spaced(INPUT *events, UINT total, int gap)
{
    if (gap <= 0 || total == 0)
        return send_batch(events, total);

#ifdef ACTIONS_DRY_RUN
    /* 自测版只记录事件、不真的等，否则一次触发要跑好几秒 */
    return send_batch(events, total);
#else
    UINT i;
    UINT sent = 0;

    for (i = 0; i < total; i++) {
        if (SendInput(1, &events[i], sizeof(INPUT)) != 1)
            break;
        sent++;
        if (i + 1 < total)
            Sleep((DWORD)gap);
    }
    return sent;
#endif
}

/* ---------------- 键名表 ---------------- */
typedef struct {
    const char *name;
    WORD        vk;
} key_name_t;

static const key_name_t g_named_keys[] = {
    {"backspace",    VK_BACK},
    {"tab",          VK_TAB},
    {"return",       VK_RETURN},
    {"enter",        VK_RETURN},
    {"escape",       VK_ESCAPE},
    {"esc",          VK_ESCAPE},
    {"space",        VK_SPACE},
    {"pageup",       VK_PRIOR},
    {"pagedown",     VK_NEXT},
    {"end",          VK_END},
    {"home",         VK_HOME},
    {"left",         VK_LEFT},
    {"up",           VK_UP},
    {"right",        VK_RIGHT},
    {"down",         VK_DOWN},
    {"insert",       VK_INSERT},
    {"delete",       VK_DELETE},
    {"del",          VK_DELETE},
    {"capslock",     VK_CAPITAL},
    {"minus",        VK_OEM_MINUS},
    {"equal",        VK_OEM_PLUS},
    {"comma",        VK_OEM_COMMA},
    {"period",       VK_OEM_PERIOD},
    {"slash",        VK_OEM_2},
    {"semicolon",    VK_OEM_1},
    {"quote",        VK_OEM_7},
    {"grave",        VK_OEM_3},
    {"bracketleft",  VK_OEM_4},
    {"bracketright", VK_OEM_6},
    {"backslash",    VK_OEM_5},
    {"numpad0",      VK_NUMPAD0},
    {"numpad1",      VK_NUMPAD1},
    {"numpad2",      VK_NUMPAD2},
    {"numpad3",      VK_NUMPAD3},
    {"numpad4",      VK_NUMPAD4},
    {"numpad5",      VK_NUMPAD5},
    {"numpad6",      VK_NUMPAD6},
    {"numpad7",      VK_NUMPAD7},
    {"numpad8",      VK_NUMPAD8},
    {"numpad9",      VK_NUMPAD9}
};

/* 忽略大小写、按长度比较（lit 必须全小写） */
static BOOL token_is(const char *tok, size_t len, const char *lit)
{
    size_t i;

    if (strlen(lit) != len)
        return FALSE;
    for (i = 0; i < len; i++) {
        if (tolower((unsigned char)tok[i]) != (int)lit[i])
            return FALSE;
    }
    return TRUE;
}

/* 把一个键名 token 翻成虚拟键码 */
static BOOL lookup_key(const char *tok, size_t len, WORD *vk)
{
    static const WORD fkeys[12] = {
        VK_F1, VK_F2, VK_F3, VK_F4, VK_F5, VK_F6,
        VK_F7, VK_F8, VK_F9, VK_F10, VK_F11, VK_F12
    };
    size_t i;

    if (len == 0)
        return FALSE;

    if (len == 1) {
        unsigned char c = (unsigned char)tok[0];
        if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')) {
            *vk = (WORD)toupper(c);
            return TRUE;
        }
        if (c >= '0' && c <= '9') {
            *vk = (WORD)c;
            return TRUE;
        }
        return FALSE;
    }

    /* f1..f12 */
    if ((tok[0] == 'f' || tok[0] == 'F') && len <= 3 && isdigit((unsigned char)tok[1])) {
        int num = atoi(tok + 1);
        if (num >= 1 && num <= 12) {
            *vk = fkeys[num - 1];
            return TRUE;
        }
        return FALSE;
    }

    for (i = 0; i < sizeof(g_named_keys) / sizeof(g_named_keys[0]); i++) {
        if (token_is(tok, len, g_named_keys[i].name)) {
            *vk = g_named_keys[i].vk;
            return TRUE;
        }
    }
    return FALSE;
}

/* 往修饰键数组里放一个修饰键（去重、最多 4 个） */
static void add_modifier(WORD *mods, size_t *count, WORD vk)
{
    size_t k;

    for (k = 0; k < *count; k++) {
        if (mods[k] == vk)
            return;
    }
    if (*count < 4)
        mods[(*count)++] = vk;
}

#ifndef MAPVK_VK_TO_VSC_EX
#define MAPVK_VK_TO_VSC_EX 4
#endif

/* 哪些键的"真键盘"扫描码带 0xE0 前缀（扩展键）。MapVirtualKeyW 在本机只返回
 * 单字节扫描码（Home=0x47、Left=0x4B…），而 0x47/0x4B 同时也是小键盘 7/4 的
 * 扫描码 —— 不带 KEYEVENTF_EXTENDEDKEY 发出去就可能被当成小键盘数字，
 * 所以这里用显式清单补上扩展位。 */
static BOOL vk_is_extended(WORD vk)
{
    switch (vk) {
    case VK_PRIOR: case VK_NEXT: case VK_END: case VK_HOME:
    case VK_LEFT:  case VK_UP:   case VK_RIGHT: case VK_DOWN:
    case VK_INSERT: case VK_DELETE:
    case VK_LWIN: case VK_RWIN: case VK_APPS:
    case VK_DIVIDE: case VK_RCONTROL: case VK_RMENU:
        return TRUE;
    default:
        return FALSE;
    }
}

/* 填一个按键事件。**必须用带扫描码的写法**：只填 wVk 的合成按键会被底层
 * 键盘钩子区别对待，输入法（小狼毫）会把组合键里的 Shift / Ctrl 当成
 * "单独的修饰键"吃掉（还会顺手切中英），应用只收到后面那个方向键 ——
 * 表现就是"光标跳过去了、但没有选中"（ss; 只跳行首、ww; 变成切输入法都
 * 是这个原因）。带上扫描码之后按键与真键盘无异，实测 shift+left 选中 1 个
 * 字符、shift+home 选中整行、ctrl+shift+left 选中前一个词（见 BUILD.md
 * "带扫描码注入"一节）。取不到扫描码时退回只填 wVk（老行为）。 */
static void fill_key(INPUT *ev, WORD vk, BOOL up)
{
    UINT sc = MapVirtualKeyW(vk, MAPVK_VK_TO_VSC_EX); /* 低字节=扫描码，高字节 0xE0/0xE1=扩展键 */

    ZeroMemory(ev, sizeof(*ev));
    ev->type = INPUT_KEYBOARD;
    ev->ki.dwFlags = up ? KEYEVENTF_KEYUP : 0;

    if ((sc & 0xFF) != 0) {
        ev->ki.wVk    = 0;              /* 有 KEYEVENTF_SCANCODE 时系统只看扫描码 */
        ev->ki.wScan  = (WORD)(sc & 0xFF);
        ev->ki.dwFlags |= KEYEVENTF_SCANCODE;
        if (((sc >> 8) & 0xFF) != 0 || vk_is_extended(vk))
            ev->ki.dwFlags |= KEYEVENTF_EXTENDEDKEY;
    } else {
        ev->ki.wVk = vk;
    }
}

/* 解析一步组合键（如 "ctrl+shift+k"），填出要发的 INPUT 序列。
 * 返回事件个数；0 表示解析失败（调用方应停手）。 */
static UINT build_chord(const char *spec, size_t len, INPUT *out)
{
    WORD  mod_vks[4];
    size_t mod_count = 0;
    WORD  main_vk = 0;
    BOOL  have_main = FALSE;
    size_t i = 0;

    while (i < len) {
        size_t start, tlen;
        const char *tok;

        while (i < len && (spec[i] == ' ' || spec[i] == '\t'))
            i++;
        start = i;
        while (i < len && spec[i] != '+')
            i++;
        tlen = i - start;
        tok = spec + start;
        while (tlen > 0 && (tok[tlen - 1] == ' ' || tok[tlen - 1] == '\t'))
            tlen--;

        if (tlen > 0) {
            if (token_is(tok, tlen, "ctrl") || token_is(tok, tlen, "control")) {
                add_modifier(mod_vks, &mod_count, VK_CONTROL);
            } else if (token_is(tok, tlen, "shift")) {
                add_modifier(mod_vks, &mod_count, VK_SHIFT);
            } else if (token_is(tok, tlen, "alt")) {
                add_modifier(mod_vks, &mod_count, VK_MENU);
            } else if (token_is(tok, tlen, "win") || token_is(tok, tlen, "super")
                       || token_is(tok, tlen, "cmd")) {
                add_modifier(mod_vks, &mod_count, VK_LWIN);
            } else {
                WORD vk;
                if (have_main || !lookup_key(tok, tlen, &vk))
                    return 0;
                main_vk = vk;
                have_main = TRUE;
            }
        }

        if (i < len && spec[i] == '+')
            i++;
    }

    if (!have_main) {
        /* 只写了修饰键：当成单独按一下它 */
        if (mod_count == 0)
            return 0;
        main_vk = mod_vks[0];
        mod_count = 0;
    }

    {
        UINT n = 0;
        size_t k;

        for (k = 0; k < mod_count; k++)
            fill_key(&out[n++], mod_vks[k], FALSE);
        fill_key(&out[n++], main_vk, FALSE);
        fill_key(&out[n++], main_vk, TRUE);
        for (k = mod_count; k > 0; k--)
            fill_key(&out[n++], mod_vks[k - 1], TRUE);
        return n;
    }
}

/* 只解析、不发送：判断 spec 是否合法（press_keys 用它提前拒绝非法串，
 * 免得调用方以为发出去了、其实什么都没发生）。 */
static BOOL spec_is_valid(const char *spec)
{
    const char *p = spec;

    while (*p) {
        const char *comma = strchr(p, ',');
        size_t len = comma ? (size_t)(comma - p) : strlen(p);
        INPUT scratch[MAX_CHORD_EVENTS];

        ZeroMemory(scratch, sizeof(scratch));
        if (len == 0 || build_chord(p, len, scratch) == 0)
            return FALSE;
        if (!comma)
            break;
        p = comma + 1;
    }
    return TRUE;
}

/* 发一串按键（逗号分隔多步），返回成功发出的步数。
 * gap = 相邻按键事件之间的间隔（毫秒） */
static int inject_keys(const char *spec, int gap)
{
    const char *p = spec;
    int steps = 0;

    while (*p) {
        const char *comma = strchr(p, ',');
        size_t len = comma ? (size_t)(comma - p) : strlen(p);
        INPUT ev[MAX_CHORD_EVENTS];
        UINT n;

        ZeroMemory(ev, sizeof(ev));
        n = len ? build_chord(p, len, ev) : 0;
        if (n == 0)
            break;  /* 解析失败就停手，剩下的不发 */
        if (send_events_spaced(ev, n, gap) != n)
            break;

        steps++;
        if (!comma)
            break;
        /* 步与步之间也按同一个节奏停一下 */
        if (gap > 0)
            Sleep((DWORD)gap);
        p = comma + 1;
    }
    return steps;
}

/* 按批次发 count 个退格（只在后台线程里调用，绝不在 Lua 回调里发） */
static int inject_backspaces(int count)
{
    INPUT batch[BATCH_SIZE * 2];
    int done = 0;

    while (done < count) {
        int n = count - done;
        int i;
        UINT total;

        if (n > BATCH_SIZE)
            n = BATCH_SIZE;

        ZeroMemory(batch, sizeof(INPUT) * (size_t)n * 2);
        for (i = 0; i < n; i++) {
            /* 退格键按下 */
            batch[i * 2].type = INPUT_KEYBOARD;
            batch[i * 2].ki.wVk = VK_BACK;
            /* 退格键抬起 */
            batch[i * 2 + 1].type = INPUT_KEYBOARD;
            batch[i * 2 + 1].ki.wVk = VK_BACK;
            batch[i * 2 + 1].ki.dwFlags = KEYEVENTF_KEYUP;
        }

        total = (UINT)n * 2;
        if (send_batch(batch, total) != total)
            break;  /* 中途失败就停手 */

        done += n;
        if (done < count)
            Sleep(BATCH_GAP_MS);
    }
    return done;
}

/* 用默认浏览器打开一个网址（只在后台线程里调用）。
 * 网址必须是 http/https、且中文已由调用方 percent-encode；这里再转成宽字符
 * 交给 ShellExecuteW —— 走的是系统"打开"关联，不经过 cmd.exe，所以不闪黑框。 */
static void open_url_now(const char *url)
{
#ifdef ACTIONS_DRY_RUN
    size_t n = strlen(url);

    InterlockedIncrement(&g_dry_run_url_opens);
    if (n > MAX_URL_LEN)
        n = MAX_URL_LEN;
    memcpy(g_dry_run_url_text, url, n);
    g_dry_run_url_text[n] = '\0';
#else
    WCHAR *wide;
    int need = MultiByteToWideChar(CP_UTF8, 0, url, -1, NULL, 0);

    if (need <= 0)
        return;

    wide = (WCHAR *)malloc(sizeof(WCHAR) * (size_t)need);
    if (!wide)
        return;

    if (MultiByteToWideChar(CP_UTF8, 0, url, -1, wide, need) > 0)
        ShellExecuteW(NULL, L"open", wide, NULL, NULL, SW_SHOWNORMAL);

    free(wide);
#endif
}

/* 后台线程：只在这里发按键 */
static void clip_read_now(void); /* 定义在"剪贴板"一节 */
static void type_text_now(const char *text); /* 定义在"直接插入文本"一节 */

static DWORD WINAPI worker_main(LPVOID param)
{
    (void)param;

    for (;;) {
        LONG kind;

        if (g_stop)
            break;

        kind = InterlockedExchange(&g_pending_kind, 0);

        if (kind > 0) {
            LONG count = InterlockedExchange(&g_pending_count, 0);
            Sleep(FIRE_DELAY_MS);
            if (kind == 1)
                inject_backspaces((int)count);
            else if (kind == 2)
                inject_keys(g_pending_spec, (int)InterlockedExchange(&g_pending_gap, STEP_GAP_MS));
            else if (kind == 3)
                open_url_now(g_pending_url);
            else if (kind == 4)
                clip_read_now();
            else if (kind == 5) {
                /* 先删掉前面 count 个字符，再把这串文字打上去：两步在同一条线程里
                 * 顺序执行，所以不会出现"一边删一边打"的错位。 */
                char *text = (char *)InterlockedExchangePointer(
                                 (PVOID volatile *)&g_pending_text, NULL);
                int want = (int)count;
                int done = 0;
                int tries;

                for (tries = 0; tries < 3 && done < want; tries++) {
                    done += inject_backspaces(want - done);
                    if (done < want)
                        Sleep(30);
                }
                if (text) {
                    /* 删除没走完就不打字：宁可少一段，也不把字打到错的位置 */
                    if (done >= want) {
                        Sleep(BATCH_GAP_MS * 2);
                        type_text_now(text);
                    }
                    free(text);
                }
                InterlockedExchange(&g_text_busy, 0);
            }
            InterlockedExchange(&g_busy, 0);
        } else {
            Sleep(WORKER_POLL_MS);
        }
    }
    return 0;
}

/* 首次使用时才创建后台线程，返回是否可用。
 * 线程先以挂起状态建出来，抢占成功才让它跑；抢占失败就直接终结掉这条还没跑过的线程，
 * 避免出现两条同时轮询的 worker。 */
static BOOL ensure_worker(void)
{
    HANDLE thread;

    if (g_worker)
        return TRUE;

    thread = CreateThread(NULL, 0, worker_main, NULL, CREATE_SUSPENDED, NULL);
    if (!thread)
        return FALSE;

    if (InterlockedCompareExchangePointer((PVOID volatile *)&g_worker, thread, NULL) != NULL) {
        /* 另一个线程先建好了：这条还没跑过，直接终结 + 关句柄 */
        TerminateThread(thread, 0);
        CloseHandle(thread);
        return TRUE;
    }

    if (ResumeThread(thread) == (DWORD)-1) {
        InterlockedCompareExchangePointer((PVOID volatile *)&g_worker, NULL, thread);
        CloseHandle(thread);
        return FALSE;
    }
    return TRUE;
}

/* 抢占"待发槽位"。成功返回 TRUE（调用方随后必须填好槽位并发布 kind），
 * 失败（上一个请求还在发、或距上次注入太近）返回 FALSE。 */
static BOOL claim_slot(void)
{
    ULONGLONG now;

    if (InterlockedCompareExchange(&g_busy, 1, 0) != 0)
        return FALSE;

    now = GetTickCount64();
    if (g_last_start != 0 && now - g_last_start < MIN_INTERVAL_MS) {
        InterlockedExchange(&g_busy, 0);
        return FALSE;
    }

    g_last_start = now;
    return TRUE;
}

static int l_press_backspace(lua_State *L)
{
    lua_Integer count = luaL_optinteger(L, 1, 1);

    /* 参数不合法：不动作 */
    if (count <= 0) {
        lua_pushinteger(L, 0);
        return 1;
    }
    if (count > MAX_BACKSPACES)
        count = MAX_BACKSPACES;

    if (!claim_slot()) {
        lua_pushinteger(L, 0);
        return 1;
    }

    if (!ensure_worker()) {
        /* 线程建不出来：明确失败，由 Lua 记日志并保留账本。
         * 绝不能在本线程直接注入——这里正处在输入法的按键回调里，
         * 同步注入会重新进入处理链。 */
        InterlockedExchange(&g_busy, 0);
        lua_pushinteger(L, 0);
        return 1;
    }

    InterlockedExchange(&g_pending_count, (LONG)count);
    InterlockedExchange(&g_pending_kind, 1);
    lua_pushinteger(L, count);
    return 1;
}

static int l_press_keys(lua_State *L)
{
    size_t len = 0;
    const char *spec = luaL_checklstring(L, 1, &len);
    lua_Integer gap_arg = luaL_optinteger(L, 2, STEP_GAP_MS);
    int gap;

    if (len == 0 || len > MAX_SPEC_LEN) {
        lua_pushinteger(L, 0);
        return 1;
    }

    if (!spec_is_valid(spec)) {
        lua_pushinteger(L, 0);
        return 1;
    }

    /* 间隔防呆：负数当 0，太大截到 MAX_KEY_GAP_MS */
    if (gap_arg < 0)
        gap_arg = 0;
    if (gap_arg > MAX_KEY_GAP_MS)
        gap_arg = MAX_KEY_GAP_MS;
    gap = (int)gap_arg;

#ifdef ACTIONS_DRY_RUN
    /* 自测用：无论这次是否被忽略，都记下解析出来的间隔 */
    InterlockedExchange(&g_dry_run_gap, (LONG)gap);
#endif

    if (!claim_slot()) {
        /* 2 = 被忽略：上一个请求还在发，或距上次注入太近（不是参数问题） */
        lua_pushinteger(L, 2);
        return 1;
    }

    if (!ensure_worker()) {
        /* 建不出线程就明确失败，不在按键回调里同步发按键 */
        InterlockedExchange(&g_busy, 0);
        lua_pushinteger(L, 0);
        return 1;
    }

    /* 先填槽位再发布 kind：后台线程只在 kind 非 0 时才读它 */
    memcpy(g_pending_spec, spec, len);
    g_pending_spec[len] = '\0';
    InterlockedExchange(&g_pending_gap, (LONG)gap);
    InterlockedExchange(&g_pending_count, 1);
    InterlockedExchange(&g_pending_kind, 2);
    lua_pushinteger(L, 1);
    return 1;
}

/* ==================================================================
 * 删除 + 打字（一个任务）
 *   press_backspace_then_text(count, text) -> 1=已安排 2=忙 0=参数或线程问题
 *
 * 先在同一个后台线程里发 count 个退格，再把这串文字打上去。两步串行执行，
 * 所以不管删除要多久，文字都不会插到"还没删完"的位置。
 * ================================================================== */
static int l_press_backspace_then_text(lua_State *L)
{
    lua_Integer count = luaL_optinteger(L, 1, 0);
    size_t len = 0;
    const char *text = luaL_checklstring(L, 2, &len);
    char *copy;

    if (count < 0 || len == 0 || len > 20000) {
        lua_pushinteger(L, 0);
        return 1;
    }
    if (count > MAX_BACKSPACES)
        count = MAX_BACKSPACES;

    /* 一段文字同时只能打一段：两段交叉会把字打乱 */
    if (InterlockedCompareExchange(&g_text_busy, 1, 0) != 0) {
        lua_pushinteger(L, 2);
        return 1;
    }

    if (!claim_slot()) {
        InterlockedExchange(&g_text_busy, 0);
        lua_pushinteger(L, 2);
        return 1;
    }

    if (!ensure_worker()) {
        InterlockedExchange(&g_text_busy, 0);
        InterlockedExchange(&g_busy, 0);
        lua_pushinteger(L, 0);
        return 1;
    }

    copy = (char *)malloc(len + 1);
    if (!copy) {
        InterlockedExchange(&g_text_busy, 0);
        InterlockedExchange(&g_busy, 0);
        lua_pushinteger(L, 0);
        return 1;
    }
    memcpy(copy, text, len);
    copy[len] = '\0';

    /* 先填槽位再发布 kind */
    InterlockedExchangePointer((PVOID volatile *)&g_pending_text, copy);
    InterlockedExchange(&g_pending_count, (LONG)count);
    InterlockedExchange(&g_pending_kind, 5);
    lua_pushinteger(L, 1);
    return 1;
}

/* 只放行 http:// 和 https://：别让这条路被用来启动本地程序或打开文件 */
static BOOL url_is_valid(const char *url)
{
    size_t i;

    if (strncmp(url, "http://", 7) != 0 && strncmp(url, "https://", 8) != 0)
        return FALSE;

    for (i = 0; url[i]; i++) {
        unsigned char c = (unsigned char)url[i];
        /* 空白、控制字符、以及会被 cmd/shell 当成元字符的几个一律拒绝 */
        if (c <= 0x20 || c == 0x7F || c == '"' || c == '<' || c == '>' || c == '|')
            return FALSE;
    }
    return TRUE;
}

static int l_open_url(lua_State *L)
{
    size_t len = 0;
    const char *url = luaL_checklstring(L, 1, &len);

    if (len == 0 || len > MAX_URL_LEN) {
        lua_pushinteger(L, 0);
        return 1;
    }

    if (!url_is_valid(url)) {
        lua_pushinteger(L, 0);
        return 1;
    }

    if (!claim_slot()) {
        /* 2 = 被忽略：上一个请求还在跑，或距上次太近 */
        lua_pushinteger(L, 2);
        return 1;
    }

    if (!ensure_worker()) {
        /* 线程建不出来时退回同步打开：这里只是开个浏览器，没有重入风险 */
        open_url_now(url);
        InterlockedExchange(&g_busy, 0);
        lua_pushinteger(L, 1);
        return 1;
    }

    memcpy(g_pending_url, url, len);
    g_pending_url[len] = '\0';
    InterlockedExchange(&g_pending_count, 1);
    InterlockedExchange(&g_pending_kind, 3);
    lua_pushinteger(L, 1);
    return 1;
}

#ifdef ACTIONS_DRY_RUN
/* 只有自测 dll 才有：返回"本来会发出去的按键总数" */static int l_dry_run_sent(lua_State *L)
{
    lua_pushinteger(L, (lua_Integer)g_dry_run_sent);
    return 1;
}

/* 只有自测 dll 才有：清零计数与事件记录 */
static int l_dry_run_reset(lua_State *L)
{
    (void)L;
    InterlockedExchange(&g_dry_run_sent, 0);
    InterlockedExchange(&g_dry_run_events, 0);
    InterlockedExchange(&g_dry_run_url_opens, 0);
    g_dry_run_url_text[0] = '\0';
    InterlockedExchange(&g_dry_run_gap, STEP_GAP_MS);
    return 0;
}

/* 只有自测 dll 才有：返回事件记录数组，每项形如
 * "2As"（按下，扫描码写法）/ "47sx-up"（扩展键抬起）/ "08"（只填 wVk 的退格） */
static int l_dry_run_events(lua_State *L)
{
    LONG n = g_dry_run_events;
    LONG i;

    if (n > DRY_RUN_MAX_EVENTS)
        n = DRY_RUN_MAX_EVENTS;
    if (n < 0)
        n = 0;

    lua_createtable(L, (int)n, 0);
    for (i = 0; i < n; i++) {
        lua_pushstring(L, g_dry_run_log[i]);
        lua_rawseti(L, -2, (lua_Integer)i + 1);
    }
    return 1;
}

/* 只有自测 dll 才有：返回"本来会打开的网址"打开了几次 */
static int l_dry_run_url_opens(lua_State *L)
{
    lua_pushinteger(L, (lua_Integer)g_dry_run_url_opens);
    return 1;
}

/* 只有自测 dll 才有：返回最后一次"本来会打开的网址" */
static int l_dry_run_url(lua_State *L)
{
    lua_pushstring(L, g_dry_run_url_text);
    return 1;
}
/* 只有自测 dll 才有：返回最后一次 press_keys 用的按键间隔（毫秒） */
static int l_dry_run_gap(lua_State *L)
{
    lua_pushinteger(L, (lua_Integer)g_dry_run_gap);
    return 1;
}
#endif

/* ==================================================================
 * 异步 HTTP（给 AI 助手用）
 *
 * 和 press_keys 同一个原则：绝不在按键回调里做网络。
 *   http_request(url, body, headers) —— 把参数丢进槽位、起一个后台线程，立刻返回序号
 *   http_state()                    —— "idle" / "running" / "done" / "failed"（外加序号）
 *   http_result()                   —— 取结果（取完自动回到 idle）
 *   http_cancel()                   —— 丢弃当前请求的结果
 *
 * 为什么不用 curl / python / powershell：WinHTTP 是系统自带的（winhttp.dll），
 * 走系统代理和证书，不需要任何外部程序。
 * ================================================================== */
#define HTTP_MAX_URL   2048
#define HTTP_MAX_HDRS  4096
#define HTTP_MAX_BODY  (256 * 1024)
#define HTTP_MAX_RESP  (4 * 1024 * 1024)

#define HTTP_CONNECT_TIMEOUT_MS  4000
#define HTTP_SEND_TIMEOUT_MS    15000
#define HTTP_RECV_TIMEOUT_MS    60000

#define HTTP_IDLE     0
#define HTTP_RUNNING  1
#define HTTP_DONE     2
#define HTTP_FAILED   3

static volatile LONG g_http_state = HTTP_IDLE;
static volatile LONG g_http_seq   = 0;
/* 请求代号：每次发起或取消都加一。后台线程跑完时若代号已经变了，
 * 说明这次的结果没人要了（被取消或被新请求取代），直接丢掉。 */
static volatile LONG g_http_gen   = 0;
static char          *g_http_result = NULL;   /* DONE 时有效，malloc 的 */
static size_t         g_http_result_len = 0;
static char           g_http_error[512];
static CRITICAL_SECTION g_http_lock;
static INIT_ONCE      g_http_once     = INIT_ONCE_STATIC_INIT;
static volatile LONG  g_http_lock_ready = 0;   /* 只在初始化回调里置 1，供卸载时判断 */

static BOOL CALLBACK http_lock_init_once(PINIT_ONCE once, PVOID param, PVOID *ctx)
{
    (void)once;
    (void)param;
    (void)ctx;
    InitializeCriticalSection(&g_http_lock);
    InterlockedExchange(&g_http_lock_ready, 1);
    return TRUE;
}

/* 真正的一次性初始化：InitOnceExecuteOnce 保证临界区先建好，
 * 别的线程才可能看到"已完成"，不会拿到没初始化的锁。 */
static void http_lock_init(void)
{
    InitOnceExecuteOnce(&g_http_once, http_lock_init_once, NULL, NULL);
}

/* UTF-8 → 宽字符（malloc 的新串，调用方 free） */
static WCHAR *http_utf8_to_wide(const char *s)
{
    int need;
    WCHAR *w;

    if (!s)
        return NULL;
    need = MultiByteToWideChar(CP_UTF8, 0, s, -1, NULL, 0);
    if (need <= 0)
        return NULL;
    w = (WCHAR *)malloc(sizeof(WCHAR) * (size_t)need);
    if (!w)
        return NULL;
    if (MultiByteToWideChar(CP_UTF8, 0, s, -1, w, need) <= 0) {
        free(w);
        return NULL;
    }
    return w;
}

typedef struct {
    char *url;
    char *body;
    DWORD body_len;
    char *headers;
    LONG  seq;
    LONG  gen;
} http_job;

/* 真正发请求（只在后台线程里跑，阻塞无所谓） */
static void http_do_request(http_job *job, char **out_body, size_t *out_len, char *err, size_t errcap)
{
    WCHAR *wurl = NULL, *whost = NULL, *wpath = NULL, *whdrs = NULL;
    URL_COMPONENTS uc;
    HINTERNET session = NULL, connect = NULL, request = NULL;
    DWORD status = 0, status_len = sizeof(status);
    char *buf = NULL;
    size_t used = 0, cap = 0;
    int secure;
    WCHAR *path_ptr;

    *out_body = NULL;
    *out_len = 0;
    err[0] = '\0';

    wurl = http_utf8_to_wide(job->url);
    if (!wurl) { snprintf(err, errcap, "网址无法转成宽字符"); goto done; }

    ZeroMemory(&uc, sizeof(uc));
    uc.dwStructSize = sizeof(uc);
    uc.dwSchemeLength = (DWORD)-1;
    uc.dwHostNameLength = (DWORD)-1;
    uc.dwUrlPathLength = (DWORD)-1;
    uc.dwExtraInfoLength = (DWORD)-1;
    if (!WinHttpCrackUrl(wurl, 0, 0, &uc)) {
        snprintf(err, errcap, "网址解析失败（要 http:// 或 https:// 开头），GetLastError=%lu", GetLastError());
        goto done;
    }
    secure = (uc.nScheme == INTERNET_SCHEME_HTTPS);

    whost = (WCHAR *)malloc(sizeof(WCHAR) * ((size_t)uc.dwHostNameLength + 1));
    if (!whost) { snprintf(err, errcap, "内存不足（host）"); goto done; }
    memcpy(whost, uc.lpszHostName, sizeof(WCHAR) * uc.dwHostNameLength);
    whost[uc.dwHostNameLength] = L'\0';

    {
        size_t plen = (size_t)uc.dwUrlPathLength + (size_t)uc.dwExtraInfoLength;
        if (plen == 0) {
            wpath = (WCHAR *)malloc(sizeof(WCHAR) * 2);
            if (!wpath) { snprintf(err, errcap, "内存不足（path）"); goto done; }
            wpath[0] = L'/';
            wpath[1] = L'\0';
        } else {
            wpath = (WCHAR *)malloc(sizeof(WCHAR) * (plen + 1));
            if (!wpath) { snprintf(err, errcap, "内存不足（path）"); goto done; }
            path_ptr = wpath;
            memcpy(path_ptr, uc.lpszUrlPath, sizeof(WCHAR) * uc.dwUrlPathLength);
            path_ptr += uc.dwUrlPathLength;
            if (uc.dwExtraInfoLength > 0)
                memcpy(path_ptr, uc.lpszExtraInfo, sizeof(WCHAR) * uc.dwExtraInfoLength);
            wpath[plen] = L'\0';
        }
    }

#ifdef WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY
    session = WinHttpOpen(L"rime-ai-assist/1.0", WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY,
                          WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    if (!session)
#endif
    {
        session = WinHttpOpen(L"rime-ai-assist/1.0", WINHTTP_ACCESS_TYPE_DEFAULT_PROXY,
                              WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    }
    if (!session) { snprintf(err, errcap, "WinHttpOpen 失败，GetLastError=%lu", GetLastError()); goto done; }

    WinHttpSetTimeouts(session, HTTP_CONNECT_TIMEOUT_MS, HTTP_SEND_TIMEOUT_MS,
                       HTTP_RECV_TIMEOUT_MS, HTTP_RECV_TIMEOUT_MS);

    connect = WinHttpConnect(session, whost, (INTERNET_PORT)uc.nPort, 0);
    if (!connect) { snprintf(err, errcap, "WinHttpConnect 失败，GetLastError=%lu", GetLastError()); goto done; }

    request = WinHttpOpenRequest(connect, L"POST", wpath, NULL, WINHTTP_NO_REFERER,
                                 WINHTTP_DEFAULT_ACCEPT_TYPES,
                                 secure ? WINHTTP_FLAG_SECURE : 0);
    if (!request) { snprintf(err, errcap, "WinHttpOpenRequest 失败，GetLastError=%lu", GetLastError()); goto done; }

    if (job->headers && job->headers[0]) {
        whdrs = http_utf8_to_wide(job->headers);
        if (whdrs) {
            WinHttpAddRequestHeaders(request, whdrs, (DWORD)-1L,
                                     WINHTTP_ADDREQ_FLAG_ADD | WINHTTP_ADDREQ_FLAG_REPLACE);
        }
    }

    if (!WinHttpSendRequest(request, WINHTTP_NO_ADDITIONAL_HEADERS, 0,
                            (LPVOID)job->body, job->body_len, job->body_len, 0)) {
        snprintf(err, errcap, "发送失败（连不上或 TLS 握手失败），GetLastError=%lu", GetLastError());
        goto done;
    }
    if (!WinHttpReceiveResponse(request, NULL)) {
        snprintf(err, errcap, "收响应失败，GetLastError=%lu", GetLastError());
        goto done;
    }

    if (!WinHttpQueryHeaders(request, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                             WINHTTP_HEADER_NAME_BY_INDEX, &status, &status_len,
                             WINHTTP_NO_HEADER_INDEX)) {
        snprintf(err, errcap, "读状态码失败，GetLastError=%lu", GetLastError());
        goto done;
    }

    for (;;) {
        DWORD avail = 0;
        DWORD got = 0;

        if (!WinHttpQueryDataAvailable(request, &avail)) {
            snprintf(err, errcap, "读取响应失败，GetLastError=%lu", GetLastError());
            goto done;
        }
        if (avail == 0)
            break;
        if (used + avail + 1 > HTTP_MAX_RESP) {
            snprintf(err, errcap, "响应太大（超过 %d 字节）", HTTP_MAX_RESP);
            goto done;
        }
        if (used + avail + 1 > cap) {
            size_t ncap = cap ? cap : 4096;
            char *nb;
            while (ncap < used + avail + 1)
                ncap *= 2;
            if (ncap > HTTP_MAX_RESP)
                ncap = HTTP_MAX_RESP;
            nb = (char *)realloc(buf, ncap);
            if (!nb) { snprintf(err, errcap, "内存不足（响应）"); goto done; }
            buf = nb;
            cap = ncap;
        }
        if (!WinHttpReadData(request, buf + used, avail, &got)) {
            snprintf(err, errcap, "读数据失败，GetLastError=%lu", GetLastError());
            goto done;
        }
        used += got;
    }

    if (used + 1 > cap) {
        char *nb = (char *)realloc(buf, used + 1);
        if (!nb) { snprintf(err, errcap, "内存不足（响应）"); goto done; }
        buf = nb;
        cap = used + 1;
    }
    buf[used] = '\0';

    if (status >= 400) {
        /* 把服务端的错误正文一起带回去，方便 Lua 侧显示 */
        snprintf(err, errcap, "HTTP %lu: %.400s", (unsigned long)status, buf ? buf : "");
        goto done;
    }

    *out_body = buf;
    *out_len = used;
    buf = NULL;

done:
    if (buf) free(buf);
    if (whdrs) free(whdrs);
    if (request) WinHttpCloseHandle(request);
    if (connect) WinHttpCloseHandle(connect);
    if (session) WinHttpCloseHandle(session);
    if (wpath) free(wpath);
    if (whost) free(whost);
    if (wurl) free(wurl);
}

static DWORD WINAPI http_thread_main(LPVOID param)
{
    http_job *job = (http_job *)param;
    char *body = NULL;
    size_t len = 0;
    char err[512];

    http_do_request(job, &body, &len, err, sizeof(err));

    EnterCriticalSection(&g_http_lock);
    if (job->gen == g_http_gen) {
        if (body) {
            if (g_http_result)
                free(g_http_result);
            g_http_result = body;
            g_http_result_len = len;
            g_http_error[0] = '\0';
            InterlockedExchange(&g_http_state, HTTP_DONE);
        } else {
            snprintf(g_http_error, sizeof(g_http_error), "%s", err[0] ? err : "未知错误");
            InterlockedExchange(&g_http_state, HTTP_FAILED);
        }
    } else {
        /* 已被取消或被新请求取代：结果没人要，释放掉，状态由取消方负责 */
        free(body);
        body = NULL;
    }
    LeaveCriticalSection(&g_http_lock);

    free(job->url);
    free(job->body);
    free(job->headers);
    free(job);
    return 0;
}

/* http_request(url, body, headers) -> 序号(>=1) | 0=参数非法 | -1=上一个还没跑完 */
static int l_http_request(lua_State *L)
{
    const char *url = luaL_checkstring(L, 1);
    size_t body_len = 0;
    const char *body = luaL_optlstring(L, 2, "", &body_len);
    const char *headers = luaL_optstring(L, 3, "");
    http_job *job;
    HANDLE th;
    LONG seq;

    if (!url || !url[0] || strlen(url) > HTTP_MAX_URL) {
        lua_pushinteger(L, 0);
        return 1;
    }
    if (body_len > HTTP_MAX_BODY) {
        lua_pushinteger(L, 0);
        return 1;
    }
    if (strncmp(url, "http://", 7) != 0 && strncmp(url, "https://", 8) != 0) {
        lua_pushinteger(L, 0);
        return 1;
    }
    if (strlen(headers) > HTTP_MAX_HDRS) {
        lua_pushinteger(L, 0);
        return 1;
    }

    http_lock_init();

    /* 上一轮的结果如果没人取过（Esc 取消后线程才跑完、或取结果那步被跳过了），
     * 状态会停在 DONE/FAILED，下面的 CAS 就永远失败——表现是"从此再也发不出去"。
     * 这里先把陈旧结果清掉。 */
    {
        LONG stale = g_http_state;
        if (stale == HTTP_DONE || stale == HTTP_FAILED) {
            EnterCriticalSection(&g_http_lock);
            if (g_http_result) {
                free(g_http_result);
                g_http_result = NULL;
                g_http_result_len = 0;
            }
            g_http_error[0] = '\0';
            LeaveCriticalSection(&g_http_lock);
            InterlockedCompareExchange(&g_http_state, HTTP_IDLE, stale);
        }
    }

    if (InterlockedCompareExchange(&g_http_state, HTTP_RUNNING, HTTP_IDLE) != HTTP_IDLE) {
        lua_pushinteger(L, -1);
        return 1;
    }

    job = (http_job *)calloc(1, sizeof(http_job));
    if (!job) {
        InterlockedExchange(&g_http_state, HTTP_IDLE);
        lua_pushinteger(L, 0);
        return 1;
    }
    job->url = _strdup(url);
    job->body = (char *)malloc(body_len + 1);
    job->headers = _strdup(headers ? headers : "");
    if (!job->url || !job->body || !job->headers) {
        free(job->url); free(job->body); free(job->headers); free(job);
        InterlockedExchange(&g_http_state, HTTP_IDLE);
        lua_pushinteger(L, 0);
        return 1;
    }
    memcpy(job->body, body, body_len);
    job->body[body_len] = '\0';
    job->body_len = (DWORD)body_len;

    seq = InterlockedIncrement(&g_http_seq);
    job->seq = seq;
    job->gen = InterlockedIncrement(&g_http_gen);

    th = CreateThread(NULL, 0, http_thread_main, job, 0, NULL);
    if (!th) {
        free(job->url); free(job->body); free(job->headers); free(job);
        InterlockedExchange(&g_http_state, HTTP_IDLE);
        lua_pushinteger(L, 0);
        return 1;
    }
    track_thread(th);
    lua_pushinteger(L, seq);
    return 1;
}

/* http_state() -> "idle"|"running"|"done"|"failed", 当前序号 */
static int l_http_state(lua_State *L)
{
    LONG st = g_http_state;

    switch (st) {
    case HTTP_RUNNING: lua_pushliteral(L, "running"); break;
    case HTTP_DONE:    lua_pushliteral(L, "done");    break;
    case HTTP_FAILED:  lua_pushliteral(L, "failed");  break;
    default:           lua_pushliteral(L, "idle");    break;
    }
    lua_pushinteger(L, (lua_Integer)g_http_seq);
    return 2;
}

/* http_result() -> body, nil   或者   nil, 错误    取完回到 idle */
static int l_http_result(lua_State *L)
{
    LONG st = g_http_state;

    if (st == HTTP_DONE) {
        EnterCriticalSection(&g_http_lock);
        lua_pushlstring(L, g_http_result ? g_http_result : "", g_http_result_len);
        if (g_http_result) {
            free(g_http_result);
            g_http_result = NULL;
            g_http_result_len = 0;
        }
        LeaveCriticalSection(&g_http_lock);
        InterlockedExchange(&g_http_state, HTTP_IDLE);
        lua_pushnil(L);
        return 2;
    }
    if (st == HTTP_FAILED) {
        EnterCriticalSection(&g_http_lock);
        lua_pushnil(L);
        lua_pushstring(L, g_http_error);
        LeaveCriticalSection(&g_http_lock);
        InterlockedExchange(&g_http_state, HTTP_IDLE);
        return 2;
    }
    lua_pushnil(L);
    if (st == HTTP_RUNNING)
        lua_pushliteral(L, "running");
    else
        lua_pushliteral(L, "idle");
    return 2;
}

/* http_cancel() -> 1（确实丢掉了一个结果/取消了一个请求）| 0
 * 请求还在跑时也能取消：把代号加一，后台线程跑完后会自己丢弃结果，
 * 状态立刻回到 idle，所以取消之后马上就能再发。 */
static int l_http_cancel(lua_State *L)
{
    LONG st = g_http_state;
    LONG dropped = 0;

    if (st == HTTP_DONE || st == HTTP_FAILED) {
        EnterCriticalSection(&g_http_lock);
        if (g_http_result) {
            free(g_http_result);
            g_http_result = NULL;
            g_http_result_len = 0;
        }
        g_http_error[0] = '\0';
        LeaveCriticalSection(&g_http_lock);
        InterlockedIncrement(&g_http_gen);
        InterlockedExchange(&g_http_state, HTTP_IDLE);
        dropped = 1;
    } else if (st == HTTP_RUNNING) {
        InterlockedIncrement(&g_http_gen);
        InterlockedCompareExchange(&g_http_state, HTTP_IDLE, HTTP_RUNNING);
        dropped = 1;
    }
    lua_pushinteger(L, dropped);
    return 1;
}

/* ==================================================================
 * 剪贴板
 *   clipboard_request() -> 1=已排队  2=忙（上一个还在读）  0=起不了线程
 *   clipboard_take()    -> 文本, nil | nil, 错误 | nil, nil（还没好）
 *   clipboard_get()     -> 文本, nil | nil, 错误      （同步版，保留兼容）
 *
 * 按键回调里不能同步读剪贴板：别的程序占着剪贴板时 OpenClipboard 会一直等，
 * 输入法就冻住了。所以正常流程走 request/take，真正的读取在后台线程里做；
 * 而且 OpenClipboard 失败只重试有限次（约 200ms）就放弃。
 * ================================================================== */
#define CLIP_MAX_BYTES  (128 * 1024)
#define CLIP_OPEN_RETRY 20
#define CLIP_OPEN_GAP_MS 10

#define CLIP_IDLE    0
#define CLIP_PENDING 1
#define CLIP_READY   2
#define CLIP_FAILED  3

static char             g_clip_text[CLIP_MAX_BYTES + 1];
static size_t           g_clip_len = 0;
static char             g_clip_error[128];
static volatile LONG    g_clip_state = CLIP_IDLE;
static CRITICAL_SECTION g_clip_lock;
static INIT_ONCE        g_clip_once       = INIT_ONCE_STATIC_INIT;
static volatile LONG    g_clip_lock_ready = 0;   /* 只在初始化回调里置 1，供卸载时判断 */

static BOOL CALLBACK clip_lock_init_once(PINIT_ONCE once, PVOID param, PVOID *ctx)
{
    (void)once;
    (void)param;
    (void)ctx;
    InitializeCriticalSection(&g_clip_lock);
    InterlockedExchange(&g_clip_lock_ready, 1);
    return TRUE;
}

static void clip_lock_init(void)
{
    InitOnceExecuteOnce(&g_clip_once, clip_lock_init_once, NULL, NULL);
}

/* 读剪贴板：成功返回 malloc 的 UTF-8（调用方 free），失败返回 NULL 并写 err */
static char *clip_read_text(char *err, size_t errcap)
{
    HANDLE h;
    WCHAR *w;
    int need, i;
    char *u;

    for (i = 0; i < CLIP_OPEN_RETRY; ++i) {
        if (OpenClipboard(NULL))
            break;
        Sleep(CLIP_OPEN_GAP_MS);
    }
    if (i >= CLIP_OPEN_RETRY) {
        snprintf(err, errcap, "打不开剪贴板");
        return NULL;
    }

    h = GetClipboardData(CF_UNICODETEXT);
    if (!h) {
        CloseClipboard();
        snprintf(err, errcap, "剪贴板里没有文本");
        return NULL;
    }
    w = (WCHAR *)GlobalLock(h);
    if (!w) {
        CloseClipboard();
        snprintf(err, errcap, "读剪贴板失败");
        return NULL;
    }

    need = WideCharToMultiByte(CP_UTF8, 0, w, -1, NULL, 0, NULL, NULL);
    u = (char *)malloc((size_t)(need > 0 ? need : 1));
    if (u && need > 0)
        WideCharToMultiByte(CP_UTF8, 0, w, -1, u, need, NULL, NULL);
    GlobalUnlock(h);
    CloseClipboard();

    if (!u) {
        snprintf(err, errcap, "内存不足");
        return NULL;
    }
    return u;
}

/* 后台线程里调用：把结果放进槽位 */
static void clip_read_now(void)
{
    char err[128];
    char *text;
    size_t len = 0;

    err[0] = '\0';
    text = clip_read_text(err, sizeof(err));

    clip_lock_init();
    EnterCriticalSection(&g_clip_lock);
    if (text) {
        len = strlen(text);
        if (len > CLIP_MAX_BYTES)
            len = CLIP_MAX_BYTES;
        memcpy(g_clip_text, text, len);
        g_clip_text[len] = '\0';
        g_clip_len = len;
        g_clip_error[0] = '\0';
    } else {
        g_clip_len = 0;
        g_clip_text[0] = '\0';
        snprintf(g_clip_error, sizeof(g_clip_error), "%s", err[0] ? err : "读剪贴板失败");
    }
    LeaveCriticalSection(&g_clip_lock);

    free(text);
    InterlockedExchange(&g_clip_state, text ? CLIP_READY : CLIP_FAILED);
}

static int l_clipboard_request(lua_State *L)
{
    if (InterlockedCompareExchange(&g_clip_state, CLIP_PENDING, CLIP_IDLE) != CLIP_IDLE) {
        lua_pushinteger(L, 2);
        return 1;
    }
    if (!claim_slot()) {
        InterlockedExchange(&g_clip_state, CLIP_IDLE);
        lua_pushinteger(L, 2);
        return 1;
    }
    if (!ensure_worker()) {
        InterlockedExchange(&g_busy, 0);
        InterlockedExchange(&g_clip_state, CLIP_IDLE);
        lua_pushinteger(L, 0);
        return 1;
    }
    InterlockedExchange(&g_pending_kind, 4);
    lua_pushinteger(L, 1);
    return 1;
}

static int l_clipboard_take(lua_State *L)
{
    LONG st = g_clip_state;

    clip_lock_init();
    if (st == CLIP_READY) {
        EnterCriticalSection(&g_clip_lock);
        lua_pushlstring(L, g_clip_text, g_clip_len);
        g_clip_len = 0;
        g_clip_text[0] = '\0';
        LeaveCriticalSection(&g_clip_lock);
        InterlockedExchange(&g_clip_state, CLIP_IDLE);
        lua_pushnil(L);
        return 2;
    }
    if (st == CLIP_FAILED) {
        EnterCriticalSection(&g_clip_lock);
        lua_pushnil(L);
        lua_pushstring(L, g_clip_error[0] ? g_clip_error : "读剪贴板失败");
        LeaveCriticalSection(&g_clip_lock);
        InterlockedExchange(&g_clip_state, CLIP_IDLE);
        return 2;
    }
    lua_pushnil(L);
    lua_pushnil(L);
    return 2;
}

static int l_clipboard_get(lua_State *L)
{
    char err[128];
    char *text;

    err[0] = '\0';
    text = clip_read_text(err, sizeof(err));
    if (!text) {
        lua_pushnil(L);
        lua_pushstring(L, err[0] ? err : "读剪贴板失败");
        return 2;
    }
    lua_pushlstring(L, text, strlen(text));
    free(text);
    lua_pushnil(L);
    return 2;
}

/* ==================================================================
 * 唤醒键
 *   nudge(delay_ms[, vk]) -> 1=已安排
 *
 * 输入法空闲时没有任何按键事件，Lua 也就不会被调用，异步拿到的结果
 * 没机会显示出来。所以要有人在"结果到了"之后轻轻敲一个键，让引擎再
 * 走一遍处理流程（我们自己把这个键吞掉，目标程序看不到它）。
 * 默认 VK_F24（0x87）：绝大多数程序都不用它。
 * ================================================================== */
typedef struct {
    DWORD delay;
    WORD  vk;
} nudge_job;

static DWORD WINAPI nudge_thread(LPVOID param)
{
    nudge_job *job = (nudge_job *)param;
    INPUT in;

    Sleep(job->delay > 5000 ? 5000 : job->delay);

    ZeroMemory(&in, sizeof(in));
    in.type = INPUT_KEYBOARD;
    in.ki.wVk = job->vk;
    in.ki.dwFlags = 0;
    SendInput(1, &in, sizeof(in));

    Sleep(30);

    ZeroMemory(&in, sizeof(in));
    in.type = INPUT_KEYBOARD;
    in.ki.wVk = job->vk;
    in.ki.dwFlags = KEYEVENTF_KEYUP;
    SendInput(1, &in, sizeof(in));

    free(job);
    return 0;
}

static int l_nudge(lua_State *L)
{
    int delay = (int)luaL_optinteger(L, 1, 0);
    int vk = (int)luaL_optinteger(L, 2, 0x87);
    nudge_job *job;
    HANDLE th;

    if (delay < 0) delay = 0;
    if (vk <= 0 || vk > 0xFE) vk = 0x87;

    job = (nudge_job *)calloc(1, sizeof(nudge_job));
    if (!job) {
        lua_pushinteger(L, 0);
        return 1;
    }
    job->delay = (DWORD)delay;
    job->vk = (WORD)vk;

    th = CreateThread(NULL, 0, nudge_thread, job, 0, NULL);
    if (!th) {
        free(job);
        lua_pushinteger(L, 0);
        return 1;
    }
    track_thread(th);
    lua_pushinteger(L, 1);
    return 1;
}

/* ==================================================================
 * 直接插入文本
 *   send_text(utf8) -> 1=已安排  2=上一段还在打  0=参数或线程问题
 *
 * 用 SendInput 的"Unicode 字符"模式把整段文字打进当前窗口（相当于逐个
 * 字符输入，但不经过输入法、不碰剪贴板）。
 * 和 press_keys 一样必须在后台线程里延时再发：正在处理按键时注入会重入。
 * 一次只允许一段文字在打，两段交叉会把字打乱。
 * ================================================================== */

/* 逐字符把 UTF-8 文本打进当前窗口。只在后台线程里调用，
 * 且调用前要确保本次按键已经处理完（worker 里已等过 FIRE_DELAY_MS）。 */
static void type_text_now(const char *text)
{
    WCHAR *w;
    int need, i;

    if (!text || !*text)
        return;

    need = MultiByteToWideChar(CP_UTF8, 0, text, -1, NULL, 0);
    if (need <= 1)
        return;
    w = (WCHAR *)malloc(sizeof(WCHAR) * (size_t)need);
    if (!w)
        return;

    if (MultiByteToWideChar(CP_UTF8, 0, text, -1, w, need) > 0) {
        for (i = 0; i < need - 1; i++) {
            INPUT in[2];

            ZeroMemory(in, sizeof(in));
            in[0].type = INPUT_KEYBOARD;
            in[0].ki.wScan = w[i];
            in[0].ki.dwFlags = KEYEVENTF_UNICODE;
            in[1].type = INPUT_KEYBOARD;
            in[1].ki.wScan = w[i];
            in[1].ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP;
            send_batch(in, 2);
            Sleep(2);
        }
    }
    free(w);
}

static DWORD WINAPI send_text_thread(LPVOID param)
{
    char *text = (char *)param;

    Sleep(250); /* 等这次按键处理结束，编码窗口稳定 */
    type_text_now(text);

    free(text);
    InterlockedExchange(&g_text_busy, 0);
    return 0;
}

static int l_send_text(lua_State *L)
{
    size_t len = 0;
    const char *text = luaL_checklstring(L, 1, &len);
    char *copy;
    HANDLE th;

    if (!text || len == 0 || len > 20000) {
        lua_pushinteger(L, 0);
        return 1;
    }
    if (InterlockedCompareExchange(&g_text_busy, 1, 0) != 0) {
        lua_pushinteger(L, 2);
        return 1;
    }
    copy = (char *)malloc(len + 1);
    if (!copy) {
        InterlockedExchange(&g_text_busy, 0);
        lua_pushinteger(L, 0);
        return 1;
    }
    memcpy(copy, text, len);
    copy[len] = '\0';

    th = CreateThread(NULL, 0, send_text_thread, copy, 0, NULL);
    if (!th) {
        free(copy);
        InterlockedExchange(&g_text_busy, 0);
        lua_pushinteger(L, 0);
        return 1;
    }
    track_thread(th);
    lua_pushinteger(L, 1);
    return 1;
}

static const luaL_Reg module_functions[] = {
    {"press_backspace", l_press_backspace},
    {"press_backspace_then_text", l_press_backspace_then_text},
    {"press_keys",      l_press_keys},
    {"open_url",        l_open_url},
    {"clipboard_get",   l_clipboard_get},
    {"clipboard_request", l_clipboard_request},
    {"clipboard_take",    l_clipboard_take},
    {"nudge",           l_nudge},
    {"send_text",       l_send_text},
    {"http_request",    l_http_request},
    {"http_state",      l_http_state},
    {"http_result",     l_http_result},
    {"http_cancel",     l_http_cancel},
#ifdef ACTIONS_DRY_RUN
    {"_dry_run_sent",   l_dry_run_sent},
    {"_dry_run_reset",  l_dry_run_reset},
    {"_dry_run_events", l_dry_run_events},
    {"_dry_run_url_opens", l_dry_run_url_opens},
    {"_dry_run_url",       l_dry_run_url},
    {"_dry_run_gap",       l_dry_run_gap},
#endif
    {NULL, NULL}
};

EXPORT int luaopen_actions(lua_State *L)
{
    luaL_checkversion_(L, LUA_VERSION_NUM, LUAL_NUMSIZES);
    http_lock_init();
    luaL_newlib(L, module_functions);
    return 1;
}

/* ==================================================================
 * DLL 生命周期
 *
 * 两种卸载要区别对待：
 *   - 进程正在退出（DllMain 的 reserved 非 NULL）：系统随后会结束所有线程，
 *     这里只发停止信号、不等待，避免在加载器锁里等一条可能还要加载 DLL 的线程；
 *   - 只是 FreeLibrary（reserved 为 NULL）：先发停止信号，再在
 *     WORKER_EXIT_WAIT_MS 预算内等 worker 与登记过的临时线程收尾，然后销毁临界区。
 * ================================================================== */
static void worker_shutdown(BOOL wait)
{
    HANDLE th = (HANDLE)InterlockedExchangePointer((PVOID volatile *)&g_worker, NULL);

    InterlockedExchange(&g_stop, 1);

    if (th) {
        if (wait)
            WaitForSingleObject(th, WORKER_EXIT_WAIT_MS);
        CloseHandle(th);
    }
    if (wait)
        shutdown_extra_threads();
}

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID reserved)
{
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(instance);
    } else if (reason == DLL_PROCESS_DETACH) {
        /* reserved 非 NULL 表示"进程正在退出" */
        BOOL exiting = (reserved != NULL);

        worker_shutdown(!exiting);

        if (!exiting) {
            if (InterlockedExchange(&g_http_lock_ready, 0) == 1)
                DeleteCriticalSection(&g_http_lock);
            if (InterlockedExchange(&g_clip_lock_ready, 0) == 1)
                DeleteCriticalSection(&g_clip_lock);
        }
    }
    return TRUE;
}
