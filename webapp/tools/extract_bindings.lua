-- Read-only local profile evaluator, extending tools/dump_binds.lua's approach.
-- Only called by build_bindings.py on installed/default and Saved Games sources.
-- Profiles execute in a constrained environment: no shell, IO, OS, require or DCS.
local install, profile, device, wizardPath, output = unpack(arg)
local sources = {}
local function path(p)
    p = p:gsub('\\', '/')
    if not p:match('^%a:/') then p = install:gsub('\\','/') .. '/' .. p end
    return p
end
local function source(p)
    p = path(p)
    sources[p] = true
    return p
end
local function plain(p, optional)
    p = source(p)
    local f, err = loadfile(p)
    if not f then if optional then return {} end error(err) end
    setfenv(f, {})
    return f()
end
local assignments = plain(install .. '/Scripts/Input/DefaultAssignments.lua')
local wizard = wizardPath ~= '-' and plain(wizardPath, true) or {}
local generic = device:gsub(' {%x+%-%x+%-%x+%-%x+%-%x+}$','')
local env = {table=table, string=string, math=math, ipairs=ipairs, pairs=pairs,
    tonumber=tonumber, tostring=tostring, type=type, unpack=unpack,
    assert=assert, error=error, select=select, next=next,
    ED_PUBLIC_AVAILABLE=true, deviceName=device, _=function(s) return s end}
local symbolic = {}
setmetatable(env, {__index=function(_, k)
    if k == 'devices' then return nil end
    if k:match('^[Ii][Cc]ommand') or k:match('^iHeadTracker') then
        if not symbolic[k] then symbolic[k] = setmetatable({}, {__tostring=function() return k end}) end
        return symbolic[k]
    end
    error('Unknown DCS environment symbol: ' .. tostring(k))
end})
env.defaultDeviceAssignmentFor = function(name)
    local w = wizard[device] and wizard[device][name]
    if w and w.key then return {w} end
    local a = assignments[generic] or assignments.default or {}
    local v = a[name]
    if type(v) == 'string' then return {{key=v}} end
    if type(v) == 'table' and v.key then return {v} end
end
env.defaultFFB = function() return {} end
env.MultiEngineDefaultDeviceAssignmentForThrust = function()
    local a,b,c = env.defaultDeviceAssignmentFor('thrust'),env.defaultDeviceAssignmentFor('thrust_left'),env.defaultDeviceAssignmentFor('thrust_right')
    if b and c then return nil,b,c end
    return a,b,c
end
env.join = function(a,b) for _,v in ipairs(b) do a[#a+1]=v end return a end
env.ignore_features = function(commands, features)
    local ignore = {}; for _,f in ipairs(features) do ignore[f]=true end
    for i=#commands,1,-1 do for _,f in ipairs(commands[i].features or {}) do
        if ignore[f] then table.remove(commands,i); break end
    end end
end
local function evaluate(p)
    p=source(p)
    local f,err=loadfile(p); if not f then error(err) end
    local oldFolder,oldFile=rawget(env,'folder'),rawget(env,'filename')
    env.folder=p:match('^(.*[/])'); env.filename=p
    setfenv(f,env)
    local result=f()
    env.folder,env.filename=oldFolder,oldFile
    return result
end
env.external_profile=evaluate
env.dofile=evaluate
local result = evaluate(profile)
local actions={}
for _,kind in ipairs({'keyCommands','axisCommands'}) do
    for _,cmd in ipairs(result[kind] or {}) do
        local item={name=cmd.name,category=cmd.category,combos=cmd.combos or {},kind=kind=='keyCommands' and 'key' or 'axis'}
        local fields={'down','pressed','up','cockpit_device_id','value_down','value_pressed','value_up','action'}
        for _,k in ipairs(fields) do
            local v=cmd[k]
            if type(v)=='table' then item[k]=tostring(v) else item[k]=v end
        end
        actions[#actions+1]=item
    end
end
-- Same JSON encoding convention as the existing dumper (no copied source assets).
local ESC={['"']='\\"',['\\']='\\\\',['\n']='\\n',['\r']='\\r',['\t']='\\t'}
local function js(s) return '"'..tostring(s):gsub('[%c"\\]',function(c) return ESC[c] or string.format('\\u%04X',c:byte()) end)..'"' end
local function j(v)
    if v==nil then return 'null' end
    if type(v)=='string' then return js(v) end
    if type(v)=='number' or type(v)=='boolean' then return tostring(v) end
    if type(v)~='table' then error('Nonserializable type') end
    local array=true; for k in pairs(v) do if type(k)~='number' then array=false; break end end
    local out={}
    if array then for i=1,#v do out[#out+1]=j(v[i]) end; return '['..table.concat(out,',')..']' end
    for k,value in pairs(v) do out[#out+1]=js(k)..':'..j(value) end
    return '{'..table.concat(out,',')..'}'
end
local list={}; for p in pairs(sources) do list[#list+1]=p end; table.sort(list)
local fh=assert(io.open(output,'w')); fh:write(j({actions=actions,raw=result,sources=list})); fh:close()
