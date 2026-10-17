# rime-revert

给小狼毫（Rime / Weasel）用的"后悔药"：

- **撤回已经上屏的字**（打完一长串才发现打错、或者选错了字）；
- **用拼音编码打出一串自定义"触发码"**，让它替你按常用快捷键（`z;` = Ctrl+Z、`ss;` = Shift+Home…）；
- **删到前一个符号**（`ff;`：从光标往回删到最近的标点，标点本身留着）；
- **用浏览器搜索**（`f;`：拿最后一次上屏的原文去百度搜，搜索引擎可配）。

它由两部分组成：

| 文件 | 作用 |
| --- | --- |
| `revert.lua` | Rime Lua 组件（`lua_processor`）：记账、识别触发码、决定删几个字 |
| `sendKeyCode.c` / `sendKeyCode.dll` | 很小的 C 模块：真正把按键发出去（退格 / 任意组合键） |

> ⚠️ 它会向当前窗口**注入真实按键**（退格、Ctrl+Z、Shift+Home…）。
> 请先在记事本、编辑器里试，别拿重要文档首发；它也可能在终端里触发终端的语义（见"限制"）。

## 它能做什么

### 方式一：撤回上屏（默认启用）

打**文本触发码**：先正常打出一串拼音（编码），最后再打末尾那个字符。

默认触发码是 `qq/`，可以改成 `aa/` 之类。触发后它会按记账把你刚上屏的那一笔删掉：

- `revert/stack_size`（默认 50）：能连续撤回多少笔；
- `revert/max_backspaces`（默认 500）：单次最多删多少个字符；
- `revert/debounce_ms`（默认 200）：连按保护，只对文本触发码生效。
- `revert/key_gap_ms`（默认 30，范围 0~500）：方式三发按键时，**相邻两个按键之间**停多少毫秒
  （同一组合键里的修饰键与主键之间、多步动作之间都算）。某个程序反应不过来（只走第一步、
  Shift 像没按住）就调大，比如 100~200；写 0 就是一口气全发。

### 方式二：组合键撤回（默认关闭）

写上 `revert/hotkey` 就启用，例如 `"Control+slash"`。它和方式一**可以同时用**；
只想留组合键的话，把 `revert/trigger` 写成 `none`。

### 方式三：快捷触发码（默认关闭）

在 `revert/shortcuts` 里写 `"触发码=动作"`。写 `"@defaults"` 可以一次启用脚本里预置的 26 项：

| 触发码 | 动作 | 触发码 | 动作 |
| --- | --- | --- | --- |
| `z;` | Ctrl+Z 撤销 | `y;` | Ctrl+Y 重做 |
| `a;` | Ctrl+A 全选 | `c;` | Ctrl+C 复制 |
| `v;` | Ctrl+V 粘贴 | `s;` | ↑ 光标上移一行 |
| `x;` | ↓ 光标下移一行 | `g;` | End 跳到行尾 |
| `t;` | Home 跳到行首 | `w;` | Ctrl+Shift+← 选中前一个词 |
| `b;` | Ctrl+Backspace 删前一个词 | `r;` | Ctrl+Delete 删后一个词 |
| `h;` | Ctrl+Home 跳到文首 | `e;` | Ctrl+End 跳到文末 |
| `d;` | Delete 删除 | `kk;` | Ctrl+Shift+K 删除整行（VS Code 等） |
| `xx;` | Ctrl+X 剪切 | `aa;` | Ctrl+A → Ctrl+C 复制全文 |
| `dd;` | Ctrl+Shift+Backspace | `ww;` | Ctrl+Shift+← → Ctrl+C 复制前一个词 |
| `hh;` | Shift+Home → Ctrl+C 复制整行 | `tt;` | Ctrl+Shift+Home → Ctrl+C 复制光标之上 |
| `gg;` | Ctrl+Shift+End → Ctrl+C 复制光标之下 | `ss;` | Shift+Home 选到行首 |
| `ff;` | 删到前一个符号（保留符号） | `f;` | 打开浏览器搜最后一次上屏 |

> 单字母触发码很好用，但收得也宽：中文输入状态下**任何"字母 + `;`"都会被这套表接管**。
> 担心误触就别用 `"@defaults"`，只写自己要的那几项。

动作除了按键序列，还可以是：

- `@to-symbol`：删到前一个符号，**保留**符号（`ff;` 就是它）；
- `@to-symbol-include`：删到前一个符号，**连符号一起删**；
- `@delete`：和文本触发码一样，删掉最近一笔上屏；
- `@search`：用浏览器搜索最后一次上屏的内容（`f;` 就是它，地址模板见 `revert/search_url`，
  默认百度）；
- 多步序列，用逗号 `,` 或短横线 `-` 分开，例如 `"shift+home,shift+left"`、
  `"shift+home-ctrl+c"`（两者等价：一步发完、全部松开，等 30ms 再发下一步）。

"符号"由 `revert/symbols` 决定。

### 规则（不满足就不生效，并写一条错误日志）

- 触发码**至少 2 个字符**，且字符要在方案的 `speller/alphabet` 里（默认含 `;` `/` 等）；
- 只有"已经拼出的编码**恰好等于前缀**、这一键是末字符"才触发，所以正常打字不会被误触；
- 触发码**重复**（包括和 `revert/trigger` 撞码）时，涉及的那些项**一律不生效**；
- 快捷键（方式三）不参与 `revert/debounce_ms`，但 dll 内部有 150ms 硬保护。

