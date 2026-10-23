"""Offline verification for backspace-build (v4: processor component).

Loads rime.dll's own embedded Lua 5.4 (the runtime WeaselServer actually uses),
requires the freshly built actions.dll, and drives actions.lua the way Rime
does for a **processor** component: every call gets a raw KeyEvent, and the
return value decides what the engine does with that key.

  1. module load + ABI check (luaL_checkversion_ inside luaL_newlib)
  2. exported API surface (press_backspace present, press_ctrl_z gone)
  3. C-level guards that inject nothing: press_backspace(0) -> 0,
     press_backspace(-5) -> 0, bad argument -> error (raised before any work)
     NOTE: the MAX_BACKSPACES clamp cannot be exercised on the *production*
     dll without really sending keys; it is checked against the dry-run dll in
     scenario F and statically with objdump (cmp $0x2710).
  4. actions.lua with press_backspace STUBBED, so no keystroke is ever injected:
        A. no schema config      -> defaults (text trigger "qq/", stack 50, max 500)
                                    and the engine feeds keys into the mock
                                    composition, exactly like Rime would
        B. config: trigger/stack -> trigger "q/", stack_size 3, max 100
        C. config: max_backspaces-> 4-char commit truncated to 2
        D. config: absurd values -> clamped to LIMITS (stack 1, max 10000)
        G. config: hotkey        -> combo mode: the hotkey is swallowed without
                                    touching the composition at all, the text
                                    trigger is inert, and while a composition is
                                    open the hotkey is refused (documented)
        E. config: wrong dll_dir -> graceful failure, nothing injected
  5. F. end-to-end async path, using a second dll compiled with
     -DACTIONS_DRY_RUN: send_batch() counts the key events it *would* send
     instead of calling SendInput. This verifies, without touching the keyboard:
        - the trigger key returns immediately (returns the accepted count)
        - the background thread fires ~FIRE_DELAY_MS later, in batches
        - a request inside MIN_INTERVAL_MS is dropped (returns 0) and the Lua
          side puts that commit back on the stack, so a later retry still deletes
        - the accepted count is clamped to MAX_BACKSPACES (10000)
     The production dll contains none of that (the macro is off there).

Nothing here sends input to Windows.
"""
import ctypes
import os
import shutil
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HERE = os.path.dirname(os.path.abspath(__file__))


def _pick_base():
    """源码目录：本目录（仓库根）或本目录下的 backspace-build（本地工作区）。"""
    forced = os.environ.get("ACTIONS_BASE")
    if forced:
        return forced
    for cand in (_HERE, os.path.join(_HERE, "backspace-build"),
                 os.path.dirname(_HERE)):
        if os.path.isfile(os.path.join(cand, "actions.c")):
            return cand
    return _HERE


BASE = _pick_base()
MINGW_BIN = os.environ.get("MINGW_BIN", r"C:\msys64\mingw64\bin")
WEASEL_DIR = os.environ.get("WEASEL_DIR", r"C:\Program Files\Rime\weasel-0.17.4")

# Optional argv[1]: path of any other rime.dll, to test whether an upgraded
# Rime still provides the Lua 5.4 ABI this module was built against.
#   python smoke_backspace.py "C:\Program Files\Rime\weasel-0.18.0\rime.dll"
RIME = sys.argv[1] if len(sys.argv) > 1 else os.path.join(WEASEL_DIR, "rime.dll")
USERDIR_ON_DISK = os.path.join(BASE, "_testdata", "userdir")
USERDIR_DRY = os.path.join(BASE, "_testdata", "dryrun", "userdir")
DLL_REL_DIR = "lua/wanxiang/ziji"
DLL_DIR = os.path.join(USERDIR_ON_DISK, "lua", "wanxiang", "ziji")
DLL_DIR_DRY = os.path.join(USERDIR_DRY, "lua", "wanxiang", "ziji")


