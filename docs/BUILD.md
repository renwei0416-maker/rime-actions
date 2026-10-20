# actions.dll 编译说明（rime-actions 上屏动作）

> 项目：rime-actions（上屏动作）

本目录是 `rime-actions`（上屏动作）的完整交付与编译说明：`actions.c`（按键注入模块的源码）、
`actions.lua`（Rime 组件）、`build.cmd`（一键构建）。

**v3 的关键改动：注入改成异步。** 原来是在 translator 回调里同步 `SendInput`；实测连续用 `qq/`
会让小狼毫崩溃（崩在 rime.dll 内部、`0xc0000005` 访问冲突，当天共 8 次）——注入的按键会在引擎
处理按键的中途重新进来（重入），一次删几十字就等于制造几十个重入窗口。现在：

- `press_backspace(n)` 只把请求排进队列就**立刻返回**；按键由后台线程等 80 ms 后**分批**发出
  （每批 8 个退格、批间 5 ms），同一时刻只跑一个，距上次注入不足 150 ms 的请求直接**丢弃**（返回 0）
- 被丢弃时 Lua 侧会把那条上屏记录**放回栈里**，稍后再按一次触发码即可，不会白丢
- 返回值为"本次被接受的次数"；万一线程创建失败会退回同步发送（功能不至于不可用）

**v4 的关键改动：Lua 从 translator 改成 processor（挂载行要改）。** v3 部署后仍崩过一次，
偏移与旧同步版完全相同（`0x19cb72`）——说明异步只削掉了"同步重入"这一半，另一半是
**在翻译器回调里 `context:clear()`**：那时引擎正遍历 translators 拼 segment，清除会触发
`update_notifier`，让方案里其它 lua 组件嵌套执行并改引擎状态。同一个 `clear()` 放在
**processor** 里是安全的（用户方案里的 `empty_code_clear.lua` 就是 processor，一直没出问题），
因为处理器跑在整条翻译流水线**之前**。因此：

- `actions.lua` 改成 processor 组件，挂载行必须是 `lua_processor@*…`（**放在 `engine/processors`**）；
  仍挂在 `translators` 里会照旧崩
- 处理器看的"文本触发码"与翻译器不同：翻译器是把整串 `qq/` 当候选输入送进来再比较，
  处理器是**逐键**判断——最后一键到达时"已拼出的前缀 + 这一键"等于触发码，就清掉前缀、
  删字，并**吞掉这一键**（所以 `qq/` 不会上屏，也不需要 `context:clear()` 之外的任何清理）
- 新增**组合键模式**（`actions/hotkey`，如 `Control+slash`）：第一个键就被整个吞掉，
  完全不用碰 context、不需要 `alphabet`、不会误触
- **v5**：组合键与文本触发码从"二选一"改成**可同时启用**——不填 `actions/hotkey` 就是只有
  `qq/`；填了则两种方式都有效（先用组合键匹配，没命中再走文本判定）；想只留组合键，把
  `actions/trigger` 写成 `none`/`off`/`-`；`actions/hotkey` 拼错时日志里会多一行
  `cannot parse actions/hotkey`。这一版只改 Lua，`mode=` 日志现在会列出生效的方式（如 `mode=hotkey+text`）。
- dll **不用重编**（C 侧接口没变；v5 只改 Lua）

**v6 的关键改动：多了"方式三"——打快捷触发码，替你按一下系统快捷键。**
`actions/shortcuts` 里写 `触发码=动作`（空格分隔），命中就吞掉触发码、让 dll 发一串真按键；
动作可以是按键序列（`ctrl+z`、多步 `shift+home,shift+left`）、`@to-symbol`（删到前一个符号、
符号留在屏幕上）、`@to-symbol-include`（连符号一起删）、`@delete`（等于 `qq/`）。
默认全不启用（脚本里预置 26 项，写 `"@defaults"` 一次启用；v6 时是 14 项、v6.1 是 17 项）。因此这一版：
- **要换新 dll**（v6 是 33457 B；v7 是 34203 B；v8 是 36190 B；v9 是 36734 B）：C 侧新增 `press_keys(spec)`，返回 1 已排队 / 2 被忽略 / 0 参数非法；
  `press_backspace` 没动，所以不用快捷键的话旧 dll 也能跑（只是日志会写 `press_keys is unavailable`）
- 记账栈从"只记字数"改成"记字数 + 上屏原文"，`@to-symbol` 靠原文往回找符号
- 三种触发方式互不排斥；快捷键分支不受 `debounce` 限制，但共用 C 侧那个 150 ms 注入窗口

**v7 的关键改动：注入改成"带扫描码"的真按键写法 —— 修 `ss;` / `ww;` 只移动光标、选不中。**

只填 `wVk` 的合成按键会被底层键盘钩子区别对待：小狼毫会把组合键里的 Shift / Ctrl 当成
"单独的修饰键"吃掉（Shift 还会顺手切中英），应用只收到后面那个方向键 —— 表现就是
"光标跳过去了、但没有选中"（`ss;` 只跳行首、`ww;` 被换成别的输入法都是这个原因，
与万象无关，也不是 Windows 的输入法切换热键在作怪）。改成 `KEYEVENTF_SCANCODE`
（+ 扩展键的 `KEYEVENTF_EXTENDEDKEY`）之后与真键盘无异。因此这一版：
- **要换新 dll**（34203 B）：C 侧只改了按键事件的构造方式，`press_backspace` / `press_keys`
  的接口、返回值、异步机制、150 ms 窗口都没变
- `actions.lua`（28721 B）**不用换**，配置和系统设置也一个字都不用改 —— 只替换 dll 即可
- dry-run 事件串的写法变了（带扫描码的写成 `"2Cs"`、扩展键 `"47sx"`），只影响自测断言

**v8 的关键改动：`-` 分步写法 + `f;` 用浏览器搜索最后一次上屏 + 默认表换成 26 项。**

- 动作里 `-` 与 `,` **完全等价**：`shift+home-ctrl+c` 和 `shift+home,ctrl+c` 发的是同一串
  （Lua 侧在 `actions.lua:340` 附近把 `-` 归一化成 `,`，C 侧一行没动）。所谓"放下再按下一步"
  本来就是 C 侧多步的行为：每步内部是"修饰键按下 → 键按下 → 键抬起 → 修饰键逆序抬起"，
  步与步之间等 `STEP_GAP_MS 30` 毫秒
- **要换新 dll**（36190 B）：C 侧新增 `open_url(url)`（`ShellExecuteW`，后台线程里调用，
  不会闪黑框），返回值同 `press_keys`：1 已排队 / 2 被忽略 / 0 参数非法。只放行 `http://`、`https://`，
  拒绝空白字符与 `"` `<` `>` `|`；链接用 `-lshell32` 链进来
- 新增 `@search` 动作 + 配置项 `actions/search_url`（默认 `https://www.baidu.com/s?wd=%s`）：
  取**最后一次上屏的原文**，UTF-8 百分号编码后替换 `%s`（没有 `%s` 就接在末尾），再交给 `open_url`
- `DEFAULT_SHORTCUTS` 从 17 项换成用户指定的 26 项（`z;`…`f;`），仍然默认一个都不启用
- **v8 要换两个文件**：`actions.dll`（36190 B）+ `actions.lua`（32444 B）；配置里再加
  `"actions/shortcuts": "@defaults"` 才会启用这 26 项

**v9 的关键改动：按键间隔可配（`actions/key_gap_ms`，默认 30 ms，范围 0~500）。**

- 原来"快慢"是写死的：步与步之间固定 `STEP_GAP_MS 30`，同一步内部的修饰键/主键之间**完全不等**
  （一次性 `SendInput` 塞进去）。有些编辑器对瞬间到达的一串按键反应不过来（只走第一步、或者
  Shift/Ctrl 像没按住），所以要能调。
