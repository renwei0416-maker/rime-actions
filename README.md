# rime-actions（上屏动作）

给小狼毫（Rime / Weasel）用的一个轻量的扩展，方便小狼毫可在系统层面执行回退，执行快捷键组合，以及连接在线模型和本地模型的能力。主要有四个功能模块。
1. 用默认的**“qq/”**或者任意自定义的编码组合，快速将误上屏的内容快速回退，而不需要连续按backspace回退，和ctrl+z的逻辑不一样，ctrl+z会还原成你上一次已删除的内容。
2. 默认配置了26项快捷键组合，比如用**z;**执行撤销，以及其它的快捷配置，比如删除整行、复制整行、光标移动等等。另外可任意配置自己需要的快捷键组合，这在不同的编辑其中用于快速操作很有助益。
3. 将最后一次上屏内容送到默认浏览器和配置的搜索引擎中搜索，默认的是**“f;”**，可以自己配置按键组合。
4. 可以任意配置的AI模式，比如输入**“AI;”**，进入AI模式，然后用  “//”将第一候选的内容发送给AI，AI会将返回的回答或者补全的诗词等输入光标处。或者上屏后也可以直接按 “//”将曾经上屏的前一句话（不是前一次上屏）
发送给AI,然后在光标处插入返回结果。也可配置成删除前一句话后再插入返回的内容。均可以任意配置你要使用的按键。还有其它如 “eng；”、“jp;”等，用于将我们的输入翻译成中文或日文等。可以配置任意多的AI模式，通过不同配置不同的按键组合以及不同的提示词让AI执行不同的返回。
5. 进入不同的AI模式后，还可以通过快捷组合比如“ctrl+;g”（默认的是ctrl+alt+;）,将你剪贴板的文字内容发给AI然后在光标处返回。比如进入的是“eng;”模式那就返回的是将你剪贴板的内容翻译成英文。
6. 拼音候选通过用“aiur;”进入AI拼音模式，AI可以根据上文分析候选框中的多个候选选择最合适的将其重排到首位，如果所有的候选都不合适，AI会根据拼音结合上文将最可能的结果返回到第一候选。原候选后移。
合适的或者生成一个更合适的排到第一位。

**这个扩展不会改变你的打字方式，每个模块具体使用那个按键组合都可以自定义。也可自己决定开启哪个模块**

## 功能

| 触发方式 | 例子 | 说明 |
| --- | --- | --- |
| 文本触发码 | `qq/` | 删掉刚上屏的那一段；连按可以一笔一笔往前撤 |
| 组合键 | `Ctrl+/` | 同上，改用组合键触发 |
| 快捷触发码 | `z;`、`ss;`、`f;` | 替你按常用快捷键（撤销、选到行首、搜索…） |
| AI 模式 | `ai;` 进入，打字后连按两下 `/` | 把文字发给模型，回复直接写到光标处 |
| 剪贴板 | AI 模式下 `Ctrl+Alt+;` | 把剪贴板里的文字直接发给模型（`actions/ai/clip_key` 可改） |
| AI 拼音候选 | `aiur;` 进入后照常打拼音 | 把拼音和已有候选交给模型，挑一个（或另造一个）排到候选第一位；选中才上屏；挑中的就是第一位时在该候选注释上显示标记 |

两个辅助开关：`"actions/history_ledger": true`（默认开，把万象快符这类绕过上屏通知的文字也补进上屏记录，否则回删会少删一个）、`"actions/debug": true`（默认 `false` 只写错误，排查时才打开）。

内置 26 项快捷触发码（默认不启用，写上 `"actions/shortcuts": "@defaults"` 才生效）：

| 触发码 | 动作 | 触发码 | 动作 |
| --- | --- | --- | --- |
| `z;` | `ctrl+z` 撤销 | `y;` | `ctrl+y` 重做 |
| `a;` | `ctrl+a` 全选 | `c;` | `ctrl+c` 复制 |
| `v;` | `ctrl+v` 粘贴 | `xx;` | `ctrl+x` 剪切 |
| `s;` | `up` 上移一行 | `x;` | `down` 下移一行 |
| `g;` | `end` 行尾 | `t;` | `home` 行首 |
| `w;` | `ctrl+shift+left` 选中前一个词 | `b;` | `ctrl+backspace` 删前一个词 |
| `r;` | `ctrl+delete` 删后一个词 | `h;` | `ctrl+home` 到文首 |
| `e;` | `ctrl+end` 到文末 | `d;` | `delete` 删除 |
| `kk;` | `ctrl+shift+k` 删整行 | `aa;` | `ctrl+a-ctrl+c` 复制全文 |
| `dd;` | `ctrl+shift+backspace` 删整行 | `ww;` | `ctrl+shift+left-ctrl+c` 复制前一个词 |
| `hh;` | `shift+home-ctrl+c` 复制整行 | `tt;` | `ctrl+shift+home-ctrl+c` 复制光标之上全部 |
| `gg;` | `ctrl+shift+end-ctrl+c` 复制光标之下全部 | `ss;` | `shift+home` 选中到行首 |
| `ff;` | `@to-symbol` 删到前一个符号 | `f;` | `@search` 搜索最后一次上屏 |