def build_dry_run_dll():
    """Compile the counting-only dll used by scenario F."""
    os.makedirs(DLL_DIR_DRY, exist_ok=True)
    out = os.path.join(DLL_DIR_DRY, "actions.dll")
    env = dict(os.environ)
    env["PATH"] = MINGW_BIN + ";" + env["PATH"]
    cmd = [
        os.path.join(MINGW_BIN, "gcc.exe"), "-shared", "-fPIC",
        "-DACTIONS_DRY_RUN", "-o", out, "actions.c",
        "-Iinclude", "-L" + WEASEL_DIR, "-luser32", "-lrime", "-lshell32", "-lwinhttp", "-O2",
        "-Wl,--no-insert-timestamp",
    ]
    proc = subprocess.run(cmd, cwd=BASE, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        print("dry-run build FAILED:", proc.stdout, proc.stderr)
        return False
    print("dry-run dll:", os.path.getsize(out), "bytes")
    return True


def prepare_fake_userdir():
    """正式 dll 不在仓库里时，用刚编出来的 dry-run dll 顶上（断言一样能跑）。"""
    os.makedirs(DLL_DIR, exist_ok=True)
    src = os.path.join(BASE, "actions.dll")
    if not os.path.isfile(src):
        src = os.path.join(DLL_DIR_DRY, "actions.dll")
    shutil.copy2(src, os.path.join(DLL_DIR, "actions.dll"))


LUA_SRC = r"""
-- ============ stubs standing in for the Rime host ============
local logs = {}
rime_api = { get_user_data_dir = function() return USERDIR end }
log = {
  info    = function(m) logs[#logs + 1] = "info: " .. tostring(m) end,
  warning = function(m) logs[#logs + 1] = "warning: " .. tostring(m) end,
  error   = function(m) logs[#logs + 1] = "error: " .. tostring(m) end,
}

-- keep the real clock for busy-waiting, then fake os.clock so actions.lua's
-- own debounce never blocks a test trigger
local real_clock = os.clock
local function sleep_ms(ms)
  local t0 = real_clock()
  while (real_clock() - t0) * 1000 < ms do end
end
local tick = 1000.0
os.clock = function() tick = tick + 1.0 return tick end

local out = {}
local function rec(k, v) out[#out + 1] = k .. "=" .. tostring(v) end
local function last_log() return logs[#logs] or "" end
local function log_has(pattern)
  for _, m in ipairs(logs) do if m:find(pattern, 1, true) then return true end end
  return false
end

-- ============ load the real component ============
local revert = assert(loadfile(ACTIONS_PATH))()
revert.set_debug(true)   -- 测试要断言 info 日志；生产默认只写 error
rec("chunk_loaded", type(revert) == "table")
rec("has_init", type(revert.init))
rec("has_func", type(revert.func))
rec("has_fini", type(revert.fini))

-- ---------- fake host: context + commit_notifier + key events ----------
local function new_env(config)
  local ctx = { input = "", cleared = 0 }
  ctx.clear = function(self) self.cleared = self.cleared + 1; self.input = "" end
  ctx.is_composing = function(self) return #(self.input or "") > 0 end
  ctx.pop_input = function(self, n)
    n = n or 1
    local len = #(self.input or "")
    if n > len then n = len end
    self.input = self.input:sub(1, len - n)
    self.popped = (self.popped or 0) + n
  end
  ctx.commit_notifier = {
    connect = function(self, fn)
      ctx.fn = fn
      return { disconnect = function() ctx.disconnected = true end }
    end,
  }
  -- librime 的 context.commit_history：万象快符走 engine:commit_text()，
  -- 只进这里、不触发 commit_notifier
  ctx.commit_history = {
    list = {},
    push = function(self, text, kind)
      self.list[#self.list + 1] = { type = kind or "raw", text = text }
      while #self.list > 20 do table.remove(self.list, 1) end
    end,
    to_table = function(self)
      local out = {}
      for i = 1, #self.list do
        out[i] = { type = self.list[i].type, text = self.list[i].text }
      end
      return out
    end,
    latest_text = function(self)
      local n = #self.list
      if n == 0 then return "" end
      return self.list[n].text
    end,
  }
  -- context 属性：处理器和滤波器之间靠它传消息（候选文字、拼音、AI 结果）
  ctx.props = {}
  ctx.set_property = function(self, name, value) self.props[name] = value end
  ctx.get_property = function(self, name) return self.props[name] end
  ctx.refreshed = 0
  ctx.refresh_non_confirmed_composition = function(self)
    self.refreshed = self.refreshed + 1
  end
  local env = { engine = { context = ctx } }
  if config then env.engine.schema = { config = config } end
  return env, ctx
end

-- a KeyEvent as librime-lua hands it to a processor
local function key(code, opts)
  opts = opts or {}
  return {
    keycode = code,
    modifier = opts.modifier or 0,
    release = function() return opts.release == true end,
    ctrl    = function() return opts.ctrl == true end,
    shift   = function() return opts.shift == true end,
    alt     = function() return opts.alt == true end,
    super   = function() return opts.super == true end,
    repr    = function() return opts.repr or string.char(code) end,
  }
end

-- call the component like Rime does, then let the "engine" append the key to
-- the composition when the component says kNoop (2) for a plain character
local function feed(comp, env, ctx, k)
  local r = comp.func(k, env)
  if r == 2 and k.keycode >= 0x20 and k.keycode <= 0x7E
     and not (k:ctrl() or k:alt() or k:super()) then
    ctx.input = ctx.input .. string.char(k.keycode)
  end
  return r
end

local function type_text(comp, env, ctx, s)
  ctx.input = ""              -- every attempt starts from an empty composition
  local res = {}
  for i = 1, #s do res[#res + 1] = tostring(feed(comp, env, ctx, key(s:byte(i)))) end
  return table.concat(res, ",")
end

local injected = {}
local function commit(ctx, text)
  if ctx.fn then ctx.fn({ get_commit_text = function() return text end }) end
  if ctx.commit_history then ctx.commit_history:push(text) end
end

-- 万象快符：只进 commit_history，不触发上屏通知
local function quick(ctx, text)
  if ctx.commit_history then ctx.commit_history:push(text) end
end

-- =====================================================================
-- A. defaults: no schema config at all (text trigger "qq/")
-- =====================================================================
local envA, ctxA = new_env(nil)
revert.init(envA)

local mod = package.loaded["actions"]
rec("A_module", type(mod))
rec("A_press_backspace", mod and type(mod.press_backspace))
rec("A_press_ctrl_z", tostring(mod and mod.press_ctrl_z))
rec("A_init_log", last_log())
rec("A_mode_text", tostring(log_has("mode=text")))

-- C-level guards: these must do nothing
rec("A_c_count_zero", mod.press_backspace(0))
rec("A_c_negative", mod.press_backspace(-5))
local ok_bad, err_bad = pcall(mod.press_backspace, "not-a-number")
rec("A_c_badarg_raises", (not ok_bad) and "true" or "false")
rec("A_c_badarg_msg_has_number", tostring(err_bad):find("number") ~= nil and "true" or "false")

-- now stub the injection so the accounting can be tested safely
injected = {}
mod.press_backspace = function(n) injected[#injected + 1] = n return n end

commit(ctxA, "\u{4f60}\u{597d}\u{4e16}\u{754c}")  -- 4 chars
commit(ctxA, "abc")                              -- 3
commit(ctxA, "")                                 -- ignored
commit(ctxA, "a\240\159\152\128b")               -- emoji: 3 codepoints
commit(ctxA, nil)                                -- ignored

rec("A_returns", type_text(revert, envA, ctxA, "qq/"))   -- 2,2,1 = noop,noop,accepted
rec("A_injected", table.concat(injected, ","))           -- newest commit = 3
rec("A_input_after", "[" .. ctxA.input .. "]")           -- trigger was swallowed

injected = {}
type_text(revert, envA, ctxA, "qq/")   -- next commit = 3
type_text(revert, envA, ctxA, "qq/")   -- next        = 4
rec("A_injected_all", table.concat(injected, ","))
type_text(revert, envA, ctxA, "qq/")   -- stack empty -> inject nothing
rec("A_log_has_nothing_to_delete", tostring(log_has("nothing to delete")))
rec("A_cleared", ctxA.cleared)         -- one clear per recognised trigger

injected = {}
rec("A_no_trigger_q_slash", type_text(revert, envA, ctxA, "q/"))     -- not the trigger
rec("A_no_trigger_other", type_text(revert, envA, ctxA, "other"))
rec("A_no_trigger_injected", table.concat(injected, ","))
revert.fini(envA)
rec("A_disconnected", tostring(ctxA.disconnected == true))

-- =====================================================================
-- B. config: trigger + stack_size   (actions/trigger, actions/stack_size)
-- =====================================================================
local cfgB = {
  values = {
    ["actions/trigger"] = "q/",
    ["actions/dll_dir"] = DLLREL,
    ["actions/stack_size"] = 3,
    ["actions/max_backspaces"] = 100,
    ["actions/debounce_ms"] = 0,
  },
}
cfgB.get_string = function(self, k) return self.values[k] end
cfgB.get_int = function(self, k) return self.values[k] end

local envB, ctxB = new_env(cfgB)
revert.init(envB)
rec("B_init_log", last_log())
rec("B_trigger_from_config", tostring(log_has("trigger=q/") and log_has("stack_size=3")
                                     and log_has("max_backspaces=100")))

commit(ctxB, "a")      -- 1
commit(ctxB, "bb")     -- 2
commit(ctxB, "ccc")    -- 3
commit(ctxB, "dddd")   -- 4
commit(ctxB, "eeeee")  -- 5  -> only the last 3 fit (3,4,5)

injected = {}
rec("B_old_trigger_returns", type_text(revert, envB, ctxB, "qq/"))  -- inert now
rec("B_old_trigger_injected", table.concat(injected, ","))
type_text(revert, envB, ctxB, "q/")   -- 5
type_text(revert, envB, ctxB, "q/")   -- 4
type_text(revert, envB, ctxB, "q/")   -- 3
rec("B_injected", table.concat(injected, ","))
type_text(revert, envB, ctxB, "q/")   -- stack empty
rec("B_cleared", ctxB.cleared)
revert.fini(envB)

-- =====================================================================
-- C. config: max_backspaces truncates a long commit
-- =====================================================================
local cfgC = {
  values = {
    ["actions/trigger"] = "qq/",
    ["actions/dll_dir"] = DLLREL,
    ["actions/stack_size"] = 50,
    ["actions/max_backspaces"] = 2,
    ["actions/debounce_ms"] = 0,
  },
}
cfgC.get_string = function(self, k) return self.values[k] end
cfgC.get_int = function(self, k) return self.values[k] end

local envC, ctxC = new_env(cfgC)
revert.init(envC)
commit(ctxC, "abcd")   -- 4 -> truncated to 2
commit(ctxC, "ee")     -- 2
injected = {}
type_text(revert, envC, ctxC, "qq/")
type_text(revert, envC, ctxC, "qq/")
rec("C_injected", table.concat(injected, ","))
rec("C_log_has_truncated", tostring(log_has("truncated to max_backspaces=2")))

-- =====================================================================
-- D. config: absurd values are clamped to LIMITS
-- =====================================================================
local cfgD = {
  values = {
    ["actions/stack_size"] = -3,
    ["actions/max_backspaces"] = 999999,
    ["actions/debounce_ms"] = -1,
  },
}
cfgD.get_string = function(self, k) return self.values[k] end
cfgD.get_int = function(self, k) return self.values[k] end

local envD = new_env(cfgD)
revert.init(envD)
rec("D_init_log", last_log())
rec("D_clamped", tostring(log_has("stack_size=1") and log_has("max_backspaces=10000")
                          and log_has("debounce_ms=0")))

-- =====================================================================
-- G. config: hotkey AND text trigger together (both modes active)
-- =====================================================================
logs = {}
local cfgG = {
  values = {
    ["actions/hotkey"] = "Control+slash",
    ["actions/trigger"] = "qq/",
    ["actions/dll_dir"] = DLLREL,
    ["actions/stack_size"] = 50,
    ["actions/max_backspaces"] = 500,
    ["actions/debounce_ms"] = 0,
  },
}
cfgG.get_string = function(self, k) return self.values[k] end
cfgG.get_int = function(self, k) return self.values[k] end

local envG, ctxG = new_env(cfgG)
revert.init(envG)
rec("G_init_log", last_log())
rec("G_mode_both", tostring(log_has("mode=hotkey+text")))
rec("G_log_hotkey_spec", tostring(log_has("hotkey=Control+slash")))

commit(ctxG, "abc")                                    -- 3
injected = {}
rec("G_text_returns", type_text(revert, envG, ctxG, "qq/"))   -- text trigger still works
rec("G_text_injected", table.concat(injected, ","))

ctxG.input = ""                                        -- no open composition
commit(ctxG, "wxyz")                                   -- 4
local cleared_before = ctxG.cleared
rec("G_hotkey_return", tostring(feed(revert, envG, ctxG,
    key(0x2F, { ctrl = true, modifier = 4, repr = "Control+slash" }))))
rec("G_hotkey_injected", table.concat(injected, ","))
rec("G_cleared_unchanged", tostring(ctxG.cleared == cleared_before))
rec("G_input_after", "[" .. ctxG.input .. "]")

-- with a composition open the hotkey is refused (documented behaviour)
ctxG.input = "ni"
local n_before = #injected
rec("G_composing_return", tostring(feed(revert, envG, ctxG,
    key(0x2F, { ctrl = true, modifier = 4, repr = "Control+slash" }))))
rec("G_composing_ignored", tostring(#injected == n_before))
rec("G_log_has_composing", tostring(log_has("hotkey ignored while composing")))
revert.fini(envG)

-- G3: actions/trigger = "none" turns the text trigger off (hotkey only)
logs = {}
local cfgG3 = {
  values = {
    ["actions/hotkey"] = "Control+slash",
    ["actions/trigger"] = "none",
    ["actions/dll_dir"] = DLLREL,
    ["actions/debounce_ms"] = 0,
  },
}
cfgG3.get_string = function(self, k) return self.values[k] end
cfgG3.get_int = function(self, k) return self.values[k] end

local envG3, ctxG3 = new_env(cfgG3)
revert.init(envG3)
rec("G3_init_log", last_log())
rec("G3_mode_hotkey_only", tostring(log_has("mode=hotkey") and not log_has("mode=hotkey+text")))
commit(ctxG3, "abc")                                   -- 3
injected = {}
rec("G3_text_returns", type_text(revert, envG3, ctxG3, "qq/"))
rec("G3_text_injected", table.concat(injected, ","))
ctxG3.input = ""                                       -- no open composition
rec("G3_hotkey_return", tostring(feed(revert, envG3, ctxG3,
    key(0x2F, { ctrl = true, modifier = 4, repr = "Control+slash" }))))
rec("G3_hotkey_injected", table.concat(injected, ","))
revert.fini(envG3)

-- G2: the one-character spelling "Ctrl+/" parses to the same thing
local cfgG2 = {
  values = {
    ["actions/hotkey"] = "Ctrl+/",
    ["actions/dll_dir"] = DLLREL,
    ["actions/debounce_ms"] = 0,
  },
}
cfgG2.get_string = function(self, k) return self.values[k] end
cfgG2.get_int = function(self, k) return self.values[k] end

local envG2, ctxG2 = new_env(cfgG2)
revert.init(envG2)
commit(ctxG2, "xyz")                                   -- 3
injected = {}
rec("G2_hotkey_return", tostring(feed(revert, envG2, ctxG2,
    key(0x2F, { ctrl = true, modifier = 4, repr = "ctrl+/" }))))
rec("G2_injected", table.concat(injected, ","))
revert.fini(envG2)

-- =====================================================================
-- H. shortcuts: actions/shortcuts -> press_keys / @to-symbol / @delete
-- =====================================================================
local function cfg_plain(values)
  local c = { values = values }
  c.get_string = function(self, k) return self.values[k] end
  c.get_int = function(self, k) return self.values[k] end
  c.get_bool = function(self, k) return self.values[k] end
  c.get_double = function(self, k) return self.values[k] end
  return c
end

local keysent = {}
local keysent_gap = {}
local keyret = 1
mod.press_keys = function(spec, gap)
  keysent[#keysent + 1] = spec
  keysent_gap[#keysent_gap + 1] = gap
  return keyret
end

local urlsent = {}
local urlret = 1
mod.open_url = function(url) urlsent[#urlsent + 1] = url return urlret end

local function count_shortcuts()
  local s = last_log():match("shortcuts=([^ ]*)")
  if not s or s == "-" then return 0 end
  local n = 0
  for _ in s:gmatch("[^,]+") do n = n + 1 end
  return n
end

logs = {}
local cfgH = cfg_plain({
  ["actions/trigger"]        = "qq/",
  ["actions/shortcuts"]      = "@defaults pq;=@delete",
  ["actions/symbols"]        = "，。！？；：、,.!?;:",
  ["actions/dll_dir"]        = DLLREL,
  ["actions/stack_size"]     = 14,
  ["actions/max_backspaces"] = 500,
  ["actions/debounce_ms"]    = 0,
})
local envH, ctxH = new_env(cfgH)
revert.init(envH)
rec("H_init_log", last_log())
rec("H_mode_text", tostring(log_has("mode=text") and log_has("shortcuts=z;")))
rec("H_default_count", count_shortcuts())      -- 26 defaults + pq;
rec("H_default_table", #revert.default_shortcuts)

-- 每个默认触发码 -> 本来会发出去的那串按键
local function h_try(tag, s)
  keysent = {}
  keysent_gap = {}
  rec(tag .. "_ret", type_text(revert, envH, ctxH, s))
  rec(tag .. "_spec", table.concat(keysent, "|"))
  if tag == "H_z" then
    rec("H_gap_default", tostring(keysent_gap[1]))
  end
end
h_try("H_z",  "z;")
h_try("H_y",  "y;")
h_try("H_a",  "a;")
h_try("H_c",  "c;")
h_try("H_v",  "v;")
h_try("H_s",  "s;")
h_try("H_x",  "x;")
h_try("H_g",  "g;")
h_try("H_t",  "t;")
h_try("H_w",  "w;")
h_try("H_b",  "b;")
h_try("H_r",  "r;")
h_try("H_h",  "h;")
h_try("H_e",  "e;")
h_try("H_d",  "d;")
h_try("H_kk", "kk;")
h_try("H_aa", "aa;")
h_try("H_dd", "dd;")
h_try("H_ww", "ww;")
h_try("H_hh", "hh;")
h_try("H_tt", "tt;")
h_try("H_gg", "gg;")
h_try("H_xx", "xx;")
h_try("H_ss", "ss;")

-- 不吞键：前缀不对 / 末字符不对 / 只打了一半 / 带修饰键
h_try("H_wrong_prefix", "qz;")
h_try("H_wrong_final", "z,")
h_try("H_half", "z")
ctxH.input = "z"
keysent = {}
rec("H_ctrl_ret", tostring(feed(revert, envH, ctxH, key(0x3B, { ctrl = true, modifier = 4 }))))
rec("H_ctrl_spec", table.concat(keysent, "|"))

-- @delete 自定义触发码（pq;）与主触发码可以同时用
ctxH.input = ""
commit(ctxH, "abc")
injected = {}
keysent = {}
rec("H_pq_ret", type_text(revert, envH, ctxH, "pq;"))
rec("H_pq_injected", table.concat(injected, ","))

ctxH.input = ""
commit(ctxH, "abcd")
injected = {}
keysent = {}
rec("H_text_trigger_ret", type_text(revert, envH, ctxH, "qq/"))
rec("H_text_trigger_injected", table.concat(injected, ","))

-- @to-symbol（ff;）：只删到符号后面，符号保留
ctxH.input = ""
commit(ctxH, "\u{4eca}\u{5929}\u{5929}\u{6c14}\u{5f88}\u{597d}\u{ff0c}\u{6211}\u{51fa}\u{95e8}\u{4e86}")
injected = {}
keysent = {}
rec("H_ff_ret", type_text(revert, envH, ctxH, "ff;"))
rec("H_ff_injected", table.concat(injected, ","))
rec("H_ff_spec", table.concat(keysent, "|"))

ctxH.input = ""
commit(ctxH, "abc\u{ff0c}def")
injected = {}
type_text(revert, envH, ctxH, "ff;")
rec("H_ff_mid_symbol", table.concat(injected, ","))

ctxH.input = ""
commit(ctxH, "abcdef")                     -- 记账里没有符号 -> 一路删到记账最早一笔
injected = {}
logs = {}
type_text(revert, envH, ctxH, "ff;")
rec("H_ff_no_symbol", "[" .. table.concat(injected, ",") .. "]")
rec("H_ff_no_symbol_log", tostring(log_has("nothing to delete before the first symbol")))

ctxH.input = ""
commit(ctxH, "abc\u{ff0c}")                 -- 最新一笔就以符号结尾 -> 没有可删的
injected = {}
logs = {}
type_text(revert, envH, ctxH, "ff;")
rec("H_ff_trailing_symbol", "[" .. table.concat(injected, ",") .. "]")
rec("H_ff_trailing_symbol_log", tostring(log_has("nothing to delete before the first symbol")))

-- press_keys 的返回值处理：2 = 忙，0 = 被拒
logs = {}
keyret = 2
rec("H_busy_ret", type_text(revert, envH, ctxH, "z;"))
rec("H_busy_log", tostring(log_has("press_keys ignored")))
keyret = 0
rec("H_refused_ret", type_text(revert, envH, ctxH, "z;"))
rec("H_refused_log", tostring(log_has("press_keys refused the sequence: ctrl+z")))
keyret = 1

-- @search（f;）：打开浏览器搜索最后一次上屏的原文
ctxH.input = ""
commit(ctxH, "\u{4eca}\u{5929}\u{5929}\u{6c14}\u{5f88}\u{597d}")
urlsent = {}
logs = {}
rec("H_f_ret", type_text(revert, envH, ctxH, "f;"))
rec("H_f_url", table.concat(urlsent, "|"))
urlret = 2
type_text(revert, envH, ctxH, "f;")
rec("H_f_busy_log", tostring(log_has("open_url ignored")))
urlret = 0
urlsent = {}
type_text(revert, envH, ctxH, "f;")
rec("H_f_refused_url", table.concat(urlsent, "|"))
rec("H_f_refused_log", tostring(log_has("open_url refused the address")))
urlret = 1
revert.fini(envH)

-- H2: @to-symbol-include / 多步序列 / @delete
logs = {}
local cfgH2 = cfg_plain({
  ["actions/trigger"]        = "qq/",
  ["actions/shortcuts"]      = "pp;=@to-symbol-include sm;=shift+home-shift+left dq;=@delete",
  ["actions/symbols"]        = "，。",
  ["actions/dll_dir"]        = DLLREL,
  ["actions/max_backspaces"] = 500,
  ["actions/debounce_ms"]    = 0,
})
local envH2, ctxH2 = new_env(cfgH2)
revert.init(envH2)
rec("H2_count", count_shortcuts())

commit(ctxH2, "\u{4eca}\u{5929}\u{5929}\u{6c14}\u{5f88}\u{597d}\u{ff0c}\u{6211}\u{51fa}\u{95e8}\u{4e86}")
injected = {}
rec("H2_pp_ret", type_text(revert, envH2, ctxH2, "pp;"))
rec("H2_pp_injected", table.concat(injected, ","))     -- 连符号一起删 -> 5

keysent = {}
rec("H2_sm_ret", type_text(revert, envH2, ctxH2, "sm;"))
rec("H2_sm_spec", table.concat(keysent, "|"))

ctxH2.input = ""
commit(ctxH2, "abc")
injected = {}
rec("H2_dq_ret", type_text(revert, envH2, ctxH2, "dq;"))
rec("H2_dq_injected", table.concat(injected, ","))
revert.fini(envH2)

-- H3: 配置写错的六种情况，每一项都要报错且不生效
logs = {}
local cfgH3 = cfg_plain({
  ["actions/trigger"]     = "qq/",
  ["actions/shortcuts"]   = "zz;=ctrl+z zz;=ctrl+y qq/=ctrl+a z=ctrl+b zz2;=@nope zz3;=ctrl+nope bareword",
  ["actions/dll_dir"]     = DLLREL,
  ["actions/debounce_ms"] = 0,
})
local envH3, ctxH3 = new_env(cfgH3)
revert.init(envH3)
rec("H3_dup_log", tostring(log_has("duplicate shortcut trigger disabled: zz;")))
rec("H3_same_log", tostring(log_has("shortcut trigger equals actions/trigger, disabled: qq/")))
rec("H3_short_log", tostring(log_has("shortcut trigger too short")))
rec("H3_unknown_log", tostring(log_has("unknown built-in action disabled: @nope")))
rec("H3_badseq_log", tostring(log_has("bad key sequence disabled: trigger=zz3;")))
rec("H3_badentry_log", tostring(log_has("bad shortcut entry ignored")))
keysent = {}
rec("H3_dup_fire_ret", type_text(revert, envH3, ctxH3, "zz;"))
rec("H3_dup_fire_spec", table.concat(keysent, "|"))
revert.fini(envH3)

-- H4/H5: @defaults 与混写
logs = {}
local cfgH4 = cfg_plain({
  ["actions/shortcuts"]   = "@defaults xy;=ctrl+shift+right",
  ["actions/dll_dir"]     = DLLREL,
  ["actions/debounce_ms"] = 0,
})
local envH4 = new_env(cfgH4)
revert.init(envH4)
rec("H4_count", count_shortcuts())
revert.fini(envH4)

logs = {}
local cfgH5 = cfg_plain({
  ["actions/shortcuts"]   = "@defaults",
  ["actions/dll_dir"]     = DLLREL,
  ["actions/debounce_ms"] = 0,
})
local envH5 = new_env(cfgH5)
revert.init(envH5)
rec("H5_count", count_shortcuts())
revert.fini(envH5)

-- H6: 自定义搜索地址模板（actions/search_url 可配置）
logs = {}
local cfgH6 = cfg_plain({
  ["actions/trigger"]     = "qq/",
  ["actions/shortcuts"]   = "f;=@search",
  ["actions/search_url"]  = "https://www.bing.com/search?q=%s",
  ["actions/dll_dir"]     = DLLREL,
  ["actions/debounce_ms"] = 0,
})
local envH6, ctxH6 = new_env(cfgH6)
revert.init(envH6)
commit(ctxH6, "abc")
urlsent = {}
rec("H6_search_ret", type_text(revert, envH6, ctxH6, "f;"))
rec("H6_search_url", table.concat(urlsent, "|"))

-- H7: 还没上屏过就按 f; -> 什么都不做
logs = {}
local envH7, ctxH7 = new_env(cfgH6)
revert.init(envH7)
urlsent = {}
rec("H7_search_ret", type_text(revert, envH7, ctxH7, "f;"))
rec("H7_search_url", "[" .. table.concat(urlsent, "|") .. "]")
rec("H7_search_log", tostring(log_has("nothing to search")))
revert.fini(envH6)
revert.fini(envH7)

-- H8: 按键间隔 actions/key_gap_ms（默认 30；可配；超上限截到 500）
logs = {}
local cfgH8 = cfg_plain({
  ["actions/shortcuts"]   = "z;=ctrl+z  hh;=shift+home-ctrl+c",
  ["actions/key_gap_ms"]  = 120,
  ["actions/dll_dir"]     = DLLREL,
  ["actions/debounce_ms"] = 0,
})
local envH8, ctxH8 = new_env(cfgH8)
revert.init(envH8)
rec("H8_log_gap", tostring(last_log():match("key_gap_ms=(%d+)")))
keysent, keysent_gap = {}, {}
rec("H8_ret", type_text(revert, envH8, ctxH8, "z;"))
rec("H8_gap", tostring(keysent_gap[1]))
keysent, keysent_gap = {}, {}
type_text(revert, envH8, ctxH8, "hh;")
rec("H8_multi_spec", table.concat(keysent, "|"))
rec("H8_multi_gap", tostring(keysent_gap[1]))
revert.fini(envH8)

logs = {}
local cfgH8b = cfg_plain({
  ["actions/shortcuts"]   = "z;=ctrl+z",
  ["actions/key_gap_ms"]  = 5000,
  ["actions/dll_dir"]     = DLLREL,
  ["actions/debounce_ms"] = 0,
})
local envH8b, ctxH8b = new_env(cfgH8b)
revert.init(envH8b)
rec("H8b_log_gap", tostring(last_log():match("key_gap_ms=(%d+)")))
keysent, keysent_gap = {}, {}
type_text(revert, envH8b, ctxH8b, "z;")
rec("H8b_gap_capped", tostring(keysent_gap[1]))
revert.fini(envH8b)

-- =====================================================================
-- I. AI 模式：@ai 触发码 / 取文 / 发送 / 结果插入 / Esc / replace
-- =====================================================================
local requests, sends, nudges = {}, {}, {}
local req_seq, req_state, result_body, result_err, cancels = 0, "idle", "", nil, 0
local clip_text, clip_err = nil, nil
local clip_pending, clip_requests = false, 0

mod.http_request = function(url, body, headers)
  requests[#requests + 1] = { url = url, body = body, headers = headers }
  req_seq = req_seq + 1
  req_state = "running"
  return req_seq
end
mod.http_state = function() return req_state, req_seq end
mod.http_result = function()
  req_state = "idle"
  if result_err then
    local e = result_err
    result_err = nil
    return nil, e
  end
  return result_body, nil
end
mod.http_cancel = function() cancels = cancels + 1; req_state = "idle"; return 1 end
mod.send_text = function(text) sends[#sends + 1] = text; return 1 end
-- "?????"???????? injected??????????????????
combined = {}
combined_return = nil
mod.press_backspace_then_text = function(n, text)
  injected[#injected + 1] = n
  combined[#combined + 1] = tostring(n) .. ":" .. text
  if combined_return then
    local r = combined_return
    combined_return = nil
    return r
  end
  return 1
end
mod.clipboard_get = function()
  if clip_err then local e = clip_err; clip_err = nil; return nil, e end
  if clip_text == nil then return nil, "剪贴板里没有文本" end
  return clip_text, nil
end
-- 异步版：request 排队，tick 用 take 取结果（按键回调里不再同步读剪贴板）
mod.clipboard_request = function()
  if clip_pending then return 2 end
  clip_pending = true
  clip_requests = clip_requests + 1
  return 1
end
mod.clipboard_take = function()
  if not clip_pending then return nil, nil end
  clip_pending = false
  if clip_err then local e = clip_err; clip_err = nil; return nil, e end
  if clip_text == nil then return nil, "剪贴板里没有文本" end
  return clip_text, nil
end
mod.nudge = function(delay, vk)
  nudges[#nudges + 1] = tostring(delay) .. "/" .. tostring(vk)
  return 1
end

-- I0: 没开 actions/ai/enabled 时 ai; 不生效
logs = {}
local cfgI0 = cfg_plain({
  ["actions/shortcuts"]   = "ai;=@ai",
  ["actions/dll_dir"]     = DLLREL,
  ["actions/debounce_ms"] = 0,
})
local envI0, ctxI0 = new_env(cfgI0)
revert.init(envI0)
rec("I0_log_ai", tostring(last_log():match("ai=(%a+)")))
rec("I0_flag", tostring(envI0.ai_enabled))
rec("I0_ret", type_text(revert, envI0, ctxI0, "ai;"))
rec("I0_mode", tostring(envI0.ai_mode))
rec("I0_log_ignored", tostring(log_has("ai shortcut ignored")))
revert.fini(envI0)

-- I1: 打开 AI -> 进模式 -> 上屏 -> 连按两次 / 发送
logs = {}
local cfgI = cfg_plain({
  ["actions/trigger"]              = "qq/",
  ["actions/shortcuts"]            = "ai;=@ai eng;=@ai:eng",
  ["actions/dll_dir"]              = DLLREL,
  ["actions/debounce_ms"]          = 0,
  ["actions/ai/enabled"]           = true,
  ["actions/ai/online/base_url"]   = "https://example.test/v1/chat/completions",
  ["actions/ai/online/model"]      = "test-model",
  ["actions/ai/online/api_key"]    = "sk-test",
  ["actions/ai/online/max_tokens"] = 128,
  ["actions/ai/prompts/eng"]       = "CUSTOM-ENG-PROMPT",
})
local envI, ctxI = new_env(cfgI)
revert.init(envI)
rec("I1_log_ai", tostring(last_log():match("ai=(%a+)")))
rec("I1_flag", tostring(envI.ai_enabled))
rec("I1_double_ms", tostring(envI.ai_double_ms))
rec("I1_send_code", tostring(envI.ai_send and envI.ai_send.code))
rec("I1_prompt_eng", tostring(envI.ai_prompts.eng))
rec("I1_enter_ret", type_text(revert, envI, ctxI, "ai;"))
rec("I1_mode", tostring(envI.ai_mode and envI.ai_mode.prompt))
rec("I1_log_mode_on", tostring(log_has("mode on (default)")))

commit(ctxI, "\u{4eca}\u{5929}\u{5929}\u{6c14}\u{5f88}\u{597d}\u{3002}\u{6211}\u{51fa}\u{95e8}\u{4e86}")
requests, sends, nudges = {}, {}, {}
rec("I1_press1", tostring(feed(revert, envI, ctxI, key(0x2F))))
rec("I1_press2", tostring(feed(revert, envI, ctxI, key(0x2F))))
rec("I1_req_count", tostring(#requests))
rec("I1_req_url", requests[1] and requests[1].url or "-")
rec("I1_req_model", requests[1] and (requests[1].body:match('"model":"([^"]*)"') or "-") or "-")
rec("I1_req_user",
    requests[1] and (requests[1].body:match('"role":"user","content":"([^"]*)"') or "-") or "-")
rec("I1_req_auth",
    requests[1] and (requests[1].headers:find("Bearer sk-test", 1, true) and "yes" or "no") or "-")
rec("I1_req_tokens", requests[1] and (requests[1].body:match('"max_tokens":(%d+)') or "-") or "-")
rec("I1_nudge", table.concat(nudges, ","))
rec("I1_log_sent", tostring(log_has("ai: sent")))

-- 上一个还没回来时再连按两次：不发第二个，只记一条日志
-- （测试里的 os.clock 每次调用前进 1 秒，等请求在跑时一次按键要花两次，所以把窗口放宽）
envI.ai_double_ms = 5000
envI.ai_last_push = os.clock() - 5
local logs_before = #logs
feed(revert, envI, ctxI, key(0x2F))
rec("I1_busy_ret", tostring(feed(revert, envI, ctxI, key(0x2F))))
rec("I1_busy_reqs", tostring(#requests))
rec("I1_busy_newlogs", tostring(#logs - logs_before))
rec("I1_busy_last", last_log())
envI.ai_double_ms = 1000

-- I2: 结果回来 -> 叫醒键取回 -> send_text 打进光标 -> 记账
req_state = "done"
result_body = '{"choices":[{"message":{"content":"\u{6211}\u{51fa}\u{95e8}\u{4e86}\u{ff0c}\u{5929}\u{6c14}\u{5f88}\u{597d}\u{3002}"}}]}'
sends, nudges, injected = {}, {}, {}
local stack_before = #envI.revert_stack
rec("I2_take_ret", tostring(feed(revert, envI, ctxI, key(0x87))))
envI.ai_typing_until = nil   -- 测试里跳过“结果正在打字”窗口
rec("I2_send_text", table.concat(sends, "|"))
rec("I2_stack_delta", tostring(#envI.revert_stack - stack_before))
rec("I2_stack_ai", tostring((envI.revert_stack[#envI.revert_stack] or {}).ai == true))
rec("I2_log_inserted", tostring(log_has("ai: inserted")))
rec("I2_injected", table.concat(injected, ","))

-- I3: replace=true 且原文在同一行 -> 先删原文再打字
logs = {}
local cfgI3 = cfg_plain({
  ["actions/shortcuts"]       = "ai;=@ai",
  ["actions/dll_dir"]         = DLLREL,
  ["actions/debounce_ms"]     = 0,
  ["actions/ai/enabled"]      = true,
  ["actions/ai/replace"]      = true,
  ["actions/ai/source"]       = "committed",
  ["actions/ai/online/model"] = "m",
})
local envI3, ctxI3 = new_env(cfgI3)
revert.init(envI3)
rec("I3_enter", type_text(revert, envI3, ctxI3, "ai;"))
commit(ctxI3, "\u{4e00}\u{4e8c}\u{4e09}")
req_state = "idle"
envI3.ai_last_push = os.clock()
requests, sends = {}, {}
feed(revert, envI3, ctxI3, key(0x2F))
req_state = "done"
result_body = '{"choices":[{"message":{"content":"XYZ"}}]}'
injected = {}
feed(revert, envI3, ctxI3, key(0x87))
envI3.ai_typing_until = nil   -- 测试里跳过“结果正在打字”窗口
rec("I3_reqs", tostring(#requests))
rec("I3_injected", table.concat(injected, ","))
rec("I3_send_text", table.concat(sends, "|"))
rec("I3_combined", combined and combined[#combined] or "")

-- I4: replace=true 但原文跨行 -> 一句都不删
commit(ctxI3, "\u{7b2c}\u{4e00}\u{884c}\n\u{7b2c}\u{4e8c}\u{884c}")
envI3.ai_last_push = os.clock()
requests, sends = {}, {}
feed(revert, envI3, ctxI3, key(0x2F))
req_state = "done"
result_body = '{"choices":[{"message":{"content":"XYZ"}}]}'
injected = {}
feed(revert, envI3, ctxI3, key(0x87))
envI3.ai_typing_until = nil   -- 测试里跳过“结果正在打字”窗口
rec("I4_injected", "[" .. table.concat(injected, ",") .. "]")
rec("I4_send_text", table.concat(sends, "|"))

-- I5: 后台报错 -> 只记日志，不插入
logs = {}
commit(ctxI3, "\u{4e00}")
envI3.ai_last_push = os.clock()
sends = {}
feed(revert, envI3, ctxI3, key(0x2F))
req_state = "done"
result_err = "HTTP 500"
injected = {}
feed(revert, envI3, ctxI3, key(0x87))
envI3.ai_typing_until = nil   -- 测试里跳过“结果正在打字”窗口
rec("I5_log_failed", tostring(log_has("ai: request failed - HTTP 500")))
rec("I5_send_text", "[" .. table.concat(sends, "|") .. "]")

-- I6: Esc 取消本次；再按一次退出 AI 模式
logs = {}
commit(ctxI3, "\u{4e00}")
envI3.ai_last_push = os.clock()
feed(revert, envI3, ctxI3, key(0x2F))
local cancels_before = cancels
rec("I6_esc_ret", tostring(feed(revert, envI3, ctxI3, key(0xFF1B))))
rec("I6_cancels", tostring(cancels - cancels_before))
rec("I6_log_cancel", tostring(log_has("cancelled by Esc")))
rec("I6_esc2_ret", tostring(feed(revert, envI3, ctxI3, key(0xFF1B))))
rec("I6_mode_after", tostring(envI3.ai_mode))
revert.fini(envI3)

-- I7: source=composition -> 发输入框里还没上屏的编码，发完清掉
logs = {}
local cfgI7 = cfg_plain({
  ["actions/shortcuts"]       = "ai;=@ai",
  ["actions/dll_dir"]         = DLLREL,
  ["actions/debounce_ms"]     = 0,
  ["actions/ai/enabled"]      = true,
  ["actions/ai/source"]       = "composition",
  ["actions/ai/online/model"] = "m",
})
local envI7, ctxI7 = new_env(cfgI7)
revert.init(envI7)
type_text(revert, envI7, ctxI7, "ai;")
for _, ch in ipairs({ "n", "i", "h", "a", "o" }) do
  feed(revert, envI7, ctxI7, key(ch:byte()))
end
rec("I7_input", ctxI7.input)
requests = {}
envI7.ai_last_push = os.clock()
feed(revert, envI7, ctxI7, key(0x2F))
rec("I7_req_user",
    requests[1] and (requests[1].body:match('"role":"user","content":"([^"]*)"') or "-") or "-")
rec("I7_input_after", "[" .. ctxI7.input .. "]")
revert.fini(envI7)

-- I8: 发送键写成组合键（ctrl+enter 按一次就发）
logs = {}
local cfgI8 = cfg_plain({
  ["actions/shortcuts"]       = "ai;=@ai",
  ["actions/dll_dir"]         = DLLREL,
  ["actions/debounce_ms"]     = 0,
  ["actions/ai/enabled"]      = true,
  ["actions/ai/send_key"]     = "ctrl+enter",
  ["actions/ai/online/model"] = "m",
})
local envI8, ctxI8 = new_env(cfgI8)
revert.init(envI8)
rec("I8_combo", tostring(envI8.ai_send and envI8.ai_send.combo ~= nil))
type_text(revert, envI8, ctxI8, "ai;")
commit(ctxI8, "\u{6c49}\u{5b57}")
requests = {}
rec("I8_ret", tostring(feed(revert, envI8, ctxI8, key(0xFF0D, { ctrl = true }))))
rec("I8_reqs", tostring(#requests))
revert.fini(envI8)

-- I9: 发送键写错 -> 整个 AI 功能关掉并报错
logs = {}
local cfgI9 = cfg_plain({
  ["actions/shortcuts"]       = "ai;=@ai",
  ["actions/dll_dir"]         = DLLREL,
  ["actions/debounce_ms"]     = 0,
  ["actions/ai/enabled"]      = true,
  ["actions/ai/send_key"]     = "nonesuch",
  ["actions/ai/online/model"] = "m",
})
local envI9 = new_env(cfgI9)
revert.init(envI9)
rec("I9_flag", tostring(envI9.ai_enabled))
rec("I9_log", tostring(log_has("cannot parse actions/ai/send_key")))
revert.fini(envI9)

-- I10: dll 是旧版（没有 http_request）-> AI 自动关掉，其他功能照常
logs = {}
local saved_request = mod.http_request
mod.http_request = nil
local envI10 = new_env(cfgI)
revert.init(envI10)
rec("I10_flag", tostring(envI10.ai_enabled))
rec("I10_log", tostring(log_has("ai disabled - dll")))
mod.http_request = saved_request
revert.fini(envI10)

-- I11: eng 模式用自己那条提示词；切模式后 prompt 变了
logs = {}
type_text(revert, envI, ctxI, "eng;")
rec("I11_mode", tostring(envI.ai_mode and envI.ai_mode.prompt))
commit(ctxI, "\u{4f60}\u{597d}\u{4e16}\u{754c}\u{3002}")
requests = {}
envI.ai_last_push = os.clock()
feed(revert, envI, ctxI, key(0x2F))
rec("I11_system",
    requests[1] and (requests[1].body:match('"role":"system","content":"([^"]*)"') or "-") or "-")
rec("I11_user",
    requests[1] and (function() local last = "-"
      for u in requests[1].body:gmatch('"role":"user","content":"([^"]*)"') do last = u end
      return last end)() or "-")
rec("I11_log_prompt", tostring(last_log():match("prompt=(%w+)")))

-- I18: 之前的问答按对话轮次带上；没答过的旧文字不进上文
local function roles_of(body)
  local out = {}
  for r in body:gmatch('"role":"(%a+)"') do out[#out + 1] = r end
  return table.concat(out, ",")
end

local function last_user(body)
  local last = "-"
  for u in body:gmatch('"role":"user","content":"([^"]*)"') do last = u end
  return last
end

local cfgI18 = cfg_plain({
  ["actions/trigger"]            = "qq/",
  ["actions/shortcuts"]          = "ai;=@ai",
  ["actions/dll_dir"]            = DLLREL,
  ["actions/debounce_ms"]        = 0,
  ["actions/ai/enabled"]         = true,
  ["actions/ai/online/base_url"] = "https://example.test/v1/chat/completions",
  ["actions/ai/online/model"]    = "test-model",
  ["actions/ai/online/api_key"]  = "sk-test",
})
local envI18, ctxI18 = new_env(cfgI18)
revert.init(envI18)
type_text(revert, envI18, ctxI18, "ai;")

commit(ctxI18, "\u{9e66}\u{9e49}\u{6700}\u{7231}\u{5403}\u{4ec0}\u{4e48}\u{ff1f}")
requests, sends = {}, {}
envI18.ai_last_push = os.clock()
feed(revert, envI18, ctxI18, key(0x2F))
req_state = "done"
result_body = '{"choices":[{"message":{"content":"A1"}}]}'
injected = {}
feed(revert, envI18, ctxI18, key(0x87))
envI18.ai_typing_until = nil

commit(ctxI18, "\u{591a}\u{5c11}\u{79cd}\u{7f57}\u{975e}\u{9c7c}\u{ff1f}")
requests = {}
envI18.ai_last_push = os.clock()
feed(revert, envI18, ctxI18, key(0x2F))
rec("I18_roles", requests[1] and roles_of(requests[1].body) or "-")
rec("I18_system", requests[1] and (requests[1].body:match('"role":"system","content":"([^"]*)"') or "-") or "-")
rec("I18_pair_q", requests[1] and (requests[1].body:match('"role":"user","content":"([^"]*)"') or "-") or "-")
rec("I18_pair_a", requests[1] and (requests[1].body:match('"role":"assistant","content":"([^"]*)"') or "-") or "-")
rec("I18_last_user", requests[1] and last_user(requests[1].body) or "-")

-- 收掉这一问，账本里就有一对问答
req_state = "done"
result_body = '{"choices":[{"message":{"content":"A2"}}]}'
injected = {}
feed(revert, envI18, ctxI18, key(0x87))
envI18.ai_typing_until = nil

-- I18b: 上一笔没被回答（只是打了一句） -> 不进上文
requests = {}
commit(ctxI18, "\u{5b64}\u{96f6}\u{96f6}\u{7684}\u{4e00}\u{53e5}\u{3002}")
commit(ctxI18, "\u{7b2c}\u{4e8c}\u{53e5}\u{8981}\u{53d1}\u{7684}\u{3002}")
envI18.ai_last_push = os.clock()
feed(revert, envI18, ctxI18, key(0x2F))
rec("I18b_roles", requests[1] and roles_of(requests[1].body) or "-")
rec("I18b_user", requests[1] and last_user(requests[1].body) or "-")
rec("I18b_hist", tostring(requests[1] ~= nil
  and requests[1].body:find("\u{3010}\u{4e0a}\u{6587}\u{3011}\u{5b64}\u{96f6}\u{96f6}\u{7684}\u{4e00}\u{53e5}\u{3002}", 1, true) ~= nil))
revert.fini(envI18)

-- I19: 分多次上屏时，往前拼到一起，直到碰到断句符
local cfgI19 = cfg_plain({
  ["actions/trigger"]            = "qq/",
  ["actions/shortcuts"]          = "ai;=@ai",
  ["actions/dll_dir"]            = DLLREL,
  ["actions/debounce_ms"]        = 0,
  ["actions/ai/enabled"]         = true,
  ["actions/ai/online/base_url"] = "https://example.test/v1/chat/completions",
  ["actions/ai/online/model"]    = "test-model",
  ["actions/ai/online/api_key"]  = "sk-test",
})
local envI19, ctxI19 = new_env(cfgI19)
revert.init(envI19)
type_text(revert, envI19, ctxI19, "ai;")

-- 一句话分两次上屏
commit(ctxI19, "\u{6700}\u{597d}\u{5403}\u{7684}\u{5927}\u{7c73}\u{662f}")
commit(ctxI19, "\u{54ea}\u{79cd}\u{ff1f}")
requests = {}
envI19.ai_last_push = os.clock()
feed(revert, envI19, ctxI19, key(0x2F))
rec("I19_split_user", requests[1] and last_user(requests[1].body) or "-")

-- 上一笔已经带句号：只取句号后面的
req_state = "done"
result_body = '{"choices":[{"message":{"content":"A1"}}]}'
injected = {}
feed(revert, envI19, ctxI19, key(0x87))
envI19.ai_typing_until = nil
commit(ctxI19, "\u{4eca}\u{5929}\u{5929}\u{6c14}\u{5f88}\u{597d}\u{3002}")
commit(ctxI19, "\u{6211}\u{51fa}\u{95e8}\u{4e86}")
requests = {}
envI19.ai_last_push = os.clock()
feed(revert, envI19, ctxI19, key(0x2F))
rec("I19_after_mark", requests[1] and last_user(requests[1].body) or "-")

-- 三笔连写，中间没有断句符：合成一句发
req_state = "done"
result_body = '{"choices":[{"message":{"content":"A2"}}]}'
injected = {}
feed(revert, envI19, ctxI19, key(0x87))
envI19.ai_typing_until = nil
commit(ctxI19, "\u{7b2c}\u{4e00}\u{53e5}\u{3002}\u{7b2c}\u{4e8c}\u{53e5}")
commit(ctxI19, "\u{7b2c}\u{4e09}\u{53e5}")
requests = {}
envI19.ai_last_push = os.clock()
feed(revert, envI19, ctxI19, key(0x2F))
rec("I19_three", requests[1] and last_user(requests[1].body) or "-")
revert.fini(envI19)

-- I20: replace 且多次上屏、中间没有断句符 -> 这两次上屏一起删
local cfgI20 = cfg_plain({
  ["actions/trigger"]            = "qq/",
  ["actions/shortcuts"]          = "ai;=@ai",
  ["actions/dll_dir"]            = DLLREL,
  ["actions/debounce_ms"]        = 0,
  ["actions/ai/enabled"]         = true,
  ["actions/ai/replace"]         = true,
  ["actions/ai/online/base_url"] = "https://example.test/v1/chat/completions",
  ["actions/ai/online/model"]    = "test-model",
  ["actions/ai/online/api_key"]  = "sk-test",
})
logs = {}
local envI20, ctxI20 = new_env(cfgI20)
revert.init(envI20)
type_text(revert, envI20, ctxI20, "ai;")
commit(ctxI20, "\u{7b2c}\u{4e00}\u{6bb5}\u{6ca1}\u{6709}\u{53e5}\u{53f7}")
commit(ctxI20, "\u{7b2c}\u{4e8c}\u{6bb5}")
requests = {}
envI20.ai_last_push = os.clock()
feed(revert, envI20, ctxI20, key(0x2F))
req_state = "done"
result_body = '{"choices":[{"message":{"content":"XYZ"}}]}'
injected = {}
sends = {}
feed(revert, envI20, ctxI20, key(0x87))
rec("I20_injected", table.concat(injected, ","))
rec("I20_send_text", table.concat(sends, "|"))
rec("I20_combined", combined and combined[#combined] or "")
rec("I20_log_skip", tostring(log_has("replace skipped")))
revert.fini(envI20)

-- I20b: replace 且多次上屏、断句符在前面那次上屏里 -> 只删断句符之后那段
logs = {}
local envI20b, ctxI20b = new_env(cfgI20)
revert.init(envI20b)
type_text(revert, envI20b, ctxI20b, "ai;")
commit(ctxI20b, "\u{4eca}\u{5929}\u{5929}\u{6c14}\u{5f88}\u{597d}\u{3002}")
commit(ctxI20b, "\u{6211}\u{51fa}\u{95e8}\u{4e86}")
requests = {}
sends = {}
injected = {}
envI20b.ai_last_push = os.clock()
feed(revert, envI20b, ctxI20b, key(0x2F))
req_state = "done"
result_body = '{"choices":[{"message":{"content":"XYZ"}}]}'
feed(revert, envI20b, ctxI20b, key(0x87))
rec("I20b_reqs", tostring(#requests))
rec("I20b_user", requests[1] and last_user(requests[1].body) or "-")
rec("I20b_injected", table.concat(injected, ","))
rec("I20b_send_text", table.concat(sends, "|"))
rec("I20b_combined", combined and combined[#combined] or "")
revert.fini(envI20b)

-- I35: dll busy -> keep the answer, insert it on the next tick
do
  logs = {}
  local envI35, ctxI35 = new_env(cfgI20)
  revert.init(envI35)
  type_text(revert, envI35, ctxI35, "ai;")
  commit(ctxI35, "\u{798f}\u{5efa}\u{7684}\u{7f8e}\u{98df}")
  requests, sends, injected, combined = {}, {}, {}, {}
  envI35.ai_last_push = os.clock()
  feed(revert, envI35, ctxI35, key(0x2F))
  req_state = "done"
  result_body = '{"choices":[{"message":{"content":"XYZ"}}]}'
  combined_return = 2
  feed(revert, envI35, ctxI35, key(0x87))
  rec("I35_pending", tostring(envI35.ai_pending_insert ~= nil))
  rec("I35_calls_first", tostring(#combined))
  rec("I35_last_first", tostring(combined[#combined]))
  rec("I35_log_deferred", tostring(log_has("insert deferred")))
  feed(revert, envI35, ctxI35, key(0x87))
  rec("I35_calls", tostring(#combined))
  rec("I35_last", tostring(combined[#combined]))
  rec("I35_pending_after", tostring(envI35.ai_pending_insert ~= nil))
  rec("I35_log_inserted", tostring(log_has("inserted")))
  revert.fini(envI35)
end

-- I21: 正文先上屏、末尾的问号单独上屏 -> 整句照删（问号那条不算"正文"）
logs = {}
local envI21, ctxI21 = new_env(cfgI20)
revert.init(envI21)
type_text(revert, envI21, ctxI21, "ai;")
commit(ctxI21, "\u{798f}\u{5efa}\u{7684}\u{7f8e}\u{98df}\u{6709}\u{54ea}\u{4e9b}")
commit(ctxI21, "\u{ff1f}")
requests = {}
sends = {}
injected = {}
envI21.ai_last_push = os.clock()
feed(revert, envI21, ctxI21, key(0x2F))
req_state = "done"
result_body = '{"choices":[{"message":{"content":"XYZ"}}]}'
feed(revert, envI21, ctxI21, key(0x87))
rec("I21_reqs", tostring(#requests))
rec("I21_user", requests[1] and last_user(requests[1].body) or "-")
rec("I21_injected", table.concat(injected, ","))
rec("I21_send_text", table.concat(sends, "|"))
rec("I21_combined", combined and combined[#combined] or "")
rec("I21_log_skip", tostring(log_has("replace skipped")))
revert.fini(envI21)

-- I22: 答案打完之后，用户再打的"同名"单字不能被当成 AI 自己的输出
-- （否则取文和删除范围都会少一个字，表现是原句只剩第一个字）
logs = {}
local envI22, ctxI22 = new_env(cfgI20)
revert.init(envI22)
type_text(revert, envI22, ctxI22, "ai;")
envI22.ai_last_text = "\u{622a}\u{81f3}2023\u{5e74}\u{ff0c}\u{4e2d}\u{56fd}\u{5171}\u{6709}\u{5730}\u{7ea7}\u{5e02}333\u{4e2a}"
envI22.ai_text_pos = #envI22.ai_last_text + 1
envI22.ai_typing_until = os.clock() + 100
commit(ctxI22, "\u{4e2d}")
commit(ctxI22, "\u{56fd}\u{6709}\u{591a}\u{5c11}\u{4e2a}\u{5e02}")
commit(ctxI22, "\u{ff1f}")
envI22.ai_typing_until = nil
requests = {}
sends = {}
injected = {}
envI22.ai_last_push = os.clock()
feed(revert, envI22, ctxI22, key(0x2F))
req_state = "done"
result_body = '{"choices":[{"message":{"content":"XYZ"}}]}'
feed(revert, envI22, ctxI22, key(0x87))
rec("I22_reqs", tostring(#requests))
rec("I22_user", requests[1] and last_user(requests[1].body) or "-")
rec("I22_injected", table.concat(injected, ","))
revert.fini(envI22)

-- I23: 发送前按过回车（光标到了下一行）-> 删除长度要算上那个换行
logs = {}
local envI23, ctxI23 = new_env(cfgI20)
revert.init(envI23)
type_text(revert, envI23, ctxI23, "ai;")
commit(ctxI23, "\u{4e2d}\u{56fd}\u{6709}\u{591a}\u{5c11}\u{4e2a}\u{5e02}\u{ff1f}")
feed(revert, envI23, ctxI23, key(0xFF0D))
requests = {}
sends = {}
injected = {}
envI23.ai_last_push = os.clock()
feed(revert, envI23, ctxI23, key(0x2F))
req_state = "done"
result_body = '{"choices":[{"message":{"content":"XYZ"}}]}'
feed(revert, envI23, ctxI23, key(0x87))
rec("I23_reqs", tostring(#requests))
rec("I23_user", requests[1] and last_user(requests[1].body) or "-")
rec("I23_injected", table.concat(injected, ","))
revert.fini(envI23)

-- I24: 连按两个回车（真实 keysym 0xFF0D / 0xFF8D）-> 两个换行都并进删除长度
logs = {}
local envI24, ctxI24 = new_env(cfgI20)
revert.init(envI24)
type_text(revert, envI24, ctxI24, "ai;")
commit(ctxI24, "\u{7b2c}\u{4e00}\u{53e5}\u{3002}")     -- 第一句。
commit(ctxI24, "\u{7b2c}\u{4e8c}\u{53e5}")            -- 第二句
feed(revert, envI24, ctxI24, key(0xFF0D))
feed(revert, envI24, ctxI24, key(0xFF8D))
requests = {}
sends = {}
injected = {}
envI24.ai_last_push = os.clock()
feed(revert, envI24, ctxI24, key(0x2F))
req_state = "done"
result_body = '{"choices":[{"message":{"content":"XYZ"}}]}'
feed(revert, envI24, ctxI24, key(0x87))
rec("I24_reqs", tostring(#requests))
rec("I24_user", requests[1] and last_user(requests[1].body) or "-")
rec("I24_injected", table.concat(injected, ","))
revert.fini(envI24)

-- I25: actions/hotkey 里写非打印键名（Control+return）-> 按键码是 keysym 0xFF0D
logs = {}
local envI25, ctxI25 = new_env(cfg_plain({
  ["actions/hotkey"]  = "Control+return",
  ["actions/trigger"] = "qq/",
  ["actions/dll_dir"] = DLLREL,
  ["actions/debounce_ms"] = 0,
}))
revert.init(envI25)
commit(ctxI25, "abc")
injected = {}
ctxI25.input = ""
rec("I25_ret", tostring(feed(revert, envI25, ctxI25, key(0xFF0D, { ctrl = true }))))
rec("I25_injected", table.concat(injected, ","))
revert.fini(envI25)

-- I26: 唤醒键用真实键码（F24 = keysym 0xFFD5，配置值仍是 VK 0x87）
logs = {}
local envI26, ctxI26 = new_env(cfgI20)
revert.init(envI26)
type_text(revert, envI26, ctxI26, "ai;")
commit(ctxI26, "\u{4e2d}\u{56fd}\u{6709}\u{591a}\u{5c11}\u{4e2a}\u{5e02}")
requests, sends, nudges, injected = {}, {}, {}, {}
envI26.ai_last_push = os.clock()
feed(revert, envI26, ctxI26, key(0x2F))
rec("I26_reqs", tostring(#requests))
rec("I26_nudge_first", table.concat(nudges, ","))
-- 结果回来时敲的是 F24 的 keysym（以前按 0x87 比，永远不相等 -> 唤醒链断掉）
req_state = "done"
result_body = '{"choices":[{"message":{"content":"XYZ"}}]}'
rec("I26_tick_ret", tostring(feed(revert, envI26, ctxI26, key(0xFFD5))))
rec("I26_send_text", table.concat(sends, ","))
rec("I26_combined", combined and combined[#combined] or "")
revert.fini(envI26)

-- I26b: 唤醒键还在等（running）-> 排下一轮唤醒，别让链子断
logs = {}
local envI26b, ctxI26b = new_env(cfgI20)
revert.init(envI26b)
type_text(revert, envI26b, ctxI26b, "ai;")
commit(ctxI26b, "\u{4e2d}\u{56fd}")
requests, sends, nudges, injected = {}, {}, {}, {}
envI26b.ai_last_push = os.clock()
feed(revert, envI26b, ctxI26b, key(0x2F))
nudges = {}
envI26b.ai_nudged = os.clock() - 1
req_state = "running"
feed(revert, envI26b, ctxI26b, key(0xFFD5))
rec("I26b_reqs", tostring(#requests))
rec("I26b_nudge_again", table.concat(nudges, ","))
revert.fini(envI26b)

-- I27: 第一笔只有一个字（"中"），后一笔是其余部分 -> 取文和回删都要算全 8 个字
local cfgI27 = cfg_plain({
  ["actions/trigger"]            = "qq/",
  ["actions/shortcuts"]          = "ai;=@ai",
  ["actions/dll_dir"]            = DLLREL,
  ["actions/debounce_ms"]        = 0,
  ["actions/ai/enabled"]         = true,
  ["actions/ai/replace"]         = true,
  ["actions/ai/online/base_url"] = "https://example.test/v1/chat/completions",
  ["actions/ai/online/model"]    = "test-model",
  ["actions/ai/online/api_key"]  = "sk-test",
})
logs = {}
local envI27, ctxI27 = new_env(cfgI27)
revert.init(envI27)
type_text(revert, envI27, ctxI27, "ai;")
commit(ctxI27, "\u{4e2d}")
commit(ctxI27, "\u{56fd}\u{6709}\u{591a}\u{5c11}\u{4e2a}\u{5e02}\u{ff1f}")
requests, sends, injected = {}, {}, {}
envI27.ai_last_push = os.clock()
feed(revert, envI27, ctxI27, key(0x2F))
rec("I27_reqs", tostring(#requests))
rec("I27_user", requests[1] and last_user(requests[1].body) or "-")
req_state = "done"
result_body = '{"choices":[{"message":{"content":"XYZ"}}]}'
feed(revert, envI27, ctxI27, key(0xFFD5))
rec("I27_injected", table.concat(injected, ","))
revert.fini(envI27)

-- I27b: 问号单独一笔上屏 -> 删除长度也要把它算进去（8 个字）
logs = {}
local envI27b, ctxI27b = new_env(cfgI27)
revert.init(envI27b)
type_text(revert, envI27b, ctxI27b, "ai;")
commit(ctxI27b, "\u{4e2d}\u{56fd}\u{6709}\u{591a}\u{5c11}\u{4e2a}\u{5e02}")
commit(ctxI27b, "\u{ff1f}")
requests, sends, injected = {}, {}, {}
envI27b.ai_last_push = os.clock()
feed(revert, envI27b, ctxI27b, key(0x2F))
rec("I27b_reqs", tostring(#requests))
rec("I27b_user", requests[1] and last_user(requests[1].body) or "-")
req_state = "done"
result_body = '{"choices":[{"message":{"content":"XYZ"}}]}'
feed(revert, envI27b, ctxI27b, key(0xFFD5))
rec("I27b_injected", table.concat(injected, ","))
revert.fini(envI27b)

-- I28: 第一下 / 进了编码（编码里只有这一个字符）-> 第二下要把它摘掉，再发
logs = {}
local envI28, ctxI28 = new_env(cfgI27)
revert.init(envI28)
type_text(revert, envI28, ctxI28, "ai;")
commit(ctxI28, "\u{4e2d}\u{56fd}\u{6709}\u{591a}\u{5c11}\u{4e2a}\u{5e02}\u{ff1f}")
requests, sends, injected = {}, {}, {}
ctxI28.popped = 0
ctxI28.input = "/"
envI28.ai_last_push = os.clock()
feed(revert, envI28, ctxI28, key(0x2F))
rec("I28_reqs", tostring(#requests))
rec("I28_popped", tostring(ctxI28.popped))
rec("I28_input", ctxI28.input)
req_state = "done"
result_body = '{"choices":[{"message":{"content":"XYZ"}}]}'
feed(revert, envI28, ctxI28, key(0xFFD5))
rec("I28_injected", table.concat(injected, ","))
revert.fini(envI28)

-- I29: 结果回来时输入框里还压着编码 -> 先清掉，退格才全部落在正文上（删 8 个字）
logs = {}
local envI29, ctxI29 = new_env(cfgI27)
revert.init(envI29)
type_text(revert, envI29, ctxI29, "ai;")
commit(ctxI29, "\u{4e2d}\u{56fd}\u{6709}\u{591a}\u{5c11}\u{4e2a}\u{5e02}\u{ff1f}")
requests, sends, injected = {}, {}, {}
envI29.ai_last_push = os.clock()
feed(revert, envI29, ctxI29, key(0x2F))
rec("I29_reqs", tostring(#requests))
ctxI29.input = "/"
ctxI29.cleared = 0
req_state = "done"
result_body = '{"choices":[{"message":{"content":"XYZ"}}]}'
feed(revert, envI29, ctxI29, key(0xFFD5))
rec("I29_cleared", tostring(ctxI29.cleared))
rec("I29_input", ctxI29.input)
rec("I29_injected", table.concat(injected, ","))
revert.fini(envI29)

-- I12: 没有任何可发的内容 -> 只记日志
logs = {}
local envI12, ctxI12 = new_env(cfgI)
revert.init(envI12)
type_text(revert, envI12, ctxI12, "ai;")
requests = {}
envI12.ai_last_push = os.clock()
feed(revert, envI12, ctxI12, key(0x2F))
rec("I12_reqs", tostring(#requests))
rec("I12_log", tostring(log_has("nothing to send")))
revert.fini(envI12)

-- I13: 末尾那串标点不算 -> 取"上一段"和它后面到末尾标点之间的内容
local function sentence_of(cfg_extra, history, opts)
  local c = cfg_plain({
    ["actions/trigger"]              = "qq/",
    ["actions/shortcuts"]            = "ai;=@ai",
    ["actions/dll_dir"]              = DLLREL,
    ["actions/debounce_ms"]          = 0,
    ["actions/ai/enabled"]           = true,
    ["actions/ai/online/base_url"]   = "https://example.test/v1/chat/completions",
    ["actions/ai/online/model"]      = "test-model",
    ["actions/ai/online/api_key"]    = "sk-test",
  })
  for k, v in pairs(cfg_extra or {}) do c.values[k] = v end
  local env, ctx = new_env(c)
  revert.init(env)
  type_text(revert, env, ctx, "ai;")
  commit(ctx, history)
  requests, nudges = {}, {}
  env.ai_last_push = os.clock()
  feed(revert, env, ctx, key(0x2F))
  local user = requests[1] and (requests[1].body:match('"role":"user","content":"([^"]*)"') or "-") or "-"
  revert.fini(env)
  return user
end

rec("I13_trailing_marks",
    sentence_of({}, "\u{4eca}\u{5929}\u{5929}\u{6c14}\u{5f88}\u{597d}\u{3002}\u{6211}\u{51fa}\u{95e8}\u{4e86}\u{ff0c}\u{3002}"))
rec("I13_marks_between",
    sentence_of({}, "\u{4f60}\u{597d}\u{3002}\u{ff01}\u{ff1f}\u{6211}\u{56de}\u{6765}\u{4e86}"))
rec("I13_only_marks",
    sentence_of({}, "\u{3002}\u{ff01}\u{ff1f}"))
rec("I13_include_marks",
    sentence_of({["actions/ai/include_marks"] = true},
                "\u{4eca}\u{5929}\u{5929}\u{6c14}\u{5f88}\u{597d}\u{3002}\u{6211}\u{51fa}\u{95e8}\u{4e86}\u{ff0c}\u{3002}"))

-- I14: replace=true 时删到"上一组符号"之后（句子 + 末尾那串符号一起删）
local cfgI14 = cfg_plain({
  ["actions/shortcuts"]       = "ai;=@ai",
  ["actions/dll_dir"]         = DLLREL,
  ["actions/debounce_ms"]     = 0,
  ["actions/ai/enabled"]      = true,
  ["actions/ai/replace"]      = true,
  ["actions/ai/source"]       = "committed",
  ["actions/ai/online/model"] = "m",
})
local envI14, ctxI14 = new_env(cfgI14)
revert.init(envI14)
type_text(revert, envI14, ctxI14, "ai;")
commit(ctxI14, "\u{4eca}\u{5929}\u{5929}\u{6c14}\u{5f88}\u{597d}\u{3002}\u{6211}\u{51fa}\u{95e8}\u{4e86}\u{ff0c}\u{3002}")
req_state = "idle"
envI14.ai_last_push = os.clock()
requests, sends = {}, {}
feed(revert, envI14, ctxI14, key(0x2F))
req_state = "done"
result_body = '{"choices":[{"message":{"content":"XYZ"}}]}'
injected = {}
feed(revert, envI14, ctxI14, key(0x87))
envI14.ai_typing_until = nil   -- 测试里跳过“结果正在打字”窗口
rec("I14_reqs", tostring(#requests))
rec("I14_injected", table.concat(injected, ","))
rec("I14_send_text", table.concat(sends, "|"))
rec("I14_combined", combined and combined[#combined] or "")
revert.fini(envI14)

-- I15: 有编码时第一下 / 放行（万象快符靠这一下），第二下才发送并把那个字符摘掉
local cfgI15 = cfg_plain({
  ["actions/shortcuts"]       = "ai;=@ai",
  ["actions/dll_dir"]         = DLLREL,
  ["actions/debounce_ms"]     = 0,
  ["actions/ai/enabled"]      = true,
  ["actions/ai/double_ms"]    = 5000,
  ["actions/ai/online/model"] = "m",
})
local envI15, ctxI15 = new_env(cfgI15)
revert.init(envI15)
type_text(revert, envI15, ctxI15, "ai;")
requests = {}
ctxI15.input = "nihao"
rec("I15_pass", tostring(feed(revert, envI15, ctxI15, key(0x2F))))
rec("I15_reqs", tostring(#requests))
ctxI15.input = "nihao/"
rec("I15_second", tostring(feed(revert, envI15, ctxI15, key(0x2F))))
rec("I15_popped", tostring(ctxI15.popped or 0))
rec("I15_reqs2", tostring(#requests))
rec("I15_body_has", requests[1] and (requests[1].body:find("nihao", 1, true) and "yes" or "no") or "-")
revert.fini(envI15)

-- I15b: 刚上屏的只是符号（没有汉字）-> 不发送
local cfgI15b = cfg_plain({
  ["actions/shortcuts"]       = "ai;=@ai",
  ["actions/dll_dir"]         = DLLREL,
  ["actions/debounce_ms"]     = 0,
  ["actions/ai/enabled"]      = true,
  ["actions/ai/source"]       = "committed",
  ["actions/ai/double_ms"]    = 5000,
  ["actions/ai/online/model"] = "m",
})
local envI15b, ctxI15b = new_env(cfgI15b)
revert.init(envI15b)
type_text(revert, envI15b, ctxI15b, "ai;")
logs = {}
requests = {}
commit(ctxI15b, "\u{ff08}\u{ff09}")
envI15b.ai_last_push = os.clock()
rec("I15b_first", tostring(feed(revert, envI15b, ctxI15b, key(0x2F))))
feed(revert, envI15b, ctxI15b, key(0x2F))
rec("I15b_reqs", tostring(#requests))
rec("I15b_log_no_cjk", tostring(log_has("nothing to send")))
revert.fini(envI15b)

-- I16: 等结果时按过的 / 不能在"取回结果的那一下"顺手再发一条
local cfgI16 = cfg_plain({
  ["actions/shortcuts"]       = "ai;=@ai",
  ["actions/dll_dir"]         = DLLREL,
  ["actions/debounce_ms"]     = 0,
  ["actions/ai/enabled"]      = true,
  ["actions/ai/source"]       = "committed",
  ["actions/ai/double_ms"]    = 5000,
  ["actions/ai/online/model"] = "m",
})
local envI16, ctxI16 = new_env(cfgI16)
revert.init(envI16)
type_text(revert, envI16, ctxI16, "ai;")
commit(ctxI16, "\u{6211}\u{51fa}\u{95e8}\u{4e86}\u{3002}")
requests, sends = {}, {}
envI16.ai_last_push = os.clock()
feed(revert, envI16, ctxI16, key(0x2F))
rec("I16_reqs_first", tostring(#requests))
envI16.ai_last_push = os.clock()
feed(revert, envI16, ctxI16, key(0x2F))
rec("I16_reqs_wait", tostring(#requests))
req_state = "done"
result_body = '{"choices":[{"message":{"content":"XYZ"}}]}'
feed(revert, envI16, ctxI16, key(0x2F))
rec("I16_reqs_after_take", tostring(#requests))
rec("I16_send_text", table.concat(sends, "|"))
envI16.ai_typing_until = nil
feed(revert, envI16, ctxI16, key(0x2F))
rec("I16_reqs_one", tostring(#requests))
feed(revert, envI16, ctxI16, key(0x2F))
rec("I16_reqs_two", tostring(#requests))
rec("I16_log_nothing", tostring(log_has("nothing to send")))
-- 再上屏一句新的，按两下就应该发出去
commit(ctxI16, "\u{65b0}\u{7684}\u{95ee}\u{9898}\u{ff1f}")
feed(revert, envI16, ctxI16, key(0x2F))
feed(revert, envI16, ctxI16, key(0x2F))
rec("I16_reqs_three", tostring(#requests))
revert.fini(envI16)

-- I17: 读剪贴板发送（Ctrl+Alt+;）—— 用户自己 Ctrl+C，脚本只负责读出来发
logs = {}
local cfgI17 = cfg_plain({
  ["actions/shortcuts"]      = "ai;=@ai",
  ["actions/dll_dir"]        = DLLREL,
  ["actions/debounce_ms"]    = 0,
  ["actions/ai/enabled"]     = true,
  ["actions/ai/online/api_key"] = "sk-test",
})
local envI17, ctxI17 = new_env(cfgI17)
revert.init(envI17)
type_text(revert, envI17, ctxI17, "ai;")
rec("I17_mode", tostring(envI17.ai_mode and envI17.ai_mode.prompt))
rec("I17_log_clip", tostring(log_has("clip_key=ctrl+alt+semicolon")))

local function clipkey() return key(0x3B, { ctrl = true, alt = true, modifier = 6 }) end

clip_text = "你好，世界"
clip_pending = false
local before_reqs = #requests
rec("I17_ret", tostring(feed(revert, envI17, ctxI17, clipkey())))
rec("I17_pending", tostring(envI17.ai_clip_started ~= nil))
rec("I17_reqs_before_tick", tostring(#requests - before_reqs))
feed(revert, envI17, ctxI17, key(0x87))   -- 唤醒键回来取剪贴板内容
rec("I17_reqs", tostring(#requests - before_reqs))
rec("I17_body_has_clip", tostring(requests[#requests] ~= nil
      and requests[#requests].body:find("你好，世界", 1, true) ~= nil))
rec("I17_prompt", tostring(envI17.ai_req and envI17.ai_req.prompt))

-- 结果照常走唤醒键取回
req_state = "done"
result_body = '{"choices":[{"message":{"content":"XYZ"}}]}'
injected, sends = {}, {}
envI17.ai_last_push = nil
feed(revert, envI17, ctxI17, key(0x87))
rec("I17_send_text", table.concat(sends, "|"))
envI17.ai_typing_until = nil
revert.fini(envI17)

-- I17b: 剪贴板里没文本
logs = {}
local envI17b, ctxI17b = new_env(cfgI17)
revert.init(envI17b)
type_text(revert, envI17b, ctxI17b, "ai;")
clip_text = nil
clip_pending = false
before_reqs = #requests
feed(revert, envI17b, ctxI17b, clipkey())
rec("I17b_reqs_before_tick", tostring(#requests - before_reqs))
feed(revert, envI17b, ctxI17b, key(0x87))
rec("I17b_reqs_after", tostring(#requests - before_reqs))
rec("I17b_log_empty", tostring(log_has("clipboard:")))
revert.fini(envI17b)

-- I17c: 超过 clip_max_chars 只发前面那些字
logs = {}
local cfgI17c = cfg_plain({
  ["actions/shortcuts"]      = "ai;=@ai",
  ["actions/dll_dir"]        = DLLREL,
  ["actions/debounce_ms"]    = 0,
  ["actions/ai/enabled"]     = true,
  ["actions/ai/online/api_key"] = "sk-test",
  ["actions/ai/clip_max_chars"] = 4,
})
local envI17c, ctxI17c = new_env(cfgI17c)
revert.init(envI17c)
type_text(revert, envI17c, ctxI17c, "ai;")
clip_text = "一二三四五六"
clip_pending = false
before_reqs = #requests
feed(revert, envI17c, ctxI17c, clipkey())
feed(revert, envI17c, ctxI17c, key(0x87))
rec("I17c_reqs", tostring(#requests - before_reqs))
rec("I17c_body_has_4", tostring(requests[#requests] ~= nil
      and requests[#requests].body:find("一二三四", 1, true) ~= nil))
rec("I17c_body_has_6", tostring(requests[#requests] ~= nil
      and requests[#requests].body:find("一二三四五六", 1, true) ~= nil))
rec("I17c_log_trunc", tostring(log_has("clipboard truncated to 4 chars")))
revert.fini(envI17c)

-- I17d: clip_key 写空 = 这个功能不启用
logs = {}
local cfgI17d = cfg_plain({
  ["actions/shortcuts"]      = "ai;=@ai",
  ["actions/dll_dir"]        = DLLREL,
  ["actions/debounce_ms"]    = 0,
  ["actions/ai/enabled"]     = true,
  ["actions/ai/online/api_key"] = "sk-test",
  ["actions/ai/clip_key"]    = "",
})
local envI17d, ctxI17d = new_env(cfgI17d)
revert.init(envI17d)
type_text(revert, envI17d, ctxI17d, "ai;")
clip_text = "不该发出去"
before_reqs = #requests
rec("I17d_ret", tostring(feed(revert, envI17d, ctxI17d, clipkey())))
rec("I17d_reqs", tostring(#requests - before_reqs))
revert.fini(envI17d)
clip_text = nil

-- I30: 分两次上屏的问题，进历史时拼成一句
do
logs = {}
local cfg = cfg_plain({
  ["actions/shortcuts"]      = "ai;=@ai",
  ["actions/dll_dir"]        = DLLREL,
  ["actions/debounce_ms"]    = 0,
  ["actions/ai/enabled"]     = true,
  ["actions/ai/online/api_key"] = "sk-test",
})
local env, ctx = new_env(cfg)
revert.init(env)
type_text(revert, env, ctx, "ai;")
commit(ctx, "\u{9e66}\u{9e49}\u{6700}\u{7231}\u{5403}")
commit(ctx, "\u{4ec0}\u{4e48}\u{ff1f}")
requests, sends = {}, {}
env.ai_last_push = os.clock()
feed(revert, env, ctx, key(0x2F))
req_state = "done"
result_body = '{"choices":[{"message":{"content":"A1"}}]}'
injected = {}
feed(revert, env, ctx, key(0x87))
env.ai_typing_until = nil

commit(ctx, "\u{591a}\u{5c11}\u{79cd}\u{7f57}\u{975e}\u{9c7c}")
requests = {}
env.ai_last_push = os.clock()
feed(revert, env, ctx, key(0x2F))
rec("I30_pair_q", requests[1] and (requests[1].body:match('"role":"user","content":"([^"]*)"') or "-") or "-")
rec("I30_last_user", requests[1] and last_user(requests[1].body) or "-")
revert.fini(env)
end

-- I31: history_chars 按字算（按字节算的话这段问答会被丢掉）
do
logs = {}
local cfg = cfg_plain({
  ["actions/shortcuts"]      = "ai;=@ai",
  ["actions/dll_dir"]        = DLLREL,
  ["actions/debounce_ms"]    = 0,
  ["actions/ai/enabled"]     = true,
  ["actions/ai/online/api_key"] = "sk-test",
  ["actions/ai/history_chars"] = 6,
})
local env, ctx = new_env(cfg)
revert.init(env)
type_text(revert, env, ctx, "ai;")
commit(ctx, "\u{4f60}\u{597d}")
env.ai_last_push = os.clock()
feed(revert, env, ctx, key(0x2F))
req_state = "done"
result_body = '{"choices":[{"message":{"content":"\u{4e16}\u{754c}"}}]}'
feed(revert, env, ctx, key(0x87))
env.ai_typing_until = nil
commit(ctx, "\u{7b2c}\u{4e09}\u{95ee}")
requests = {}
env.ai_last_push = os.clock()
feed(revert, env, ctx, key(0x2F))
rec("I31_roles", requests[1] and roles_of(requests[1].body) or "-")
revert.fini(env)
end

-- I32: 汉字判定：全角标点不算汉字（不发），扩展 A 区汉字算（发）
do
logs = {}
local cfg = cfg_plain({
  ["actions/shortcuts"]      = "ai;=@ai",
  ["actions/dll_dir"]        = DLLREL,
  ["actions/debounce_ms"]    = 0,
  ["actions/ai/enabled"]     = true,
  ["actions/ai/online/api_key"] = "sk-test",
})
local env, ctx = new_env(cfg)
revert.init(env)
type_text(revert, env, ctx, "ai;")
commit(ctx, "abc")
requests = {}
env.ai_last_push = os.clock()
feed(revert, env, ctx, key(0x2F))
rec("I32_reqs", tostring(#requests))
rec("I32_log_nocjk", tostring(log_has("no 汉字 in the committed text")))
commit(ctx, "\u{3400}")
requests = {}
env.ai_last_push = os.clock()
feed(revert, env, ctx, key(0x2F))
rec("I32b_reqs", tostring(#requests))
revert.fini(env)
end

-- I33: 按键写法里出现空步（双逗号）就算写坏，不生效
do
logs = {}
local cfg = cfg_plain({
  ["actions/shortcuts"]      = "z;=ctrl+z,,shift+home",
  ["actions/dll_dir"]        = DLLREL,
  ["actions/debounce_ms"]    = 0,
})
local env, ctx = new_env(cfg)
revert.init(env)
injected = {}
type_text(revert, env, ctx, "z;")
rec("I33_log_bad", tostring(log_has("bad key sequence disabled")))
rec("I33_injected", table.concat(injected, ","))
revert.fini(env)
end

-- I34: "@defaults" 必须整词才算（写成 @defaultsx 不启用默认表）
do
logs = {}
local cfg = cfg_plain({
  ["actions/shortcuts"]      = "@defaultsx",
  ["actions/dll_dir"]        = DLLREL,
  ["actions/debounce_ms"]    = 0,
})
local env, ctx = new_env(cfg)
revert.init(env)
injected = {}
rec("I34_log_bad", tostring(log_has("bad shortcut entry ignored")))
type_text(revert, env, ctx, "z;")
rec("I34_injected", table.concat(injected, ","))
revert.fini(env)
end

revert.fini(envI)

-- =====================================================================
-- E. config: wrong dll_dir -> graceful failure, nothing injected
-- =====================================================================
local cfgE = {
  values = {
    ["actions/trigger"] = "qq/",
    ["actions/dll_dir"] = "lua/does/not/exist",
  },
}
cfgE.get_string = function(self, k) return self.values[k] end
cfgE.get_int = function(self, k) return self.values[k] end

-- simulate a fresh process: forget the module and drop the good cpath entry
-- (plain find + slicing: "?" and "." are magic in Lua patterns)
package.loaded["actions"] = nil
local entry = USERDIR .. "/" .. DLLREL .. "/?.dll;"
local s, e = package.cpath:find(entry, 1, true)
if s then package.cpath = package.cpath:sub(1, s - 1) .. package.cpath:sub(e + 1) end
rec("E_good_entry_removed", tostring(s ~= nil))
rec("E_cpath_clean", tostring(package.cpath:find(USERDIR .. "/" .. DLLREL, 1, true) == nil))

local envE, ctxE = new_env(cfgE)
revert.init(envE)
rec("E_init_log", last_log())
rec("E_log_has_load_failure", tostring(log_has("failed to load actions")))
commit(ctxE, "abc")
injected = {}

-- Safety net: only fire when the module really failed to load. If require had
-- somehow succeeded, module-level Actions would hold the REAL C function
-- and firing would inject genuine keystrokes into the focused window.
if package.loaded["actions"] == nil then
  rec("E_fire_returns", type_text(revert, envE, ctxE, "qq/"))
  rec("E_injected", table.concat(injected, ","))
  rec("E_log_has_unavailable", tostring(log_has("press_backspace is unavailable")))
else
  rec("E_fire_returns", "SKIPPED-module-loaded")
  rec("E_injected", "SKIPPED")
  rec("E_log_has_unavailable", "SKIPPED")
end

-- =====================================================================
-- I36. 日志开关：默认只写 error，actions/debug: true 才写 info
-- =====================================================================
do
  local cfgI36 = cfg_plain({ ["actions/trigger"] = "qq/", ["actions/shortcuts"] = "zz" })
  local envI36 = new_env(cfgI36)

  revert.set_debug(false)
  logs = {}
  revert.init(envI36)
  rec("I36_debug", tostring(envI36.revert_debug))
  rec("I36_no_info", tostring(not log_has("mode=")))
  rec("I36_error_kept", tostring(log_has("bad shortcut entry ignored")))

  revert.set_debug(true)
  logs = {}
  revert.init(envI36)
  rec("I36_debug_on", tostring(envI36.revert_debug))
  rec("I36_info_back", tostring(log_has("mode=")))
  revert.fini(envI36)
end

-- =====================================================================
-- I37. 快符补记：engine:commit_text 打出来的符号只进 commit_history，
--      开了 actions/history_ledger 也要进账本、能回删
-- =====================================================================
do
  -- 场景 E 把 package.loaded["actions"] 清过（故意测加载失败），这里放回桩
  package.loaded["actions"] = mod
  local cnt = {}
  mod.press_backspace = function(n) cnt[#cnt + 1] = n return n end

  local cfgI37 = cfg_plain({ ["actions/trigger"] = "qq/" })
  local envI37, ctxI37 = new_env(cfgI37)
  revert.init(envI37)
  rec("I37_hist_ready", tostring(envI37.hist_ready))

  commit(ctxI37, "海南有多大面积")
  quick(ctxI37, "？")
  rec("I37_ret", type_text(revert, envI37, ctxI37, "qq/"))
  rec("I37_injected", table.concat(cnt, ","))
  rec("I37_stack", tostring(#envI37.revert_stack))

  -- 同一笔不重复记：正常上屏只算一次
  cnt = {}
  commit(ctxI37, "abc")
  type_text(revert, envI37, ctxI37, "qq/")
  rec("I37b_injected", table.concat(cnt, ","))
  revert.fini(envI37)

  -- 关掉开关：退回只靠上屏通知，符号漏掉（回删少一个字）
  local cfgI37c = cfg_plain({ ["actions/trigger"] = "qq/", ["actions/history_ledger"] = false })
  local envI37c, ctxI37c = new_env(cfgI37c)
  cnt = {}
  revert.init(envI37c)
  rec("I37c_hist_ready", tostring(envI37c.hist_ready))
  commit(ctxI37c, "海南有多大面积")
  quick(ctxI37c, "？")
  type_text(revert, envI37c, ctxI37c, "qq/")
  rec("I37c_injected", table.concat(cnt, ","))
  revert.fini(envI37c)

  -- 历史只有 20 条：写满之后仍然能跟进
  local cfgI37d = cfg_plain({ ["actions/trigger"] = "qq/" })
  local envI37d, ctxI37d = new_env(cfgI37d)
  cnt = {}
  revert.init(envI37d)
  for i = 1, 18 do ctxI37d.commit_history:push("x") end
  type_text(revert, envI37d, ctxI37d, "q")
  rec("I37d_stack", tostring(#envI37d.revert_stack))
  quick(ctxI37d, "？")
  type_text(revert, envI37d, ctxI37d, "q")
  rec("I37d_latest", tostring((envI37d.revert_stack[#envI37d.revert_stack] or {}).text))
  revert.fini(envI37d)

  mod.press_backspace = function(n) injected[#injected + 1] = n return n end
end

-- =====================================================================
-- I38. AI 拼音候选：开关、拼音闸门、去抖、结果校验
do
  local cfgI38 = cfg_plain({
    ["actions/trigger"]             = "qq/",
    ["actions/dll_dir"]             = DLLREL,
    ["actions/debounce_ms"]         = 0,
    ["actions/ai_pinyin/enabled"]   = true,
    ["actions/ai_pinyin/debounce_ms"] = 400,
    ["actions/ai/online/base_url"]  = "https://example.test/v1/chat/completions",
    ["actions/ai/online/api_key"]   = "sk-py",
  })
  local envI38, ctxI38 = new_env(cfgI38)
  logs = {}
  revert.init(envI38)
  rec("I38_ready", tostring(envI38.ai_pinyin ~= nil))
  rec("I38_off_at_start", tostring(envI38.ai_pinyin and envI38.ai_pinyin.on))

  -- aiur; 打开
  ctxI38.input = "aiur"
  rec("I38_ret", feed(revert, envI38, ctxI38, key(0x3B)))
  rec("I38_on", tostring(envI38.ai_pinyin and envI38.ai_pinyin.on))
  rec("I38_live_py", tostring(envI38.ai_pinyin and envI38.ai_pinyin.prefix))

  -- 第一候选没有拼音（自定义短语、日期这类）：一个请求都不发
  requests, nudges = {}, {}
  ctxI38.input = "jys"
  ctxI38.props["ai_pinyin/first"] = "\u{9759}\u{591c}\u{601d}"
  ctxI38.props["ai_pinyin/first_py"] = ""
  feed(revert, envI38, ctxI38, key(0x87))
  feed(revert, envI38, ctxI38, key(0x87))
  rec("I38_nopy_reqs", tostring(#requests))

  -- 有拼音：第一下只计时，第二下才发
  ctxI38.input = "nihc"
  ctxI38.props["ai_pinyin/first"] = "\u{4f60}\u{597d}"
  ctxI38.props["ai_pinyin/first_py"] = "ni hao"
  ctxI38.props["ai_pinyin/map"] = "\u{4f60}\u{597d}\tni hao\n\u{62df}\u{597d}\tni hao\n\u{6ce5}\u{84bf}\tni hao"
  requests, nudges = {}, {}
  feed(revert, envI38, ctxI38, key(0x87))
  rec("I38_wait_reqs", tostring(#requests))
  feed(revert, envI38, ctxI38, key(0x87))
  rec("I38_reqs", tostring(#requests))
  rec("I38_user", requests[1] and (requests[1].body:match('"role":"user","content":"(.-)"') or "") or "")
  rec("I38_url", requests[1] and requests[1].url or "")
  rec("I38_auth", requests[1] and (requests[1].headers:find("Bearer sk%-py") and "yes" or "no") or "")
  local sys = requests[1] and (requests[1].body:match('"role":"system","content":"(.-)"') or "") or ""
  rec("I38_system_has_prompt", tostring(sys:find("拼音") ~= nil))
  rec("I38_system_head", (sys:match("^(.-)\\n") or sys))

  -- 2 个字配 2 个音节 -> 挂到属性上并主动刷新候选
  req_state = "done"
  result_body = '{"choices":[{"message":{"content":"\u{60a8}\u{597d}"}}]}'
  local before = ctxI38.refreshed
  feed(revert, envI38, ctxI38, key(0x87))
  rec("I38_result", ctxI38.props["ai_pinyin/result"] or "")
  rec("I38_result_sig", ctxI38.props["ai_pinyin/result_sig"] or "")
  rec("I38_refreshed", tostring(ctxI38.refreshed > before))

  -- 字数与音节数不符 -> 不采纳
  requests = {}
  ctxI38.input = "nihma"
  ctxI38.props["ai_pinyin/first_py"] = "ni hao ma"
  ctxI38.props["ai_pinyin/result"] = ""
  req_state = "idle"
  feed(revert, envI38, ctxI38, key(0x87))
  feed(revert, envI38, ctxI38, key(0x87))
  rec("I38_mismatch_reqs", tostring(#requests))
  req_state = "done"
  result_body = '{"choices":[{"message":{"content":"\u{4f60}\u{597d}"}}]}'
  logs = {}
  feed(revert, envI38, ctxI38, key(0x87))
  rec("I38_mismatch_result", ctxI38.props["ai_pinyin/result"] or "")
  rec("I38_mismatch_log", tostring(log_has("数量不符")))

  -- 同一条输入失败后进入冷却：再来 tick 不再发请求
  envI38.ai_pinyin.retry = 600000
  requests = {}
  req_state = "idle"
  feed(revert, envI38, ctxI38, key(0x87))
  feed(revert, envI38, ctxI38, key(0x87))
  rec("I38_retry_reqs", tostring(#requests))

  -- 换了输入（新的签名）照常发
  ctxI38.input = "nihd"
  ctxI38.props["ai_pinyin/first_py"] = "ni hao"
  feed(revert, envI38, ctxI38, key(0x87))
  feed(revert, envI38, ctxI38, key(0x87))
  rec("I38_retry_new_sig", tostring(#requests))

  -- 回答里带标点 -> 不采纳
  requests = {}
  ctxI38.input = "nihb"
  ctxI38.props["ai_pinyin/first_py"] = "ni hao"
  req_state = "idle"
  feed(revert, envI38, ctxI38, key(0x87))
  feed(revert, envI38, ctxI38, key(0x87))
  req_state = "done"
  result_body = '{"choices":[{"message":{"content":"\u{4f60}\u{597d}\u{3002}"}}]}'
  feed(revert, envI38, ctxI38, key(0x87))
  rec("I38_punct_result", ctxI38.props["ai_pinyin/result"] or "")

  -- 再按一次触发码 -> 关掉
  ctxI38.input = "aiur"
  rec("I38_off_ret", feed(revert, envI38, ctxI38, key(0x3B)))
  rec("I38_off", tostring(envI38.ai_pinyin and envI38.ai_pinyin.on))

  -- 打开后按 Esc：只丢掉这次等待，模式仍然开着
  ctxI38.input = "aiur"
  feed(revert, envI38, ctxI38, key(0x3B))
  rec("I38_on2", tostring(envI38.ai_pinyin and envI38.ai_pinyin.on))
  ctxI38.input = "nihc"
  envI38.ai_pinyin.req = { seq = 1, sig = "nihc", started = os.clock() }
  feed(revert, envI38, ctxI38, key(0xFF1B))
  rec("I38_esc_off", tostring(envI38.ai_pinyin and envI38.ai_pinyin.on))
  rec("I38_esc_req", tostring(envI38.ai_pinyin and envI38.ai_pinyin.req ~= nil))
  revert.fini(envI38)
end

-- I38c. 模型挑的就是当前第一位 -> 不插入
do
  local cfgI38c = cfg_plain({
    ["actions/trigger"]             = "qq/",
    ["actions/dll_dir"]             = DLLREL,
    ["actions/debounce_ms"]         = 0,
    ["actions/ai_pinyin/enabled"]   = true,
    ["actions/ai_pinyin/debounce_ms"] = 400,
    ["actions/ai/online/base_url"]  = "https://example.test/v1/chat/completions",
    ["actions/ai/online/api_key"]   = "sk-py",
  })
  local envI38c, ctxI38c = new_env(cfgI38c)
  logs = {}
  revert.init(envI38c)
  ctxI38c.input = "aiur"
  feed(revert, envI38c, ctxI38c, key(0x3B))
  ctxI38c.input = "nihc"
  ctxI38c.props["ai_pinyin/first"] = "\u{4f60}\u{597d}"
  ctxI38c.props["ai_pinyin/first_py"] = "ni hao"
  ctxI38c.props["ai_pinyin/map"] = "\u{4f60}\u{597d}\tni hao\n\u{62df}\u{597d}\tni hao"
  requests, nudges = {}, {}
  feed(revert, envI38c, ctxI38c, key(0x87))
  feed(revert, envI38c, ctxI38c, key(0x87))
  rec("I38c_reqs", tostring(#requests))
  req_state = "done"
  result_body = '{"choices":[{"message":{"content":"\u{4f60}\u{597d}"}}]}'
  local before = ctxI38c.refreshed
  feed(revert, envI38c, ctxI38c, key(0x87))
  rec("I38c_result", ctxI38c.props["ai_pinyin/result"] or "")
  rec("I38c_refreshed", tostring(ctxI38c.refreshed > before))
  rec("I38c_log", tostring(log_has("is the first candidate already")))
  revert.fini(envI38c)
end

-- I38b. 没开 actions/ai_pinyin/enabled 时触发码不生效
do
  local cfgI38b = cfg_plain({
    ["actions/trigger"] = "qq/",
    ["actions/dll_dir"] = DLLREL,
    ["actions/debounce_ms"] = 0,
  })
  local envI38b, ctxI38b = new_env(cfgI38b)
  revert.init(envI38b)
  rec("I38b_ready", tostring(envI38b.ai_pinyin == nil))
  ctxI38b.input = "aiur"
  rec("I38b_ret", feed(revert, envI38b, ctxI38b, key(0x3B)))
  rec("I38b_input", ctxI38b.input)
  revert.fini(envI38b)
end

-- I39. 两个滤波器的行为：捕获拼音 / 插 AI 候选
do
  _G.yield = function(c)
    if #_G.__cap == 0 then _G.__first_cmt = c.comment or "" end
    _G.__cap[#_G.__cap + 1] = c.text
  end
  _G.Candidate = function(kind, start, stop, text, comment)
    return { kind = kind, start = start, _end = stop, text = text, comment = comment }
  end
  _G.__cap = {}

  local function fake_input(list)
    return { iter = function()
      local i = 0
      return function() i = i + 1; return list[i] end
    end }
  end

  -- 捕获滤波器：把 comment 里的带调拼音去调后写进属性
  local capture = loadfile(ACTIONS_DIR .. "/ai_pinyin_capture.lua")()
  local props = {}
  local ctx = { input = "nihc", props = props }
  ctx.get_property = function(self, n) return self.props[n] end
  ctx.set_property = function(self, n, v) self.props[n] = v end
  local env = { engine = { context = ctx } }

  -- 模式没开（没有 ai_pinyin/active）：两个滤波器都不许写属性、不许改候选
  capture.func(fake_input({
    { text = "\u{4f60}\u{597d}", comment = "nǐ;rx hǎo;nz" },
  }), env)
  rec("I39b_capture_props", tostring(next(props) ~= nil))
  _G.__cap = {}
  local insert0 = loadfile(ACTIONS_DIR .. "/ai_pinyin_insert.lua")()
  insert0.func(fake_input({
    { text = "\u{4f60}\u{597d}", start = 0, _end = 4 },
    { text = "\u{59ae}", start = 0, _end = 2 },
  }), env)
  rec("I39b_insert_order", table.concat(_G.__cap, ","))
  rec("I39b_insert_props", tostring(next(props) ~= nil))

  props["ai_pinyin/active"] = "1"
  capture.func(fake_input({
    { text = "\u{4f60}\u{597d}", comment = "nǐ;rx hǎo;nz" },
    { text = "\u{59ae}", comment = "" },
  }), env)
  rec("I39_map", props["ai_pinyin/map"] or "")

  -- 插入滤波器：编码一致时 AI 词排第一，其余原样原顺序
  local insert = loadfile(ACTIONS_DIR .. "/ai_pinyin_insert.lua")()
  props["ai_pinyin/result"] = "\u{60a8}\u{597d}"
  props["ai_pinyin/result_sig"] = "nihc"
  props["ai_pinyin/marker"] = "AI"
  _G.__cap = {}
  insert.func(fake_input({
    { text = "\u{4f60}\u{597d}", start = 0, _end = 4 },
    { text = "\u{59ae}", start = 0, _end = 2 },
  }), env)
  rec("I39_order", table.concat(_G.__cap, ","))
  rec("I39_first_py", props["ai_pinyin/first_py"] or "")

  -- 编码变了：不插 AI 词
  props["ai_pinyin/result_sig"] = "other"
  _G.__cap = {}
  insert.func(fake_input({
    { text = "\u{4f60}\u{597d}", start = 0, _end = 4 },
    { text = "\u{59ae}", start = 0, _end = 2 },
  }), env)
  rec("I39_stale", table.concat(_G.__cap, ","))

  -- AI 的答案就是当前第一位：不新增候选，只给第一位的注释加标记
  props["ai_pinyin/result_sig"] = "nihc"
  props["ai_pinyin/result"] = "\u{4f60}\u{597d}"
  props["ai_pinyin/same"] = "1"
  _G.__cap = {}
  _G.__first_cmt = nil
  insert.func(fake_input({
    { text = "\u{4f60}\u{597d}", start = 0, _end = 4 },
    { text = "\u{59ae}", start = 0, _end = 2 },
  }), env)
  rec("I39c_order", table.concat(_G.__cap, ","))
  rec("I39c_comment", tostring(_G.__first_cmt))

  -- 标记只对同一个编码有效：编码变了就退回普通候选
  props["ai_pinyin/result_sig"] = "other"
  _G.__cap = {}
  _G.__first_cmt = nil
  insert.func(fake_input({
    { text = "\u{4f60}\u{597d}", start = 0, _end = 4 },
    { text = "\u{59ae}", start = 0, _end = 2 },
  }), env)
  rec("I39c_stale_comment", tostring(_G.__first_cmt))
  props["ai_pinyin/same"] = "0"

  _G.yield = nil
  _G.Candidate = nil
  _G.__cap = nil
end

-- I40. 手按退格要跟着扣账本（编码中 / 按住修饰键 / 注入回环 都不记）
do
  local cfgI40 = cfg_plain({
    ["actions/trigger"]     = "qq/",
    ["actions/dll_dir"]     = DLLREL,
    ["actions/debounce_ms"] = 0,
    ["actions/shortcuts"]   = "dd;=@delete",
  })
  local envI40, ctxI40 = new_env(cfgI40)
  revert.init(envI40)

  local function delete_newest()
    injected = {}
    ctxI40.input = "dd"
    feed(revert, envI40, ctxI40, key(0x3B))
    return tostring(injected[1] or "")
  end

  -- 1) 上屏 3 个字，手按一次退格 -> 账本只剩 2 个
  commit(ctxI40, "\u{4f60}\u{597d}\u{5417}")
  local ret_bs = feed(revert, envI40, ctxI40, key(0xFF08))
  rec("I40_ret", tostring(ret_bs))
  rec("I40_after_one", delete_newest())

  -- 2) 编码还在时按退格：删的是编码，账本不动
  commit(ctxI40, "\u{4f60}\u{597d}\u{5417}")
  ctxI40.input = "ni"
  feed(revert, envI40, ctxI40, key(0xFF08))
  ctxI40.input = ""
  rec("I40_composing", delete_newest())

  -- 3) 注入期的退格不重复扣账
  commit(ctxI40, "\u{4f60}\u{597d}")
  envI40.bs_ignore = 2
  envI40.bs_ignore_until = os.clock() + 100
  feed(revert, envI40, ctxI40, key(0xFF08))
  feed(revert, envI40, ctxI40, key(0xFF08))
  rec("I40_suppressed", delete_newest())

  -- 4) 带 Shift 的退格（选中后删除）不记
  commit(ctxI40, "\u{4f60}\u{597d}\u{5417}")
  feed(revert, envI40, ctxI40, key(0xFF08, { shift = true }))
  rec("I40_shifted", delete_newest())

  revert.fini(envI40)
end

-- F. end-to-end async path, using the counting-only dry-run dll
-- =====================================================================
logs = {}                           -- only look at what F itself logs
package.loaded["actions"] = nil
local dry_entry = USERDIR_DRY .. "/" .. DLLREL .. "/?.dll;"
local ds, de = package.cpath:find(dry_entry, 1, true)
if ds then package.cpath = package.cpath:sub(1, ds - 1) .. package.cpath:sub(de + 1) end
rime_api.get_user_data_dir = function() return USERDIR_DRY end

local revertF = assert(loadfile(ACTIONS_PATH))()
revertF.set_debug(true)   -- F 段重新 load 了一遍，开关要重新打开
local envF, ctxF = new_env(nil)
revertF.init(envF)
local modF = package.loaded["actions"]
rec("F_module_has_dry_run", modF and type(modF._dry_run_sent))
rec("F_module_has_press_backspace", modF and type(modF.press_backspace))

local function sent() return modF._dry_run_sent() end
local function wait_events(target, timeout_ms)
  local waited = 0
  while waited < timeout_ms do
    if sent() >= target then return sent() end
    sleep_ms(2)
    waited = waited + 2
  end
  return sent()
end

commit(ctxF, "aaaaa")   -- 5 -> 10 events (down+up per key)
commit(ctxF, "bbbb")    -- 4 ->  8
commit(ctxF, "ccc")     -- 3 ->  6   (newest commit is popped first)

sleep_ms(300)                        -- make sure the worker is idle and out of the window
local base = sent()
type_text(revertF, envF, ctxF, "qq/")     -- accepted 3
type_text(revertF, envF, ctxF, "qq/")     -- microseconds later: C side is busy -> 0,
                                          -- Lua puts the 4 back on the stack
rec("F_events1", tostring(wait_events(base + 6, 800) - base))
sleep_ms(60)
rec("F_dropped_events", tostring(sent() - base))       -- still 6: no second burst

-- leave the window, then retry: the 4 that was put back must be deleted
sleep_ms(250)
type_text(revertF, envF, ctxF, "qq/")
rec("F_events_after_retry", tostring(wait_events(base + 14, 800) - base))

sleep_ms(250)
type_text(revertF, envF, ctxF, "qq/")                  -- accepted 5
rec("F_events_after_fourth", tostring(wait_events(base + 24, 800) - base))

sleep_ms(250)
type_text(revertF, envF, ctxF, "qq/")                  -- stack empty
sleep_ms(60)
rec("F_stack_empty_events", tostring(sent() - base))
rec("F_log_has_nothing_to_delete", tostring(log_has("nothing to delete")))

-- C-side guards on this build too
rec("F_c_zero", tostring(modF.press_backspace(0)))
rec("F_c_negative", tostring(modF.press_backspace(-5)))
local okF = pcall(modF.press_backspace, "x")
rec("F_c_badarg", tostring(not okF))

-- MIN_INTERVAL_MS guard, on its own: accept one request, then ask again inside
-- the window.  The second request must be refused (0) and start no burst.
sleep_ms(300)
rec("F_interval_first_return", tostring(modF.press_backspace(2)))
rec("F_interval_busy_return", tostring(modF.press_backspace(2)))   -- still busy -> 0
local after_burst = wait_events(sent() + 4, 800)                   -- burst done (~80ms)
rec("F_interval_return", tostring(modF.press_backspace(2)))        -- inside 150ms -> 0
sleep_ms(120)
rec("F_interval_no_new_events", tostring(sent() == after_burst))

-- clamp to MAX_BACKSPACES, and prove the call returns before the keys are sent
-- (the count keeps growing in the background afterwards)
sleep_ms(300)
local clamp_base = sent()
rec("F_clamp_return", tostring(modF.press_backspace(1000000)))
sleep_ms(120)                       -- past FIRE_DELAY_MS: first batch is out
local m1 = sent()
sleep_ms(60)
local m2 = sent()
rec("F_async_progress", tostring(m2 > m1))

-- let the big burst finish (10000 backspaces = 20000 counted events) and the
-- worker fall back to its idle poll loop before this process exits
local done = wait_events(clamp_base + 20000, 30000)
rec("F_clamp_burst_done", tostring(done >= clamp_base + 20000))
sleep_ms(200)
revertF.fini(envF)

return table.concat(out, "|")
"""


def main():
    # 别让当前目录里的 actions.lua 被 require 抢先当成模块（它只是组件脚本）
    os.chdir(os.path.dirname(BASE))
    if not build_dry_run_dll():
        return 1
    prepare_fake_userdir()

    lua_src = LUA_SRC.replace("USERDIR_DRY", repr(USERDIR_DRY.replace("\\", "/")))
    lua_src = lua_src.replace("USERDIR", repr(USERDIR_ON_DISK.replace("\\", "/")))
    lua_src = lua_src.replace("DLLREL", repr(DLL_REL_DIR))
    lua_src = lua_src.replace("ACTIONS_PATH", repr(os.path.join(BASE, "actions.lua").replace("\\", "/")))
    lua_src = lua_src.replace("ACTIONS_DIR", repr(BASE.replace("\\", "/")))

    # debug aid: SMOKE_SKIP_F=1 drops the async end-to-end scenario
    skip_f = bool(os.environ.get("SMOKE_SKIP_F"))
    if skip_f:
        head, _, _tail = lua_src.partition("-- F. end-to-end async path")
        lua_src = head + '\nreturn table.concat(out, "|")\n'

    dll = ctypes.CDLL(RIME)
    dll.luaL_newstate.restype = ctypes.c_void_p
    dll.luaL_openlibs.argtypes = [ctypes.c_void_p]
    dll.luaL_loadstring.restype = ctypes.c_int
    dll.luaL_loadstring.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    dll.lua_pcallk.restype = ctypes.c_int
    dll.lua_pcallk.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_void_p, ctypes.c_void_p]
    dll.lua_tolstring.restype = ctypes.c_char_p
    dll.lua_tolstring.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_size_t)]
    dll.lua_close.argtypes = [ctypes.c_void_p]

    L = dll.luaL_newstate()
    dll.luaL_openlibs(L)

    src = lua_src.encode("utf-8")
    rc = dll.luaL_loadstring(L, src)
    if rc != 0:
        n = ctypes.c_size_t()
        print("LOAD FAILED:", dll.lua_tolstring(L, -1, ctypes.byref(n)).decode("utf-8", "replace"))
        return 1

    rc = dll.lua_pcallk(L, 0, 1, 0, None, None)
    if rc != 0:
        n = ctypes.c_size_t()
        print("RUN FAILED (lua error):",
              dll.lua_tolstring(L, -1, ctypes.byref(n)).decode("utf-8", "replace"))
        return 1

    n = ctypes.c_size_t()
    result = dll.lua_tolstring(L, -1, ctypes.byref(n)).decode("utf-8", "replace")
    print("LUA-RESULT:")
    for item in result.split("|"):
        print("   ", item)

    dll.lua_close(L)
    print("lua_close: clean")

    got = dict(kv.split("=", 1) for kv in result.split("|") if "=" in kv)
    checks = {
        # module + ABI
        "chunk_loaded": "true",
        "has_init": "function",
        "has_func": "function",
        "has_fini": "function",
        "A_module": "table",
        "A_press_backspace": "function",
        "A_press_ctrl_z": "nil",
        # C guards (do nothing)
        "A_c_count_zero": "0",
        "A_c_negative": "0",
        "A_c_badarg_raises": "true",
        "A_c_badarg_msg_has_number": "true",
        # A: defaults + text trigger through key events
        "A_mode_text": "true",
        "A_returns": "2,2,1",
        "A_injected": "3",
        "A_cleared": "4",
        "A_input_after": "[]",
        "A_injected_all": "3,4",
        "A_log_has_nothing_to_delete": "true",
        "A_no_trigger_q_slash": "2,2",
        "A_no_trigger_other": "2,2,2,2,2",
        "A_no_trigger_injected": "",
        "A_disconnected": "true",
        # B: trigger + stack_size from config
        "B_trigger_from_config": "true",
        "B_old_trigger_returns": "2,2,2",
        "B_old_trigger_injected": "",
        "B_injected": "5,4,3",
        "B_cleared": "4",
        # C: max_backspaces
        "C_injected": "2,2",
        "C_log_has_truncated": "true",
        # D: clamping
        "D_clamped": "true",
        # G: hotkey + text trigger, both active at once
        "G_mode_both": "true",
        "G_log_hotkey_spec": "true",
        "G_text_returns": "2,2,1",
        "G_text_injected": "3",
        "G_hotkey_return": "1",
        "G_hotkey_injected": "3,4",
        "G_cleared_unchanged": "true",
        "G_input_after": "[]",
        "G_composing_return": "1",
        "G_composing_ignored": "true",
        "G_log_has_composing": "true",
        # G3: actions/trigger "none" -> hotkey only
        "G3_mode_hotkey_only": "true",
        "G3_text_returns": "2,2,2",
        "G3_text_injected": "",
        "G3_hotkey_return": "1",
        "G3_hotkey_injected": "3",
        "G2_hotkey_return": "1",
        "G2_injected": "3",
        # H: shortcuts -> press_keys / @to-symbol / @delete
        "H_mode_text": "true",
        "H_default_count": "27",
        "H_default_table": "26",
        "H_z_ret": "2,1",   "H_z_spec": "ctrl+z",
        "H_y_ret": "2,1",   "H_y_spec": "ctrl+y",
        "H_a_ret": "2,1",   "H_a_spec": "ctrl+a",
        "H_c_ret": "2,1",   "H_c_spec": "ctrl+c",
        "H_v_ret": "2,1",   "H_v_spec": "ctrl+v",
        "H_s_ret": "2,1",   "H_s_spec": "up",
        "H_x_ret": "2,1",   "H_x_spec": "down",
        "H_g_ret": "2,1",   "H_g_spec": "end",
        "H_t_ret": "2,1",   "H_t_spec": "home",
        "H_w_ret": "2,1",   "H_w_spec": "ctrl+shift+left",
        "H_b_ret": "2,1",   "H_b_spec": "ctrl+backspace",
        "H_r_ret": "2,1",   "H_r_spec": "ctrl+delete",
        "H_h_ret": "2,1",   "H_h_spec": "ctrl+home",
        "H_e_ret": "2,1",   "H_e_spec": "ctrl+end",
        "H_d_ret": "2,1",   "H_d_spec": "delete",
        "H_kk_ret": "2,2,1", "H_kk_spec": "ctrl+shift+k",
        "H_aa_ret": "2,2,1", "H_aa_spec": "ctrl+a,ctrl+c",
        "H_dd_ret": "2,2,1", "H_dd_spec": "ctrl+shift+backspace",
        "H_ww_ret": "2,2,1", "H_ww_spec": "ctrl+shift+left,ctrl+c",
        "H_hh_ret": "2,2,1", "H_hh_spec": "shift+home,ctrl+c",
        "H_tt_ret": "2,2,1", "H_tt_spec": "ctrl+shift+home,ctrl+c",
        "H_gg_ret": "2,2,1", "H_gg_spec": "ctrl+shift+end,ctrl+c",
        "H_xx_ret": "2,2,1", "H_xx_spec": "ctrl+x",
        "H_ss_ret": "2,2,1", "H_ss_spec": "shift+home",
        "H_wrong_prefix_ret": "2,2,2",
        "H_wrong_prefix_spec": "",
        "H_wrong_final_ret": "2,2",
        "H_wrong_final_spec": "",
        "H_half_ret": "2",
        "H_half_spec": "",
        "H_ctrl_ret": "2",
        "H_ctrl_spec": "",
        "H_pq_ret": "2,2,1",
        "H_pq_injected": "3",
        "H_text_trigger_ret": "2,2,1",
        "H_text_trigger_injected": "4",
        "H_ff_ret": "2,2,1",
        "H_ff_injected": "4",
        "H_ff_spec": "",
        "H_ff_mid_symbol": "3",
        "H_ff_no_symbol": "[6]",
        "H_ff_no_symbol_log": "false",
        "H_ff_trailing_symbol": "[]",
        "H_ff_trailing_symbol_log": "true",
        "H_busy_ret": "2,1",
        "H_busy_log": "true",
        "H_refused_ret": "2,1",
        "H_refused_log": "true",
        "H_f_ret": "2,1",
        "H_f_url": "https://www.baidu.com/s?wd=%E4%BB%8A%E5%A4%A9%E5%A4%A9%E6%B0%94%E5%BE%88%E5%A5%BD",
        "H_f_busy_log": "true",
        "H_f_refused_url": "https://www.baidu.com/s?wd=%E4%BB%8A%E5%A4%A9%E5%A4%A9%E6%B0%94%E5%BE%88%E5%A5%BD",
        "H_f_refused_log": "true",
        "H2_count": "3",
        "H2_pp_ret": "2,2,1",
        "H2_pp_injected": "5",
        "H2_sm_ret": "2,2,1",
        "H2_sm_spec": "shift+home,shift+left",
        "H2_dq_ret": "2,2,1",
        "H2_dq_injected": "3",
        "H3_dup_log": "true",
        "H3_same_log": "true",
        "H3_short_log": "true",
        "H3_unknown_log": "true",
        "H3_badseq_log": "true",
        "H3_badentry_log": "true",
        "H3_dup_fire_ret": "2,2,2",
        "H3_dup_fire_spec": "",
        "H4_count": "27",
        "H5_count": "26",
        "H6_search_ret": "2,1",
        "H6_search_url": "https://www.bing.com/search?q=abc",
        "H7_search_ret": "2,1",
        "H7_search_url": "[]",
        "H7_search_log": "true",
        # H8: key gap
        "H_gap_default": "30",
        "H8_log_gap": "120",
        "H8_ret": "2,1",
        "H8_gap": "120",
        "H8_multi_spec": "shift+home,ctrl+c",
        "H8_multi_gap": "120",
        "H8b_log_gap": "500",
        "H8b_gap_capped": "500",
        # I: AI 模式（@ai 触发码 / 取文 / 发送 / 插入 / Esc / replace）
        "I0_log_ai": "off",
        "I0_flag": "false",
        "I0_ret": "2,2,1",
        "I0_mode": "nil",
        "I0_log_ignored": "true",
        "I1_log_ai": "on",
        "I1_flag": "true",
        "I1_double_ms": "1000",
        "I1_send_code": "47",
        "I1_prompt_eng": "CUSTOM-ENG-PROMPT",
        "I1_enter_ret": "2,2,1",
        "I1_mode": "default",
        "I1_log_mode_on": "true",
        "I1_press1": "1",
        "I1_press2": "1",
        "I1_req_count": "1",
        "I1_req_url": "https://example.test/v1/chat/completions",
        "I1_req_model": "test-model",
        "I1_req_user": "我出门了",
        "I1_req_auth": "yes",
        "I1_req_tokens": "128",
        "I1_nudge": "350/135",
        "I1_log_sent": "true",
        "I1_busy_ret": "1",
        "I1_busy_reqs": "1",
        "I1_busy_newlogs": "1",
        "I1_busy_last":
            "info: actions.lua: ai: previous request still running, not sending again",
        "I2_take_ret": "1",
        "I2_send_text": "我出门了，天气很好。",
        "I2_stack_delta": "1",
        "I2_stack_ai": "true",
        "I2_log_inserted": "true",
        "I2_injected": "",
        "I3_enter": "2,2,1",
        "I3_reqs": "1",
        "I3_injected": "3",
        "I3_send_text": "",
        "I3_combined": "3:XYZ",
        "I4_injected": "[]",
        "I4_send_text": "XYZ",
        "I5_log_failed": "true",
        "I5_send_text": "[]",
        "I6_esc_ret": "1",
        "I6_cancels": "1",
        "I6_log_cancel": "true",
        "I6_esc2_ret": "1",
        "I6_mode_after": "nil",
        "I7_input": "nihao",
        "I7_req_user": "nihao",
        "I7_input_after": "[]",
        "I8_combo": "true",
        "I8_ret": "1",
        "I8_reqs": "1",
        "I9_flag": "false",
        "I9_log": "true",
        "I10_flag": "false",
        "I10_log": "true",
        "I11_mode": "eng",
        "I11_system": "CUSTOM-ENG-PROMPT",
        "I11_user": "你好世界",
        "I11_log_prompt": "eng",
        "I18_roles": "system,user,assistant,user",
        "I18_system": "你是中文输入助手。如果是问题就进行精准简短的回复，如果是诗词或名句就回复原文和作者等。只输出结果正文，不要解释、不要提问、不加引号。每一次接收到的内容，单独回复不要理会之前提交的内容。",
        "I18_pair_q": "鹦鹉最爱吃什么",
        "I18_pair_a": "A1",
        "I18_last_user": "多少种罗非鱼",
        "I18b_roles": "system,user,assistant,user,assistant,user",
        "I18b_user": "第二句要发的",
        "I18b_hist": "true",
        "I19_split_user": "最好吃的大米是哪种",
        "I19_after_mark": "我出门了",
        "I19_three": "第二句第三句",
        "I20_injected": "10",
        "I20_send_text": "",
        "I20_combined": "10:XYZ",
        "I20_log_skip": "false",
        "I20b_reqs": "1",
        "I20b_user": "我出门了",
        "I20b_injected": "4",
        "I20b_send_text": "",
        "I20b_combined": "4:XYZ",
        "I21_reqs": "1",
        "I21_user": "福建的美食有哪些",
        "I21_injected": "9",
        "I21_send_text": "",
        "I21_combined": "9:XYZ",
        "I35_pending": "true",
        "I35_calls_first": "1",
        "I35_last_first": "5:XYZ",
        "I35_log_deferred": "true",
        "I35_calls": "2",
        "I35_last": "5:XYZ",
        "I35_pending_after": "false",
        "I35_log_inserted": "true",
        "I21_log_skip": "false",
        "I12_reqs": "0",
        "I12_log": "true",
        "I22_reqs": "1",
        "I22_user": "中国有多少个市",
        "I22_injected": "8",
        "I23_reqs": "1",
        "I23_user": "中国有多少个市",
        "I23_injected": "9",
  "I24_reqs": "1",
  "I24_user": "第二句",
  "I24_injected": "5",
  "I25_ret": "1",
  "I25_injected": "3",
  "I26_reqs": "1",
  "I26_nudge_first": "350/135",
  "I26_tick_ret": "1",
  "I26_send_text": "",
        "I26_combined": "7:XYZ",
  "I26b_reqs": "1",
  "I26b_nudge_again": "350/135",
  "I27_reqs": "1",
  "I27_user": "中国有多少个市",
  "I27_injected": "8",
  "I27b_reqs": "1",
  "I27b_user": "中国有多少个市",
  "I27b_injected": "8",
  "I28_reqs": "1",
  "I28_popped": "1",
  "I28_input": "",
  "I28_injected": "8",
  "I29_reqs": "1",
  "I29_cleared": "1",
  "I29_input": "",
  "I29_injected": "8",
        "I13_trailing_marks": "我出门了",
        "I13_marks_between": "我回来了",
        "I13_only_marks": "-",
        "I13_include_marks": "我出门了，。",
        "I14_reqs": "1",
        "I14_injected": "6",
        "I14_send_text": "",
        "I14_combined": "6:XYZ",
        "I15_pass": "2",
        "I15_reqs": "0",
        "I15_second": "1",
        "I15_popped": "1",
        "I15_reqs2": "1",
        "I15_body_has": "yes",
        "I15b_first": "1",
        "I15b_reqs": "0",
        "I15b_log_no_cjk": "true",
        "I16_reqs_first": "1",
        "I16_reqs_wait": "1",
        "I16_reqs_after_take": "1",
        "I16_send_text": "XYZ",
        "I16_reqs_one": "1",
        "I16_reqs_two": "1",
        "I16_log_nothing": "true",
        "I16_reqs_three": "2",
        # I17: 读剪贴板发送
        "I17_mode": "default",
        "I17_log_clip": "true",
        "I17_ret": "1",
        "I17_pending": "true",
        "I17_reqs_before_tick": "0",
        "I17_reqs": "1",
        "I17_body_has_clip": "true",
        "I17_prompt": "default",
        "I17_send_text": "XYZ",
        "I17b_reqs_before_tick": "0",
        "I17b_reqs_after": "0",
        "I17b_log_empty": "true",
        "I17c_reqs": "1",
        "I17c_body_has_4": "true",
        "I17c_body_has_6": "false",
        "I17c_log_trunc": "true",
        "I17d_ret": "2",
        "I17d_reqs": "0",
        # I30–I34: 上下文按字算 / 分次上屏拼句 / 汉字判定 / 写法校验
        "I30_pair_q": "鹦鹉最爱吃什么",
        "I30_last_user": "多少种罗非鱼",
        "I31_roles": "system,user,assistant,user",
        "I32_reqs": "0",
        "I32_log_nocjk": "true",
        "I32b_reqs": "1",
        "I33_log_bad": "true",
        "I33_injected": "",
        "I34_log_bad": "true",
        "I34_injected": "",
        # I36: 日志开关（生产默认只写 error）
        "I36_debug": "false",
        "I36_no_info": "true",
        "I36_error_kept": "true",
        "I36_debug_on": "true",
        "I36_info_back": "true",
        # I37: 快符补记（commit_history）
        # I38: AI 拼音候选
        "I38_ready": "true",
        "I38_off_at_start": "false",
        "I38_ret": "1",
        "I38_on": "true",
        "I38_live_py": "aiur",
        "I38_nopy_reqs": "0",
        "I38_wait_reqs": "0",
        "I38_reqs": "1",
        "I38_user": "拼音：ni hao\\n现有候选：你好 / 拟好 / 泥蒿",
        "I38_url": "https://example.test/v1/chat/completions",
        "I38_auth": "yes",
        "I38_system_has_prompt": "true",
  "I38_system_head": "你是拼音输入法的补全工具。用户会给你三样东西：",
        "I38_result": "\u60a8\u597d",
        "I38_result_sig": "nihc",
        "I38_refreshed": "true",
        "I38_mismatch_reqs": "1",
        "I38_mismatch_result": "",
        "I38_mismatch_log": "true",
        "I38_punct_result": "",
  "I38_retry_reqs": "0",
  "I38_retry_new_sig": "1",
        "I38_off_ret": "1",
        "I38_off": "false",
        "I38_on2": "true",
        "I38_esc_off": "true",
        "I38_esc_req": "false",
        "I38c_reqs": "1",
        "I38c_result": "",
        "I38c_refreshed": "true",
        "I38c_log": "true",
        "I38b_ready": "true",
        "I38b_ret": "2",
        "I38b_input": "aiur;",
        # I39: 滤波器
        "I39b_capture_props": "false",
        "I39b_insert_order": "\u4f60\u597d,\u59ae",
        "I39b_insert_props": "false",
  "I40_ret": "2",
  "I40_after_one": "2",
  "I40_composing": "3",
  "I40_suppressed": "2",
  "I40_shifted": "3",
        "I39_map": "\u4f60\u597d\tni hao",
        "I39_order": "\u60a8\u597d,\u4f60\u597d,\u59ae",
        "I39_first_py": "ni hao",
        "I39_stale": "\u4f60\u597d,\u59ae",
        "I39c_order": "\u4f60\u597d,\u59ae",
        "I39c_comment": "AI",
        "I39c_stale_comment": "",
        "I37_hist_ready": "true",
        "I37_ret": "2,2,1",
        "I37_injected": "1",
        "I37_stack": "1",
        "I37b_injected": "3",
        "I37c_hist_ready": "false",
        "I37c_injected": "7",
        "I37d_stack": "18",
        "I37d_latest": "？",
        # E: broken dll_dir
        "E_good_entry_removed": "true",
        "E_cpath_clean": "true",
        "E_log_has_load_failure": "true",
        "E_fire_returns": "2,2,1",
        "E_injected": "",
        "E_log_has_unavailable": "true",
        # F: async injection (dry-run dll)
        "F_module_has_dry_run": "function",
        "F_module_has_press_backspace": "function",
        "F_events1": "6",
        "F_dropped_events": "6",
        "F_events_after_retry": "14",
        "F_events_after_fourth": "24",
        "F_stack_empty_events": "24",
        "F_log_has_nothing_to_delete": "true",
        "F_c_zero": "0",
        "F_c_negative": "0",
        "F_c_badarg": "true",
        "F_interval_first_return": "2",
        "F_interval_busy_return": "0",
        "F_interval_return": "0",
        "F_interval_no_new_events": "true",
        "F_clamp_return": "10000",
        "F_async_progress": "true",
        "F_clamp_burst_done": "true",
    }
    bad = {k: (v, got.get(k)) for k, v in checks.items() if got.get(k) != v}
    if skip_f:
        bad = {k: v for k, v in bad.items() if not k.startswith("F_")}
    print(f"CHECKS: {len(checks)} asserted,", "ALL PASS" if not bad else f"FAILED -> {bad}")
    return 0 if not bad else 2


if __name__ == "__main__":
    sys.exit(main())
