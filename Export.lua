--[[----------------------------------------------------------------------------
  DCS Copilot -- Phase 1 exporter + export capability probe
  Location (installed): %USERPROFILE%\Saved Games\DCS\Scripts\Export.lua

  READ-ONLY. This script never sends input to the sim. It only reads exported
  state and pushes it out over loopback UDP.

  Design rules honoured here:
    * Chains politely -- any previously defined LuaExport* hook is captured and
      called first, inside pcall, so other tools (Tacview/SRS/DCS-BIOS) survive.
    * Never throws into the sim's main loop -- every callback body is pcall'd and
      always returns a sane value. Failures go to a log file, never to the user.
    * Cheap -- telemetry is pumped from LuaExportActivityNextEvent, which the sim
      calls at OUR requested cadence, not every frame.
    * Non-blocking -- unconnected UDP socket, settimeout(0), sendto() fire-and-
      forget. Errors are counted, never raised.

  Outputs:
    Logs\dcs-copilot.log         -- diagnostics
    Logs\dcs-copilot-probe.txt   -- human readable capability table
    Logs\dcs-copilot-probe.json  -- machine readable capability report
    UDP 127.0.0.1:17781          -- {"type":"hello"|"probe"|"telemetry", ...}
------------------------------------------------------------------------------]]

local CFG = {
    host                 = "127.0.0.1",
    port                 = 17781,
    rate_hz              = 8,      -- telemetry rate (5-10 is the sane band)
    send_sensor          = true,   -- only actually sent if the server permits it
    send_objects         = false,  -- world object COUNT is always sent; full list is huge
    max_objects_listed   = 40,     -- hard cap if send_objects is enabled
    max_sensor_contacts  = 10,
    log_file             = "dcs-copilot.log",
    probe_txt            = "dcs-copilot-probe.txt",
    probe_json           = "dcs-copilot-probe.json",
    probe_retry_seconds  = 2.0,    -- re-probe cadence until we are settled in a cockpit
    probe_settle_timeout = 300.0,  -- give up waiting for a cockpit after this long
    max_packet_bytes     = 8192,   -- keep datagrams comfortably small

    -- Cockpit display scraping (list_indication). This is what lets a reader see
    -- UFC / DDI text instead of guessing which option button is which.
    send_indications     = true,
    indication_hz        = 1.0,    -- displays change slowly; 1 Hz is plenty
    -- Verified on the F/A-18C 2026-08-15 by survey:
    --   2 = left DDI (stores/menu)   3 = radar page   4 = AMPCD/HSI
    --   5 = DDI       6 = UFC  <-- the one that removes button guessing
    -- 2026-08-16: widened from {2,3,4,5,6}. The AMPCD/HSI option labels and the
    -- HSEL/CSEL/POS/SCL readouts are visibly on screen but absent from ids 2-6,
    -- so they live on a display that was never streamed. Surveys have reported
    -- text on 0, 1 and 7 at various times; 9-15 carry picture layers only
    -- (RWR_Bake_picture etc., 0 text elements) and are deliberately excluded.
    indication_ids       = { 0, 1, 2, 3, 4, 5, 6, 7 },
    indication_max_chars = 3000,   -- per indicator, truncated with a marker
    indication_survey    = "dcs-copilot-indications.txt",
    indication_survey_max = 20,    -- probe ids 0..N-1 once, to find what exists
    indication_raw_chars = 900,    -- raw bytes per id dumped into the survey file
}

--------------------------------------------------------------------------------
-- Chain: capture whatever was defined before us.
--------------------------------------------------------------------------------
local prev = {
    Start             = LuaExportStart,
    Stop              = LuaExportStop,
    ActivityNextEvent = LuaExportActivityNextEvent,
    BeforeNextFrame   = LuaExportBeforeNextFrame,
    AfterNextFrame    = LuaExportAfterNextFrame,
    OnHumanStart      = LuaExportOnHumanStart,
    OnHumanStop       = LuaExportOnHumanStop,
}

