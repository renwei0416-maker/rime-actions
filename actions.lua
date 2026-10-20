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
local DEFAULTS = {
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

    -- AI（可选，默认关闭；不写 actions/ai/enabled: true 就不会启用）
    ai_enabled     = false,
    ai_send_key    = "/",
    ai_double_ms   = 1000,
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
    ai_history_chars = 5000,
    ai_nudge_key   = 255,
    ai_nudge_interval = 60000,
    ai_max_wait_s  = 600,
    ai_max_tokens  = 100000,
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
    backspace = 0x08, tab = 0x09, ["return"] = 0x0D, escape = 0x1B, space = 0x20,
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
    if spec:find("@defaults", 1, true) then
        for _, item in ipairs(DEFAULT_SHORTCUTS) do
            entries[#entries + 1] = { trigger = item[1], action = item[2] }
        end
    end

    local rest = spec:gsub("@defaults", " ")
    for token in rest:gmatch("%S+") do
        local trigger, action = token:match("^([^=]+)=(.+)$")
        if trigger and action then
            entries[#entries + 1] = { trigger = trigger, action = action }
        elseif log and log.error then
            log.error("actions.lua: bad shortcut entry ignored (want 触发码=动作): " .. token)
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

local function on_commit(text, env)
    if not text or text == "" then return end

    local length = utf8.len(text)
    if not length or length < 1 then return end

    local stack = env.revert_stack
    if not stack then return end

    stack[#stack + 1] = { n = length, text = text }
    trim_stack(env)
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
    consume_from_stack(env, accepted)
    return true
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

    local limit = env.revert_limit or DEFAULTS.max_backspaces
    local count = entry.n
    if count > limit then
        if log and log.info then
            log.info(string.format(
                "actions.lua: commit of %d chars truncated to max_backspaces=%d", count, limit))
        end
        count = limit
    end

    inject_backspaces(env, count)
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

    local limit = env.revert_limit or DEFAULTS.max_backspaces
    if need > limit then
        if log and log.info then
            log.info(string.format(
                "actions.lua: %d chars to the previous symbol truncated to max_backspaces=%d",
                need, limit))
        end
        need = limit
    end

    -- 成功注入后 consume_from_stack 已经按"实际删掉的字符数"扣过账了
    inject_backspaces(env, need)
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
    default = "你是中文写作助手。把用户给你的文字整理通顺，保持原意和语气，只输出结果正文，不要解释、不要加引号。",
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

local function ai_has_cjk(text)
    if type(text) ~= "string" then return false end
    for i = 1, #text do
        local b = text:byte(i)
        if b and b >= 0xE4 and b <= 0xE9 then return true end
    end
    return false
end

local function ai_safe_str(fn)
    local ok, value = pcall(fn)
    if ok and type(value) == "string" then return value end
    return ""
end

-- 输入框里已经拼好的文字：优先取含汉字的那个，都没有才退回编码本身
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

-- 已经上屏过的内容（从旧到新拼起来），默认跳过 AI 自己打进去的那些
local function ai_history_text(env)
    local stack = env.revert_stack or {}
    local parts = {}
    for i = 1, #stack do
        local entry = stack[i]
        if entry and type(entry.text) == "string" and entry.text ~= ""
            and not (env.ai_skip_own and entry.ai) then
            parts[#parts + 1] = entry.text
        end
    end
    return table.concat(parts)
end

-- 从后往前找最后一个分隔符，取它之后的内容；一个都没有就整段都要
-- 返回：要发的句子、如果 replace 要往回删几个字符、断句点之前的上文、以及"断句点之后到末尾"
-- 的原文（replace 只在这一段没有换行时才动手）
local function ai_last_sentence(text, marks, symbols, include_marks)
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

local function ai_send_now(env)
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
    if source == "composition" then
        text = ai_composition_text(ctx)
        replace = false
        cut = 0
        before = ""
        span = ""
    else
        text, cut, before, span = ai_last_sentence(ai_history_text(env), env.ai_marks,
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
    if source ~= "composition" and not ai_has_cjk(text) then
        if log and log.info then
            log.info("actions.lua: ai: nothing to send (no 汉字 in the committed text)")
        end
        return
    end

    -- 输入框里的内容已经"发走"了，不再留在屏幕上
    if source == "composition" and composing then ctx:clear() end

    local prompt = env.ai_mode and env.ai_mode.prompt or "default"
    local system_prompt = (env.ai_prompts and (env.ai_prompts[prompt] or env.ai_prompts.default)) or ""

    local history = ""
    if (env.ai_history_chars or 0) > 0 and before and before ~= "" then
        history = ai_tail(before, env.ai_history_chars)
    end

    local parts = {}
    parts[#parts + 1] = '{"model":"' .. json_escape(env.ai_model or "") .. '","messages":['
    if system_prompt ~= "" then
        local sys = system_prompt
        if history ~= "" then sys = sys .. "\n\n【上文】" .. history end
        parts[#parts + 1] = '{"role":"system","content":"' .. json_escape(sys) .. '"},'
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
        cut     = replace and cut or 0,
        span    = span or "",
        started = os.clock(),
    }
    if log and log.info then
        log.info(string.format("actions.lua: ai: sent %d chars (prompt=%s, source=%s)",
            utf8.len(text) or #text, prompt, source))
    end
    if env.ai_nudge and Actions.nudge then
        Actions.nudge(env.ai_nudge_interval or DEFAULTS.ai_nudge_interval, env.ai_nudge_key)
    end
end

-- 轮询后台请求；返回 true = 这一次有了结论（成功或失败）
local function ai_take(env)
    local req = env.ai_req
    if not req or not Actions or not Actions.http_state then return false end

    local ok, state = pcall(Actions.http_state)
    if not ok then return false end

    if state == "running" then
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

    -- 开了 replace：先把原文删掉——从光标往回删到"上一组符号"之后，也就是删掉那一整句
    -- 连同它末尾的那串符号；这一段里有换行就不删（跨行不动）
    if req.replace and req.cut and req.cut > 0
        and not (req.span or ""):find("\n", 1, true) then
        inject_backspaces(env, req.cut)
    end

    local sent, send_err = pcall(Actions.send_text, content)
    if not sent then
        if log and log.error then
            log.error("actions.lua: ai: send_text failed: " .. tostring(send_err))
        end
        return true
    end

    local length = utf8.len(content) or 0
    if length > 0 then push_back(env, { n = length, text = content, ai = true }) end
    if log and log.info then
        log.info(string.format("actions.lua: ai: inserted %d chars", length))
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

-- 按键入口：返回 1/2 表示"这一键我处理了/我没处理"，返回 nil 表示交给主流程
local function ai_handle_key(key, env)
    local kc = key.keycode

    -- 唤醒键：把结果取回来；还在等就再约一次
    if kc == env.ai_nudge_key then
        ai_take(env)
        if env.ai_req and env.ai_nudge and Actions.nudge then
            Actions.nudge(env.ai_nudge_interval or DEFAULTS.ai_nudge_interval, env.ai_nudge_key)
        end
        return kAccepted
    end

    if key:release() then return nil end

    if env.ai_req then ai_take(env) end

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
                if inp:sub(-1) == env.ai_send.char and #inp > 1 and ctx.pop_input then
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
            "actions.lua: ai enabled (send_key=%s double_ms=%d source=%s marks=%s nudge=%s provider=%s model=%s prompts=%s)",
            send_spec, env.ai_double_ms, env.ai_source,
            cfg_string(config, "actions/ai/sentence_marks", DEFAULTS.ai_marks),
            env.ai_nudge and "on" or "off", provider, tostring(env.ai_model),
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
    -- ---- 方式四：AI（默认关闭；没开就完全跳过）----
    if env.ai_enabled then
        local handled = ai_handle_key(key, env)
        if handled ~= nil then return handled end
        -- AI 模式下其他按键都正常进输入法，不走下面的触发码
        if env.ai_mode then return kNoop end
    end

    if key:release() then return kNoop end

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
}