- C 侧新增 `send_events_spaced(events, total, gap)`：`gap > 0` 时**逐个事件**发
  （`SendInput(1, ...)`）并在相邻事件之间 `Sleep(gap)`；`gap <= 0` 退回原来"一次塞进去"的写法。
  `inject_keys(spec, gap)` 每步用它，步与步之间也停 `gap`。
- `press_keys` 多了**可选第二参数**：`press_keys(spec[, gap_ms])`，省略 = 30；负数当 0，
  超过 `MAX_KEY_GAP_MS 500` 截到 500。旧版 Lua 只传一个参数也照旧能用。
- Lua 侧读 `actions/key_gap_ms`，`fire_keys` 把它作为第二个参数传下去；`DEFAULTS.key_gap_ms = 30`、
  `LIMITS.key_gap_ms = 500`；启动日志多一列 `key_gap_ms=`。
- dry-run 版新增 `_dry_run_gap()`（返回最后一次解析出来的间隔），供探针断言"配置真的传到了 C"。
- **v9 也要换两个文件**：`actions.dll`（36734 B）+ `actions.lua`；配置里加
  `"actions/key_gap_ms": 30`（不写就是默认 30，行为与 v8 基本一致，只是同一组合键内部也按 30 ms 逐个发了）。

- 触发方式：默认文本触发码 `qq/`（默认值在 `actions.lua:61` 的 `DEFAULTS`，`trigger` 在 `:62`，可用 `actions/trigger` 覆盖，
  写 `none`/`off`/`-` 即关闭）；可选组合键 `actions/hotkey`（默认空 = 不启用，填了与文本触发码**同时生效**）
- 行为：触发后向当前窗口注入 N 次退格（`VK_BACK`），N = 上一次上屏的字符数（单次上限
  `actions/max_backspaces`，默认 500）
- 参数全部可配（默认在 `actions.lua:61` 的 `DEFAULTS`，可用方案 / `custom.yaml` 覆盖，不用重编）：
  `actions/trigger`、`actions/hotkey`、`actions/shortcuts`、`actions/search_url`、`actions/symbols`、
  `actions/dll_dir`、`actions/stack_size`、`actions/max_backspaces`、`actions/debounce_ms`、`actions/key_gap_ms`
- 模块内部名必须是 `actions.dll`，入口 `luaopen_actions`

---

## 1. 交付物

| 文件 | 大小 | 说明 |
| --- | --- | --- |
| `actions.dll` | 89182 B | 模块本体，x64。sha256 `187dfb8568ccb57b3ff0d8f6ac136bff2253d333f28cf61335fb34d7bec556ae` |
| `actions.c` | 46049 B | 编译用的 C 源（v10 起 = 上游 AI 版那份文件的**超集**：`press_backspace` + `press_keys` + `open_url` + 扫描码注入 + `send_events_spaced` + `http_*`/`clipboard_get`/`send_text`/`nudge`，diff 见第 2 节）。sha256 `184a924b0a9cc36af1f94870872dd88c81ad128b4ec59f6f23fb6a58d57ccaac` |
| `actions.lua` | 60502 B | 配套 Lua **processor** 组件（`commit_notifier` 记账 + **四种触发方式可同时启用** + 26 项快捷键动作表 + `-`/`,` 多步 + 删到前一个符号 + `@search` 浏览器搜索 + 可配按键间隔 + **v10 的 AI 模式 `@ai`** + 可配置参数 + 被忽略时回填记录）。sha256 `c0e5a42bb8f044cacf58837b1d0c4f2f201cfe9372f72fcbac38aae55356cc4d` |
| `build.cmd` | 1626 B | 一键构建（ASCII + CRLF，已加 `-lshell32` 与 `-lwinhttp`）。sha256 `da3bcf2988aed5bfe414509f3a066d8a8337021dac5b26bd4fe050b27e0cb8ef` |
| `include\lua.h` / `luaconf.h` / `lauxlib.h` | 16282 / 22044 / 9122 B | Lua 5.4.0 头文件，ABI 基准 |
| `lib\rime-lua.def` + `lib\librime-lua.a` | 91 / 4672 B | 备选导入库（不想直接链 rime.dll 时用） |

你原始的 `C:\Users\Administrator\Downloads\actions.c` **未改动**
（1139 B，sha256 `EF885BA3A26D3DA08A7FE76071337F9FDA84BB79EE4C0A05CA16D36B018033C9`）。
上一版 Ctrl+Z 的交付仍在 `..\ctrlz-build\`，未被覆盖。

---

## 2. 相对你给的 actions.c 改了什么

- 删除 `l_press_ctrl_z()`（原来组 4 个 INPUT 发 Ctrl+Z）
- 新增 `l_press_backspace()`（**不再在回调里同步发按键**）：
  - `lua_Integer count = luaL_optinteger(L, 1, 1);`（缺省 1 次），夹到 `[0, MAX_BACKSPACES]`
    （`actions.c:35` `#define MAX_BACKSPACES 10000`，只用来拦 Lua 侧传错超大值；真正的
    策略上限是配置项 `actions/max_backspaces`，默认 500）
  - 忙标志（`InterlockedCompareExchange`）+ 距上次注入 150 ms 的窗口判定；不通过就返回 0
  - 通过则把数量写进"待发槽位"并返回该数量；后台线程（首次使用时 `CreateThread`）等 80 ms 后
    由 `inject_backspaces()` 分批 `SendInput`（每批 8 个退格、批间 5 ms，中途失败就停手）
  - 时间用 `GetTickCount64` 真时钟（Lua 的 `os.clock()` 是 CPU 时间，不能当墙上时钟用）
- `module_functions[]`：`{"press_ctrl_z", ...}` → `{"press_backspace", ...}`
- `EXPORT int luaopen_actions` + `luaL_newlib` 未动；`#include` 未动
- 离线自测另编一份带 `-DACTIONS_DRY_RUN` 的 dll（只数不发）；**正式 dll 里没有这个钩子**
- **v6 新增 `l_press_keys()`**（`actions.c:484`）：发一串原生组合键，语法 `"ctrl+z"`、
  `"shift+home"`、`"ctrl+shift+k"`，多步用逗号 `"shift+home,shift+left"`。
  事件顺序是"修饰键先按下、键后按下、键先抬起、修饰键最后抬起"（v7 起 `ctrl+z` → `1Ds,2Cs,2Cs-up,1Ds-up`；
  v6 是只填 VK 的 `11,5A,5A-up,11-up`）。
  返回值：`1` 已排队 / `2` 被忽略（忙，或距上次注入不足 `MIN_INTERVAL_MS 150`）/ `0` 参数非法或解析失败。
  上限：`MAX_SPEC_LEN 255`（`:67`）、`MAX_CHORD_EVENTS 12`（`:68`）、步与步之间等 `STEP_GAP_MS 30`（`:65`）；
  键名解析在 `actions.c:307` 附近（只解析不发送），键名清单见文件头注释 `:8-20`。
- v6 的 dry-run 钩子扩展成三个：`_dry_run_sent()`（`:523`）、`_dry_run_reset()`（`:530`）、
  `_dry_run_events()`（`:539`，返回 `"2Cs"`（按下）/ `"47sx-up"`（扩展键抬起）这种事件串；不带 `s`
  的是只填 `wVk` 的退格。v6 时代全是裸 VK，如 `"5A"`），存储用
  `g_dry_run_log[DRY_RUN_MAX_EVENTS][20]`（`:81-84`）；同样只在 `-DACTIONS_DRY_RUN` 的构建里存在。