--------------------------------------------------------------------------------
-- State
--------------------------------------------------------------------------------
local S = {
    lfs          = nil,
    socket       = nil,
    udp          = nil,
    logfh        = nil,
    started      = false,
    interval     = 1.0 / (CFG.rate_hz or 8),
    lastSend     = -1e9,
    lastProbe    = -1e9,
    probeUntil   = nil,     -- model time after which we stop waiting for a cockpit.
                            -- Anchored to when probing was ARMED, not to absolute
                            -- model time: in multiplayer you join a mission already
                            -- in progress (t was 649 s on one join), so comparing t
                            -- directly against a 300 s DURATION shut the gate before
                            -- the first activity event and no re-probe could ever run.
    probeCount   = 0,
    settled      = false,   -- have we ever probed while actually in a cockpit?
    finalWritten = false,
    sendErrors   = 0,
    sendOk       = 0,
    lastSockRetry= -1e9,
    surveyed     = false,   -- has the one-shot indication survey run?
    lastIndication = -1e9,
    caps         = { always = "UNKNOWN", ownship = "UNKNOWN",
                     sensor = "UNKNOWN", object = "UNKNOWN", cockpit = "UNKNOWN" },
}

--------------------------------------------------------------------------------
-- Logging (fail-silent)
--------------------------------------------------------------------------------
local function logLine(msg)
    if not S.logfh then return end
    pcall(function()
        S.logfh:write(string.format("[%s] %s\n", os.date("%Y-%m-%d %H:%M:%S"), tostring(msg)))
        S.logfh:flush()
    end)
end

local function openLog()
    pcall(function()
        if S.lfs then
            S.logfh = io.open(S.lfs.writedir() .. "Logs\\" .. CFG.log_file, "w")
        end
    end)
end

--------------------------------------------------------------------------------
-- Minimal JSON encoder (no dependencies, depth-capped, NaN/inf safe)
--------------------------------------------------------------------------------
local ESCAPES = {
    ['"'] = '\\"', ['\\'] = '\\\\', ['\b'] = '\\b',
    ['\f'] = '\\f', ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t',
}

local function jsonStr(s)
    s = string.gsub(s, '[%c"\\]', function(c)
        return ESCAPES[c] or string.format('\\u%04X', string.byte(c))
    end)
    return '"' .. s .. '"'
end

local function jsonNum(n)
    if n ~= n then return "null" end                       -- NaN
    if n == math.huge or n == -math.huge then return "null" end
    if n == math.floor(n) and math.abs(n) < 1e15 then
        return string.format("%d", n)
    end
    return string.format("%.6g", n)
end

local function isArray(t)
    local n = 0
    for k in pairs(t) do
        if type(k) ~= "number" or k < 1 or k ~= math.floor(k) then return false, 0 end
        if k > n then n = k end
    end
    return true, n
end

