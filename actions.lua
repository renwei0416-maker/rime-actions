-- rime-actions / 上屏动作
-- Rime 处理器：管理"文字上屏之后"的四种动作。
--
-- 组件类型：processor（在翻译流程最前面接收原始按键）。
--
-- 四种触发方式（可任选，也可同时启用；默认只启用文本触发码）：
--   1) 文本触发码 actions/trigger（默认 "qq/"）—— 删掉最近一次上屏的文字；
--   2) 组合键 actions/hotkey —— 同上，用单个组合键触发；
--   3) 快捷触发码 actions/shortcuts —— 每个触发码对应一串原生按键或内置动作；
--   4) AI 模式 actions/ai/* —— 把最后一句或输入框里的编码交给大模型，结果打到光标处。
--
-- 安装位置（两个文件放同一目录）：
--   <Rime 用户目录>/lua/wanxiang/ziji/actions.lua
--   <Rime 用户目录>/lua/wanxiang/ziji/actions.dll
--
-- 接线（<方案>.custom.yaml）：
--   patch:
--     "engine/processors/@before 0": lua_processor@*wanxiang.ziji.actions
--
-- 配置项（全部可选）：
--   actions/trigger          文本触发码；写 none/off/- 关闭
--   actions/hotkey           组合键删字，如 "Control+slash"；不填=不启用
--   actions/shortcuts        快捷触发码表，如 "z;=ctrl+z ss;=shift+home ff;=@to-symbol"；不填=不启用
--   actions/symbols          @to-symbol 认定的符号集合
--   actions/dll_dir          actions.dll 所在目录（相对 <Rime 用户目录>）
--   actions/search_url       @search 的地址模板，%s 处替换成搜索词
--   actions/stack_size       可连续撤回的笔数上限（默认 50）
--   actions/max_backspaces   单次最多删多少字符（默认 500）
--   actions/debounce_ms      文本触发码的连按保护，毫秒（默认 200）
--   actions/key_gap_ms       相邻两个按键之间的间隔，毫秒（默认 30，0..500）
--   actions/ai/enabled       是否启用 AI 模式（默认 false）
--   其余 actions/ai/* 参数见使用说明。
--
-- actions/shortcuts 写法：每项 "触发码=动作"，空格或换行分隔。
--   动作类型：
--     按键序列            "ctrl+z"、"shift+home"
--     多步序列            "shift+home-ctrl+c"（- 与 , 等价；每步按键全部抬起后再发下一步）
--     @to-symbol          删到前一个符号，保留符号
--     @to-symbol-include  删到前一个符号，连同符号一起删
--     @delete             删掉最近一次上屏的文字
--     @search             用默认浏览器搜索最后一次上屏的文字
--     @ai / @ai:<名字>    进入 AI 模式，使用 actions/ai/prompts/<名字> 的提示词
--   写 "@defaults" 表示启用脚本内置的默认表，可与自定义项混写。
--
-- 规则：
--   * 触发码至少 2 个字符，且字符要在方案的 speller/alphabet 内；
--     只有"已拼出的编码恰好等于前缀、本次按键是末字符"时才触发。
--   * 触发码重复（含与 actions/trigger 相同）时，重复的项不生效并记录错误日志。
--   * 快捷触发码不受 actions/debounce_ms 影响；dll 内部另有 150 毫秒最短间隔。
-- ===========================================================================
-- --------------------------------------------------------------- 日志开关
-- 默认只写 error；actions/debug: true 时才写 info（排查用）
local DEBUG = false
local host_log = log
local log = {
    info = function(m)
        if DEBUG and host_log and host_log.info then host_log.info(m) end
    end,
    warning = function(m)
        if host_log and host_log.warning then host_log.warning(m) end
    end,
    error = function(m)
        if host_log and host_log.error then host_log.error(m) end
    end,
}
local DEFAULTS = {
    debug          = false,
    trigger        = "qq/",
    hotkey         = "",
    shortcuts      = "",
    search_url     = "https://www.baidu.com/s?wd=%s",
    symbols        = "，。！？；：、,.!?;:\\\"“”‘’…—～「」『』【】《》〈〉（）()［］[]｛｝{}<>+*/\\\\|&^%$#@~`·°±×÷",
    dll_dir        = "lua/wanxiang/ziji",
    stack_size     = 50,
    max_backspaces = 500,
    debounce_ms    = 200,
    key_gap_ms     = 30,

    -- 补记"绕过上屏通知"的文字：万象快符这类由 Lua 直接调 engine:commit_text()
    -- 打出来的符号只进 librime 的 context.commit_history，不触发
    -- context.commit_notifier，单靠通知会漏掉它们，回删就少删一个字。
    -- 开启后每次按键读一次 context.commit_history 把新增记录补进账本，用于回删。
    history_ledger = true,

    -- AI（可选，默认关闭；不写 actions/ai/enabled: true 就不会启用）
    ai_enabled     = false,
    ai_send_key    = "/",
    ai_double_ms   = 1000,
    ai_clip_key    = "ctrl+alt+semicolon",
    ai_clip_max_chars = 4000,
    ai_source      = "auto",
    ai_marks       = "。！？",
    ai_include_marks = false,
    ai_replace     = false,
    ai_skip_own    = true,
    ai_history_chars = 200,
    ai_nudge       = true,
    ai_nudge_key   = 135,
    ai_nudge_interval = 350,
    ai_max_wait_s  = 60,
    ai_provider    = "online",
    ai_online_url  = "https://api.deepseek.com/chat/completions",
    ai_online_model = "deepseek-chat",
    ai_online_key  = "",
    ai_online_think_off = false,
    ai_online_max_tokens = 1024,
    ai_online_temperature = 0.7,
    ai_local_url   = "http://127.0.0.1:11434/v1/chat/completions",
    ai_local_model = "qwen2.5:7b",
    ai_local_key   = "",
    ai_local_think_off = false,
    ai_local_max_tokens = 1024,
    ai_local_temperature = 0.7,

    -- AI 拼音候选（可选，默认关闭）：打字时把 AI 补出的词挂到候选第一位
    ai_pinyin_enabled = false,
    ai_pinyin_trigger = "aiur;",
    ai_pinyin_debounce = 400,
    ai_pinyin_context_chars = 160,
    ai_pinyin_max_chars = 12,
    ai_pinyin_send_candidates = 5,
    ai_pinyin_marker = "AI",
    ai_pinyin_retry = 10000,
}

-- 安全上限：只用来挡住异常大的配置值，正常使用碰不到。
-- max_backspaces 不要超过 actions.c 里的 MAX_BACKSPACES（当前 10000）；
-- key_gap_ms 不要超过 actions.c 里的 MAX_KEY_GAP_MS（当前 500）。
local LIMITS = {
    stack_size     = 1000,
    max_backspaces = 10000,
    debounce_ms    = 10000,
    key_gap_ms     = 500,

    ai_double_ms   = 5000,
    ai_clip_max_chars = 20000,
    ai_history_chars = 5000,
    ai_nudge_key   = 255,
    ai_nudge_interval = 60000,
    ai_max_wait_s  = 600,
    ai_max_tokens  = 100000,

    ai_pinyin_debounce = 5000,
    ai_pinyin_max_chars = 32,
    ai_pinyin_send_candidates = 50,
    ai_pinyin_retry = 600000,
}

-- 把 actions/trigger 写成下面任一个值 = 关掉文本触发（只留组合键 / 快捷键）
local TRIGGER_OFF = { none = true, off = true, ["-"] = true }

-- 脚本里预置的快捷键表：默认一个都不启用，只有在 custom 里写
--   "actions/shortcuts": "@defaults"
-- （或把它抄进自己的配置里）之后才生效。
local DEFAULT_SHORTCUTS = {
    { "z;",  "ctrl+z" },                  -- 撤销
    { "y;",  "ctrl+y" },                  -- 重做
    { "a;",  "ctrl+a" },                  -- 全选
    { "c;",  "ctrl+c" },                  -- 复制
    { "v;",  "ctrl+v" },                  -- 粘贴
    { "s;",  "up" },                      -- 上移一行
    { "x;",  "down" },                    -- 下移一行
    { "g;",  "end" },                     -- 跳到行尾
    { "t;",  "home" },                    -- 跳到行首
    { "w;",  "ctrl+shift+left" },         -- 选中前一个词
    { "b;",  "ctrl+backspace" },          -- 删前一个词
    { "r;",  "ctrl+delete" },             -- 删后一个词
    { "h;",  "ctrl+home" },               -- 跳到文首
    { "e;",  "ctrl+end" },                -- 跳到文末
    { "d;",  "delete" },                  -- 删除（Del）
    { "kk;", "ctrl+shift+k" },            -- 删除整行（VS Code 等）
    { "aa;", "ctrl+a-ctrl+c" },           -- 复制全文
    { "dd;", "ctrl+shift+backspace" },    -- 删整行（并非所有程序都是"删整行"）
    { "ww;", "ctrl+shift+left-ctrl+c" },  -- 复制前一个词
    { "hh;", "shift+home-ctrl+c" },       -- 复制整行
    { "tt;", "ctrl+shift+home-ctrl+c" },  -- 复制光标之上所有内容
    { "gg;", "ctrl+shift+end-ctrl+c" },   -- 复制光标之下所有内容
    { "xx;", "ctrl+x" },                  -- 剪切
    { "ss;", "shift+home" },              -- 从光标选到行首
    { "ff;", "@to-symbol" },              -- 删到前一个符号（保留符号）
    { "f;",  "@search" },                 -- 用浏览器搜索最后一次上屏的内容
}

-- 处理器返回值：2 = 我不处理，交给后面的组件；1 = 我处理了，后面的都别管
local kNoop = 2
local kAccepted = 1

local Actions = nil -- 由下面的 load_module() 填上
local load_error = nil

-- ===========================================================================
-- 键名表（必须和 actions.c 里认的那套一致，否则这里会误判成"配置写错"）
-- ===========================================================================
local KEY_NAMES = {
    -- 非打印键用 X11 keysym（Rime 的按键码就是 keysym，不是 Windows 的 VK）
    backspace = 0xFF08, tab = 0xFF09, ["return"] = 0xFF0D, enter = 0xFF0D,
    escape = 0xFF1B, space = 0x20, delete = 0xFFFF, insert = 0xFF63,
    home = 0xFF50, ["end"] = 0xFF57, pageup = 0xFF55, pagedown = 0xFF56,
    left = 0xFF51, up = 0xFF52, right = 0xFF53, down = 0xFF54,
    exclam = 0x21, quotedbl = 0x22, numbersign = 0x23, dollar = 0x24, percent = 0x25,
    ampersand = 0x26, apostrophe = 0x27, parenleft = 0x28, parenright = 0x29,
    asterisk = 0x2A, plus = 0x2B, comma = 0x2C, minus = 0x2D, period = 0x2E, slash = 0x2F,
    colon = 0x3A, semicolon = 0x3B, less = 0x3C, equal = 0x3D, greater = 0x3E,
    question = 0x3F, at = 0x40, bracketleft = 0x5B, backslash = 0x5C,
    bracketright = 0x5D, asciicircum = 0x5E, underscore = 0x5F, grave = 0x60,
    braceleft = 0x7B, bar = 0x7C, braceright = 0x7D, asciitilde = 0x7E,
}

-- press_keys 认的键名（单字母/单数字另算）
local KEYS_FOR_SPEC = {
    backspace = true, tab = true, ["return"] = true, enter = true, escape = true,
    esc = true, space = true, pageup = true, pagedown = true, ["end"] = true, home = true,
    left = true, up = true, right = true, down = true, insert = true, delete = true,
    del = true, capslock = true, minus = true, equal = true, comma = true, period = true,
    slash = true, semicolon = true, quote = true, grave = true, bracketleft = true,
    bracketright = true, backslash = true,
}
for i = 1, 12 do KEYS_FOR_SPEC["f" .. i] = true end
for i = 0, 9 do KEYS_FOR_SPEC["numpad" .. i] = true end

local MODIFIERS = {
    ctrl = true, control = true, shift = true, alt = true, win = true,
    super = true, cmd = true,
}

-- ---------------------------------------------------------------- 工具函数
local function cfg_string(config, key, default)
    if not config then return default end
    local value = config:get_string(key)
    if type(value) == "string" and value ~= "" then return value end
    return default
end

local function cfg_int(config, key, default, minimum, maximum)
    if not config then return default end
    local value = config:get_int(key)
    if type(value) ~= "number" then return default end
    if minimum and value < minimum then value = minimum end
    if maximum and value > maximum then value = maximum end
    return value
end