- 两层的严格程度不同（实际调用永远先过 Lua，所以只是备查）：C 层对"有空 token"的写法宽容
  （`"ctrl+"`、`"+z"`、`"ctrl+z,"` 会被当合法并真的发键），把关的是 `actions.lua:220` 的
  `valid_key_spec()`（拒绝开头/结尾/连写的 `+`，且每个键名必须在白名单里）；`press_keys(nil)`
  由 `luaL_checkstring` 抛 `bad argument #1 (string expected)`。
- **v7 新增 `fill_key()` / `vk_is_extended()`**（都在 `build_chord()` 前）：按键事件改用
  `MapVirtualKeyW(vk, MAPVK_VK_TO_VSC_EX)` 取扫描码，填 `wScan` + `KEYEVENTF_SCANCODE`（`wVk = 0`）；
  取不到扫描码才退回只填 `wVk`。**`MapVirtualKeyW` 在本机只返回单字节扫描码**（Home=`0x47`、
  Left=`0x4B`…），而 `0x47`/`0x4B` 同时也是小键盘 7/4 的扫描码，所以扩展位由
  `vk_is_extended()` 的显式清单补（`VK_PRIOR/NEXT/END/HOME/LEFT/UP/RIGHT/DOWN/INSERT/DELETE`、
  `VK_LWIN/RWIN/APPS`、`VK_DIVIDE`、`VK_RCONTROL`、`VK_RMENU`）—— 漏了就会把方向键发成小键盘数字。
  非扩展键的例子：`z`→`2Cs`、`ctrl`→`1Ds`、`shift`→`2As`、`backspace`→`0Es`、`slash`→`35s`。
- **退格那条路（`inject_backspaces()`）刻意不动**：它一直工作正常（只填 `wVk` 的退格不受影响），
  不冒回归风险。所以同一份 dll 里两种写法并存：组合键走扫描码，退格走老写法。
- **v8 新增 `open_url(url)`**（走同一套异步槽位：`g_pending_kind` 多了 `3 = 打开网址`）：
  - `static void open_url_now(const char *url)`：dry-run 下只记录次数与文本；正式版
    `MultiByteToWideChar(CP_UTF8, …)` 转宽字符后 `ShellExecuteW(NULL, L"open", wide, NULL, NULL, SW_SHOWNORMAL)`
  - `static BOOL url_is_valid(const char *url)`：只放行 `http://` / `https://`，拒绝 `<=0x20`、`0x7F`、
    `"`、`<`、`>`、`|`（不给自己留"拼错就打开奇怪协议"的口子）
  - `l_open_url`：`luaL_checklstring` → 长度 1..`MAX_URL_LEN`(2048) → 校验 → `claim_slot()` 失败返 2 →
    `ensure_worker()` 失败就当场同步打开 → 否则填 `g_pending_url` + `InterlockedExchange(&g_pending_kind, 3)` 返 1
  - dry-run 侧新增 `_dry_run_url_opens()` / `_dry_run_url()`，`_dry_run_reset()` 一并清零
- **v10：C 源换成上游 AI 版的超集**（不再逐个补丁，改为整份替换；v6~v9 的改动一行不少）：
  - 校验：上游那份 1393 行里，我们 v9 的 797 行**逐行都能按序找到**（0 处不匹配），
    所以替换是纯增量；函数面从 25 个变成 38 个，多的 13 个全是 AI 用到的
  - 新增 `http_request(url, body, headers)` / `http_state()` / `http_result()` / `http_cancel()`
    （WinHTTP 后台线程，按键回调里绝不联网）；`clipboard_get()`；`send_text(utf8)`
    （`SendInput` + `KEYEVENTF_UNICODE` 逐字打，延时 250 ms 起）；`nudge(delay[, vk])`
    （延时敲一个空键把引擎叫醒，默认 `0x87` = F24）
  - 链接多一个 `-lwinhttp`（`build.cmd:32`、`smoke_backspace.py:71` 的 gcc 参数都加了）
  - 返回值约定：`http_request` → 序号 ≥1 / `0` 参数非法 / `-1` 上一个还没回来；
    `http_state` → 状态串 + 序号；`http_result` → `body,nil` 或 `nil,错误`（取完回 idle）；
    `http_cancel` → 丢弃了 1 否则 0；`send_text` / `nudge` → 1 已排队 / 0 起不来
  - **头之间必须是真的 CRLF**：`WinHttpAddRequestHeaders` 按 CRLF 切行，写成字面 `\r\n`
    会被当成一行怪名字丢掉（Lua 侧 `actions.lua:1015` 用的是 `table.concat(headers, "\r\n")`，
    是真 CRLF，已用本机 HTTP 服务验证过）
- **v10 的 Lua 侧（`@ai` 模式）**：`AI_PROMPTS`（`actions.lua:741`，default/eng/jp 三条）、
  `ai_composition_text()`（commit → 候选 → preedit，优先取含汉字者）、`ai_history_text()`
  （上屏记账拼"上文"，跳过 AI 自己写进去的那些）、`ai_last_sentence()`（`:829`，先把末尾那串符号
  与空白跳过，再从后往前找最后一个 `。！？` 分隔符，找不到就整段）、`parse_send_key()`（`:911`，单字符=连按两下，
  `ctrl+enter` 这类=按一次即发）、`ai_send_now()`（`:946`，判源 → 取文 → 组 JSON/headers →
  `http_request` → 记 `env.ai_req` → `nudge`）、`ai_take()`（`:1046`，`http_state` 轮询：
  running 超时就 `http_cancel`，done/failed 就取结果 → 插入 / `replace` 时先删原文）、
  `ai_handle_key()`（`:1133`，AI 模式下的按键状态机：唤醒键、Esc、发送键、换提示词）、
  `init_ai()`（`:1204`，dll 能力检查 + 30 项配置）。`init/fini/func` 相应多了
  `env.ai_enabled`、`http_cancel`、AI 分支（`actions.lua:1307` / `:1392` / `:1403`）。

---

## 3. 依赖清单

**头文件**：`lua.h` + `luaconf.h` + `lauxlib.h`（Lua 5.4.0，`LUA_VERSION_NUM 504`）。
本机 MSYS2 没装 Lua 包，也不存在 `C:\msys64\mingw64\include\lua.h`；这套头文件取自
`..\rime-lua-actions\lua\`（随项目附带的 Lua 5.4.0 原树），已放进 `include\`，编译自足。

**链接库**：必须解析 5 个 Lua 符号 → 由 **rime.dll** 提供（rime.dll 内嵌 Lua 5.4，725 个具名导出
里含 154 个 `lua*`/`luaL*`）。实测去掉 `-L… -lrime` 会报
`undefined reference to 'luaL_optinteger'` / `'lua_pushinteger'` / `'lua_createtable'` 等，
所以**这些符号必须来自 rime.dll 或由它生成的导入库**（本机没有独立 Lua 运行库可用）。
系统库只需 `-luser32`（`SendInput`、`MapVirtualKeyW`）和 `-lshell32`（`ShellExecuteW`）；
msvcrt / KERNEL32 由 MinGW 运行时自动带上。

**工具链**：MSYS2 MinGW-w64 x64 `gcc 16.2.0` + `binutils 2.47-4`（ld / as / objdump / dlltool）。
不用 make，不编译其它 C 文件。

---

## 4. 完整可复现命令

一键：`build.cmd`（可先 `set WEASEL_DIR=...` / `set MINGW_BIN=...` 覆盖路径）。

手工等价命令：

```bat
set "PATH=C:\msys64\mingw64\bin;%PATH%"
cd /d <本目录>
gcc -shared -fPIC -o actions.dll actions.c -Iinclude ^
    -L"C:\Program Files\Rime\weasel-0.17.4" -luser32 -lshell32 -lwinhttp -lrime -O2 ^
    -Wl,--no-insert-timestamp
