# rime-actions（上屏动作）

给小狼毫（Rime / Weasel）用的一个扩展：字上屏之后，用几个触发码把它**撤回、选中、复制、删除**，
或者拿去搜索、交给 AI 处理。

它不改变打字方式，只在你敲出触发码时替你按几下键。

## 功能

| 触发方式 | 例子 | 说明 |
| --- | --- | --- |
| 文本触发码 | `qq/` | 删掉刚上屏的那一段；连按可以一笔一笔往前撤 |
| 组合键 | `Ctrl+/` | 同上，改用组合键触发 |
| 快捷触发码 | `z;`、`ss;`、`f;` | 替你按常用快捷键（撤销、选到行首、搜索…） |
| AI 模式 | `ai;` 进入，打字后连按两下 `/` | 把文字发给模型，回复直接写到光标处 |

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
4. 托盘图标右键 → **重新部署**。

## 配置

全部配置项、默认值、日志与排查见 **[docs/使用说明.md](docs/使用说明.md)**；
可直接抄的完整示例见 [examples/actions.sample.yaml](examples/actions.sample.yaml)。

## 自检

```bat
python tests\smoke_backspace.py
```

需要 Python 3.12、MSYS2 mingw64 gcc、装好的小狼毫。脚本先现编一份 `-DACTIONS_DRY_RUN`
的"只记账、不注入"的 dll，再跑 265 项断言（触发码、记账、去抖、26 项快捷键、多步、
删到符号、浏览器搜索、AI、配置校验）。路径可用环境变量覆盖：
`MINGW_BIN`、`WEASEL_DIR`、`RIME_DLL`、`DLLDIR`。

> 脚本进程退出时可能返回 `0xC0000005`，那是 dry-run dll 后台线程在进程退出阶段的已知竞态
> （`lua_close: clean` 与 `ALL PASS` 都已打印）。以输出里的
> `CHECKS: 265 asserted, ALL PASS` 为准。

## 限制

- 注入的是**真按键**：终端里的 `Ctrl+Z` 是挂起、`Ctrl+Y` / `Ctrl+A` 语义也不同；
  管理员权限窗口受 UIPI 拦截，注入不进去。
- `@to-symbol` 只对"用输入法上屏的字"精确：之后按过退格、动过光标、换过窗口就会偏。
- `ss;` 依赖目标程序自己的 `Home` 语义；`dd;` 不是所有程序都表示"删整行"。
- `Ctrl+Shift+←/→` 可能被 Windows 的"切换键盘布局"热键抢走：到
  「设置 → 时间和语言 → 输入 → 高级键盘设置 → 输入语言热键」把它改成**未分配**，
  之后用 `Win+Space` 切输入法。
- AI 会把文字发到网上（只有开启并触发时才发），回复直接写进当前光标处。
- 只测过 x64 的小狼毫；ARM64 需要重新编译。

## 许可

MIT，见 [LICENSE](LICENSE)。第三方作品与署名见 [NOTICE](NOTICE)。