-- 开关：配 true/false 都认；配成字符串 "true"/"false" 也认（防止 YAML 里加了引号）
local function cfg_bool(config, key, default)
    if not config then return default end

    local ok, value = pcall(function() return config:get_bool(key) end)
    if ok and type(value) == "boolean" then return value end

    local text = config:get_string(key)
    if type(text) == "string" then
        local lower = text:lower()
        if lower == "true" or lower == "yes" or lower == "on" or lower == "1" then return true end
        if lower == "false" or lower == "no" or lower == "off" or lower == "0" then return false end
    end
    return default
end

-- 小数（温度这类）：先试 get_double，再退回字符串，最后退回整数
local function cfg_number(config, key, default, minimum, maximum)
    if not config then return default end

    local value
    local ok, double = pcall(function() return config:get_double(key) end)
    if ok and type(double) == "number" then value = double end

    if value == nil then
        local text = config:get_string(key)
        if type(text) == "string" then value = tonumber(text) end
    end
    if value == nil then
        local integer = config:get_int(key)
        if type(integer) == "number" then value = integer end
    end
    if type(value) ~= "number" then return default end

    if minimum and value < minimum then value = minimum end
    if maximum and value > maximum then value = maximum end
    return value
end

-- 把字符串切成 UTF-8 字符数组；不是合法 UTF-8 时返回 nil
local function to_chars(text)
    if type(text) ~= "string" or text == "" then return {} end

    local chars, n = {}, 0
    local ok = pcall(function()
        for _, code in utf8.codes(text) do
            n = n + 1
            chars[n] = utf8.char(code)
        end
    end)
    if not ok then return nil end
    return chars
end

local function head_of(entry, keep)
    if keep <= 0 then return nil end
    if keep >= entry.n then return entry end

    local chars = to_chars(entry.text)
    if not chars then return nil end
    return { n = keep, text = table.concat(chars, "", 1, keep) }
end

-- "Control+slash" / "Ctrl+/" / "Alt+Z" -> { ctrl, shift, alt, super, keycode }
local function parse_hotkey(spec)
    if type(spec) ~= "string" or spec == "" then return nil end

    local parsed = { ctrl = false, shift = false, alt = false, super = false, keycode = nil }
    for token in spec:gmatch("[^+]+") do
        local name = token:lower()
        if name == "ctrl" or name == "control" then
            parsed.ctrl = true
        elseif name == "shift" then
            parsed.shift = true
        elseif name == "alt" then
            parsed.alt = true
        elseif name == "super" or name == "win" or name == "cmd" then
            parsed.super = true
        elseif #token == 1 then
            parsed.keycode = string.byte(token)
        else
            parsed.keycode = KEY_NAMES[name]
        end
    end

    if not parsed.keycode then return nil end
    return parsed
end

local function hotkey_matches(key, spec)
    if not spec then return false end
    if key:release() then return false end
    if key.keycode ~= spec.keycode then return false end
    if key:ctrl() ~= spec.ctrl then return false end
    if key:shift() ~= spec.shift then return false end
    if key:alt() ~= spec.alt then return false end
    if key:super() ~= spec.super then return false end
    return true
end

-- 检查一条"按键序列"（"ctrl+z"、"shift+home,shift+left"）是否合法。
-- 这里只是提前把写错的配置捞出来（报错+不生效），真正解析仍在 dll 里做。
local function valid_key_spec(spec)
    if type(spec) ~= "string" or spec == "" then return false end
    -- 逗号分隔每一步：空步（开头、结尾、连写）都不算合法
    if spec:find("^,") or spec:find(",$") or spec:find(",,", 1, true) then return false end

    for step in spec:gmatch("[^,]+") do
        -- 多余的 +：开头、结尾、连写
        if step:find("^%+") or step:find("%+$") or step:find("%+%+") then return false end

        local keys = 0
        for token in step:gmatch("[^+]+") do
            local name = token:lower()
            if MODIFIERS[name] then
                -- 修饰键
            elseif #name == 1 then
                if not name:match("[a-z0-9]") then return false end
                keys = keys + 1
            elseif KEYS_FOR_SPEC[name] then
                keys = keys + 1
            else
                return false
            end
        end
        if keys ~= 1 then return false end
    end
    return true
end

-- 把 dll 所在目录加进 package.cpath，然后加载模块
local function add_windows_module_path(dll_dir)
    local api = rime_api
    if not api or not api.get_user_data_dir then
        return false, "rime_api.get_user_data_dir() is unavailable"
    end

    local user_dir = api.get_user_data_dir()
    if not user_dir or user_dir == "" then
        return false, "Rime user data directory is empty"
    end

    -- 正斜杠在 Windows 上也能用
    local entry = user_dir .. "/" .. dll_dir .. "/?.dll"
    if not package.cpath:find(entry, 1, true) then
        package.cpath = entry .. ";" .. package.cpath
    end
    return true
end

local function load_module(dll_dir)
    local path_ok, path_err = add_windows_module_path(dll_dir)
    if not path_ok then
        Actions, load_error = nil, tostring(path_err)
    else
        local ok, result = pcall(require, "actions")
        if ok and type(result) == "table" then
            Actions, load_error = result, nil
        else
            Actions, load_error = nil, tostring(result)
        end
    end

    if not Actions and log and log.error then
        log.error("actions.lua: failed to load actions: " .. tostring(load_error))
    end
end