```

备选（不把 rime.dll 直接交给链接器，改用由它的导出生成的导入库）：

```bat
dlltool -d lib\rime-lua.def -l lib\librime-lua.a -D rime.dll
gcc -shared -fPIC -o actions.dll actions.c -Iinclude lib\librime-lua.a ^
    -luser32 -lshell32 -lwinhttp -O2 -Wl,--no-insert-timestamp
```

> `lib\rime-lua.def` 只列了 v6 那 5 个符号，v10 的 C 源还要 `luaL_checklstring` /
> `luaL_optlstring` / `lua_pushlstring` / `lua_pushnil` / `lua_pushstring`
> （`objdump -p rime.dll | findstr lua` 能列全），要用备选这条路得先补齐 def。

**可重复性**：连续两次构建逐字节一致（`--no-insert-timestamp` 抹掉时间戳，
PE `Time/Date` 显示 1970-01-01）。

---

## 5. 产物核实（`objdump -p actions.dll`）

- 架构：`pei-x86-64`（x86-64）
- 导出表：仅 `luaopen_actions`（Number in Export Address Table = 1）
- 导入表：
  - `rime.dll`：`luaL_checklstring`、`luaL_checkversion_`、`luaL_optinteger`、`luaL_optlstring`、
    `luaL_setfuncs`、`lua_createtable`、`lua_pushinteger`、`lua_pushlstring`、`lua_pushnil`、
    `lua_pushstring`（v10 起多了 4 个）
  - `USER32.dll`：`MapVirtualKeyW`（v7 起）、`SendInput`、`OpenClipboard` / `GetClipboardData` /
    `CloseClipboard`（v10 起）
  - `SHELL32.dll`：`ShellExecuteW`（v8 起，`@search` 用）
  - `WINHTTP.dll`（v10 起，`@ai` 用）：`WinHttpAddRequestHeaders`、`WinHttpCloseHandle`、
    `WinHttpConnect`、`WinHttpCrackUrl`、`WinHttpOpen`、`WinHttpOpenRequest`、
    `WinHttpQueryDataAvailable`、`WinHttpQueryHeaders`、`WinHttpReadData`、
    `WinHttpReceiveResponse`、`WinHttpSendRequest`、`WinHttpSetTimeouts`
  - `KERNEL32.dll`：`CloseHandle`、`CreateThread`、`DeleteCriticalSection`、`EnterCriticalSection`、
    `GetCPInfo`、`GetLastError`、`GetModuleHandleA`、`GetProcAddress`、`GetTickCount64`、
    `GlobalLock`、`GlobalUnlock`、`InitializeCriticalSection`、`LeaveCriticalSection`、
    `MultiByteToWideChar`、`Sleep`、`VirtualProtect`、`VirtualQuery`、`WideCharToMultiByte`
  - `msvcrt.dll`：`__iob_func`、`_initterm`、`_lock`、`_strdup`、`_unlock`、`abort`、`calloc`、
    `free`、`malloc`、`memcpy`、`memset`、`realloc`、`strchr`、`strlen`、`strstr`、`vfprintf` 等
- 无 `libgcc*` / `libwinpthread*` 依赖（拷到别的机器不会缺 DLL；`winhttp.dll` 是系统自带的）
- `press_ctrl_z` 已不在导出/注册表内（真的删干净了，不是留着不用）

---

## 6. 验证情况：做了什么、没做什么

验证脚本：`C:\Users\Administrator\Desktop\Ai_Work\DSH_harness\smoke_backspace.py`

**已做（全部离线，且未注入任何一次按键）**

1. 用 **rime.dll 自带的 Lua 5.4**（就是 WeaselServer 实际用的那份运行时）进程外
   `require("actions")` → 载入成功。这一步同时证明 `luaL_newlib` 内部的
   `luaL_checkversion_` 通过，即头文件 ABI 与 rime.dll 内嵌 Lua 一致（README 里最常踩的坑）。
2. 导出面：`press_backspace`、`press_keys` 都是 function，`press_ctrl_z` 为 `nil`。
3. C 侧护栏（这些调用不会产生任何按键）：
   - `press_backspace(0)` → 返回 `0`；`press_backspace(-5)` → 返回 `0`
   - `press_backspace("x")` → 抛 `bad argument #1 ... (number expected, got string)`
4. **异步流程端到端**（用 `-DACTIONS_DRY_RUN` 那份"只数不发"的 dll，仍然不碰真实按键）：

   | 步骤 | 期望 | 实测 |
   | --- | --- | --- |
   | 调用后立刻看计数 | 还是 0（按键没同步发出） | ✅ |
   | 约 80 ms 后 | 计数一次加上 2×N（后台线程分批发出） | ✅ |
   | 上屏 5 / 4 / 3，连触发两次 | 第一次删 3（6 个按键事件），第二次被丢掉，计数不变 | ✅ |
   | 等过窗口后再触发 | 刚被丢掉的那笔 4 被补删（14），再触发删 5（24） | ✅ |
   | 栈空后再触发 | 不动作，只写日志 | ✅ |
   | 忙时 / 150 ms 窗口内再请求 | 返回 `0`，且不产生新按键 | ✅ |
   | `press_backspace(1000000)` | 返回 `10000`（上限夹取） | ✅ |