## 安装

1. **拿到 `sendKeyCode.dll`**
   到 [Releases](https://github.com/renwei0416-maker/rime-revert/releases) 下载（v1.1.0 的 `sendKeyCode.dll`
   是 36734 字节，sha256 `c1d95fc430dc24abd879777c73430ffad0a13815c9333f33dcf0f558e01c6b2b`），
   或者照 `docs/重新编译说明.md` 自己编一份。

2. **放进 Rime 用户目录**
   把 `revert.lua` 和 `sendKeyCode.dll` 放同一个目录，例如
   `%APPDATA%\Rime\lua\myfolder\` —— 目录名随你，但**要和接线里的模块名对上**：
   文件在 `lua\myfolder\revert.lua`，接线就写 `lua_processor@*myfolder.revert`。
   然后把这行通知给组件：

   ```yaml
   "revert/dll_dir": "lua/myfolder"      # 相对 Rime 用户目录，用正斜杠
   ```

3. **接线**（让它在按键处理链里拿到按键）
   在方案 yaml 的 `engine/processors` 里，把它**放在最前面**：

   ```yaml
   engine/processors/@before 0: lua_processor@*myfolder.revert
   ```

   放在最前面是有意的：`recognizer`、`key_binder`、`punctuator` 等都在链的中间，
   排在它们后面时触发码可能先被它们吃掉（表现为"按了没反应"）。

4. **配好 `revert/*`**（见 `examples/revert.sample.yaml` 可以直接抄），
   然后托盘图标右键 → **重新部署**。

5. 排查（按了没反应 / 删的位置不对 / 快捷键发出去程序没响应）看
   `docs/使用说明.md` 的排查一节。

## 目录结构

```
rime-revert/
├─ revert.lua              Lua 组件（主体）
├─ sendKeyCode.c           C 模块源码
├─ build.cmd               Windows 一键编译（MSYS2 mingw64 + Weasel）
├─ include/                Lua 5.4 头文件（编译用，随仓库提供以便离线编译）
├─ tests/                  离线自检脚本（用 dry-run dll，不注入真实按键）
├─ docs/
│  ├─ 使用说明.md          配置项、日志、排查表 —— 先看这个
│  ├─ BUILD.md             设计与实现细节、逐项验证记录
│  └─ 重新编译说明.md       自己编译的完整步骤（工具链、命令、常见错误）
└─ examples/               配置示例
```

## 自检

```bat
python tests\smoke_backspace.py
```

需要 Python 3.12、MSYS2 的 mingw64 gcc、以及装好的小狼毫。
脚本会先现编一份 `-DSENDKEYCODE_DRY_RUN` 的"只记账、不注入"的 dll，然后在 Lua 里跑 182 项断言
（触发码、记账、去抖、26 项快捷键、多步、删到符号、浏览器搜索、配置校验…）。可用环境变量覆盖路径：
`MINGW_BIN`、`WEASEL_DIR`、`RIME_DLL`、`DLLDIR`。

> 说明：脚本进程退出时可能返回非 0（`0xC0000005`），那是 dry-run dll 后台线程在进程退出阶段的
> 已知竞态（`lua_close: clean` 与 `ALL PASS` 都已打印）。以输出里的
> `CHECKS: 182 asserted, ALL PASS` 为准。

## 已测环境与限制

- Windows + 小狼毫 Weasel 0.17.4（librime 1.17.0，Lua 5.4）。
- 注入的是**真按键**：控制台/终端里的 `Ctrl+Z` 是挂起、`Ctrl+Y` / `Ctrl+A` 语义也和编辑器不同；
  管理员权限窗口受 UIPI 拦截，注入不进去。
- `@to-symbol` 只对"用输入法上屏的字"精确；之后按过退格、动过光标、换过窗口就会偏。
- `ss;`（Shift+Home）依赖目标程序自己的 Home 语义（可能是"智能 Home"）；
  `dd;`（Ctrl+Shift+Backspace）也不是所有程序都表示"删整行"。
- `f;` 会**真的启动默认浏览器**，搜索词是"最后一次上屏的原文"：之后删过一部分、动过光标，
  搜到的就可能不是你以为的那段；搜索引擎地址由 `revert/search_url` 决定（默认百度）。
- 多步动作（`hh;`、`ww;`、`tt;`、`gg;`、`aa;`）与组合键内部的按键之间都有间隔，默认 30ms，
  由 `revert/key_gap_ms` 控制（0~500）。个别程序对连续按键不敏感时，偶发只执行了第一步，
  把 `revert/key_gap_ms` 调大（100~200）即可，不用重编。
- `Ctrl+Shift+←/→` 可能被 Windows 自己的"切换键盘布局"热键抢走（表现为换成别的输入法）：
  到「设置 → 时间和语言 → 输入 → 高级键盘设置 → 输入语言热键」把该项改成**未分配**即可，
  之后用 `Win+Space` 切输入法。
- 只测过 x64 的小狼毫；ARM64 需要重新编译。

## 许可

MIT，见 [LICENSE](LICENSE)。本项目包含/依赖的第三方作品与署名见 [NOTICE](NOTICE)。