-- --------------------------------------------------- 快捷键表（方式三）解析
-- 返回动作表：{ trigger, prefix, code, kind = "keys"|"symbol"|"delete", spec, include }
local function parse_shortcuts(spec, env)
    if type(spec) ~= "string" or spec == "" then return {} end

    local entries = {}
    for token in spec:gmatch("%S+") do
        if token == "@defaults" then
            for _, item in ipairs(DEFAULT_SHORTCUTS) do
                entries[#entries + 1] = { trigger = item[1], action = item[2] }
            end
        else
            local trigger, action = token:match("^([^=]+)=(.+)$")
            if trigger and action then
                entries[#entries + 1] = { trigger = trigger, action = action }
            elseif log and log.error then
                log.error("actions.lua: bad shortcut entry ignored (want 触发码=动作): " .. token)
            end
        end
    end

    -- 重复的触发码：涉及的那些项都不生效（用户明确要求）
    local count = {}
    for _, item in ipairs(entries) do
        local key = item.trigger:lower()
        count[key] = (count[key] or 0) + 1
    end

    local main = env.revert_trigger
    if type(main) == "string" then main = main:lower() end

    local actions = {}
    for _, item in ipairs(entries) do
        local trigger = item.trigger
        local lower = trigger:lower()
        local action = {
            trigger = trigger,
            prefix  = trigger:sub(1, #trigger - 1),
            code    = string.byte(trigger, #trigger),
        }

        if count[lower] > 1 then
            if log and log.error then
                log.error("actions.lua: duplicate shortcut trigger disabled: " .. trigger)
            end
        elseif #trigger < 2 then
            if log and log.error then
                log.error("actions.lua: shortcut trigger too short (need >= 2 chars) disabled: "
                    .. trigger)
            end
        elseif main and env.revert_final_code and lower == main then
            if log and log.error then
                log.error("actions.lua: shortcut trigger equals actions/trigger, disabled: " .. trigger)
            end
        elseif item.action == "@search" then
            action.kind = "search"
        elseif item.action == "@to-symbol" then
            action.kind, action.include = "symbol", false
        elseif item.action == "@to-symbol-include" then
            action.kind, action.include = "symbol", true
        elseif item.action == "@delete" then
            action.kind = "delete"
        elseif item.action:sub(1, 3) == "@ai" then
            -- @ai / @ai:eng / @ai:任意名字 —— 名字对应一份 actions/ai/prompts/<名字>
            action.kind = "ai"
            action.prompt = item.action:match("^@ai[:%s]+(.+)$")
            if action.prompt then
                action.prompt = action.prompt:gsub("%s+$", "")
            else
                action.prompt = "default"
            end
        elseif item.action:sub(1, 1) == "@" then
            if log and log.error then
                log.error("actions.lua: unknown built-in action disabled: " .. item.action)
            end
        else
            -- 多步可以写成 "shift+home-ctrl+c"（用户习惯的 -），发给 dll 前统一成 ","
            local normalized = item.action:gsub("%-", ",")
            if valid_key_spec(normalized) then
                action.kind, action.spec = "keys", normalized
            elseif log and log.error then
                log.error("actions.lua: bad key sequence disabled: trigger=" .. trigger
                    .. " action=" .. tostring(item.action))
            end
        end

        if action.kind then actions[#actions + 1] = action end
    end
    return actions
end

local function make_symbol_set(text)
    local set = {}
    if type(text) ~= "string" then return set end

    -- 允许 \n \t \\ 这样的写法，方便把换行/制表符也算作"符号"
    local body = text:gsub("\\n", "\n"):gsub("\\t", "\t"):gsub("\\\\", "\\")
    for _, ch in ipairs(to_chars(body) or {}) do
        set[ch] = true
    end
    return set
end

-- ------------------------------------------------------------- 上屏字数记账
-- 栈里每一项 = { n = 字符数, text = 上屏的原文 }；text 是给"删到前一个符号"用的
local function trim_stack(env)
    local stack = env.revert_stack
    if not stack then return end

    local stack_size = env.revert_stack_size or DEFAULTS.stack_size
    while #stack > stack_size do
        table.remove(stack, 1)
    end
end

local function ai_preview(text, limit)
    if type(text) ~= "string" then return "" end
    limit = limit or 40
    if (utf8.len(text) or 0) <= limit then return text end
    local stop = utf8.offset(text, limit + 1)
    return stop and (text:sub(1, stop - 1) .. "…") or text
end

local function push_commit(env, text, own)
    local length = utf8.len(text)
    if not length or length < 1 then return end

    local stack = env.revert_stack
    if not stack then return end

    stack[#stack + 1] = { n = length, text = text, ai = own }
    trim_stack(env)
end

-- 通知已经记过的文字：读历史时跳过，免得同一笔上屏被记两次
local function owe_commit(env, text)
    local owed = env.hist_owed
    if not owed then return end
    owed[#owed + 1] = text
    while #owed > 8 do table.remove(owed, 1) end
end

-- 补记快符这类"绕过上屏通知"的文字。
-- 万象快符走 env.engine:commit_text()，文字进 librime 的 context.commit_history
-- （type = "raw"），但不触发 context.commit_notifier，所以光靠通知会漏掉它们。
-- 每次按键读一次 commit_history，把上次之后新增的记录补进账本，回删才数得准。
-- type = "thru" 是没被吃掉的按键，不是上屏文字，跳过；历史最多 20 条，所以每键跟进。
local function sync_history(env, ctx)
    if not env.hist_ready or not ctx then return end

    local hist = ctx.commit_history
    if not (hist and hist.to_table) then return end

    local ok, all = pcall(hist.to_table, hist)
    if not ok or type(all) ~= "table" then return end

    local seen = env.hist_seen or 0
    if #all < seen then seen = 0 end

    for i = seen + 1, #all do
        local rec = all[i]
        local text = rec and rec.text
        if type(text) == "string" and text ~= "" and rec.type ~= "thru" then
            local owed = env.hist_owed
            if owed and #owed > 0 and owed[1] == text then
                table.remove(owed, 1)
            else
                push_commit(env, text, nil)
                if log and log.info and env.ai_mode then
                    log.info(string.format("actions.lua: history n=%d text=%s",
                        utf8.len(text) or 0, ai_preview(text, 16)))
                end
            end
        end
    end
    env.hist_seen = #all
end

local function on_commit(text, env)
    if not text or text == "" then return end

    local length = utf8.len(text)
    if not length or length < 1 then return end

    local stack = env.revert_stack
    if not stack then return end

    -- 结果打进光标的字会重新变成"上屏"，标成 ai；只认答案里当前位置上的那一段，
    -- 位置对不上的一律算用户自己打的（同一串字出现在答案里也不会被误判）
    local own = nil
    if env.ai_last_text and env.ai_typing_until
        and os.clock() <= env.ai_typing_until + 2 then
        local pos = env.ai_text_pos or 1
        if env.ai_last_text:sub(pos, pos + #text - 1) == text then
            own = true
            env.ai_text_pos = pos + #text
        end
    end

    if log and log.info and env.ai_mode then
        log.info(string.format("actions.lua: commit n=%d own=%s text=%s",
            length, tostring(own), ai_preview(text, 16)))
    end

    if own then
        -- 自己打进去的答案要立刻记账（带 ai 标记），历史里那条同名的跳过
        push_commit(env, text, true)
        owe_commit(env, text)
    elseif not env.hist_ready then
        -- 读不到历史：只能靠上屏通知这一条路
        push_commit(env, text, nil)
    end
    -- 能读历史时这里不记：sync_history 会按历史顺序补进来（快符也在同一条序列里）

    -- 这笔文字一上屏，之前按过的回车就都跑到它前面去了，不再算"光标前的空行"
    env.ai_newlines = 0
end

-- 把记录放回栈尾：dll 拒绝了这次请求时用，免得白按
local function push_back(env, entry)
    local stack = env.revert_stack
    if not stack or not entry then return end

    stack[#stack + 1] = entry
    trim_stack(env)
end

-- 从最新一笔往回扣掉 count 个字符（注入成功后调用，保证账目和实际删除一致）
local function consume_from_stack(env, count)
    local stack = env.revert_stack
    if not stack or count < 1 then return end

    local remaining = count
    while remaining > 0 and #stack > 0 do
        local entry = stack[#stack]
        if entry.n <= remaining then
            remaining = remaining - entry.n
            table.remove(stack)
        else
            local kept = head_of(entry, entry.n - remaining)
            if kept then
                stack[#stack] = kept
            else
                table.remove(stack)
            end
            remaining = 0
        end
    end
end

-- 真正去发退格：被拒（返回 0）或者抛错就什么账都不动，稍后再按一次即可
local function inject_backspaces(env, count)
    if not Actions or not Actions.press_backspace then
        if log and log.error then
            log.error("actions.lua: press_backspace is unavailable"
                .. (load_error and (" (" .. tostring(load_error) .. ")") or ""))
        end
        return false
    end

    local ok, result = pcall(Actions.press_backspace, count)
    if not ok then
        if log and log.error then
            log.error("actions.lua: press_backspace failed: " .. tostring(result))
        end
        return false
    end
    if result == 0 then
        if log and log.info then
            log.info("actions.lua: request ignored (previous burst still running or too soon)")
        end
        return false
    end

    -- 记账以 dll 实际接受的字符数为准（正常就等于 count）
    local accepted = count
    if type(result) == "number" and result >= 1 and result <= count then
        accepted = result
    end
    if accepted < count and log and log.info then
        log.info(string.format("actions.lua: dll accepted %d of %d backspaces", accepted, count))
    end
    consume_from_stack(env, accepted)

    -- 注入的退格会重新进输入法钩子：这段时间内别把它们当成"手按退格"再扣一次
    env.bs_ignore = (env.bs_ignore or 0) + accepted
    env.bs_ignore_until = os.clock() + 3
    return true
end

-- 按 max_backspaces 截断后注入；label 只用于超限日志
local function run_delete(env, count, label)
    local limit = env.revert_limit or DEFAULTS.max_backspaces
    if count > limit then
        if log and log.info then
            log.info(string.format("actions.lua: %s truncated to max_backspaces=%d", label, limit))
        end
        count = limit
    end
    return inject_backspaces(env, count)
end

-- 删掉最近一笔上屏（文本触发码 / 组合键 / @delete 都走这里）
local function fire_delete(env)
    local stack = env.revert_stack
    local entry = stack and stack[#stack]
    if not entry or not entry.n or entry.n < 1 then
        -- 还没上屏过（或者已经撤完了）：什么也不做，免得删掉你手打的内容
        if log and log.info then
            log.info("actions.lua: nothing to delete, no commit recorded")
        end
        return
    end

    local count = entry.n
    run_delete(env, count, string.format("commit of %d chars", count))
end

-- 删到光标前第一个"符号"为止（@to-symbol / @to-symbol-include）
local function fire_to_symbol(env, include)
    local stack = env.revert_stack
    if not stack or #stack == 0 then
        if log and log.info then
            log.info("actions.lua: nothing to delete, no commit recorded")
        end
        return
    end

    local symbols = env.revert_symbols or make_symbol_set(DEFAULTS.symbols)

    -- 从最新一笔往回数，直到遇到符号
    local need, found = 0, false
    for i = #stack, 1, -1 do
        local chars = to_chars(stack[i].text)
        if chars then
            for k = #chars, 1, -1 do
                if symbols[chars[k]] then
                    found = true
                    if include then need = need + 1 end
                    break
                end
                need = need + 1
            end
        else
            -- 不是合法 UTF-8 的记账：按整笔长度算，别把它晾在那儿
            need = need + stack[i].n
        end
        if found then break end
    end

    if need < 1 then
        if log and log.info then
            log.info("actions.lua: nothing to delete before the first symbol")
        end
        return
    end

    -- 成功注入后 consume_from_stack 已经按"实际删掉的字符数"扣过账了
    run_delete(env, need, string.format("%d chars to the previous symbol", need))
end

-- 发一串原生按键（快捷键动作）
local function fire_keys(env, action)
    if not Actions or not Actions.press_keys then
        if log and log.error then
            log.error("actions.lua: press_keys is unavailable"
                .. (load_error and (" (" .. tostring(load_error) .. ")") or ""))
        end
        return
    end

    -- 按键节奏：相邻两个按键之间等多少毫秒（旧版 dll 会忽略这个参数）
    local gap = env.revert_key_gap or DEFAULTS.key_gap_ms
    local ok, result = pcall(Actions.press_keys, action.spec, gap)
    if not ok then
        if log and log.error then
            log.error("actions.lua: press_keys failed: " .. tostring(result))
        end
    elseif result == 2 then
        if log and log.info then
            log.info("actions.lua: press_keys ignored (previous burst still running or too soon)")
        end
    elseif result == 0 then
        if log and log.error then
            log.error("actions.lua: press_keys refused the sequence: " .. action.spec)
        end
    elseif log and log.info then
        log.info("actions.lua: shortcut " .. action.trigger .. " -> " .. action.spec)
    end
end

-- 把一段 UTF-8 文本转成百分号编码（浏览器地址栏要的就是这个）
local function percent_encode(text)
    if type(text) ~= "string" or text == "" then return "" end

    return (text:gsub("[^A-Za-z0-9%-%._~]", function(ch)
        return string.format("%%%02X", string.byte(ch))
    end))
end

-- 打开默认浏览器搜索"最后一次上屏"的原文（@search）
local function fire_search(env)
    if not Actions or not Actions.open_url then
        if log and log.error then
            log.error("actions.lua: open_url is unavailable"
                .. (load_error and (" (" .. tostring(load_error) .. ")") or ""))
        end
        return
    end

    local stack = env.revert_stack
    local entry = stack and stack[#stack]
    local text = entry and entry.text
    if type(text) ~= "string" or text == "" then
        if log and log.info then
            log.info("actions.lua: nothing to search, no commit recorded")
        end
        return
    end

    local encoded = percent_encode(text)
    local template = env.revert_search_url or DEFAULTS.search_url
    local url
    if template:find("%%s", 1) then
        url = template:gsub("%%s", function() return encoded end)
    else
        -- 模板里没写 %s：把词接在末尾，至少不会把查的内容丢掉
        url = template .. encoded
    end

    local ok, result = pcall(Actions.open_url, url)
    if not ok then
        if log and log.error then
            log.error("actions.lua: open_url failed: " .. tostring(result))
        end
    elseif result == 2 then
        if log and log.info then
            log.info("actions.lua: open_url ignored (previous action still running or too soon)")
        end
    elseif result == 0 then
        if log and log.error then
            log.error("actions.lua: open_url refused the address: " .. tostring(url))
        end
    elseif log and log.info then
        log.info("actions.lua: search " .. text .. " -> " .. url)
    end
end

-- ===========================================================================
-- AI（可选，默认关）：把"输入框里的字"或"已经上屏的字"发给大模型，
-- 结果直接打到光标处。只有 actions/ai/enabled 为 true、并且写了 @ai 触发码时才生效。
-- ===========================================================================

local AI_PROMPTS = {
    default = "你是中文输入助手。如果是问题就进行精准简短的回复，如果是诗词或名句就回复原文和作者等。"
        .. "只输出结果正文，不要解释、不要提问、不加引号。"
        .. "每一次接收到的内容，单独回复不要理会之前提交的内容。",
    eng     = "Translate the user's Chinese text into natural, idiomatic English. Output only the translation, no explanations, no quotes.",
    jp      = "把用户的中文翻译成自然、地道的日语。只输出译文，不要解释，不要加引号。",
}

-- 发送键写成键名时（"enter" 之类）用的别名
local AI_KEY_ALIASES = {
    enter = 0x0D, ["return"] = 0x0D, esc = 0x1B, escape = 0x1B,
    space = 0x20, tab = 0x09, backspace = 0x08,
}

-- 组合键写法里，"enter" 要换成 Rime 的键名 "return"
local AI_KEY_NAMES = {
    enter = "return", ["return"] = "return", esc = "escape", escape = "escape",
}

local function ai_trim(text)
    if type(text) ~= "string" then return "" end
    return (text:gsub("^%s+", ""):gsub("%s+$", ""))
end

-- 有没有汉字：基本区 U+4E00–U+9FFF 与扩展 A 区 U+3400–U+4DBF；
-- 全角标点（，。！？等）不算汉字
local function ai_has_cjk(text)
    local chars = to_chars(text)
    if not chars then return false end
    for _, ch in ipairs(chars) do
        local ok, cp = pcall(utf8.codepoint, ch)
        if ok and cp then
            if (cp >= 0x4E00 and cp <= 0x9FFF) or (cp >= 0x3400 and cp <= 0x4DBF) then
                return true
            end
        end
    end
    return false
end

local function ai_safe_str(fn)
    local ok, value = pcall(fn)
    if ok and type(value) == "string" then return value end
    return ""
end

-- 日志里只放前 limit 个字，别把整段正文写进日志
local function ai_composition_text(ctx)
    if not ctx then return "" end

    local commit = ai_safe_str(function() return ctx:get_commit_text() end)

    local sel = ""
    local ok, cand = pcall(function() return ctx:get_selected_candidate() end)
    if ok and type(cand) == "table" and type(cand.text) == "string" then sel = cand.text end

    local pre = ai_safe_str(function()
        local p = ctx:get_preedit()
        if type(p) == "table" then return p.text end
    end)

    local pick
    if ai_has_cjk(commit) then pick = commit
    elseif ai_has_cjk(sel) then pick = sel
    elseif ai_has_cjk(pre) then pick = pre
    elseif commit ~= "" then pick = commit
    elseif sel ~= "" then pick = sel
    else pick = pre end

    if pick == "" then pick = ctx.input or "" end
    return pick
end

-- 把分隔符字符串变成"字符集合"（支持 \n \t \\ 写法）
local function ai_mark_set(text)
    local set = {}
    if type(text) ~= "string" or text == "" then return set end
    local body = text:gsub("\\n", "\n"):gsub("\\t", "\t"):gsub("\\\\", "\\")
    for _, ch in ipairs(to_chars(body) or {}) do set[ch] = true end
    return set
end

-- 最近这一段的原文：从账本尾部往前拼上屏的文字，拼到已经出现断句符为止
-- （末尾那串符号不算断句符）。遇到 AI 自己写进去的那条也停：前面是本轮回答，
-- 不属于用户这次的输入。
-- 倒序排列的字符里，跳过末尾那串断句符/符号/空白之后，前面还有没有断句符
local function ai_has_mark_before_tail(rev, marks, symbols)
    if not marks or not rev then return false end
    local j = 1
    while j <= #rev do
        local ch = rev[j]
        if (marks[ch] ~= nil) or (symbols and symbols[ch] ~= nil)
            or (ch:match("^%s$") ~= nil) then
            j = j + 1
        else
            break
        end
    end
    for x = j, #rev do
        if marks[rev[x]] ~= nil then return true end
    end
    return false
end

local function ai_current_text(env)
    local stack = env.revert_stack or {}
    local symbols = env.revert_symbols or make_symbol_set(DEFAULTS.symbols)
    local marks = env.ai_marks
    local parts, rev, total = {}, {}, 0
    for i = #stack, 1, -1 do
        local entry = stack[i]
        if entry and type(entry.text) == "string" and entry.text ~= "" then
            if env.ai_skip_own and entry.ai then break end
            table.insert(parts, 1, entry.text)
            total = total + #entry.text
            local chars = to_chars(entry.text)
            if chars then
                -- rev 是倒序（新的在前）
                for k = #chars, 1, -1 do rev[#rev + 1] = chars[k] end
                -- 末尾那串断句符/符号/空白跳过后，前面还有断句符就到此为止
                if ai_has_mark_before_tail(rev, marks, symbols) then break end
            end
            if total >= 2000 then break end
        end
    end
    return table.concat(parts, "")
end

-- 上下文一律按"字"算（history_chars 也是字数）
local function ai_chars(text)
    if type(text) ~= "string" then return 0 end
    return utf8.len(text) or #text
end

local ai_last_sentence

-- 之前已经答过的问答：往回找 AI 写进去的那一条当答案，它前面连续上屏的非 AI 内容当问题
-- （问题可能分几次上屏，所以往回拼到断句符为止，规则与"要发的句子"一致）；总字数不超过 budget
local function ai_history_pairs(env, budget)
    local stack = env.revert_stack or {}
    local out = {}
    local used = 0
    local i = #stack
    local marks, symbols = env.ai_marks, env.ai_symbols
    local include = env.ai_include_marks

    while i >= 1 do
        local answer = stack[i]
        if answer and answer.ai and type(answer.text) == "string" and answer.text ~= "" then
            local parts, oldest = {}, nil
            local revc = {}
            local j = i - 1
            while j >= 1 do
                local entry = stack[j]
                if not entry or entry.ai or type(entry.text) ~= "string" or entry.text == "" then
                    break
                end
                parts[#parts + 1] = entry.text
                oldest = j
                local chars = to_chars(entry.text)
                if chars then
                    for k = #chars, 1, -1 do revc[#revc + 1] = chars[k] end
                end
                if #parts >= 2 and ai_has_mark_before_tail(revc, marks, symbols) then
                    break
                end
                j = j - 1
            end

            if oldest then
                local joined = {}
                for k = #parts, 1, -1 do joined[#joined + 1] = parts[k] end
                local question = ai_last_sentence(table.concat(joined), marks, symbols, include)
                local need = ai_chars(question) + ai_chars(answer.text)
                if used + need > budget then break end
                if question ~= "" then
                    used = used + need
                    table.insert(out, 1, { q = question, a = answer.text })
                    i = oldest - 1
                else
                    i = i - 1
                end
            else
                i = i - 1
            end
        else
            i = i - 1
        end
    end
    return out
end

-- 从后往前找最后一个分隔符，取它之后的内容；一个都没有就整段都要
-- 返回：要发的句子、如果 replace 要往回删几个字符、断句点之前的上文、以及"断句点之后到末尾"
-- 的原文（replace 只在这一段没有换行时才动手）
ai_last_sentence = function(text, marks, symbols, include_marks)
    local chars = to_chars(text)
    if not chars or #chars == 0 then return "", 0, "", "" end

    local function is_mark(ch)
        if not marks then return false end
        return marks[ch] ~= nil
    end

    local function is_symbol(ch)
        if not symbols then return false end
        return symbols[ch] ~= nil
    end

    local function is_space(ch)
        return ch:match("^%s$") ~= nil
    end

    -- 末尾那串符号（以及夹在中间的空白）属于"上一段"的结尾，先跳过去不算
    local last = #chars
    while last >= 1 and (is_mark(chars[last]) or is_symbol(chars[last])
                         or is_space(chars[last])) do
        last = last - 1
    end
    if last < 1 then return "", 0, "", table.concat(chars) end

    local tail = table.concat(chars, "", last + 1, #chars)

    local boundary = 0
    for i = last, 1, -1 do
        if is_mark(chars[i]) then
            boundary = i
            break
        end
    end

    local from = boundary + 1
    while from <= last and is_space(chars[from]) do from = from + 1 end

    local body = ""
    if from <= last then body = table.concat(chars, "", from, last) end
    if include_marks then body = body .. tail end

    -- replace 要删的是"断句点之后到末尾"这一整段（句子 + 末尾那串符号），
    -- 这样新文字就落在原来那句的位置上
    local before = boundary > 0 and table.concat(chars, "", 1, boundary) or ""
    local whole_span = table.concat(chars, "", boundary + 1, #chars)

    return body, #chars - boundary, before, whole_span
end

-- 只留最后 limit 个字符（按字符截，不会切坏 UTF-8）
local function ai_tail(text, limit)
    if limit <= 0 or type(text) ~= "string" then return "" end
    local chars = to_chars(text)
    if not chars or #chars <= limit then return text end
    return table.concat(chars, "", #chars - limit + 1, #chars)
end

local function json_escape(text)
    if type(text) ~= "string" then return "" end
    return (text:gsub('[%z\1-\31\\"]', function(ch)
        if ch == '"' then return '\\"' end
        if ch == "\\" then return "\\\\" end
        if ch == "\n" then return "\\n" end
        if ch == "\r" then return "\\r" end
        if ch == "\t" then return "\\t" end
        return string.format("\\u%04x", string.byte(ch))
    end))
end

-- 从 JSON 里取一个字符串字段（够用就行：找 "名字":"..." 再逐字符解转义）
local function json_string_field(body, name)
    if type(body) ~= "string" or type(name) ~= "string" then return nil end

    local _, value_start = body:find('"' .. name .. '"%s*:%s*"')
    if not value_start then return nil end

    local out = {}
    local i = value_start + 1
    while i <= #body do
        local ch = body:sub(i, i)
        if ch == '"' then return table.concat(out) end
        if ch == "\\" then
            local nxt = body:sub(i + 1, i + 1)
            if nxt == "u" then
                local hex = body:sub(i + 2, i + 5)
                local code = tonumber(hex, 16)
                local step = 6
                if code and code >= 0xD800 and code <= 0xDBFF then
                    local low = body:sub(i + 6, i + 11)
                    local lo = low:match("^\\u(%x%x%x%x)")
                    if lo then
                        local lcode = tonumber(lo, 16)
                        if lcode and lcode >= 0xDC00 and lcode <= 0xDFFF then
                            code = 0x10000 + (code - 0xD800) * 0x400 + (lcode - 0xDC00)
                            step = 12
                        end
                    end
                end
                if code then
                    local ok, char = pcall(utf8.char, code)
                    if ok then out[#out + 1] = char end
                end
                i = i + step
            else
                local map = { n = "\n", r = "\r", t = "\t", b = "\b", f = "\f",
                              ['"'] = '"', ["\\"] = "\\", ["/"] = "/" }
                out[#out + 1] = map[nxt] or nxt
                i = i + 2
            end
        else
            out[#out + 1] = ch
            i = i + 1
        end
    end
    return table.concat(out)
end

-- "/" → 连按两次；"//" → 同上；"ctrl+enter" → 组合键，按一次就发
local function parse_send_key(spec)
    if type(spec) ~= "string" or spec == "" then return nil end

    if spec:find("+", 1, true) then
        -- 组合键里允许写 enter/esc 这类口语名字
        local pieces, count = {}, 0
        for piece in spec:gmatch("[^+]+") do
            count = count + 1
            pieces[count] = AI_KEY_NAMES[piece:lower()] or piece
        end
        if count == 0 then return nil end
        local combo = parse_hotkey(table.concat(pieces, "+"))
        if combo then return { combo = combo } end
        return nil
    end

    -- "//" / ";;" 这类写法当成"连按两次同一个键"
    local first = spec:sub(1, 1)
    local same = true
    for i = 2, #spec do
        if spec:sub(i, i) ~= first then same = false break end
    end
    if same then spec = first end

    local code
    if #spec == 1 then
        code = string.byte(spec)
    else
        local lower = spec:lower()
        code = KEY_NAMES[lower] or AI_KEY_ALIASES[lower]
    end
    if not code then return nil end
    return { code = code, char = (#spec == 1) and spec or nil }
end

-- preset_text 不为空时：直接发这段文字（读剪贴板那条路用），不取编码/上屏记录
local function ai_send_now(env, preset_text)
    if env.ai_req then
        if log and log.info then
            log.info("actions.lua: ai: previous request still running, not sending again")
        end
        return
    end

    local ctx = env.engine.context
    local composing = ctx and ctx:is_composing()

    local source = env.ai_source or "auto"
    if source == "auto" then
        source = composing and "composition" or "committed"
    end

    local text, replace, cut, before, span
    if preset_text then
        text = preset_text
        replace = false
        cut = 0
        before = ""
        span = ""
    elseif source == "composition" then
        text = ai_composition_text(ctx)
        replace = false
        cut = 0
        before = ""
        span = ""
    else
        local recent = ai_current_text(env)
        text, cut, before, span = ai_last_sentence(recent, env.ai_marks,
                                                   env.revert_symbols or make_symbol_set(DEFAULTS.symbols),
                                                   env.ai_include_marks)
        replace = env.ai_replace and true or false
    end

    text = ai_trim(text)
    if text == "" then
        if log and log.error then
            log.error("actions.lua: ai: nothing to send (no composition, no commit recorded)")
        end
        return
    end

    -- 没有汉字就不值得发（比如刚上屏的是个符号）；输入框里自己打的拼音不算
    -- 剪贴板那条路不设这道门：复制来的英文/代码也要能发（比如 eng; 模式）
    if not preset_text and source ~= "composition" and not ai_has_cjk(text) then
        if log and log.info then
            log.info("actions.lua: ai: nothing to send (no 汉字 in the committed text)")
        end
        return
    end

    -- 要发的就是刚打进去的那段结果：这是自己喂自己，不发
    if env.ai_last_text and text == ai_trim(env.ai_last_text) then
        if log and log.info then
            log.info("actions.lua: ai: nothing to send (same as the last answer)")
        end
        return
    end

    -- 输入框里的内容已经"发走"了，不再留在屏幕上
    if not preset_text and source == "composition" and composing then ctx:clear() end

    local prompt = env.ai_mode and env.ai_mode.prompt or "default"
    local system_prompt = (env.ai_prompts and (env.ai_prompts[prompt] or env.ai_prompts.default)) or ""

    local history = ""
    if (env.ai_history_chars or 0) > 0 and before and before ~= "" then
        history = ai_tail(before, env.ai_history_chars)
    end

    -- 之前答过的问答作为真正的对话轮次带上；没答过的旧文字不算上文
    local turns = {}
    if not preset_text and source == "committed" and (env.ai_history_chars or 0) > 0 then
        local budget = (env.ai_history_chars or 0) - ai_chars(history)
        if budget > 0 then turns = ai_history_pairs(env, budget) end
    end

    local parts = {}
    parts[#parts + 1] = '{"model":"' .. json_escape(env.ai_model or "") .. '","messages":['
    if system_prompt ~= "" then
        local sys = system_prompt
        if history ~= "" then sys = sys .. "\n\n【上文】" .. history end
        parts[#parts + 1] = '{"role":"system","content":"' .. json_escape(sys) .. '"},'
    end
    for _, turn in ipairs(turns) do
        parts[#parts + 1] = '{"role":"user","content":"' .. json_escape(turn.q) .. '"},'
        parts[#parts + 1] = '{"role":"assistant","content":"' .. json_escape(turn.a) .. '"},'
    end
    parts[#parts + 1] = '{"role":"user","content":"' .. json_escape(text) .. '"}],'
    parts[#parts + 1] = string.format('"max_tokens":%d,"temperature":%.2f',
        env.ai_max_tokens or DEFAULTS.ai_online_max_tokens, env.ai_temperature or 0.7)
    if env.ai_thought_off then
        parts[#parts + 1] = ',"thinking":{"type":"disabled"}'
    end
    parts[#parts + 1] = "}"
    local body = table.concat(parts)

    local headers = { "Content-Type: application/json" }
    if env.ai_key and env.ai_key ~= "" then
        headers[#headers + 1] = "Authorization: Bearer " .. env.ai_key
    end

    local ok, seq = pcall(Actions.http_request, env.ai_url, body, table.concat(headers, "\r\n"))
    if not ok then
        if log and log.error then
            log.error("actions.lua: ai: http_request failed: " .. tostring(seq))
        end
        return
    end
    if type(seq) ~= "number" or seq <= 0 then
        if log and log.error then
            log.error("actions.lua: ai: request not sent (seq=" .. tostring(seq)
                .. ", -1 = previous request still running)")
        end
        return
    end

    env.ai_req = {
        text    = text,
        prompt  = prompt,
        replace = replace,
        -- 光标前的回车不进记账，但挡在光标和原句之间，删的时候一起算掉
        cut     = replace and (cut + (env.ai_newlines or 0)) or 0,
        span    = span or "",
        started = os.clock(),
    }
    if log and log.info then
        log.info(string.format("actions.lua: ai: sent %d chars (prompt=%s, source=%s, replace=%d, text=%s)",
            utf8.len(text) or #text, prompt, source,
            env.ai_req.cut, ai_preview(text, 40)))
    end
    if env.ai_nudge and Actions.nudge then
        env.ai_nudged = os.clock()
        Actions.nudge(env.ai_nudge_interval or DEFAULTS.ai_nudge_interval, env.ai_nudge_key)
    end
end

-- 读剪贴板当"要发的内容"：用户自己 Ctrl+C 复制，脚本只负责读出来发出去。
-- 读剪贴板要开剪贴板，被别的程序占着时会一直等，所以不能放在按键回调里同步做：
-- 先在后台线程读（clipboard_request），读好了靠唤醒键回来取（clipboard_take）。
local CLIP_TIMEOUT_S = 5

local function ai_clip_received(env, text)
    env.ai_clip_started = nil

    text = ai_trim(text)
    if text == "" then
        if log and log.info then
            log.info("actions.lua: ai: nothing to send (clipboard is empty)")
        end
        return
    end

    local limit = env.ai_clip_max or DEFAULTS.ai_clip_max_chars
    local total = utf8.len(text) or #text
    local head = head_of({ n = total, text = text }, limit)
    if head and head.n < total then
        if log and log.info then
            log.info(string.format(
                "actions.lua: ai: clipboard truncated to %d chars", limit))
        end
        text = head.text
    end

    ai_send_now(env, text)
end

local function ai_send_clip(env)
    if env.ai_req then
        if log and log.info then
            log.info("actions.lua: ai: previous request still running, not sending again")
        end
        return
    end
    if env.ai_clip_started then
        if log and log.info then
            log.info("actions.lua: ai: clipboard is still being read, not asking again")
        end
        return
    end
    if not Actions or not Actions.clipboard_request then
        if log and log.error then
            log.error("actions.lua: ai: dll 里没有 clipboard_request")
        end
        return
    end

    local ok, ret = pcall(Actions.clipboard_request)
    if not ok then
        if log and log.error then
            log.error("actions.lua: ai: clipboard_request failed: " .. tostring(ret))
        end
        return
    end
    if ret == 2 then
        if log and log.info then
            log.info("actions.lua: ai: clipboard is busy, not reading again")
        end
        return
    end
    if ret ~= 1 then
        if log and log.error then
            log.error("actions.lua: ai: clipboard_request refused (the worker thread cannot start)")
        end
        return
    end

    env.ai_clip_started = os.clock()
    if env.ai_nudge and Actions.nudge then
        env.ai_nudged = os.clock()
        Actions.nudge(env.ai_nudge_interval or DEFAULTS.ai_nudge_interval, env.ai_nudge_key)
    end
end

-- 唤醒键回来时调用：读好了就发出去。返回 true 表示还在等后台读。
local function ai_clip_poll(env)
    if not env.ai_clip_started then return false end
    if not Actions or not Actions.clipboard_take then
        env.ai_clip_started = nil
        if log and log.error then
            log.error("actions.lua: ai: dll 里没有 clipboard_take")
        end
        return false
    end

    local ok, text, err = pcall(Actions.clipboard_take)
    if not ok then
        env.ai_clip_started = nil
        if log and log.error then
            log.error("actions.lua: ai: clipboard_take failed: " .. tostring(text))
        end
        return false
    end
    if type(text) == "string" then
        ai_clip_received(env, text)
        return false
    end
    if err then
        env.ai_clip_started = nil
        if log and log.info then
            log.info("actions.lua: ai: nothing to send (clipboard: " .. tostring(err) .. ")")
        end
        return false
    end
    if os.clock() - env.ai_clip_started > CLIP_TIMEOUT_S then
        env.ai_clip_started = nil
        if log and log.info then
            log.info("actions.lua: ai: clipboard read timed out")
        end
        return false
    end
    return true
end

-- 把一段文字打到光标处。cut > 0 时先删掉这么多字符，再打文字：两步交给 dll
-- 的同一个任务，按顺序执行，所以不会出现"还没删完就开始打字"。
-- 返回 true = 已安排；false = 这次没成（dll 忙或出错），由调用方决定是否重试。
local function ai_insert_text(env, cut, text)
    local ok, ret
    local merged = (cut > 0 and Actions.press_backspace_then_text) and true or false

    if merged then
        ok, ret = pcall(Actions.press_backspace_then_text, cut, text)
        if not ok then
            if log and log.error then
                log.error("actions.lua: ai: press_backspace_then_text failed: " .. tostring(ret))
            end
            return false
        end
    else
        -- 旧 dll 没有"删完再打字"的合并接口：退回先删后打两步（顺序不保证）
        if cut > 0 then inject_backspaces(env, cut) end
        ok, ret = pcall(Actions.send_text, text)
        if not ok then
            if log and log.error then
                log.error("actions.lua: ai: send_text failed: " .. tostring(ret))
            end
            return false
        end
    end

    if ret == 2 then
        return false
    end
    if ret ~= 1 then
        if log and log.info then
            log.info("actions.lua: ai: the dll refused to insert the answer")
        end
        return false
    end
    if merged and cut > 0 then
        -- 合并接口自己删掉的那几个字：账本扣掉，并挡住回环回来的退格
        consume_from_stack(env, cut)
        env.bs_ignore = (env.bs_ignore or 0) + cut
        env.bs_ignore_until = os.clock() + 3
    end
    return true
end

-- 打进去的字会一个个重新经过输入法：把它们标成自己的输出、并开一个打字窗口
local function ai_after_insert(env, content)
    local length = utf8.len(content) or 0

    if length > 0 then push_back(env, { n = length, text = content, ai = true }) end
    env.ai_last_text = content
    env.ai_text_pos = 1
    env.ai_typing_until = os.clock() + 0.25 + 0.002 * math.max(#content, 1) + 0.5
    env.ai_last_push = nil
end

-- 上一次没插进去的回答（dll 忙）在这里补插；返回 true = 还要再约一次唤醒键
local function ai_flush_insert(env)
    local pending = env.ai_pending_insert

    if not pending then return false end
    if os.clock() - (env.ai_pending_since or 0) > 10 then
        env.ai_pending_insert = nil
        if log and log.info then
            log.info("actions.lua: ai: insert given up (the dll stayed busy)")
        end
        return false
    end
    if not ai_insert_text(env, pending.cut or 0, pending.text) then
        return true
    end
    env.ai_pending_insert = nil
    ai_after_insert(env, pending.text)
    if log and log.info then
        log.info(string.format("actions.lua: ai: inserted %d chars (deferred)", #pending.text))
    end
    return false
end

-- 轮询后台请求；返回 true = 这一次有了结论（成功或失败）
local function ai_take(env)
    local req = env.ai_req
    if not req or not Actions or not Actions.http_state then return false end

    local ok, state, seq = pcall(Actions.http_state)
    if not ok then return false end

    if state == "running" then
        -- os.clock() 是进程启动以来的秒数，约 49.7 天后回绕成 0；
        -- 这里只比较几秒到几分钟的差值，回绕最多让一次等待提前收场
        if (env.ai_max_wait_s or 0) > 0
            and (os.clock() - (req.started or os.clock())) > env.ai_max_wait_s then
            if Actions.http_cancel then pcall(Actions.http_cancel) end
            env.ai_req = nil
            if log and log.error then
                log.error(string.format("actions.lua: ai: gave up after %d s", env.ai_max_wait_s))
            end
            return true
        end
        return false
    end

    if state == "idle" then
        env.ai_req = nil
        return false
    end

    -- 同一个请求序号只取一次；状态万一没复位，也不会把同一份结果再打一遍
    if type(seq) == "number" then
        if env.ai_done_seq == seq then
            env.ai_req = nil
            return false
        end
        env.ai_done_seq = seq
    end

    local got, body, err = pcall(Actions.http_result)
    if not got then body, err = nil, body end
    env.ai_req = nil

    local content = body and json_string_field(body, "content") or nil
    content = ai_trim(content or "")
    if content == "" then
        local message = body and json_string_field(body, "message") or nil
        if log and log.error then
            log.error("actions.lua: ai: request failed - "
                .. tostring(message or err or "empty response"))
        end
        return true
    end

    -- 开了 replace：先把原句删掉再插回答。删的范围和"发给 AI 的那一句"一致——
    -- 从光标往回删到上一个 sentence_marks 符号为止（符号之后的内容连末尾那串标点一起删）。
    -- 删的这段里有换行就不删（跨行不动）。
    local cut = 0
    if req.replace and req.cut and req.cut > 0 then
        if (req.span or ""):find("\n", 1, true) then
            if log and log.info then
                log.info("actions.lua: ai: replace skipped (spans a line break)")
            end
        else
            -- 输入框里还压着没上屏的编码时先清掉：否则第一个退格会被输入法拿去删编码，
            -- 正文就少删一个字
            local ctx = env.engine and env.engine.context
            if ctx and ctx.clear and ctx:is_composing() then ctx:clear() end
            if log and log.info then
                log.info(string.format("actions.lua: ai: replace %d chars (span=%s, text=%s)",
                    req.cut, ai_preview(req.span, 24), ai_preview(req.text, 40)))
            end
            cut = req.cut
            -- 原句连同光标前的回车一起删掉了，计数清零
            env.ai_newlines = 0
        end
    end

    if not ai_insert_text(env, cut, content) then
        -- dll 正忙（上一段还在打、或距上次注入太近）：留着，下一次唤醒键再补插，
        -- 不要静默丢掉这一份回答
        env.ai_pending_insert = { cut = cut, text = content }
        env.ai_pending_since = os.clock()
        if log and log.info then
            log.info("actions.lua: ai: dll is busy, insert deferred")
        end
        return true
    end

    ai_after_insert(env, content)

    if log and log.info then
        log.info(string.format("actions.lua: ai: inserted %d chars", #content))
    end
    return true
end

local function ai_toggle(env, prompt)
    prompt = prompt or "default"

    if env.ai_mode and env.ai_mode.prompt == prompt then
        env.ai_mode = nil
        env.ai_last_push = nil
        if env.ai_req and Actions and Actions.http_cancel then
            pcall(Actions.http_cancel)
        end
        env.ai_req = nil
        if log and log.info then
            log.info("actions.lua: ai: mode off (" .. prompt .. ")")
        end
        return
    end

    env.ai_mode = { prompt = prompt }
    env.ai_last_push = nil
    if log and log.info then
        log.info("actions.lua: ai: mode on (" .. prompt .. ")")
    end
end

-- 配置里写的是 Windows 虚拟键码（发给 dll 用），Rime 给的却是 keysym，
-- 认键的时候两种都要认（如 F24：配置 0x87，Rime 报 0xFFD5）
local function vk_to_keysym(vk)
    if vk >= 0x70 and vk <= 0x87 then return 0xFFBE + (vk - 0x70) end   -- F1..F24
    if vk >= 0x41 and vk <= 0x5A then return vk + 0x20 end               -- A..Z
    if vk >= 0x30 and vk <= 0x39 then return vk end                      -- 0..9
    if vk == 0x20 then return 0x20 end                                   -- 空格
    return nil
end

-- 按键入口：返回 1/2 表示"这一键我处理了/我没处理"，返回 nil 表示交给主流程
local function ai_handle_key(key, env)
    local kc = key.keycode

    -- 唤醒键：先把上次没插进去的回答补上，再取结果；还在等就再约一次
    if kc == env.ai_nudge_key or kc == vk_to_keysym(env.ai_nudge_key) then
        ai_flush_insert(env)
        ai_take(env)
        local waiting_clip = ai_clip_poll(env)
        -- 续约：一次唤醒最多排一次，且只按"按下"排。抬起事件也走到这里，
        -- 如果两边都排，唤醒键的个数每轮翻一倍。
        if (env.ai_req or waiting_clip or env.ai_pending_insert)
            and env.ai_nudge and Actions.nudge and not key:release() then
            local now = os.clock()
            local interval = (env.ai_nudge_interval or DEFAULTS.ai_nudge_interval) / 1000
            if not env.ai_nudged or (now - env.ai_nudged) >= interval * 0.9 then
                env.ai_nudged = now
                Actions.nudge(env.ai_nudge_interval or DEFAULTS.ai_nudge_interval, env.ai_nudge_key)
            end
        end
        return kAccepted
    end

    if key:release() then return nil end

    -- 刚把结果打进光标：这些字符正一个个经过输入法，别当触发码/发送键用
    if env.ai_typing_until then
        if os.clock() < env.ai_typing_until then
            return kNoop
        end
        env.ai_typing_until = nil
    end

    if env.ai_req and ai_take(env) then
        -- 取回结果的那一下不算"连按发送的第一下"
        env.ai_last_push = nil
        if env.ai_send and not env.ai_send.combo and kc == env.ai_send.code then
            return kAccepted
        end
    end

    -- 中间按了别的键，就不算"连按两下发送"了
    if env.ai_last_push and not (env.ai_send and kc == env.ai_send.code
                                 and not (key:ctrl() or key:alt() or key:super())) then
        env.ai_last_push = nil
    end

    -- Esc：先取消这次请求；没有请求就退出 AI 模式
    if kc == 0xFF1B then
        if env.ai_req then
            if Actions.http_cancel then pcall(Actions.http_cancel) end
            env.ai_req = nil
            if log and log.info then log.info("actions.lua: ai: cancelled by Esc") end
            return kAccepted
        end
        if env.ai_mode then
            env.ai_mode = nil
            env.ai_last_push = nil
            if log and log.info then log.info("actions.lua: ai: mode off (Esc)") end
            return kAccepted
        end
        return nil
    end

    if not env.ai_mode or not env.ai_send then return nil end

    -- 剪贴板发送：只在 AI 模式下生效（用当前模式的提示词）
    if env.ai_clip and hotkey_matches(key, env.ai_clip) then
        ai_send_clip(env)
        return kAccepted
    end

    if env.ai_send.combo then
        if hotkey_matches(key, env.ai_send.combo) then
            ai_send_now(env)
            return kAccepted
        end
        return nil
    end

    if kc == env.ai_send.code and not (key:ctrl() or key:alt() or key:super()) then
        local ctx = env.engine.context
        local composing = ctx and ctx:is_composing()
        local now = os.clock()
        local last = env.ai_last_push

        if last and (now - last) * 1000 <= (env.ai_double_ms or DEFAULTS.ai_double_ms) then
            -- 第二下：发送。第一下如果进了编码（`nihao` + `/`），先把这个字符摘掉
            env.ai_last_push = nil
            if composing and env.ai_send.char then
                local inp = ctx.input or ""
                -- 编码里只剩这个字符时也要摘掉，否则它留在输入法里，第一个退格会被
                -- 输入法拿去删它，正文里就少删一个字
                if inp:sub(-1) == env.ai_send.char and #inp >= 1 and ctx.pop_input then
                    ctx:pop_input(1)
                end
            end
            ai_send_now(env)
            return kAccepted
        end

        -- 第一下：有编码就放行（万象的快符、符号上屏都靠这一下），只在窗口内等第二下
        env.ai_last_push = now
        if composing then return nil end
        return kAccepted
    end

    -- AI 模式下也能直接按别的 @ai 触发码换提示词（不用先退出去）
    local context = env.engine.context
    if context and not (key:ctrl() or key:alt() or key:super()) then
        for _, action in ipairs(env.revert_actions or {}) do
            if action.kind == "ai" and key.keycode == action.code
                and context.input == action.prefix then
                context:clear()
                ai_toggle(env, action.prompt)
                return kAccepted
            end
        end
    end

    return nil
end

-- ------------------------------------------------------- AI 拼音候选（可选）
-- 拼音来自 ai_pinyin_capture.lua：它排在 super_comment_preedit 之前，只读不改，
-- 把「候选文字 + 词库带调拼音」写进 context 属性 ai_pinyin/map。
-- 候选插入由 ai_pinyin_insert.lua 完成：它排在 uniquifier 之前，把 AI 词放第一位。
-- 这里只负责开关、去抖、发请求、取结果、刷新候选。
local AI_PINYIN_PROMPT = [[
你是拼音输入法的补全工具。用户会给你三样东西：
1. 一串无声调拼音，音节之间用空格分开；
2. 输入法现有的前几个候选词；
3. 光标前的一小段上文（可能为空）。
请结合上文判断用户真正想打哪个词：先看看现有的候选是否合理，合理就把它原样输出；
如果候选中的词不合理时，你再按拼音另造一个。要求：
- 汉字个数必须等于拼音音节数；
- 每个字的读音必须与对应的音节一致；
- 只输出这一个词，不要标点、空格、拼音或解释；]]

local function ai_pinyin_is_cjk(cp)
    return (cp >= 0x4E00 and cp <= 0x9FFF) or (cp >= 0x3400 and cp <= 0x4DBF)
end

-- 返回 字数, 是否全是汉字
local function ai_pinyin_count(text)
    local n = 0
    for _, ch in ipairs(to_chars(text)) do
        local ok, cp = pcall(utf8.codepoint, ch)
        if not ok or not ai_pinyin_is_cjk(cp) then
            return n, false
        end
        n = n + 1
    end
    return n, n > 0
end

local function ai_pinyin_clean(text)
    if type(text) ~= "string" then return "" end
    return (text:gsub("%s+", ""))
end

local function ai_pinyin_clear(env)
    local ctx = env.engine and env.engine.context
    if ctx and ctx.set_property then
        pcall(ctx.set_property, ctx, "ai_pinyin/result", "")
        pcall(ctx.set_property, ctx, "ai_pinyin/result_sig", "")
    end
end

-- 结果到达时没有按键事件，要主动重算候选才看得见
local function ai_pinyin_publish(env, text, sig, same)
    local st = env.ai_pinyin
    local ctx = env.engine and env.engine.context
    if not (st and ctx) then return end
    if ctx.set_property then
        pcall(ctx.set_property, ctx, "ai_pinyin/result", text)
        pcall(ctx.set_property, ctx, "ai_pinyin/result_sig", sig)
        pcall(ctx.set_property, ctx, "ai_pinyin/marker", st.marker)
        pcall(ctx.set_property, ctx, "ai_pinyin/same", same and "1" or "0")
    end
    if ctx.refresh_non_confirmed_composition then
        pcall(ctx.refresh_non_confirmed_composition, ctx)
    end
end

-- 叫醒输入法：Lua 只在有事件时被调用，请求和结果都得靠定时敲一下这个空键
local function ai_pinyin_schedule(env, st)
    if not (Actions and Actions.nudge and env.ai_nudge) then return end
    local now = os.clock()
    if env.ai_pinyin_nudge_at and now < env.ai_pinyin_nudge_at then return end
    local interval = st.nudge_interval
    env.ai_pinyin_nudge_at = now + (interval / 1000) * 0.9
    pcall(Actions.nudge, interval, st.nudge_key)
end

local function ai_pinyin_fail(env, st, reason, sig)
    st.req = nil
    -- 同一串拼音失败过就先冷却一会儿，别每个 tick 重复问同一个问题
    if sig and sig ~= "" and (st.retry or 0) > 0 then
        st.fail = st.fail or {}
        local n = 0
        for _ in pairs(st.fail) do n = n + 1 end
        if n > 16 then st.fail = {} end
        st.fail[sig] = os.clock() * 1000 + st.retry
    end
    ai_pinyin_clear(env)
    if reason and reason ~= "" then
        log.warning("actions.lua: ai_pinyin: " .. reason)
    end
end

local function ai_pinyin_accept(env, st, body)
    local req = st.req
    st.req = nil
    if not req then return end
    local content = ai_pinyin_clean(json_string_field(body or "", "content"))
    if content == "" then
        ai_pinyin_fail(env, st, "回答里没有可用文字 (pinyin=" .. req.pinyin .. ")", req.sig)
        return
    end
    if content:upper() == "NONE" then
        ai_pinyin_fail(env, st, "模型说无法确定 (pinyin=" .. req.pinyin .. ")", req.sig)
        return
    end
    local count, ok = ai_pinyin_count(content)
    if not ok then
        ai_pinyin_fail(env, st, "回答里有非汉字 (pinyin=" .. req.pinyin .. "): "
            .. ai_preview(content, 20), req.sig)
        return
    end
    if count ~= req.syllables then
        ai_pinyin_fail(env, st, string.format(
            "回答 %d 个字，拼音 %d 个音节，数量不符 (pinyin=%s): %s",
            count, req.syllables, req.pinyin, ai_preview(content, 20)), req.sig)
        return
    end
    st.done[req.sig] = content
    if req.first and content == req.first then
        -- 模型挑的就是眼下第一位：不新增候选，只在它的注释上打个标记，
        -- 让用户知道这次查询成功、结论一致
        st.done[req.sig] = ""
        st.same[req.sig] = true
        if log and log.info then
            log.info(string.format(
                "actions.lua: ai_pinyin: %s is the first candidate already, marked only",
                content))
        end
        ai_pinyin_publish(env, "", req.sig, true)
        return
    end
    if log and log.info then
        log.info(string.format("actions.lua: ai_pinyin: %s <- %s", content, req.pinyin))
    end
    ai_pinyin_publish(env, content, req.sig)
end

-- 从 capture 写的 ai_pinyin/map 里取前 n 个候选的文字（顺序与候选窗一致）
local function ai_pinyin_top_texts(env, st, n)
    local ctx = env.engine and env.engine.context
    if not (ctx and ctx.get_property and n and n > 0) then return {} end
    local ok, raw = pcall(ctx.get_property, ctx, "ai_pinyin/map")
    if not ok or type(raw) ~= "string" or raw == "" then return {} end
    local names, seen = {}, {}
    for line in raw:gmatch("[^\r\n]+") do
        local text = line:match("^([^\t]+)")
        if text and text ~= "" and not seen[text] then
            seen[text] = true
            names[#names + 1] = text
            if #names >= n then break end
        end
    end
    return names
end

local function ai_pinyin_send(env, st, sig, pinyin)
    local words, syllables = {}, 0
    for token in pinyin:gmatch("%S+") do
        words[#words + 1] = token
        syllables = syllables + 1
    end
    if syllables == 0 then return end
    if syllables > st.max_chars then
        if log and log.info then
            log.info(string.format(
                "actions.lua: ai_pinyin: %d syllables is over max_candidate_chars=%d, skipped",
                syllables, st.max_chars))
        end
        return
    end
    local text = table.concat(words, " ")

    -- 把现有候选一起发过去，模型可以只在里面挑，也可以另造
    local top = ai_pinyin_top_texts(env, st, st.send_candidates)
    local user_text = "拼音：" .. text
    if #top > 0 then
        user_text = user_text .. "\n现有候选：" .. table.concat(top, " / ")
    end

    local system_prompt = st.prompt
    local before = ai_tail(ai_current_text(env), st.context_chars)
    if before ~= "" then system_prompt = system_prompt .. "\n\n【上文】" .. before end

    local parts = {}
    parts[#parts + 1] = '{"model":"' .. json_escape(st.model or "") .. '","messages":['
    parts[#parts + 1] = '{"role":"system","content":"' .. json_escape(system_prompt) .. '"},'
    parts[#parts + 1] = '{"role":"user","content":"' .. json_escape(user_text) .. '"}],'
    parts[#parts + 1] = string.format('"max_tokens":%d,"temperature":%.2f',
        st.max_tokens or 64, st.temperature or 0.7)
    if st.thought_off then
        parts[#parts + 1] = ',"thinking":{"type":"disabled"}'
    end
    parts[#parts + 1] = "}"
    local body = table.concat(parts)

    local headers = { "Content-Type: application/json" }
    if st.key and st.key ~= "" then
        headers[#headers + 1] = "Authorization: Bearer " .. st.key
    end

    local ok, seq = pcall(Actions.http_request, st.url, body, table.concat(headers, "\r\n"))
    if not ok then
        ai_pinyin_fail(env, st, "http_request failed: " .. tostring(seq), sig)
        return
    end
    if type(seq) ~= "number" or seq <= 0 then
        ai_pinyin_fail(env, st, "request not sent (seq=" .. tostring(seq) .. ")", sig)
        return
    end
    st.req = { seq = seq, sig = sig, pinyin = text, syllables = syllables,
        first = top[1], started = os.clock() }
    if log and log.info then
        log.info(string.format("actions.lua: ai_pinyin: asked %d syllables (%s) seq=%d",
            syllables, text, seq))
    end
    ai_pinyin_schedule(env, st)
end

local function ai_pinyin_tick(env)
    local st = env.ai_pinyin
    local ctx = env.engine and env.engine.context
    if not (st and ctx) then return end
    env.ai_pinyin_nudge_at = nil
    local now = os.clock()

    -- 1) 有请求在跑：看回来没有
    if st.req then
        local rsig = st.req.sig
        local state
        if Actions.http_state then
            local ok, s = pcall(Actions.http_state)
            if ok then state = s end
        end
        if state == "done" then
            local ok, body = pcall(Actions.http_result)
            if not ok then
                ai_pinyin_fail(env, st, "http_result failed", rsig)
            else
                ai_pinyin_accept(env, st, body)
            end
            return
        elseif state == "failed" then
            local ok, _, err = pcall(Actions.http_result)
            ai_pinyin_fail(env, st, "request failed - " .. tostring(ok and err or "unknown"), rsig)
            return
        elseif state ~= "running" then
            ai_pinyin_fail(env, st, "request vanished (state=" .. tostring(state) .. ")", rsig)
            return
        end
        if st.max_wait > 0 and (now - st.req.started) > st.max_wait then
            if Actions.http_cancel then pcall(Actions.http_cancel) end
            ai_pinyin_fail(env, st, "wait too long, cancelled", rsig)
            return
        end
        ai_pinyin_schedule(env, st)
        return
    end

    -- 2) 没有请求：决定发不发
    if not st.on then return end
    local sig = ctx.input or ""
    if sig == "" then
        st.seen_sig = nil
        return
    end
    if st.done[sig] then
        local shown = ""
        if ctx.get_property then
            local ok, value = pcall(ctx.get_property, ctx, "ai_pinyin/result_sig")
            if ok and type(value) == "string" then shown = value end
        end
        if shown ~= sig then
            ai_pinyin_publish(env, st.done[sig], sig, st.same and st.same[sig])
        end
        return
    end
    local pinyin = ""
    if ctx.get_property then
        local ok, value = pcall(ctx.get_property, ctx, "ai_pinyin/first_py")
        if ok and type(value) == "string" then pinyin = value end
    end
    if pinyin == "" then
        -- 第一个候选没有可靠拼音（自定义短语、日期、英文这类），不动
        st.seen_sig = nil
        return
    end
    if st.fail and st.fail[sig] then
        if now * 1000 < st.fail[sig] then return end
        st.fail[sig] = nil
    end
    if sig ~= st.seen_sig then
        st.seen_sig = sig
        st.seen_at = now
        ai_pinyin_schedule(env, st)
        return
    end
    if (now - (st.seen_at or 0)) * 1000 < st.debounce then
        ai_pinyin_schedule(env, st)
        return
    end
    ai_pinyin_send(env, st, sig, pinyin)
end

-- 两个滤波器只在 ai_pinyin/active == "1" 时工作；模式关着就完全不写属性、不改候选，
-- 跟没有接那两个滤波器一样。
local function ai_pinyin_active(env, on)
    local ctx = env.engine and env.engine.context
    if ctx and ctx.set_property then
        pcall(ctx.set_property, ctx, "ai_pinyin/active", on and "1" or "0")
    end
end

local function ai_pinyin_toggle(env, st)
    st.on = not st.on
    st.seen_sig = nil
    ai_pinyin_active(env, st.on)
    if not st.on then
        ai_pinyin_clear(env)
        if log and log.info then log.info("actions.lua: ai_pinyin: off") end
        return
    end
    local ctx = env.engine and env.engine.context
    if ctx and ctx.set_property then
        pcall(ctx.set_property, ctx, "ai_pinyin/marker", st.marker)
    end
    if log and log.info then log.info("actions.lua: ai_pinyin: on") end
end

local function ai_pinyin_handle_key(key, env)
    local st = env.ai_pinyin
    if not st then return nil end
    local kc = key.keycode
    local ctx = env.engine and env.engine.context

    -- 唤醒键：先把这边到期的活干完；AI 模式也用它，那就交回去继续处理
    if kc == st.nudge_key or kc == vk_to_keysym(st.nudge_key) then
        if not key:release() then ai_pinyin_tick(env) end
        if env.ai_enabled then return nil end
        return kAccepted
    end

    if key:release() then return nil end

    -- 开关触发码
    if st.final_code and kc == st.final_code
        and not (key:ctrl() or key:alt() or key:super())
        and ctx and ctx.input == st.prefix then
        if ctx.clear then pcall(ctx.clear, ctx) end
        ai_pinyin_toggle(env, st)
        return kAccepted
    end

    if not st.on then return nil end

    -- Esc：只丢掉正在等的这次回答，模式照旧开着（退出模式只认触发码）
    if kc == 0xFF1B and not (key:ctrl() or key:alt() or key:super()) then
        if st.req then
            st.req = nil
            st.seen_sig = nil
            pcall(Actions.http_cancel)
            if log and log.info then log.info("actions.lua: ai_pinyin: cancelled by esc, mode kept") end
        end
        return nil
    end

    -- 还在打字：重新计时，等手停下来再发
    if ctx and ctx.is_composing and ctx:is_composing() then
        st.seen_sig = nil
        ai_pinyin_schedule(env, st)
    end
    return nil
end

local function init_ai_pinyin(env, config)
    env.ai_pinyin = nil
    env.ai_pinyin_nudge_at = nil
    if not cfg_bool(config, "actions/ai_pinyin/enabled", DEFAULTS.ai_pinyin_enabled) then
        return
    end
    if not (Actions and Actions.http_request and Actions.http_state and Actions.http_result) then
        log.error("actions.lua: ai_pinyin disabled - dll 里没有 http_request/http_state/http_result")
        return
    end

    local spec = cfg_string(config, "actions/ai_pinyin/trigger", DEFAULTS.ai_pinyin_trigger)
    if type(spec) ~= "string" or #spec < 2 or TRIGGER_OFF[spec:lower()] then
        log.error("actions.lua: ai_pinyin disabled - bad trigger: " .. tostring(spec))
        return
    end

    local st = {
        on = false,
        prefix = spec:sub(1, #spec - 1),
        final_code = string.byte(spec, #spec),
        marker = cfg_string(config, "actions/ai_pinyin/marker", DEFAULTS.ai_pinyin_marker),
        debounce = cfg_int(config, "actions/ai_pinyin/debounce_ms",
                           DEFAULTS.ai_pinyin_debounce, 0, LIMITS.ai_pinyin_debounce),
        context_chars = cfg_int(config, "actions/ai_pinyin/context_chars",
                                DEFAULTS.ai_pinyin_context_chars, 0, LIMITS.ai_history_chars),
        max_chars = cfg_int(config, "actions/ai_pinyin/max_chars",
                            DEFAULTS.ai_pinyin_max_chars, 1, LIMITS.ai_pinyin_max_chars),
        send_candidates = cfg_int(config, "actions/ai_pinyin/send_candidates",
            DEFAULTS.ai_pinyin_send_candidates, 0, LIMITS.ai_pinyin_send_candidates),
        prompt = cfg_string(config, "actions/ai_pinyin/prompt", AI_PINYIN_PROMPT),
        retry = cfg_int(config, "actions/ai_pinyin/retry_ms",
                         DEFAULTS.ai_pinyin_retry, 0, LIMITS.ai_pinyin_retry),
        done = {},
        same = {},
        fail = {},
        seen_sig = nil,
        req = nil,
        nudge_interval = env.ai_nudge_interval or DEFAULTS.ai_nudge_interval,
        nudge_key = env.ai_nudge_key or DEFAULTS.ai_nudge_key,
        max_wait = env.ai_max_wait_s or DEFAULTS.ai_max_wait_s,
    }

    -- 接口配置和 AI 模式共用同一组键（actions/ai/provider 与 online|local 子键）
    if env.ai_enabled and env.ai_url then
        st.url = env.ai_url
        st.key = env.ai_key
        st.model = env.ai_model
        st.thought_off = env.ai_thought_off
        st.max_tokens = env.ai_max_tokens
        st.temperature = env.ai_temperature
    else
        local provider = cfg_string(config, "actions/ai/provider", DEFAULTS.ai_provider):lower()
        if provider ~= "local" then provider = "online" end
        local prefix = "actions/ai/" .. provider .. "/"
        local default_url   = provider == "local" and DEFAULTS.ai_local_url   or DEFAULTS.ai_online_url
        local default_model = provider == "local" and DEFAULTS.ai_local_model or DEFAULTS.ai_online_model
        local default_key   = provider == "local" and DEFAULTS.ai_local_key   or DEFAULTS.ai_online_key
        local default_off   = provider == "local" and DEFAULTS.ai_local_think_off
                                                  or DEFAULTS.ai_online_think_off
        local default_tok   = provider == "local" and DEFAULTS.ai_local_max_tokens
                                                  or DEFAULTS.ai_online_max_tokens
        local default_temp  = provider == "local" and DEFAULTS.ai_local_temperature
                                                  or DEFAULTS.ai_online_temperature
        st.url         = cfg_string(config, prefix .. "base_url", default_url)
        st.model       = cfg_string(config, prefix .. "model", default_model)
        st.key         = cfg_string(config, prefix .. "api_key", default_key)
        st.thought_off = cfg_bool(config, prefix .. "thinking_disabled", default_off)
        st.max_tokens  = cfg_int(config, prefix .. "max_tokens", default_tok, 1, LIMITS.ai_max_tokens)
        st.temperature = cfg_number(config, prefix .. "temperature", default_temp, 0, 2)
    end

    env.ai_pinyin = st
    local ctx = env.engine and env.engine.context
    if ctx and ctx.set_property then
        pcall(ctx.set_property, ctx, "ai_pinyin/marker", st.marker)
    end
    if log and log.info then
        log.info(string.format(
            "actions.lua: ai_pinyin on trigger=%s debounce_ms=%d context_chars=%d max_chars=%d model=%s",
            spec, st.debounce, st.context_chars, st.max_chars, tostring(st.model)))
    end
end

local function init_ai(env, config)
    local missing
    if not Actions then
        missing = "actions.dll 没加载"
    elseif not Actions.http_request then
        missing = "dll 里没有 http_request"
    elseif not Actions.send_text then
        missing = "dll 里没有 send_text"
    elseif not Actions.nudge then
        missing = "dll 里没有 nudge"
    end
    if missing then
        if log and log.error then
            log.error("actions.lua: ai disabled - " .. missing .. " (need the new actions.dll)")
        end
        return
    end

    local send_spec = cfg_string(config, "actions/ai/send_key", DEFAULTS.ai_send_key)
    local send = parse_send_key(send_spec)
    if not send then
        if log and log.error then
            log.error("actions.lua: ai disabled - cannot parse actions/ai/send_key: "
                .. tostring(send_spec))
        end
        return
    end

    local provider = cfg_string(config, "actions/ai/provider", DEFAULTS.ai_provider):lower()
    if provider ~= "local" then provider = "online" end
    local prefix = "actions/ai/" .. provider .. "/"

    -- 读剪贴板发送：ctrl+alt+semicolon；写空、none、off 就是不启用
    -- （这里直接读原值，cfg_string 会把空字符串当成"没配"从而回退成默认）
    local clip_spec = config and config:get_string("actions/ai/clip_key")
    if type(clip_spec) ~= "string" then clip_spec = DEFAULTS.ai_clip_key end
    env.ai_clip = nil
    local clip_lower = clip_spec:lower()
    if clip_spec ~= "" and clip_lower ~= "none" and clip_lower ~= "off" then
        env.ai_clip = parse_hotkey(clip_spec)
        if not env.ai_clip and log and log.error then
            log.error("actions.lua: ai: cannot parse actions/ai/clip_key: " .. tostring(clip_spec))
        end
    end
    env.ai_clip_max = cfg_int(config, "actions/ai/clip_max_chars", DEFAULTS.ai_clip_max_chars,
                              1, LIMITS.ai_clip_max_chars)
    if env.ai_clip and not (Actions.clipboard_request and Actions.clipboard_take) then
        env.ai_clip = nil
        if log and log.error then
            log.error("actions.lua: ai: clipboard sending disabled - dll 里没有 clipboard_request/clipboard_take")
        end
    end

    env.ai_send            = send
    env.ai_double_ms       = cfg_int(config, "actions/ai/double_ms", DEFAULTS.ai_double_ms,
                                     0, LIMITS.ai_double_ms)
    env.ai_source          = cfg_string(config, "actions/ai/source", DEFAULTS.ai_source):lower()
    if env.ai_source ~= "composition" and env.ai_source ~= "committed" then
        env.ai_source = "auto"
    end
    env.ai_marks           = ai_mark_set(cfg_string(config, "actions/ai/sentence_marks",
                                                    DEFAULTS.ai_marks))
    env.ai_include_marks   = cfg_bool(config, "actions/ai/include_marks", DEFAULTS.ai_include_marks)
    env.ai_replace         = cfg_bool(config, "actions/ai/replace", DEFAULTS.ai_replace)
    env.ai_skip_own        = cfg_bool(config, "actions/ai/skip_ai_output", DEFAULTS.ai_skip_own)
    env.ai_history_chars   = cfg_int(config, "actions/ai/history_chars",
                                     DEFAULTS.ai_history_chars, 0, LIMITS.ai_history_chars)
    env.ai_nudge           = cfg_bool(config, "actions/ai/nudge", DEFAULTS.ai_nudge)
    env.ai_nudge_key       = cfg_int(config, "actions/ai/nudge_key", DEFAULTS.ai_nudge_key,
                                     1, LIMITS.ai_nudge_key)
    env.ai_nudge_interval  = cfg_int(config, "actions/ai/nudge_interval_ms",
                                     DEFAULTS.ai_nudge_interval, 50, LIMITS.ai_nudge_interval)
    env.ai_max_wait_s      = cfg_int(config, "actions/ai/max_wait_s", DEFAULTS.ai_max_wait_s,
                                     0, LIMITS.ai_max_wait_s)

    local default_url   = provider == "local" and DEFAULTS.ai_local_url   or DEFAULTS.ai_online_url
    local default_model = provider == "local" and DEFAULTS.ai_local_model or DEFAULTS.ai_online_model
    local default_key   = provider == "local" and DEFAULTS.ai_local_key   or DEFAULTS.ai_online_key
    local default_off   = provider == "local" and DEFAULTS.ai_local_think_off
                                             or DEFAULTS.ai_online_think_off
    local default_tok   = provider == "local" and DEFAULTS.ai_local_max_tokens
                                             or DEFAULTS.ai_online_max_tokens
    local default_temp  = provider == "local" and DEFAULTS.ai_local_temperature
                                             or DEFAULTS.ai_online_temperature

    env.ai_url         = cfg_string(config, prefix .. "base_url", default_url)
    env.ai_model       = cfg_string(config, prefix .. "model", default_model)
    env.ai_key         = cfg_string(config, prefix .. "api_key", default_key)
    env.ai_thought_off = cfg_bool(config, prefix .. "thinking_disabled", default_off)
    env.ai_max_tokens  = cfg_int(config, prefix .. "max_tokens", default_tok, 1, LIMITS.ai_max_tokens)
    env.ai_temperature = cfg_number(config, prefix .. "temperature", default_temp, 0, 2)

    -- 提示词：@ai:<名字> 里的名字就是 actions/ai/prompts/<名字>
    env.ai_prompts = {}
    for _, action in ipairs(env.revert_actions or {}) do
        if action.kind == "ai" then
            local name = action.prompt or "default"
            if env.ai_prompts[name] == nil then
                env.ai_prompts[name] = cfg_string(config, "actions/ai/prompts/" .. name, nil)
                    or AI_PROMPTS[name] or AI_PROMPTS.default
            end
        end
    end
    if next(env.ai_prompts) == nil then
        env.ai_prompts.default = AI_PROMPTS.default
    end

    env.ai_enabled = true
    if log and log.info then
        log.info(string.format(
            "actions.lua: ai enabled (send_key=%s double_ms=%d source=%s marks=%s nudge=%s provider=%s model=%s clip_key=%s clip_max=%d prompts=%s)",
            send_spec, env.ai_double_ms, env.ai_source,
            cfg_string(config, "actions/ai/sentence_marks", DEFAULTS.ai_marks),
            env.ai_nudge and "on" or "off", provider, tostring(env.ai_model),
            env.ai_clip and clip_spec or "-", env.ai_clip_max,
            table.concat((function()
                local names = {}
                for name in pairs(env.ai_prompts) do names[#names + 1] = name end
                table.sort(names)
                return names
            end)(), ",")))
    end
end

-- ------------------------------------------------------------- 组件接口
local function init(env)
    local config = env.engine and env.engine.schema and env.engine.schema.config

    -- 日志开关：只有配置里明确写了才改（不写就沿用当前值，默认只记 error）
    if config and config.get_bool then
        local want = config:get_bool("actions/debug")
        if want ~= nil then DEBUG = (want == true) end
    end
    env.revert_debug = DEBUG

    env.revert_trigger    = cfg_string(config, "actions/trigger", DEFAULTS.trigger)
    env.revert_dll_dir    = cfg_string(config, "actions/dll_dir", DEFAULTS.dll_dir)
    env.revert_stack_size = cfg_int(config, "actions/stack_size", DEFAULTS.stack_size,
                                    1, LIMITS.stack_size)
    env.revert_limit      = cfg_int(config, "actions/max_backspaces", DEFAULTS.max_backspaces,
                                    1, LIMITS.max_backspaces)
    env.revert_debounce   = cfg_int(config, "actions/debounce_ms", DEFAULTS.debounce_ms,
                                    0, LIMITS.debounce_ms) / 1000
    env.revert_symbols    = make_symbol_set(cfg_string(config, "actions/symbols", DEFAULTS.symbols))
    env.revert_search_url = cfg_string(config, "actions/search_url", DEFAULTS.search_url)
    env.revert_key_gap    = cfg_int(config, "actions/key_gap_ms", DEFAULTS.key_gap_ms,
                                    0, LIMITS.key_gap_ms)

    env.revert_stack = {}
    env.revert_last = nil

    -- 快符补记：要求 rime 的 context.commit_history 能读（有 to_table 方法），
    -- 读不到就退回只靠上屏通知记账
    env.hist_ready = cfg_bool(config, "actions/history_ledger", DEFAULTS.history_ledger)
    env.hist_owed = {}
    env.hist_seen = 0
    if env.hist_ready then
        local ctx = env.engine and env.engine.context
        local hist = ctx and ctx.commit_history
        if not (hist and hist.to_table) then
            env.hist_ready = false
            if log and log.warning then
                log.warning("actions.lua: actions/history_ledger needs context.commit_history, ignored")
            end
        else
            local ok, all = pcall(hist.to_table, hist)
            if ok and type(all) == "table" then
                -- 从当前历史长度起算：初始化之前的记录不算进账本
                env.hist_seen = #all
            end
        end
    end

    -- 组合键：不填（默认空）= 不启用；填了就多一种触发方式，几种方式可以同时用
    local hotkey_spec = cfg_string(config, "actions/hotkey", DEFAULTS.hotkey)
    env.revert_hotkey = parse_hotkey(hotkey_spec)
    if hotkey_spec ~= "" and not env.revert_hotkey and log and log.error then
        log.error("actions.lua: cannot parse actions/hotkey: " .. tostring(hotkey_spec))
    end

    -- 文本触发码：至少两个字符，否则"前缀为空"会到处触发；
    -- 写成 none / off / - 表示关掉文本触发，只留组合键 / 快捷键
    env.revert_final_code = nil
    env.revert_prefix = nil
    local trigger = env.revert_trigger
    if type(trigger) == "string" and #trigger >= 2
        and not TRIGGER_OFF[trigger:lower()] then
        env.revert_prefix = trigger:sub(1, #trigger - 1)
        env.revert_final_code = string.byte(trigger, #trigger)
    end

    -- dll 目录来自配置，所以在这里加载模块
    load_module(env.revert_dll_dir)

    -- 快捷键表（默认空 = 全不启用）
    local shortcuts_spec = cfg_string(config, "actions/shortcuts", DEFAULTS.shortcuts)
    env.revert_actions = parse_shortcuts(shortcuts_spec, env)

    -- AI（可选）：只有 actions/ai/enabled = true 才读后面那一组配置
    env.ai_enabled = false
    env.ai_mode = nil
    env.ai_req = nil
    env.ai_last_push = nil
    env.ai_prompts = {}
    if cfg_bool(config, "actions/ai/enabled", DEFAULTS.ai_enabled) then
        init_ai(env, config)
    end

    -- AI 拼音候选（可选）：和 AI 模式各自独立，配置项在 actions/ai_pinyin/*
    init_ai_pinyin(env, config)

    env.commit_notifier = env.engine.context.commit_notifier:connect(function(ctx)
        on_commit(ctx:get_commit_text(), env)
    end)

    if log and log.info then
        local modes = {}
        if env.revert_hotkey then modes[#modes + 1] = "hotkey" end
        if env.revert_final_code then modes[#modes + 1] = "text" end
        if #env.revert_actions > 0 then modes[#modes + 1] = "shortcuts" end

        local codes = {}
        for _, action in ipairs(env.revert_actions) do
            codes[#codes + 1] = action.trigger
        end

        log.info(string.format(
            "actions.lua: mode=%s trigger=%s hotkey=%s shortcuts=%s search_url=%s key_gap_ms=%d stack_size=%d max_backspaces=%d debounce_ms=%d ai=%s dll_dir=%s module=%s",
            #modes > 0 and table.concat(modes, "+") or "off",
            env.revert_final_code and tostring(env.revert_trigger) or "-",
            hotkey_spec ~= "" and hotkey_spec or "-",
            #codes > 0 and table.concat(codes, ",") or "-",
            tostring(env.revert_search_url),
            env.revert_key_gap or DEFAULTS.key_gap_ms,
            env.revert_stack_size, env.revert_limit,
            math.floor(env.revert_debounce * 1000 + 0.5),
            env.ai_enabled and "on" or "off",
            env.revert_dll_dir,
            Actions and "loaded" or "MISSING"))
    end
end

local function fini(env)
    if env.commit_notifier then
        env.commit_notifier:disconnect()
        env.commit_notifier = nil
    end
    if env.ai_req and Actions and Actions.http_cancel then
        pcall(Actions.http_cancel)
    end
    env.ai_req = nil
end

local function func(key, env)
    -- 先把历史里新增的记录补进账本（快符这类不触发上屏通知的文字靠这一步），
    -- 这样下面的回删动作数到的字数是完整的
    sync_history(env, env.engine and env.engine.context)

    -- 光标前的回车不会上屏（不进记账），但 replace 要从光标往回删，
    -- 这些回车挡在光标和原句之间，先记下来，发送时并进删除长度里。
    if env.ai_mode and not key:release()
        and (key.keycode == 0xFF0D or key.keycode == 0xFF8D or key.keycode == 0x0D)
        and not (key:ctrl() or key:alt() or key:super()) then
        local ctx = env.engine.context
        if not (ctx and ctx:is_composing()) then
            env.ai_newlines = (env.ai_newlines or 0) + 1
        end
    end

    -- ---- AI 拼音候选（默认关闭）----
    if env.ai_pinyin then
        local handled = ai_pinyin_handle_key(key, env)
        if handled ~= nil then return handled end
    end

    -- ---- 方式四：AI（默认关闭；没开就完全跳过）----
    if env.ai_enabled then
        local handled = ai_handle_key(key, env)
        if handled ~= nil then return handled end
        -- AI 模式下其他按键都正常进输入法，不走下面的触发码
        if env.ai_mode then return kNoop end
    end

    if key:release() then return kNoop end

    -- ---- 手按退格：账本跟着少一个字 ----
    -- 只认没有修饰键的退格；编码还在时那是删编码，不动账本。
    -- 返回 kNoop 放行（librime 的处理器不接受 nil 作返回值），程序照常删字。
    if key.keycode == 0xFF08
        and not (key:ctrl() or key:alt() or key:super() or key:shift()) then
        local ctx = env.engine and env.engine.context
        if not (ctx and ctx:is_composing()) then
            if (env.bs_ignore or 0) > 0 and env.bs_ignore_until
                and os.clock() <= env.bs_ignore_until then
                env.bs_ignore = env.bs_ignore - 1
            else
                env.bs_ignore = 0
                consume_from_stack(env, 1)
                if log and log.info then
                    log.info("actions.lua: backspace took one char off the ledger")
                end
            end
        end
        return kNoop
    end

    -- ---- 方式二：组合键（完全不碰 context）----
    -- 组合键与文本触发码可以同时启用：先看组合键，没命中再往下走文本逻辑
    if env.revert_hotkey and hotkey_matches(key, env.revert_hotkey) then
        local context = env.engine.context
        -- 有没打完的编码时不动：注入的退格会被编码吃掉，反而删不掉东西
        if context and context:is_composing() then
            if log and log.info then
                log.info("actions.lua: hotkey ignored while composing")
            end
            return kAccepted
        end

        fire_delete(env)
        return kAccepted
    end

    -- ---- 方式三：文本快捷触发码（zz; 之类）----
    -- 每个触发码自带前缀，和文本触发码互不干扰；不参与 debounce（dll 那边有硬保护）
    local context = env.engine.context
    if context and env.revert_actions and #env.revert_actions > 0 then
        if not (key:ctrl() or key:alt() or key:super()) then
            for i = 1, #env.revert_actions do
                local action = env.revert_actions[i]
                if key.keycode == action.code and context.input == action.prefix then
                    context:clear()

                    if action.kind == "keys" then
                        fire_keys(env, action)
                    elseif action.kind == "symbol" then
                        fire_to_symbol(env, action.include)
                    elseif action.kind == "search" then
                        fire_search(env)
                    elseif action.kind == "ai" then
                        if env.ai_enabled then
                            ai_toggle(env, action.prompt)
                        elseif log and log.error then
                            log.error("actions.lua: ai shortcut ignored (actions/ai/enabled is false): "
                                .. action.trigger)
                        end
                    else
                        fire_delete(env)
                    end
                    return kAccepted
                end
            end
        end
    end

    -- ---- 方式一：文本触发码（在翻译流水线开始前把前缀清掉）----
    if not env.revert_final_code then return kNoop end
    if key:ctrl() or key:alt() or key:super() then return kNoop end
    if key.keycode ~= env.revert_final_code then return kNoop end
    if not context or context.input ~= env.revert_prefix then return kNoop end

    -- 先把已经拼出来的前缀清掉；这一键也不会再进入编码
    context:clear()

    local now = os.clock()
    local debounce = env.revert_debounce or (DEFAULTS.debounce_ms / 1000)
    if env.revert_last and (now - env.revert_last) < debounce then
        return kAccepted
    end
    env.revert_last = now

    fire_delete(env)
    return kAccepted
end

return {
    init = init,
    func = func,
    fini = fini,
    -- 给离线测试看的：预置的快捷键表
    default_shortcuts = DEFAULT_SHORTCUTS,
    -- 给离线测试看的：跳过配置直接切日志开关
    set_debug = function(on) DEBUG = (on == true) end,
}
