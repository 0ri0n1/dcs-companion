-- One-time, read-only Terrain API exporter for a trusted DCS/ME Lua context.
-- It installs no hooks and touches neither Export.lua nor DCS configuration.
-- The output folder must already exist. See webapp/docs/NAVIGATION.md.
local Terrain = require('terrain')
local function quote(s)
    return '"' .. tostring(s):gsub('[%z\1-\31\\"]', function(c)
        local escapes = { ['\\']='\\\\', ['"']='\\"', ['\n']='\\n', ['\r']='\\r', ['\t']='\\t' }
        return escapes[c] or string.format('\\u%04x', string.byte(c))
    end) .. '"'
end
local function encode(v, depth)
    depth = depth or 0
    assert(depth < 16, 'Unexpected deeply nested API result')
    local t = type(v)
    if t == 'nil' then return 'null' end
    if t == 'string' then return quote(v) end
    if t == 'boolean' then return tostring(v) end
    if t == 'number' then
        assert(v == v and v ~= math.huge and v ~= -math.huge, 'Nonfinite API number')
        return string.format('%.17g', v)
    end
    assert(t == 'table', 'Unsupported API data type')
    local count, max = 0, 0
    local isArray = true
    for k in pairs(v) do
        count = count + 1
        if type(k) ~= 'number' or k < 1 or k % 1 ~= 0 then isArray = false
        elseif k > max then max = k end
    end
    isArray = isArray and max == count
    local parts = {}
    if isArray then
        for i=1,count do parts[#parts+1] = encode(v[i], depth+1) end
        return '[' .. table.concat(parts, ',') .. ']'
    end
    for k, value in pairs(v) do parts[#parts+1] = quote(k) .. ':' .. encode(value, depth+1) end
    return '{' .. table.concat(parts, ',') .. '}'
end
local function position(x, z)
    if type(x) ~= 'number' or type(z) ~= 'number' then return {} end
    local lat, lon = Terrain.convertMetersToLatLon(x, z)
    local ok, height = pcall(Terrain.GetHeight, x, z)
    return { x=x, z=z, lat=lat, lon=lon, elevation_m=ok and height or nil }
end

return function(outputDirectory, installRoot)
    assert(io and io.open and os and os.date, 'Trusted runtime IO unavailable; do not desanitize mission scripting')
    assert(type(outputDirectory) == 'string' and #outputDirectory > 0, 'Supply the existing output directory')
    assert(type(installRoot) == 'string' and #installRoot > 0, 'Supply the DCS installation directory')
    local outputDir = outputDirectory:gsub('[/\\]+$', '') .. '/'
    local versionFile = assert(io.open(installRoot:gsub('[/\\]+$', '') .. '/autoupdate.cfg', 'rb'))
    local versionText = versionFile:read('*a'); versionFile:close()
    local version = assert(versionText:match('"version"%s*:%s*"([%d%.]+)"'), 'DCS version unavailable')
    local terrain = assert(Terrain.GetTerrainConfig('id'), 'No terrain loaded')
    assert(tostring(terrain):match('^[%w_%-]+$'), 'Unexpected terrain identity')
    local result = { schema='dcs-terrain-api/1', dcs_version=version, terrain=terrain,
        exported_utc=os.date('!%Y-%m-%dT%H:%M:%SZ'),
        extraction_method="Terrain.GetTerrainConfig('Airdromes'); Terrain.getRunwayList(roadnet)", airfields={} }
    for id, airdrome in pairs(Terrain.GetTerrainConfig('Airdromes') or {}) do
        assert(#result.airfields < 1000, 'Unexpected airfield count')
        local ref = airdrome.reference_point or {}
        local field = { dcs_id=id, name=(airdrome.names or {}).en or airdrome.name,
            reference=position(ref.x, ref.y), abandoned=airdrome.abandoned, runways={} }
        if airdrome.roadnet then
            local ok, runways = pcall(Terrain.getRunwayList, airdrome.roadnet)
            if ok then
                for key, rw in pairs(runways or {}) do
                    assert(#field.runways < 100, 'Unexpected runway count')
                    field.runways[#field.runways+1] = { id=key, length_m=rw.length, width_m=rw.width,
                        surface=rw.surface, course_grid_rad=rw.course,
                        ends={ {designator=rw.edge1name, threshold=position(rw.edge1x, rw.edge1y)},
                               {designator=rw.edge2name, threshold=position(rw.edge2x, rw.edge2y)} } }
                end
            else field.runway_error=tostring(runways) end
        end
        result.airfields[#result.airfields+1] = field
    end
    local filename = outputDir .. terrain .. '-' .. version .. '-' .. os.date('!%Y%m%dT%H%M%SZ') .. '.json'
    local existing = io.open(filename, 'rb')
    if existing then existing:close(); error('Refusing to overwrite existing export') end
    local file = assert(io.open(filename, 'wb'))
    file:write(encode(result)); file:close()
    return filename
end
