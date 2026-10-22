-- 只读滤波器：把每个候选在"注释被万象改写之前"的拼音记录下来。
--
-- 万象给词典里的候选填了 spelling_hints，注释形如
--     nǐ;rx hǎo;nz
-- 即每个音节"带调拼音;辅助码"，音节之间用空格分开。注释随后会被
-- super_comment_preedit 改写，所以这个滤波器必须排在它前面，并且
-- 只读取、不修改任何候选。
--
-- 记录结果写进 context 属性，供插入滤波器与处理器读取：
--     ai_pinyin/map       每行 "候选文本<TAB>拼音音节(空格分隔)"
--     ai_pinyin/map_input 记录时的输入码
local M = {}

local TONE = {
    ["ā"] = "a", ["á"] = "a", ["ǎ"] = "a", ["à"] = "a",
    ["ē"] = "e", ["é"] = "e", ["ě"] = "e", ["è"] = "e",
    ["ī"] = "i", ["í"] = "i", ["ǐ"] = "i", ["ì"] = "i",
    ["ō"] = "o", ["ó"] = "o", ["ǒ"] = "o", ["ò"] = "o",
    ["ū"] = "u", ["ú"] = "u", ["ǔ"] = "u", ["ù"] = "u",
    ["ǖ"] = "v", ["ǘ"] = "v", ["ǚ"] = "v", ["ǜ"] = "v", ["ü"] = "v",
    ["ń"] = "n", ["ň"] = "n", ["ǹ"] = "n", ["ḿ"] = "m",
}

local function split_chars(text)
    local chars = {}
    local ok = pcall(function()
        for _, code in utf8.codes(text) do
            chars[#chars + 1] = utf8.char(code)
        end
    end)
    if not ok then return chars end
    return chars
end

-- 去声调，只留字母；认不出的字符一律丢掉
local function plain(text)
    local out = {}
    for _, ch in ipairs(split_chars(text)) do
        local t = TONE[ch]
        if t then
            out[#out + 1] = t
        elseif ch:match("^[a-zA-Z]$") then
            out[#out + 1] = ch:lower()
        end
    end
    return table.concat(out)
end

-- "nǐ;rx hǎo;nz" -> { "ni", "hao" }；有任何一段不像拼音就返回 nil
local function parse_comment(comment)
    if type(comment) ~= "string" or comment == "" then return nil end
    local list = {}
    for token in comment:gmatch("%S+") do
        local py = token:match("^([^;]+)") or token
        py = plain(py)
        if not py:match("^[a-z]+$") then return nil end
        list[#list + 1] = py
    end
    if #list == 0 then return nil end
    return list
end

-- 只有处理器把 AI 拼音模式打开时（ai_pinyin/active == "1"）才记录拼音；
-- 关着的时候什么都不写，跟没有接这个滤波器一样。
local function reading(ctx)
    if not (ctx and ctx.get_property) then return false end
    local ok, value = pcall(ctx.get_property, ctx, "ai_pinyin/active")
    return ok and value == "1"
end

local function stored(ctx, key)
    if not (ctx and ctx.get_property) then return nil end
    local ok, value = pcall(ctx.get_property, ctx, key)
    if ok then return value end
    return nil
end

local function store(ctx, key, value)
    if not (ctx and ctx.set_property) then return end
    if stored(ctx, key) == value then return end
    pcall(ctx.set_property, ctx, key, value)
end

function M.init(env)
end

function M.fini(env)
end

function M.func(input, env)
    local ctx = env and env.engine and env.engine.context
    if not reading(ctx) then
        for cand in input:iter() do
            yield(cand)
        end
        return
    end
    local lines = {}
    for cand in input:iter() do
        local list = parse_comment(cand.comment)
        if list then
            lines[#lines + 1] = cand.text .. "\t" .. table.concat(list, " ")
        end
        yield(cand)
    end
    store(ctx, "ai_pinyin/map", table.concat(lines, "\n"))
    store(ctx, "ai_pinyin/map_input", ctx.input or "")
end

return M