动作还可以是 `@to-symbol-include`（连符号一起删）、`@delete`（删最近一笔）、
`@ai` / `@ai:<名字>`（进入 AI 模式），以及任意多步序列（`shift+home-ctrl+c`）。

## 组成

| 文件 | 作用 |
| --- | --- |
| `actions.lua` | Rime Lua 组件（`lua_processor`）：记账、识别触发码、算出要删多少字 |
| `actions.c` / `actions.dll` | 很小的 C 模块：真正把按键发出去（退格 / 组合键 / 打开网址 / 请求接口 / 打字） |
| `ai_pinyin_capture.lua` | 可选滤波器（`lua_filter`）：读候选的词典拼音，写进 context 属性 |
| `ai_pinyin_insert.lua` | 可选滤波器（`lua_filter`）：把 AI 挑出的词插到候选第一位；结果与第一位相同时给第一位加标记 |

> ⚠️ 它会向当前窗口**注入真实按键**，也会改动文档内容。先在记事本里试。

## 安装

1. 到 [Releases](../../releases) 下载 x64 包（`rime-actions-<版本>-x64.zip`，里面是
   `actions.dll` 和 `actions.lua`），解压即用；也可以照 `docs/重新编译说明.md` 自己编一份。
   目前只提供 x64 版本，ARM64 版小狼毫需要自己重编。
2. 两个文件放进同一个目录，例如 `%APPDATA%\Rime\lua\myfolder\`（目录名随你）。
3. 在方案的 `*.custom.yaml` 里接线并告诉它 dll 在哪：

   ```yaml
   patch:
     "engine/processors/@before 0": lua_processor@*myfolder.actions
     "actions/dll_dir": "lua/myfolder"      # 相对 Rime 用户目录，用正斜杠
   ```

   文件在 `lua\myfolder\actions.lua`，模块名就是 `myfolder.actions`。
   放在处理器链最前面是有意的：排在 `recognizer`、`key_binder`、`punctuator` 后面时，
   触发码可能先被它们吃掉。
4. （可选）用 AI 拼音候选时，把 `ai_pinyin_capture.lua`、`ai_pinyin_insert.lua` 放进同一目录，
   再接两行滤波器 —— 一个要排在 `super_comment_preedit` 之前（那时候选注释里还是词典拼音），
   一个排在 `uniquifier` 之前：

   ```yaml
   patch:
     "engine/filters/@before 5": lua_filter@*myfolder.ai_pinyin_capture
     "engine/filters/@before 17": lua_filter@*myfolder.ai_pinyin_insert
     "actions/ai_pinyin/enabled": true
   ```

   两个 `@before` 的数字按你方案里 `engine/filters` 的条目数算：`capture` 要插在
   `super_comment_preedit` 前面，`insert` 要在 `uniquifier` 前面。下标不对时日志里会写
   `module … not found`，或候选注释里读不到拼音、一个请求都不发。
5. 托盘图标右键 → **重新部署**。

## 配置

全部配置项、默认值、日志与排查见 **[docs/使用说明.md](docs/使用说明.md)**；
可直接抄的完整示例见 [examples/actions.sample.yaml](examples/actions.sample.yaml)。

## 自检

```bat
python tests\smoke_backspace.py
```

需要 Python 3.12、MSYS2 mingw64 gcc、装好的小狼毫。脚本先现编一份 `-DACTIONS_DRY_RUN`
的"只记账、不注入"的 dll，再跑 426 项断言（触发码、记账、退格扣账、去抖、26 项快捷键、多步、
删到符号、浏览器搜索、AI、AI 拼音候选、配置校验）。路径可用环境变量覆盖：
`MINGW_BIN`、`WEASEL_DIR`、`RIME_DLL`、`DLLDIR`。

全部通过时输出 `CHECKS: 426 asserted, ALL PASS`，进程退出码 0。

## 限制

- 注入的是**真按键**：终端里的 `Ctrl+Z` 是挂起、`Ctrl+Y` / `Ctrl+A` 语义也不同；
  管理员权限窗口受 UIPI 拦截，注入不进去。
- `@to-symbol` 只对"用输入法上屏的字"精确：之后按过退格、动过光标、换过窗口就会偏。
- `ss;` 依赖目标程序自己的 `Home` 语义；`dd;` 不是所有程序都表示"删整行"。
- `Ctrl+Shift+←/→` 可能被 Windows 的"切换键盘布局"热键抢走：到
  「设置 → 时间和语言 → 输入 → 高级键盘设置 → 输入语言热键」把它改成**未分配**，
  之后用 `Win+Space` 切输入法。
- AI 会把文字发到网上（只有开启并触发时才发），回复直接写进当前光标处。iur; 的 AI 拼音候选同样要联网，只在停手后发一次拼音和已有候选。
- AI 拼音候选只对候选注释里带拼音的词有效：自定义短语、日期、英文、super_replacer 派生出来的候选通常没有拼音，会直接跳过（不发请求）。它给的是候选，选中才上屏。
- 只测过 x64 的小狼毫；ARM64 需要重新编译。

## 许可

MIT，见 [LICENSE](LICENSE)。第三方作品与署名见 [NOTICE](NOTICE)。