5. `actions.lua` 逻辑（processor 调用约定：mock 里造 KeyEvent、模拟引擎把非 kNoop 的可打印键拼进 `context.input`，
   并把模块的 `press_backspace` 换成桩函数后运行）：

   | 步骤 | 期望 | 实测 |
   | --- | --- | --- |
   | 上屏「你好世界」/「abc」/空串/「a😀b」/nil | 入栈 4、3、跳过、3、跳过 | ✅ |
   | 逐键喂 `q` `q` `/`，第四次触发 | 返回 2、2、1（最后一键被吞），注入 3、3、4，`ctx.input` 为空 | ✅ |
   | 上屏 4 字后逐键喂 `q` `q` | 全程 kNoop，不触发 | ✅ |
   | 配置 `trigger=q/ stack_size=3 max_backspaces=100` | 老触发码 `qq/` 全 kNoop，只认 `q/`，只记最后 3 笔 | ✅ |
   | 配置 `max_backspaces=2`（上屏 4 字） | 只删 2 并写截断日志 | ✅ |
   | 配置组合键 `actions/hotkey=Control+slash`（v5 起与文本触发码**可同时用**） | 第一键即 kAccepted 并吞掉，`context` 不被 clear；文本触发码照旧有效 | ✅ |
   | 同上，但有未上屏编码 | 只 kAccepted 吞掉，不删字并写 `hotkey ignored while composing` | ✅ |
   | 组合键写成 `Ctrl+/` | 同样识别 | ✅ |
   | 配置非法值（-3 / 999999 / -1） | 夹到 `LIMITS`：1 / 10000 / 0 | ✅ |
   | 配置错的 `dll_dir` | 记 `module=MISSING`，触发不动作 | ✅ |
   | 配置 `actions/shortcuts="@defaults pq;=@delete"`（v8 的 26 项表） | 默认 26 项 + `pq;` 共 27 项生效；`z;`→`ctrl+z`、`y;`→`ctrl+y`、`a;`→`ctrl+a`、`c;`→`ctrl+c`、`v;`→`ctrl+v`、`s;`→`up`、`x;`→`down`、`g;`→`end`、`t;`→`home`、`w;`→`ctrl+shift+left`、`b;`→`ctrl+backspace`、`r;`→`ctrl+delete`、`h;`→`ctrl+home`、`e;`→`ctrl+end`、`d;`→`delete`、`kk;`→`ctrl+shift+k`、`aa;`→`ctrl+a,ctrl+c`、`dd;`→`ctrl+shift+backspace`、`ww;`→`ctrl+shift+left,ctrl+c`、`hh;`→`shift+home,ctrl+c`、`tt;`→`ctrl+shift+home,ctrl+c`、`gg;`→`ctrl+shift+end,ctrl+c`、`xx;`→`ctrl+x`、`ss;`→`shift+home`、`ff;`→`@to-symbol`、`f;`→`@search`；单字母码是"首键 kNoop、末键 kAccepted" | ✅ |
   | 前缀不对（`qz;`）/ 末键不对（`z,`）/ 只打一半（`z`）/ 带 Ctrl 的 `;` | 全程 kNoop，什么都不发 | ✅ |
   | `@defaults` 与 `@defaults xy;=ctrl+shift+right` | 分别生效 26 项 / 27 项，`revert.default_shortcuts` 表里有 26 项 | ✅ |
   | `pp;=@to-symbol-include`、`sm;=shift+home-shift+left`（v8 用 `-` 代替 `,`） | 归一化后按 `shift+home,shift+left` 原样交给 `press_keys` | ✅ |
   | `ff;=@to-symbol`（上屏「今天天气很好，我出门了」11 字，`，`在第 7 位） | 只删 4 个字符（保留 `今天天气很好，`），不发按键 | ✅ |
   | `ff;`（上屏「abc，def」/「abcdef」/「abc，」） | 分别删 3 / 6 / 0（末字符就是符号时记 `nothing to delete before the first symbol`） | ✅ |
   | `f;=@search`（上屏「abc」） | 调 `open_url`，URL = `https://www.baidu.com/s?wd=abc`；返回 2 / 0 时分别记 `open_url ignored (previous action still running or too soon)` / `open_url refused the address: …` | ✅ |
   | `actions/search_url` 换成必应模板 | URL = `https://www.bing.com/search?q=abc`（`%s` 被替换，中文按 UTF-8 百分号编码） | ✅ |
   | 一次都没上屏就按 `f;` | 不调 `open_url`，只记 `nothing to search, no commit recorded` | ✅ |
   | `actions/shortcuts` 里写重复触发码 | 两个都不生效并记 `duplicate shortcut trigger disabled: z;`，按键不吞 | ✅ |
   | 与 `actions/trigger` 撞码 / 触发码只有 1 字符 / 少写 `=` / `@未知动作` / 键名写错 | 分别记 `shortcut trigger equals actions/trigger`、`shortcut trigger too short`、`bad shortcut entry ignored`、`unknown built-in action disabled`、`bad key sequence disabled` | ✅ |
   | `press_keys` 返回 2 / 0 | 分别记 `press_keys ignored (previous burst still running or too soon)` / `press_keys refused the sequence: ctrl+z` | ✅ |
   | 同一个环境里三种触发方式同时配 | `mode=text+shortcuts`（含组合键时再加 `hotkey`），`qq/` 与 `z;` 都能用 | ✅ |

   **259 项断言全部 PASS**，`lua_close` 干净（v5 72 项；v6 136 项；v8 再 +38 项；v9 再 +8 项；v10 场景 I 再 +61 项 + `replace` 再 +3 项 + 发送键放行再 +9 项；
   v10 的场景 I 再 +65 项）。

   v10 新增的场景 I（AI 模式，12 组 61 条）覆盖：总开关关着时 `ai;` 不生效（`ai=off` +
   `ai shortcut ignored`）、`send_key` 默认 `/`（码 47）与 `double_ms=1000`、只发一个请求、
   请求里的 URL/model/`Bearer`/`max_tokens` 都对、第一下 `/` 放行、第二下才发（`pop_input` 摘掉那个字符）、没汉字不发、**取最后一句**（默认分隔符 `。！？` 不含标点）、
   `nudge` 带 `350/135`、上一个请求没回来时不再发（`previous request still running`）、
   结果落地写进记账（`ai=true`）且不注入退格、`replace` 删的是"上一组符号之后到光标"那一段
   （`我出门了，。` 6 个字 → 注入 6 次退格）、这一段含换行就不删、
   `HTTP 500` 只记错不插入、Esc 取消再 Esc 退出、`source=composition` 发输入框并清空、
   `ctrl+enter` 按一次即发、发送键写错就关掉 AI、旧 dll 缺 `http_request` 时关掉 AI 且别的功能照常、
   `eng;` 换上自定义提示词并带 `【上文】`、没内容可发时只记 `nothing to send`；
   以及断句（I13）：末尾那串符号整串跳过（`今天天气很好。我出门了，。` → 只发 `我出门了`）、
   句子之间连着一串 `。！？` 时取最后那句、整段只有符号时算"没内容"、`include_marks=true` 时
   额外把末尾那串符号补回去。

6. **v7 扫描码修复的验证**（三层，都不碰你的前台窗口）：

   - `..\probe_press_keys.py`（dry-run dll）：**36 个用例 ALL PASS** —— 12 个组合键 + 18 个裸命名键
     的事件串逐字节核对（`ctrl+shift+left` → `1Ds,2As,4Bsx,4Bsx-up,2As-up,1Ds-up`、`home` → `47sx`、
     `numpad5` → `4Cs`、`escape` → `01s`），6 个非法 spec 仍返回 `0`
   - `..\smoke_backspace.py`：**182 项断言仍全部 PASS**（场景 F 只数 `_dry_run_sent`、场景 H 用桩
     `press_keys` / `open_url` 断言 spec 字符串与 URL，所以事件串格式变化不影响它们）
   - `..\probe_scan_e2e.py`：**正式 dll**（用 rime.dll 自带的 Lua 载入）向自建测试窗口真注入：

     | 用例（`alpha beta gamma delta`，光标在末尾） | 修复前（只填 wVk） | 修复后（扫描码） |
     | --- | --- | --- |
     | `shift+left` | 21..21 只移光标 | **21..22 选中 `a`** ✅ |
     | `shift+home`（= `ss;`） | 0..0 只跳到行首 | **0..22 选中整行** ✅ |
     | `ctrl+shift+left`（= `ww;`） | 17..17 等于 Ctrl+Left | **17..22 选中 `delta`** ✅ |
     | `left`（对照） | 21..21 | 21..21，不选中 ✅ |
     | `ctrl+backspace`（= `bb;`） | 单行 EDIT 里插入一个字面 DEL | 同样插入 DEL（与旧写法**逐字一致**，不是回归） |

     最后一行只是这个极简 EDIT 控件自己的行为（`..\probe_ctrl_backspace_cmp.py` 把两种写法对比过，
     结果相同）；真实编辑器里多半是"删掉前一个词"，请在你自己常用的编辑器里确认 `bb;`。
     另外实验还量到：一次 `SendInput` 全塞 vs 分步 30 ms 发送**没有差别**，起作用的只有"有没有扫描码"。

   > 顺带核对过的对照实验（`..\probe_inject_variants.py`）：`V1/V4/V7/V9`（只填 wVk）分别表现为
   > "只移光标 / 只跳行首 / 等于 Ctrl+Left"，`V2/V5/V8/V10`（扫描码）分别选中 1 字符 / 整行 /
   > 前一个词。同一窗口里注入裸 `a` 会被小狼毫吃掉（说明测试窗口确实处在输入法的钩子之下，
   > 与你的环境一致）。

   > 另外发现一处**仅影响进程退出**的竞态：跑测试的宿主进程若在后台批次还没发完时退出，
   > 偶发 `0xC0000005`（断言早已全过、`lua_close` 也已干净返回）。诊断脚本
   > `..\probe_exit_av.py` 复现了它（大批次时有时崩、小批次不崩）；测试里改成"等批次跑完再退出"
   > 后连跑 3 次退出码全 0。**同性质的风险在产品侧也存在**：小狼毫退出时若正好在批次中，
   > 卸载竞态也可能触发访问冲突。彻底修法是 C 侧加 `DllMain(DLL_PROCESS_DETACH)` 等待工作线程
   > 结束（本版未做）。

