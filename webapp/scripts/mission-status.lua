-- Registered example: one short in-game message, no cockpit inputs.
local seconds = math.floor(timer.getTime())
local hours = math.floor(seconds / 3600)
local minutes = math.floor(seconds / 60) % 60
local remaining = seconds % 60
trigger.action.outText(string.format("Companion connected - mission time %02d:%02d:%02d", hours, minutes, remaining), 5)