local function jsonEncode(v, depth)
    depth = depth or 0
    local tv = type(v)
    if v == nil                then return "null" end
    if tv == "boolean"         then return tostring(v) end
    if tv == "number"          then return jsonNum(v) end
    if tv == "string"          then return jsonStr(v) end
    if tv ~= "table"           then return jsonStr("<" .. tv .. ">") end
    if depth >= 7              then return jsonStr("<max-depth>") end

    local arr, n = isArray(v)
    local out = {}
    if arr then
        for i = 1, n do out[#out + 1] = jsonEncode(v[i], depth + 1) end
        return "[" .. table.concat(out, ",") .. "]"
    end
    for k, val in pairs(v) do
        local tk = type(k)
        if tk == "string" or tk == "number" then
            out[#out + 1] = jsonStr(tostring(k)) .. ":" .. jsonEncode(val, depth + 1)
        end
    end
    return "{" .. table.concat(out, ",") .. "}"
end

--------------------------------------------------------------------------------
-- Safe call helper.
--   returns: status ("ok" | "missing" | "error"), value, errmsg
--------------------------------------------------------------------------------
local function safe(fn, ...)
    if type(fn) ~= "function" then return "missing", nil, nil end
    local ok, res = pcall(fn, ...)
    if not ok then return "error", nil, tostring(res) end
    return "ok", res, nil
end

-- convenience: value or nil, never raises
local function val(fn, ...)
    local st, v = safe(fn, ...)
    if st == "ok" then return v end
    return nil
end

--------------------------------------------------------------------------------
-- UDP transport
--------------------------------------------------------------------------------
local function openSocket()
    local ok, err = pcall(function()
        -- DCS loads bin\lua-socket.dll itself, so a bare require normally works.
        -- Fallbacks cover older layouts where the DLL lived in LuaSocket\.
        local sock
        local got = pcall(function() sock = require("socket") end)
        if not got or not sock then
            pcall(function()
                local cur = S.lfs and S.lfs.currentdir() or ""
                package.path  = package.path  .. ";" .. cur .. "LuaSocket\\?.lua"
                package.cpath = package.cpath .. ";" .. cur .. "LuaSocket\\?.dll"
                                              .. ";" .. cur .. "bin\\lua-?.dll"
                                              .. ";" .. cur .. "bin-mt\\lua-?.dll"
                sock = require("socket")
            end)
        end
        if not sock then error("LuaSocket unavailable") end
        S.socket = sock
        -- Unconnected socket + sendto(): avoids the Windows WSAECONNRESET trap
        -- that permanently breaks a *connected* UDP socket when no listener is up.
        S.udp = sock.udp()
        S.udp:settimeout(0)
    end)
    if not ok then
        logLine("socket init FAILED: " .. tostring(err))
        S.udp = nil
        return false
    end
    logLine(string.format("socket ready -> %s:%d", CFG.host, CFG.port))
    return true
end

local function send(tbl)
    if not S.udp then return end
    local ok, payload = pcall(jsonEncode, tbl)
    if not ok or type(payload) ~= "string" then
        logLine("encode failed: " .. tostring(payload))
        return
    end
    if #payload > CFG.max_packet_bytes then
        -- Never silently ship a truncated (invalid) JSON body; send a marker.
        payload = jsonEncode({ type = "oversize", dropped_bytes = #payload,
                               kind = tbl.type })
    end
    local sok, serr = pcall(function()
        return S.udp:sendto(payload, CFG.host, CFG.port)
    end)
    if sok then
        S.sendOk = S.sendOk + 1
    else
        S.sendErrors = S.sendErrors + 1
        if S.sendErrors <= 3 then logLine("send error: " .. tostring(serr)) end
    end
end

--------------------------------------------------------------------------------
-- Capability probe
--------------------------------------------------------------------------------
local PROBE_SPEC = {
    -- always available, used as the control group
    { cat = "always",  fn = "LoGetPilotName"              },
    { cat = "always",  fn = "LoGetModelTime"              },
    { cat = "always",  fn = "LoGetVersionInfo"            },
    -- allow_ownship_export
    { cat = "ownship", fn = "LoGetSelfData"               },
    { cat = "ownship", fn = "LoGetAltitudeAboveSeaLevel"  },
    { cat = "ownship", fn = "LoGetIndicatedAirSpeed"      },
    { cat = "ownship", fn = "LoGetEngineInfo"             },
    -- allow_sensor_export
    { cat = "sensor",  fn = "LoGetTWSInfo"                },
    { cat = "sensor",  fn = "LoGetTargetInformation"      },
    { cat = "sensor",  fn = "LoGetLockedTargetInformation"},
    { cat = "sensor",  fn = "LoGetSnares"                 },
    { cat = "sensor",  fn = "LoGetSightingSystemInfo"     },
    -- allow_object_export
    { cat = "object",  fn = "LoGetWorldObjects"           },
    { cat = "object",  fn = "LoGetPlayers"                },
    -- cockpit device reads -- THE OPEN QUESTION.
    -- If these work while sensor/object are denied, that is the loophole for
    -- sensor-grade data on a locked server. Probe decides; we do not assume.
    { cat = "cockpit", fn = "LoGetControlPanel_HSI"       },
    { cat = "cockpit", fn = "LoGetMCPState"               },
    { cat = "cockpit", fn = "LoGetNavigationInfo"         },
}

local function summarize(v)
    local tv = type(v)
    if tv == "table" then
        local n = 0
        for _ in pairs(v) do n = n + 1 end
        local ok, s = pcall(jsonEncode, v)
        if not ok then s = "<encode-error>" end
        if #s > 300 then s = string.sub(s, 1, 300) .. "..." end
        return { kind = "table", count = n, sample = s }
    elseif tv == "string" then
        -- An empty string carries no data (e.g. a display that is simply off).
        -- Distinguishing it keeps the cockpit verdict honest.
        if #v == 0 then return { kind = "empty", value = "" } end
        return { kind = "string", value = (#v > 120) and (string.sub(v, 1, 120) .. "...") or v }
    elseif tv == "nil" then
        return { kind = "nil" }
    else
        return { kind = tv, value = v }
    end
end

-- Probe the cockpit device layer separately -- these are not plain LoGet* calls.
-- Note: existence is checked BEFORE calling, so an absent API reports as
-- "missing" rather than being disguised as a call "error".
local function probeCockpitDevices(results)
    -- GetDevice(0) is the cockpit argument device used by DCS-BIOS / Helios.
    if type(GetDevice) ~= "function" then
        results[#results + 1] = { category = "cockpit", fn = "GetDevice(0)",
                                  status = "missing", summary = summarize(nil) }
    else
        local st, dev, err = safe(function() return GetDevice(0) end)
        results[#results + 1] = { category = "cockpit", fn = "GetDevice(0)",
                                  status = st, error = err, summary = summarize(dev) }
        if st == "ok" and dev ~= nil then
            local st2, av, err2 = safe(function() return dev:get_argument_value(0) end)
            results[#results + 1] = { category = "cockpit",
                                      fn = "GetDevice(0):get_argument_value(0)",
                                      status = st2, error = err2, summary = summarize(av) }
        end
    end

    -- list_indication(n) scrapes rendered cockpit displays (DDI/UFC/RWR text).
    -- This is the highest-value read if it survives a locked server.
    if type(list_indication) ~= "function" then
        results[#results + 1] = { category = "cockpit", fn = "list_indication(n)",
                                  status = "missing", summary = summarize(nil) }
    else
        for _, idx in ipairs({ 0, 1, 2 }) do
            local st3, txt, err3 = safe(function() return list_indication(idx) end)
            local sum = summarize(txt)
            if st3 == "ok" and type(txt) == "string" then
                sum.length = #txt
                sum.nonempty = (#txt > 0)
            end
            results[#results + 1] = { category = "cockpit",
                                      fn = string.format("list_indication(%d)", idx),
                                      status = st3, error = err3, summary = sum }
        end
    end
end

-- Decide a verdict per category given the raw results and whether we are settled.
local function verdictFor(cat, results, settled)
    local anyOk, anyMissing, anyError, allNil = false, false, false, true
    local seen = false
    for _, r in ipairs(results) do
        if r.category == cat then
            seen = true
            if r.status == "ok" then
                anyOk = true
                if r.summary and r.summary.kind ~= "nil"
                              and r.summary.kind ~= "empty" then allNil = false end
            elseif r.status == "missing" then anyMissing = true
            elseif r.status == "error"   then anyError = true end
        end
    end
    if not seen then return "UNKNOWN" end
    if anyOk and not allNil then return "AVAILABLE" end
    if anyError then return "ERROR" end
    if anyOk and allNil then
        -- Calls succeed but every one returns nil.
        if settled then return "DENIED" end       -- in a cockpit => really blocked
        return "PENDING"                          -- not in a cockpit yet => inconclusive
    end
    if anyMissing then return "MISSING_API" end
    return "UNKNOWN"
end

local function writeProbeFiles(report)
    if not S.lfs then return end
    -- JSON
    pcall(function()
        local fh = io.open(S.lfs.writedir() .. "Logs\\" .. CFG.probe_json, "w")
        if fh then fh:write(jsonEncode(report)); fh:close() end
    end)
    -- human readable
    pcall(function()
        local fh = io.open(S.lfs.writedir() .. "Logs\\" .. CFG.probe_txt, "w")
        if not fh then return end
        local w = function(s) fh:write(s .. "\n") end
        w("================================================================")
        w(" DCS COPILOT -- EXPORT CAPABILITY PROBE")
        w("================================================================")
        w(" written      : " .. tostring(report.wallclock))
        w(" model time   : " .. tostring(report.model_time))
        w(" probe #      : " .. tostring(report.probe_number))
        w(" settled      : " .. tostring(report.settled) ..
          "   (true = judged from inside a cockpit; false = inconclusive)")
        w(" pilot        : " .. tostring(report.pilot))
        w(" aircraft     : " .. tostring(report.aircraft))
        w("")
        w(" VERDICTS")
        w(" ---------------------------------------------------------------")
        local order = { "always", "ownship", "sensor", "object", "cockpit" }
        for _, c in ipairs(order) do
            w(string.format("   %-10s %s", c, tostring(report.verdicts[c])))
        end
        w("")
        w(" DETAIL")
        w(" ---------------------------------------------------------------")
        w(string.format("   %-8s %-38s %-8s %s", "CAT", "FUNCTION", "STATUS", "RESULT"))
        for _, r in ipairs(report.results) do
            local res
            if r.status ~= "ok" then
                res = tostring(r.error or r.status)
            elseif r.summary.kind == "nil" then
                res = "nil"
            elseif r.summary.kind == "table" then
                res = string.format("table(%d)", r.summary.count or 0)
            else
                res = tostring(r.summary.value)
            end
            if #res > 60 then res = string.sub(res, 1, 60) .. "..." end
            w(string.format("   %-8s %-38s %-8s %s", r.category, r.fn, r.status, res))
        end
        w("")
        w(" NOTES")
        w(" ---------------------------------------------------------------")
        w("  AVAILABLE   category returned real data")
        w("  DENIED      calls ran but returned nil while in a cockpit")
        w("              => server flag is off (or data genuinely absent)")
        w("  PENDING     not in a cockpit yet; verdict not trustworthy")
        w("  MISSING_API function does not exist in this DCS build")
        w("")
        w("  Run once in single player and once on a server, then diff the")
        w("  two 'VERDICTS' blocks. That difference is the real ceiling.")
        w("================================================================")
        fh:close()
    end)
end

local function runProbe(modelTime)
    S.probeCount = S.probeCount + 1
    local results = {}

    for _, spec in ipairs(PROBE_SPEC) do
        local f = _G[spec.fn]
        local st, v, err = safe(f)
        results[#results + 1] = {
            category = spec.cat, fn = spec.fn, status = st,
            error = err, summary = summarize(v),
        }
    end
    pcall(probeCockpitDevices, results)

    -- Are we actually sitting in an aircraft? That is what makes a nil meaningful.
    local pilot     = val(LoGetPilotName)
    local selfData  = val(LoGetSelfData)
    local planeId   = val(LoGetPlayerPlaneId)
    local settled   = (selfData ~= nil) or (planeId ~= nil and planeId ~= 0)
    if settled then S.settled = true end

    local verdicts = {}
    for _, c in ipairs({ "always", "ownship", "sensor", "object", "cockpit" }) do
        verdicts[c] = verdictFor(c, results, settled)
    end
    S.caps = verdicts

    local report = {
        type         = "probe",
        wallclock    = os.date("%Y-%m-%d %H:%M:%S"),
        model_time   = modelTime,
        probe_number = S.probeCount,
        settled      = settled,
        pilot        = pilot,
        aircraft     = (type(selfData) == "table") and selfData.Name or nil,
        dcs_version  = val(LoGetVersionInfo),
        verdicts     = verdicts,
        results      = results,
    }

    pcall(writeProbeFiles, report)
    -- Ship a trimmed copy over the wire (full detail stays in the files).
    send({
        type = "probe", wallclock = report.wallclock, model_time = modelTime,
        probe_number = S.probeCount, settled = settled, pilot = pilot,
        aircraft = report.aircraft, verdicts = verdicts,
    })

    logLine(string.format(
        "probe #%d settled=%s always=%s ownship=%s sensor=%s object=%s cockpit=%s",
        S.probeCount, tostring(settled), verdicts.always, verdicts.ownship,
        verdicts.sensor, verdicts.object, verdicts.cockpit))

    return settled
end

--------------------------------------------------------------------------------
-- Cockpit display scraping
--
-- list_indication(n) returns the rendered text of a cockpit display as a blob of
-- "-----" separated blocks. Which n maps to which display is aircraft specific
-- and undocumented, so we SURVEY once rather than guessing -- the same approach
-- the capability probe uses.
--------------------------------------------------------------------------------
local function readIndication(id)
    if type(list_indication) ~= "function" then return nil end
    local st, txt = safe(list_indication, id)
    if st ~= "ok" or type(txt) ~= "string" or #txt == 0 then return nil end
    return txt
end

-- Pull the human-meaningful strings out of an indication blob.
--
-- VERIFIED against real DCS output 2026-08-15. The actual format is NOT
-- "name { text = value; }" -- it is line oriented:
--
--     -----------------------------------------
--     ELEMENT_NAME
--     DISPLAYED TEXT            <- optional, may be absent
--     -----------------------------------------
--     CONTAINER_NAME
--     children are {
--     }
--
-- i.e. a dashed separator, the element name on the next line, then any
-- displayed text on the lines after it. "children are {", "{" and "}" are
-- structural and carry no text.
local function parseIndication(blob)
    local out, n = {}, 0
    local pendingName, name, vals = false, nil, {}

    local function flush()
        if name then
            local text = table.concat(vals, " ")
            text = string.gsub(text, "^%s+", "")
            text = string.gsub(text, "%s+$", "")
            if #text > 0 then
                n = n + 1
                if n <= 200 and out[name] == nil then out[name] = text end
            end
        end
        name, vals = nil, {}
    end

    for line in string.gmatch(blob, "[^\r\n]+") do
        if string.find(line, "^%-%-%-%-") then
            flush()
            pendingName = true
        elseif pendingName then
            name = line
            vals = {}
            pendingName = false
        elseif name then
            if line ~= "children are {" and line ~= "}" and line ~= "{" then
                vals[#vals + 1] = line
            end
        end
    end
    flush()
    return out, n
end

local function surveyIndications()
    if type(list_indication) ~= "function" then
        logLine("indication survey: list_indication unavailable")
        return
    end
    if not S.lfs then return end
    local found = {}
    pcall(function()
        local fh = io.open(S.lfs.writedir() .. "Logs\\" .. CFG.indication_survey, "w")
        if not fh then return end
        fh:write("DCS COPILOT -- list_indication survey\n")
        fh:write("written: " .. os.date("%Y-%m-%d %H:%M:%S") .. "\n")
        local sd = val(LoGetSelfData)
        fh:write("aircraft: " .. tostring(type(sd) == "table" and sd.Name or "?") .. "\n")
        fh:write(string.rep("=", 70) .. "\n\n")
        for id = 0, CFG.indication_survey_max - 1 do
            local txt = readIndication(id)
            if txt then
                found[#found + 1] = id
                local parsed, count = parseIndication(txt)
                fh:write(string.format("---- indication %d : %d bytes, %d text elements ----\n",
                                       id, #txt, count))
                local shown = 0
                for k, v in pairs(parsed) do
                    shown = shown + 1
                    if shown <= 25 then
                        fh:write(string.format("      %-34s = %s\n", k, v))
                    end
                end
                -- RAW EXCERPT. The parsed view above depends on guessing the
                -- format; this does not. Keep it until the parser is proven
                -- against real output.
                fh:write("      [RAW EXCERPT]\n")
                local raw = string.sub(txt, 1, CFG.indication_raw_chars)
                raw = string.gsub(raw, "\r", "")
                for line in string.gmatch(raw, "[^\n]*") do
                    if #line > 0 then fh:write("      | " .. line .. "\n") end
                end
                fh:write("\n")
            end
        end
        fh:write(string.rep("=", 70) .. "\n")
        fh:write("non-empty indication ids: " .. table.concat(found, ", ") .. "\n")
        fh:write("Set CFG.indication_ids in Export.lua to the ones worth streaming.\n")
        fh:close()
    end)
    logLine("indication survey complete; non-empty ids: " .. table.concat(found, ", "))
    send({ type = "indication_survey", ids = found,
           wallclock = os.date("%Y-%m-%d %H:%M:%S") })
end

-- One packet PER display. Bundling them all would blow the datagram size cap
-- (indication 4 alone is ~12 KB raw) and get dropped as oversize.
local function sendIndications(t)
    for _, id in ipairs(CFG.indication_ids) do
        local txt = readIndication(id)
        if txt then
            if #txt > CFG.indication_max_chars then
                txt = string.sub(txt, 1, CFG.indication_max_chars)
            end
            local parsed, count = parseIndication(txt)
            if next(parsed) ~= nil then
                send({ type = "indications", t = t, id = tostring(id),
                       element_count = count, elements = parsed })
            end
        end
    end
end

--------------------------------------------------------------------------------
-- Telemetry
--------------------------------------------------------------------------------
local function collectTelemetry(t)
    local d = { type = "telemetry", t = t }

    local sd = val(LoGetSelfData)
    if type(sd) == "table" then
        d.name      = sd.Name
        d.unit      = sd.UnitName
        d.coalition = sd.Coalition
        d.country   = sd.Country
        d.heading   = sd.Heading
        d.pitch     = sd.Pitch
        d.bank      = sd.Bank
        if type(sd.LatLongAlt) == "table" then
            d.lat = sd.LatLongAlt.Lat
            d.lon = sd.LatLongAlt.Long
            d.alt = sd.LatLongAlt.Alt
        end
    end

    d.alt_msl = val(LoGetAltitudeAboveSeaLevel)
    d.alt_agl = val(LoGetAltitudeAboveGroundLevel)
    d.ias     = val(LoGetIndicatedAirSpeed)
    d.tas     = val(LoGetTrueAirSpeed)
    d.mach    = val(LoGetMachNumber)
    d.vv      = val(LoGetVerticalVelocity)
    d.aoa     = val(LoGetAngleOfAttack)
    d.yaw_mag = val(LoGetMagneticYaw)

    local g = val(LoGetAccelerationUnits)
    if type(g) == "table" then d.g = { x = g.x, y = g.y, z = g.z } end

    local eng = val(LoGetEngineInfo)
    if type(eng) == "table" then
        d.fuel_internal = eng.fuel_internal
        d.fuel_external = eng.fuel_external
        if type(eng.RPM) == "table" then d.rpm = { l = eng.RPM.left, r = eng.RPM.right } end
    end

    local mech = val(LoGetMechInfo)
    if type(mech) == "table" then
        d.gear   = (type(mech.gear) == "table") and mech.gear.value or nil
        d.flaps  = (type(mech.flaps) == "table") and mech.flaps.value or nil
        d.brake  = (type(mech.speedbrakes) == "table") and mech.speedbrakes.value or nil
        d.hook   = (type(mech.hook) == "table") and mech.hook.value or nil
        d.wing   = (type(mech.wing) == "table") and mech.wing.value or nil
    end

    local pay = val(LoGetPayloadInfo)
    if type(pay) == "table" then
        d.station_current = pay.CurrentStation
        if type(pay.Cannon) == "table" then d.shells = pay.Cannon.shells end
        if type(pay.Stations) == "table" then
            local n, stns = 0, {}
            for i, s in pairs(pay.Stations) do
                n = n + 1
                if n <= 12 and type(s) == "table" then
                    stns[#stns + 1] = { i = i, clsid = s.CLSID, count = s.count }
                end
            end
            d.stations_total = n
            d.stations = stns
        end
    end

    -- Sensor block, only if the probe says we are allowed and config wants it.
    if CFG.send_sensor and S.caps.sensor == "AVAILABLE" then
        local sens = {}
        local tws = val(LoGetTWSInfo)
        if type(tws) == "table" then
            local n, contacts = 0, {}
            for _, c in pairs(tws) do
                n = n + 1
                if n <= CFG.max_sensor_contacts and type(c) == "table" then
                    contacts[#contacts + 1] = {
                        az = c.Azimuth, el = c.Elevation,
                        dist = c.Distance, type = c.Type,
                    }
                end
            end
            sens.tws_count = n
            sens.tws = contacts
        end
        local locked = val(LoGetLockedTargetInformation)
        if type(locked) == "table" then sens.locked = locked end
        local rwr = val(LoGetSnares)
        if type(rwr) == "table" then
            local n = 0
            for _ in pairs(rwr) do n = n + 1 end
            sens.rwr_count = n
        end
        if next(sens) ~= nil then d.sensor = sens end
    end

    -- Object block: count is cheap and always informative; full list is opt-in.
    if S.caps.object == "AVAILABLE" then
        local objs = val(LoGetWorldObjects)
        if type(objs) == "table" then
            local n = 0
            for _ in pairs(objs) do n = n + 1 end
            d.world_object_count = n
            if CFG.send_objects then
                local listed, i = {}, 0
                for id, o in pairs(objs) do
                    i = i + 1
                    if i > CFG.max_objects_listed then break end
                    if type(o) == "table" then
                        listed[#listed + 1] = {
                            id = id, name = o.Name, coalition = o.Coalition,
                            lat = (type(o.LatLongAlt) == "table") and o.LatLongAlt.Lat or nil,
                            lon = (type(o.LatLongAlt) == "table") and o.LatLongAlt.Long or nil,
                        }
                    end
                end
                d.world_objects = listed
            end
        end
    end

    return d
end

--------------------------------------------------------------------------------
-- Exported callbacks. Each one: chain first, then our work, all pcall'd.
--------------------------------------------------------------------------------
function LuaExportStart()
    pcall(function() if prev.Start then prev.Start() end end)

    pcall(function()
        local ok, l = pcall(require, "lfs")
        if ok then S.lfs = l end
        openLog()
        logLine("=== DCS Copilot Phase 1 exporter starting (read-only) ===")
        openSocket()
        send({ type = "hello", wallclock = os.date("%Y-%m-%d %H:%M:%S"),
               rate_hz = CFG.rate_hz, note = "dcs-copilot phase 1" })
        -- First probe immediately. It will usually be PENDING (no cockpit yet);
        -- LuaExportActivityNextEvent re-probes until it settles.
        runProbe(0)
        S.started = true
    end)
end

function LuaExportStop()
    pcall(function()
        logLine(string.format("stopping. packets ok=%d err=%d probes=%d settled=%s",
            S.sendOk, S.sendErrors, S.probeCount, tostring(S.settled)))
        send({ type = "bye", sent = S.sendOk, errors = S.sendErrors })
        if S.udp   then pcall(function() S.udp:close() end)   end
        if S.logfh then pcall(function() S.logfh:close() end) end
        S.udp, S.logfh = nil, nil
    end)
    pcall(function() if prev.Stop then prev.Stop() end end)
end

function LuaExportActivityNextEvent(t)
    local ours = t + S.interval

    -- Chain: let a prior hook run and respect an earlier wake-up if it asks.
    local prevNext
    if prev.ActivityNextEvent then
        local ok, r = pcall(prev.ActivityNextEvent, t)
        if ok and type(r) == "number" then prevNext = r end
    end

    pcall(function()
        -- Re-probe until settled, then stop churning.
        -- The deadline is set on the FIRST event after arming, so it measures
        -- elapsed time since arming rather than absolute mission time.
        if S.probeUntil == nil then
            S.probeUntil = t + CFG.probe_settle_timeout
        end
        if not S.settled
           and (t - S.lastProbe) >= CFG.probe_retry_seconds
           and t < S.probeUntil then
            S.lastProbe = t
            if runProbe(t) and not S.finalWritten then
                S.finalWritten = true
                logLine("probe settled -- capability report is now authoritative")
            end
        end

        -- One-shot display survey, once we are actually in a cockpit and the
        -- probe says cockpit reads are permitted.
        if not S.surveyed and S.settled and CFG.send_indications
           and S.caps.cockpit == "AVAILABLE" then
            S.surveyed = true
            pcall(surveyIndications)
        end

        -- Cockpit displays, at their own slower rate.
        if CFG.send_indications and S.caps.cockpit == "AVAILABLE"
           and (t - S.lastIndication) >= (1.0 / CFG.indication_hz) then
            S.lastIndication = t
            pcall(sendIndications, t)
        end

        -- Recover a dead socket occasionally (listener restarted, etc).
        if not S.udp and (t - S.lastSockRetry) > 5.0 then
            S.lastSockRetry = t
            openSocket()
        end

        if (t - S.lastSend) >= S.interval then
            S.lastSend = t
            send(collectTelemetry(t))
        end
    end)

    if prevNext and prevNext < ours then return prevNext end
    return ours
end

function LuaExportBeforeNextFrame()
    -- Deliberately does no work of ours -- this runs every frame.
    pcall(function() if prev.BeforeNextFrame then prev.BeforeNextFrame() end end)
end

function LuaExportAfterNextFrame()
    -- Deliberately does no work of ours -- this runs every frame.
    pcall(function() if prev.AfterNextFrame then prev.AfterNextFrame() end end)
end

function LuaExportOnHumanStart()
    pcall(function() if prev.OnHumanStart then prev.OnHumanStart() end end)
    -- Entering a slot invalidates any PENDING verdict: allow a fresh probe.
    pcall(function()
        S.settled, S.finalWritten, S.lastProbe = false, false, -1e9
        S.probeUntil = nil   -- fresh settle window from this slot entry onward
        S.surveyed = false   -- new airframe => displays differ, re-survey
        logLine("human start -- re-arming capability probe and display survey")
    end)
end

function LuaExportOnHumanStop()
    pcall(function() if prev.OnHumanStop then prev.OnHumanStop() end end)
end