7. **v8（`-` 分步 + `@search`）的验证**：

   - `..\smoke_backspace.py`：**182 项断言 ALL PASS**（v6 的 136 项、v8 的 174 项全部保留并通过，另加 8 项：
     26 个默认触发码逐个核对 spec、`f;` 的 URL 与两种失败日志、`-` 归一化、`actions/search_url`
     换成必应、没上屏就按 `f;` 不发请求）
   - `..\probe_prod_dll.py`（正式 dll）：`press_keys` / `press_backspace` / `open_url` 都是 function、
     没有 dry-run 钩子；7 个非法地址（空串、`ftp://x`、`file:///C:/x`、`nonesuch`、带空格、带 `|`、
     3000 字符）全部返回 `0` → **RESULT: ALL PASS**
   - `..\probe_open_url_e2e.py`（正式 dll + 真浏览器）：用 rime.dll 的 Lua 调
     `open_url("http://127.0.0.1:8765/probe-open-url.txt")`，本机起一个 `http.server` 当靶子，
     服务端日志出现 `"GET /probe-open-url.txt HTTP/1.1" 200` —— **证明 `ShellExecuteW`
     真的把默认浏览器拉起来了**（测试时弹过一次浏览器标签页，事后已关掉服务）

8. **v10（AI 模式）的验证**：

   - **C 源超集核对**：把 v9 的 `actions.c`（797 行）按行序在上游那份（1393 行）里逐行查，
     797 行**全部命中、0 处不匹配** → 换文件是纯增量，我们原有行为一行没丢
   - `..\smoke_backspace.py`：**259 项断言 ALL PASS**（v9 的 182 项一条没坏，场景 I 再 +77 项）
   - `..\probe_ai_dll.py`（正式 dll + rime.dll 自带的 Lua）：**24 条 ALL PASS** —— 7 个新函数
     都是 function；`http_request` 对空串/`ftp://` 返回 `0`；`send_text('')` 与 20001 字节返回 `0`；
     对**本脚本自己起的** `127.0.0.1` 服务真发一次 POST：`seq=1` → `done` → `http_result` 取回
     `{"choices":[{"message":{"content":"你好世界"}}]}` → 回到 `idle`，服务端收到了正确的
     path / body / `Authorization` 头；`HTTP 500` 走 `failed` 且错误串是 `HTTP 500: boom`；
     `clipboard_get` 在剪贴板为空时返回错误字符串；`nudge(-1, 0)` 回 `1`
   - 探针边界：`send_text` **从不带正文调用**（会真的把字打进当前窗口），`nudge` 只发一次
     空键 F24，`clipboard_get` 只读不写

**未做（重要）**：没有启动小狼毫、没有在 Weasel 里输入过 `qq/`；v7/v8 的"真注入"只发生在本目录
那几个探针**自己创建的测试窗口**里（`probe_scan_e2e.py` / `probe_inject_variants.py`，注入前会把
自己的窗口抢到前台，读完选区就关掉），**没有往你的任何窗口发过按键**；`open_url` 的真实验证
也只开了 `127.0.0.1` 上的测试页。v10 的 AI 链路只连过本机的假服务器，**没有连过真的模型接口**，
也没有在真机上验证过"回答落到光标处"、`replace` 删原文、`nudge` 唤醒这些真机手感。真正的
删除手感、`ss;`/`ww;`/`f;` 在你常用编辑器里的表现，以及 AI 模式的实际体验，仍必须由你部署后实测。

---

## 7. 部署（3 步）

1. **先停服务**：任务栏小狼毫图标右键 → 停止服务（或结束 `WeaselServer.exe`）。
   否则 `actions.dll` 正被加载，覆盖会失败（文件占用）。
