-- ai_pinyin_insert.lua —— AI 拼音候选：把 AI 给出的词插到候选第一位
--
-- 这个滤波器排在方案 filters 的末尾（uniquifier 之前），此时排序和去重都已完成，
-- 所以第一个 yield 出去的候选就是用户看到的第一个。
--
-- 它做两件事：
--   1. 把"第一个候选"和它的拼音告诉处理器（处理器据此发请求）；
--   2. 结果回来以后，把 AI 词插到第一位，其余候选原样、原顺序照旧放出去。
--
-- 拼音和候选的对应关系来自 ai_pinyin_capture.lua（它排在
-- super_comment_preedit 之前，只读、不改候选），写进 context 属性
-- ai_pinyin/map，每行是「候选文字 \t 拼音音节 拼音音节」。
--
-- 处理器写进来的四个属性：
--   ai_pinyin/result      要插入的 AI 词（空字符串表示这次没有）
--   ai_pinyin/result_sig  这个结果对应的输入编码
--   ai_pinyin/marker      插在候选注释位置的标记（可配，默认 "AI"）
--   ai_pinyin/same        "1" 表示 AI 的答案就是当前第一位（只标记、不新增候选）

local M = {}

local function read_property(ctx, name)
    if not (ctx and ctx.get_property) then return "" end
    local ok, value = pcall(ctx.get_property, ctx, name)
    if ok and type(value) == "string" then return value end
    return ""
end

local function write_property(ctx, name, value)
    if not (ctx and ctx.set_property) then return end
    local value_now = value or ""
    if read_property(ctx, name) == value_now then return end
    pcall(ctx.set_property, ctx, name, value_now)
end

-- 只有处理器把 AI 拼音模式打开时（ai_pinyin/active == "1"）才插候选、才写属性；
-- 关着的时候跟没有接这个滤波器一样。
local function reading(ctx)
    if not (ctx and ctx.get_property) then return false end
    return read_property(ctx, "ai_pinyin/active") == "1"
end

-- 把 ai_pinyin/map 的文本解析成 表[候选文字] = 拼音
local function parse_map(raw)
    local map = {}
    if type(raw) ~= "string" or raw == "" then return map end
    for line in raw:gmatch("[^\n]+") do
        local text, py = line:match("^([^\t]+)\t(.*)$")
        if text and py and py ~= "" then map[text] = py end
    end
    return map
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

    -- 先把所有候选收下来：既要知道第一个是谁，也要一个个原样放出去
    local cands = {}
    for cand in input:iter() do
        cands[#cands + 1] = cand
    end

    -- 第一个候选的音节（没有拼音的候选——编码类、日期类——在这里就是空的）
    local map = parse_map(read_property(ctx, "ai_pinyin/map"))
    local first = cands[1]
    local first_text = first and first.text or ""
    local first_py = first and (map[first_text] or "") or ""
    write_property(ctx, "ai_pinyin/first", first_text)
    write_property(ctx, "ai_pinyin/first_py", first_py)

    -- 结果只对"同一个输入编码"有效，编码变了就不插、也不标记
    local result = read_property(ctx, "ai_pinyin/result")
    local result_sig = read_property(ctx, "ai_pinyin/result_sig")
    local same = read_property(ctx, "ai_pinyin/same")
    local sig = (ctx and ctx.input) or ""
    local marker = read_property(ctx, "ai_pinyin/marker")

    if result_sig == sig and first then
        local start = first.start or 0
        local stop = first._end or start
        if same == "1" then
            -- AI 的答案就是眼下第一位：不新增候选，只给它的注释加标记，
            -- 让用户知道"查询过了、结果一致"
            local comment = marker
            if first.comment and first.comment ~= "" then
                comment = (marker ~= "" and (marker .. " " .. first.comment)) or first.comment
            end
            if comment == "" then comment = "-" end
            yield(Candidate("ai_pinyin_same", start, stop, first.text, comment))
            for i = 2, #cands do yield(cands[i]) end
            return
        end
        if result ~= "" then
            yield(Candidate("ai_pinyin", start, stop, result, marker))
        end
    end

    for _, cand in ipairs(cands) do
        yield(cand)
    end
end

return M
