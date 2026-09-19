--[[----------------------------------------------------------------------------
  Bind dumper -- run with luae.exe from your DCS installation's bin directory

  Evaluates a DCS module's keyboard default.lua and the user's Keyboard.diff.lua
  and dumps both to JSON. Executing them is far more reliable than regex: these
  are real Lua files with includes, symbol tables and helper calls.

  usage:  luae.exe dump_binds.lua <installRoot> <moduleInputDir> <userDiff> <outDir>
------------------------------------------------------------------------------]]

local installRoot = (...) or arg[1]
local args = arg or {}
installRoot   = args[1]
local moduleDir = args[2]     -- ...\Mods\aircraft\FA-18C\Input\FA-18C\keyboard\
local userDiff  = args[3]     -- ...\Saved Games\DCS\Config\Input\FA-18C_hornet\keyboard\Keyboard.diff.lua
local outDir    = args[4]

local function norm(p)
    if not p then return p end
    p = p:gsub("\\", "/")
    if p:sub(-1) ~= "/" then p = p .. "/" end
    return p
end

installRoot = norm(installRoot)
moduleDir   = norm(moduleDir)
outDir      = norm(outDir)

--------------------------------------------------------------------------------
-- JSON out (small, local)
--------------------------------------------------------------------------------
local ESC = { ['"'] = '\\"', ['\\'] = '\\\\', ['\b'] = '\\b', ['\f'] = '\\f',
              ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t' }
local function jstr(s)
    return '"' .. tostring(s):gsub('[%c"\\]', function(c)
        return ESC[c] or string.format('\\u%04X', c:byte()) end) .. '"'
end
local function jval(v, d)
    d = d or 0
    local t = type(v)
    if v == nil then return "null" end
    if t == "boolean" then return tostring(v) end
    if t == "number" then
        if v ~= v or v == math.huge or v == -math.huge then return "null" end
        if v == math.floor(v) and math.abs(v) < 1e15 then return string.format("%d", v) end
        return string.format("%.6g", v)
    end
    if t == "string" then return jstr(v) end
    if t ~= "table" or d > 12 then return jstr(tostring(v)) end
    local isArr, n = true, 0
    for k in pairs(v) do
        if type(k) ~= "number" then isArr = false break end
        if k > n then n = k end
    end
    local out = {}
    if isArr then
        for i = 1, n do out[#out+1] = jval(v[i], d+1) end
        return "[" .. table.concat(out, ",") .. "]"
    end
    for k, val in pairs(v) do
        if type(k) == "string" or type(k) == "number" then
            out[#out+1] = jstr(tostring(k)) .. ":" .. jval(val, d+1)
        end
    end
    return "{" .. table.concat(out, ",") .. "}"
end

local function writeJson(path, tbl)
    local fh, err = io.open(path, "w")
    if not fh then print("!! cannot write " .. path .. ": " .. tostring(err)) return false end
    fh:write(jval(tbl)); fh:close()
    return true
end

--------------------------------------------------------------------------------
-- Permissive sandbox.
-- default.lua references DCS engine globals (iCommandXxx) that only exist inside
-- the sim. We don't need their values -- only combos + names -- so unknown
-- globals resolve to an inert proxy instead of erroring.
--------------------------------------------------------------------------------
local function makeProxy(name)
    local p = {}
    setmetatable(p, {
        __index    = function(_, k) return makeProxy(name .. "." .. tostring(k)) end,
        __call     = function() return 0 end,
        __tostring = function() return "<" .. name .. ">" end,
        __concat   = function(a, b) return tostring(a) .. tostring(b) end,
        __add = function() return 0 end, __sub = function() return 0 end,
        __eq  = function() return false end,
    })
    return p
end

local realG = {}
for k, v in pairs(_G) do realG[k] = v end

-- Do not install an unknown-global proxy: truthy proxies alter conditional
-- profile includes and can silently hide valid common keyboard commands.

-- helpers the input files expect
_G._ = function(s) return s end

-- Resolve the keyboard assignments and engine command IDs from this installed
-- DCS build. Run luae with the DCS installation as its working directory.
-- Treat missing input sources as an error: silent omissions create false
-- "unbound" controls and incomplete collision checks.
local Input = require('Input')
for name, value in pairs(Input.getEnvTable().Actions) do _G[name] = value end
local assignments = dofile(installRoot .. 'Scripts/Input/DefaultAssignments.lua')
_G.defaultDeviceAssignmentFor = function(name)
    local value = assignments.Keyboard[name]
    if type(value) == 'string' then return {{key = value}} end
    if type(value) == 'table' and value.key then return {value} end
    return nil
end

_G.join = function(dst, src)
    if type(dst) ~= "table" or type(src) ~= "table" then return dst end
    for _, v in ipairs(src) do dst[#dst+1] = v end
    return dst
end

local loadedProfiles = {}
_G.external_profile = function(rel)
    rel = tostring(rel):gsub("\\", "/")
    local path = installRoot .. rel
    if loadedProfiles[path] then return loadedProfiles[path] end
    local f = loadfile(path)
    if not f then
        error("external_profile missing: " .. rel)
    end
    local ok, res = pcall(f)
    if not ok or type(res) ~= "table" then
        error("external_profile failed: " .. rel .. " -> " .. tostring(res))
    end
    res.keyCommands  = res.keyCommands  or {}
    res.axisCommands = res.axisCommands or {}
    loadedProfiles[path] = res
    return res
end

--------------------------------------------------------------------------------
-- Load the module default profile
--------------------------------------------------------------------------------
local function loadDefaults()
    _G.folder = moduleDir
    local path = moduleDir .. "default.lua"
    local f, err = loadfile(path)
    if not f then return nil, "loadfile failed: " .. tostring(err) end
    local ok, res = pcall(f)
    if not ok then return nil, "execution failed: " .. tostring(res) end
    if type(res) ~= "table" or type(res.keyCommands) ~= "table" then
        return nil, "no keyCommands table returned"
    end
    return res.keyCommands
end

local function normCategory(c)
    if type(c) == "string" then return { c } end
    if type(c) == "table" then
        local out = {}
        for _, v in ipairs(c) do out[#out+1] = tostring(v) end
        return out
    end
    return {}
end

local function extract(keyCommands)
    local out, skipped = {}, 0
    for _, cmd in ipairs(keyCommands) do
        if type(cmd) == "table" and type(cmd.name) == "string" then
            local combos = {}
            if type(cmd.combos) == "table" then
                for _, cb in ipairs(cmd.combos) do
                    if type(cb) == "table" and type(cb.key) == "string" then
                        local refs = {}
                        if type(cb.reformers) == "table" then
                            for _, r in ipairs(cb.reformers) do refs[#refs+1] = tostring(r) end
                        end
                        combos[#combos+1] = { key = cb.key, reformers = refs }
                    end
                end
            end
            -- Numeric command fields. devices.lua and command_defs.lua are
            -- dofile'd by default.lua, so cockpit-device commands resolve to
            -- real numbers here. Engine-level commands (iCommandXxx) stay nil
            -- because they only exist inside the sim -- those are not bindable
            -- by writing a diff, and we exclude them later.
            local function num(v) return (type(v) == "number") and v or nil end
            out[#out+1] = {
                name     = cmd.name,
                category = normCategory(cmd.category),
                combos   = combos,
                cockpit_device_id = num(cmd.cockpit_device_id),
                down     = num(cmd.down),
                up       = num(cmd.up),
                pressed  = num(cmd.pressed),
                value_down    = num(cmd.value_down),
                value_up      = num(cmd.value_up),
                value_pressed = num(cmd.value_pressed),
            }
        else
            skipped = skipped + 1
        end
    end
    return out, skipped
end

--------------------------------------------------------------------------------
-- Load the user's diff (plain Lua, returns a table)
--------------------------------------------------------------------------------
local function loadDiff()
    if not userDiff or userDiff == "" then return nil, "no diff path given" end
    local f, err = loadfile(userDiff)
    if not f then return nil, "loadfile failed: " .. tostring(err) end
    local ok, res = pcall(f)
    if not ok then return nil, "execution failed: " .. tostring(res) end
    if type(res) ~= "table" then return nil, "diff did not return a table" end
    return res
end

--------------------------------------------------------------------------------
-- Run
--------------------------------------------------------------------------------
print("install root : " .. tostring(installRoot))
print("module dir   : " .. tostring(moduleDir))
print("user diff    : " .. tostring(userDiff))

local kc, err = loadDefaults()
if not kc then
    error("DEFAULTS FAILED: " .. tostring(err))
else
    local actions, skipped = extract(kc)
    print(string.format("defaults: %d actions extracted (%d entries skipped)", #actions, skipped))
    writeJson(outDir .. "defaults.json", { source = moduleDir .. "default.lua", actions = actions })
    print("wrote " .. outDir .. "defaults.json")
end

local diff, derr = loadDiff()
if not diff then
    error("DIFF FAILED: " .. tostring(derr))
else
    local n = 0
    if type(diff.keyDiffs) == "table" then for _ in pairs(diff.keyDiffs) do n = n + 1 end end
    print(string.format("diff: %d keyDiff entries", n))
    writeJson(outDir .. "userdiff.json", { source = userDiff, diff = diff })
    print("wrote " .. outDir .. "userdiff.json")
end

print("done.")