2. 覆盖到 `C:\Users\Administrator\AppData\Roaming\Rime\lua\wanxiang\ziji\actions\`：
   - `actions.dll` ← 本目录那个（89186 B）
   - `actions.lua` ← 本目录那个（62091 B）
   - 想让 26 项快捷键生效，配置里要有一行 `"actions/shortcuts": "@defaults"`
   - 想让 AI 模式生效，还要 `"actions/ai/enabled": true` + 自己的接口地址和密钥
   > **v10 只换两个文件**：`actions.dll`（89186 B）+ `actions.lua`（62091 B）。配置里不写
   > `actions/ai/*` 的话，AI 完全不会启用，四种老功能的行为和 v9 一样。
   > 从 v9（36734 B）或更早升上来时同样两个文件都换；**千万别**只换 dll 不换 Lua：dll 还是好的，
   > 可只要 Lua 还挂在 `translators` 里，崩溃就照旧。
3. 启动服务并**重新部署**（右键 → 重新部署）。

**接线必须改（v4 起）**：`actions.lua` 现在是 processor，必须挂在 `engine/processors`。
`C:\Users\Administrator\AppData\Roaming\Rime\renwei_wx_zidingyi.yaml` 里那行要改成**放最前**：

```yaml
engine/processors/@before 0: lua_processor@*wanxiang.ziji.actions.revert
```

`@before 0` 由 librime 1.17.0 `config_data.cc:129`（`kBefore`）支持，效果是插到 processors
列表的第一位。也可以按序号插在后面（写法与同目录的 `empty_code_clear` 一致）：

```yaml
engine/processors/@after 15: lua_processor@*wanxiang.ziji.actions.revert
```

（原来的 `engine/translators/@after 1:: lua_translator@*wanxiang.ziji.actions.revert`
**要么删掉、要么改成上面这样**；注意只写一个冒号。`@after 15` 的含义是"插在第 16 项
（`navigator`）之后、`express_editor` 之前"，不是列表末尾。）

**位置怎么选（实测结论：放最前）**：

- 排在 `ascii_composer` / `recognizer` / `key_binder` / `speller` / `punctuator` 之后时，
  触发键可能在到达本组件之前就被链中间某个组件消费掉（`key_binder` 改写 `; , .`、
  `punctuator` 把符号当标点提交、`recognizer` 按前缀把串吃掉）——**实测表现就是"按了没反应"**。
  依据：`ascii_composer.cc` 里 `Ctrl/Alt/Super+键` 是 `return kNoop` 继续往下传的，所以
  英文模式下排在它后面的处理器也收得到组合键；但裸字母/符号会受到 `ascii_mode` 的
  `kRejected`（结束整条链）影响。
- 放最前（`@before 0`）后本组件先看到每一个按键：只有命中触发码/组合键时才 `kAccepted`
  吞掉这一键，其余一律 `kNoop` 放行，因此不会影响别的组件正常输入。
- 插到最前不会打乱已有组件的相对顺序：其它 `@after N` 补丁已先按各自位置插入，`@before 0`
  只是把它们整体后移一位。

**回滚**：把接线改回 `translators` 那行，并换回 processor 之前那一套（`actions.lua` 9588 B +
跟它配套的 dll）；或直接注释掉接线。只想退回"删字"功能、不要快捷键的话，把 `actions/shortcuts`
留空即可（不用换 dll）。

---

## 8. 调参（默认在 `actions.lua`，可用方案配置覆盖，不用重编）

| 配置项 | `actions.lua` 默认（`actions.lua:63` 的 `DEFAULTS`） | 作用 |
| --- | --- | --- |
| `actions/trigger` | `"qq/"` | 方式一的触发码，换 `aa/`、`q/` 就改这里 |
| `actions/hotkey` | `""`（不启用） | 方式二的组合键，如 `"Control+slash"`；填了它**不挤掉**别的触发方式，四种可以同时用 |
| `actions/shortcuts` | `""`（全不启用） | 方式三的快捷键表（`"z;=ctrl+z  ss;=shift+home"`，空格分隔、`触发码=动作`，多步用 `,` 或 `-`）；写 `"@defaults"` 展开 `actions.lua:127` 里那 26 项默认；`@to-symbol` / `@to-symbol-include` / `@delete` / `@search` / `@ai` / `@ai:<名字>` 是内置动作 |
| `actions/search_url` | `"https://www.baidu.com/s?wd=%s"` | `@search`（`f;`）打开的地址模板，`%s` 换成搜索词（中文自动 percent-encode） |
| `actions/symbols` | 一串中英标点（`DEFAULTS.symbols`，不含 `-` `_`） | `@to-symbol` 认定哪些字符算"符号"；支持 `\n` `\t` `\\` 转义 |
| `actions/dll_dir` | `"lua/wanxiang/ziji"` | dll 所在目录（相对 `<Rime 用户目录>`） |
| `actions/stack_size` | `50` | 最多能连撤多少次上屏 |
| `actions/max_backspaces` | `500` | 单次最多删多少字符（超出部分不删，日志记一条） |
| `actions/debounce_ms` | `200` | 触发按键之间的防抖（毫秒），只对方式一生效 |
| `actions/key_gap_ms` | `30` | **相邻两个按键之间**停多少毫秒（0~500，v9）；同一组合键内的修饰键/主键之间、多步之间都用它。程序反应不过来就调大（100~200），`0` = 一口气全发 |
| `actions/ai/enabled` | `false` | 方式四（AI）总开关；关着时 `ai;` 这类触发码不生效（记 `ai shortcut ignored (actions/ai/enabled is false)`） |
| `actions/ai/send_key` | `"/"` | 发送键：**单字符 = 连按两下**（`/`、`;`、`'` 都行）；带 `+` 如 `"ctrl+enter"` = **按一次即发** |
| `actions/ai/double_ms` | `1000` | 单字符发送键的双击窗口（毫秒，50~5000）；太短会把手快的两下当两次无关输入 |
| `actions/ai/source` | `"auto"` | 发什么：`auto`（有未上屏编码就发编码、没有就发已上屏）/ `composition` / `committed`（写别的值一律当 `auto`） |
| `actions/ai/sentence_marks` | `"。！？"` | 分隔符：**先把末尾那串符号（`actions/symbols` 认定的，含连续多个）和空白跳过**，再从后往前找最后一个分隔符、只发它后面那句；支持 `\n` `\t` `\\` 转义 |
| `actions/ai/include_marks` | `false` | 要不要把末尾跳过的那串符号补回去一起发（默认 false=只发句子本身） |
| `actions/ai/replace` | `false` | 结果回来后先删掉原文再打字。删的是"上一组符号之后到光标"这一整段（句子 + 末尾那串符号，长度由 `ai_last_sentence()` 的第二个返回值 `cut` 给出）；这一段里含 `\n` 就不删 |
| `actions/ai/history_chars` | `200` | 跟在后面的"上文"最多多少字（0~5000），取自本组件的上屏记账，跳过 AI 自己写进去的 |
| `actions/ai/provider` | `"online"` | 用哪一组接口配置：`online` / `local` |
| `actions/ai/online/base_url` | DeepSeek | OpenAI 格式的接口根地址，请求会打 `base_url` 本身 |
| `actions/ai/online/model` | `"deepseek-chat"` | 模型名 |
| `actions/ai/online/api_key` | `""` | 密钥，发成 `Authorization: Bearer <api_key>` |
| `actions/ai/online/thinking_disabled` | `true` | 发 `"thinking": {"type": "disabled"}` 关思考（DeepSeek/智谱认这个；不认的接口会忽略） |
| `actions/ai/online/max_tokens` | `1024` | 回话长度上限（1~100000） |
| `actions/ai/online/temperature` | `0.7` | 随机性（0~2，支持小数） |
| `actions/ai/local/*` | 空 / `localhost` | 同上 6 项，给本地 llama.cpp / Ollama 这类没有密钥的接口用 |
| `actions/ai/prompts/<名字>` | 内置 3 条 | 提示词；`@ai` 用 `default`，`@ai:eng` 用 `eng`，也可自己加 `actions/ai/prompts/polish` + `p;=@ai:polish` |
| `actions/ai/nudge` | `true` | 异步结果靠定时敲一个空键把引擎叫醒（Lua 只在按键时才会被调用，不叫醒结果要等你下次按键才出现） |
| `actions/ai/nudge_key` | `135` | 叫醒用哪个键（1~255）；`135` = `0x87` = F24（键盘上没有、输入法没绑、程序不响应） |
| `actions/ai/nudge_interval` | `350` | 多久敲一次（毫秒，50~60000） |
| `actions/ai/max_wait_s` | `60` | 一个请求最多等多久（秒，1~600），超时就取消 |
| `actions.c:35` | `MAX_BACKSPACES 10000` | dll 侧防呆上限（要超过 10000 才需重编） |

异步注入的固定参数（编在 dll 里，改要重编）：`actions.c:38-42` 的 `FIRE_DELAY_MS 80`、
`BATCH_SIZE 8`、`BATCH_GAP_MS 5`、`MIN_INTERVAL_MS 150`、`WORKER_POLL_MS 5`。

| 固定参数 | 值 | 作用 |
| --- | --- | --- |
| `FIRE_DELAY_MS` | 80 ms | 收到请求后先等这么久（让本次按键先处理完），再开始发 |
| `BATCH_SIZE` | 8 | 每次 `SendInput` 发几个退格 |
| `BATCH_GAP_MS` | 5 ms | 批与批之间歇多久 |
| `MIN_INTERVAL_MS` | 150 ms | 距上次注入不足这个时间的请求直接丢弃（返回 0）；`press_backspace` 和 `press_keys` 共用这一个窗口 |
| `STEP_GAP_MS` | 30 ms | `press_keys` **默认**的按键间隔：相邻两个按键事件之间、以及多步之间等多久（`actions.c:78`；实际值由 `actions/key_gap_ms` 传入） |
| `MAX_KEY_GAP_MS` | 500 | `press_keys` 第二参数的防呆上限（`:79`） |
| `MAX_CHORD_EVENTS` | 12 | 一步里最多几个键事件（`actions.c:68`） |
| `MAX_SPEC_LEN` | 255 | `press_keys` 接受的 spec 字符串上限（`actions.c:67`） |
| `MAX_URL_LEN` | 2048 | `open_url` 接受的 URL 上限（v8） |

覆盖示例（`xxx.custom.yaml`）：

```yaml
patch:
  "actions/trigger": "q/"
  "actions/hotkey": "Control+slash"          # 不写 = 方式二不启用
  "actions/shortcuts": "@defaults"           # 不写 = 方式三全不启用；见 使用说明.md 第三节
  "actions/search_url": "https://www.baidu.com/s?wd=%s"  # f; 用哪个搜索引擎
  "actions/symbols": "，。！？；：、,.!?;:"   # @to-symbol 认哪些字符
  "actions/stack_size": 50
  "actions/max_backspaces": 500
  "actions/debounce_ms": 200
  "actions/key_gap_ms": 30
  # ---- AI（不写这段就完全不启用）----
  "actions/ai/enabled": true
  "actions/ai/send_key": "/"                 # 连按两下 / 就发；写 "ctrl+enter" 则按一次即发
  "actions/ai/online/base_url": "https://api.deepseek.com/v1/chat/completions"
  "actions/ai/online/model": "deepseek-chat"
  "actions/ai/online/api_key": "sk-……"
  "actions/shortcuts": "@defaults ai;=@ai eng;=@ai:eng jp;=@ai:jp"  # 加三个进 AI 的触发码
```

读取与回落逻辑在 `actions.lua:1307` 的 `init(env)`（AI 那组在 `actions.lua:1204` 的 `init_ai(env, config)`；
整数/小数读用 `cfg_int` / `cfg_number`（`actions.lua:228`），布尔用 `cfg_bool`（`actions.lua:212`）；
组合键解析在 `actions.lua:1307` 之后，文本触发码的前缀/末键同理，`actions/search_url` 在同段，
快捷键表由 `actions.lua:379` 的 `parse_shortcuts()` 处理，`@defaults` 展开
`actions.lua:127` 的 `DEFAULT_SHORTCUTS`）：
配置为空/非法就用 `DEFAULTS`（`actions.lua:63`），负数与超大值夹到 `LIMITS`（`actions.lua:107`），
最终生效值打进 `actions.lua:1307` 之后那行日志（`mode=` / `trigger=` / `hotkey=` / `shortcuts=` /
`search_url=` / `key_gap_ms=` / `ai=`）。
触发处理在 `actions.lua:1403` 的 `func(key, env)`：先判 `key:release()`，AI 开着时先交给
`ai_handle_key()`（AI 模式下其余按键一律 `kNoop`），然后依次是
组合键分支 → **快捷键分支**（要求 `key` 不带修饰键、`context.input == action.prefix`、
`key.keycode == action.code`，命中就 `context:clear()` 后按动作类型分发）→ 主文本触发码分支
（前缀 + 末键 + `debounce`）。四种方式互不排斥。删字走 `actions.lua:572` 的 `fire_delete(env)`
与 `actions.lua:597` 的 `fire_to_symbol(env, include)`，发键走 `actions.lua:650` 的 `fire_keys(env, action)`，
搜索走 `actions.lua:689` 的 `fire_search(env)`，AI 发送/取回走 `actions.lua:946` / `actions.lua:1046`；
C 侧返回 0（被忽略）时由 `actions.lua:507` 的 `push_back()` 把那条记录放回栈尾。

触发码规则（**方式一和方式三的触发码都要**）：**每个字符都必须在 `speller/alphabet` 里**。`/` 与 `;`
都在 `wanxiang_pro.schema.yaml:276` 的 alphabet 末尾（`` ` `` `;` `/` `\`），所以 `qq/`、`aa/`、`z;`
直接可用，不用改方案；保持"不要以 `/` 开头"即可避开万象自己的 `/xxx` 触发码。
方式二（`actions/hotkey` 组合键）**不经过输入编码**，既不需要 alphabet、也不会被标点或按键绑定抢走。

---

## 9. 已知局限（"删字"这条路本身的代价）

1. **只能删"刚刚连续上屏"的文字**：上屏后只要动过光标（点鼠标、方向键、换行、切窗口），
   再触发就会删到别的地方。这是它最大的风险，Ctrl+Z 不会有这个问题。
2. **emoji / 代理对**：个别程序把 1 个 emoji 当 2 个 UTF-16 单位 → 少删 1 个；
   有些编辑器的"智能退格"一次吃掉一个字素簇 → 可能多删。
3. **含换行时**：多数程序退格删不掉换行，会一直删到上一行末尾。
4. 栈空时**不动作**（原版 `pop() or 1` 会误删 1 个字，这里已改）。
5. 与 Ctrl+Z 一样**无法跨程序**；`SendInput` 受 UIPI 限制——目标程序以管理员身份运行时注入会被
   系统拦截（与 dll 无关）。
6. 触发码 `qq/` 本身不会上屏（先 `engine.context:clear()` 再注入）。
7. **按键是延迟发出的**：约 80 ms 后才开始分批发（删 500 字总共约再花 0.3 s）。这段时间里
   别急着打字或点鼠标，否则可能删到别处。
8. **150 ms 内的重复触发会被忽略**（C 侧返回 0）：被忽略的那笔记录会留在栈里，稍后再按即可。
   这是为了避免连按把引擎搞乱（之前的崩溃就是这么来的）。
9. **方式三发的是"真按键"**，目标程序怎么解释由它自己决定：`Ctrl+Z` 在 Windows 控制台里是
   挂起/结束输入，bash 系 `Ctrl+Y` 是"粘贴"、`Ctrl+A` 是"到行首"，`Ctrl+Shift+Backspace`
   也不一定等于"删整行"；`ss;`（Shift+Home）依赖目标程序的 Home 语义（有的编辑器是"智能 Home"）。
   这类动作**不参与上屏记账**（`@to-symbol`、`@delete` 除外）。
10. **`@to-symbol` 只对"用输入法上屏的字"精确**：它按记账往回找符号，找不到就一路删到记账里
    最早的一笔；最新一笔的末字符本身就是符号时不删任何东西（日志 `nothing to delete before
    the first symbol`）。上屏之后动过光标、按过退格、换过窗口，位置就可能不对。
11. **`actions/shortcuts` 里重复的触发码一律不生效**（同一触发码写两遍、或与 `actions/trigger`
    撞码）——有意设计，避免"一个触发码两个动作"；日志会写明是哪一项被停用。
12. **`f;`（`@search`）会真的启动默认浏览器**：它用**最后一次上屏的原文**拼 URL，删过一部分或
    动过光标就可能搜到别的东西；`ShellExecuteW` 被安全软件/UIPI 拦住时不会打开，也不会报错给
    前台，只能看日志有没有 `search … -> …`。地址模板只接受 `http(s)://`，别的协议一律拒绝。
13. **`-` 与 `,` 完全等价**，两种写法混用也行（`shift+home-ctrl+c`、`shift+home,ctrl+c` 同一个
    结果）；每一步之间会等 30 ms，并按"修饰键按下 → 键按下 → 键抬起 → 修饰键抬起"发完再下一步。
14. **AI 模式要把中文发到网上**：`actions/ai/enabled` 一开，你选中的句子和最近 200 字上文都会
    发到你配的接口（默认 DeepSeek）。密钥以明文写在方案配置里，别提交、别外传；涉密内容别用。
15. **AI 的结果是"打到光标处"**：不经过候选窗，用 `SendInput` 逐字打（0.25 s 一个字，快也快不了）。
    这段时间鼠标焦点跑到别处，字就打进别的窗口；打断的办法是 Esc 或者别切窗口。
16. **只认 OpenAI 格式的回应**：解析 `choices[0].message.content`，别的格式（Gemini 原生、
    Anthropic 原生）拿到的会是 `request failed - 没找到 content`。
17. **异步结果靠敲空键唤醒**：Lua 只有按键时才被调用，所以后台拿到回答后会每隔 350 ms 敲一下
    F24 把你"叫醒"一次，直到结果落到光标处。极少数程序会对未知键有反应（如老游戏的快捷键），
    可以把 `actions/ai/nudge_key` 换成别的几乎没人用的键（如 `0x88` = F25）。
18. **AI 模式下除了 Esc / 发送键 / 别的 `@ai` 触发码，其余按键都不进正文**：这是有意的——
    避免回答还没落地时你打的字和它混在一起。要正常打字就按 Esc 或再按一次触发码退出。

---

## 10. 本机工具链坑（记录备查）

- gcc 必须让 `C:\msys64\mingw64\bin` 在 `PATH` 里，否则 `cc1.exe` **静默 exit 1**
  （`0xC0000135` STATUS_DLL_NOT_FOUND），编译器一句错误都不打印。
- 更早一次 `as.exe` 报 `0xC0000139`（STATUS_ENTRYPOINT_NOT_FOUND）：Windows 先在
  `System32` 命中旧的 `zlib1.dll`（缺 `uncompress2`），已通过把
  `C:\msys64\mingw64\bin\zlib1.dll` 复制到 `C:\msys64\mingw64\x86_64-w64-mingw32\bin\` 修复。
- `build.cmd` 必须是 ASCII 内容 + **CRLF** 换行；LF 会让 cmd 把括号块拆坏。
- 产物文件名必须是 `actions.dll`；改成别的名字时
  `require("actions")` 会报 `module 'actions' not found`。
