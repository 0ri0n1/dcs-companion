#Requires -RunAsAdministrator
param(
    [Parameter(Mandatory=$true)][string]$Address,
    [Parameter(Mandatory=$true)][string]$InterfaceAlias,
    [Parameter(Mandatory=$true)][string]$PythonPath
)
$ErrorActionPreference = 'Stop'
$ip = [Net.IPAddress]::Parse($Address)
if ($ip.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) { throw 'An IPv4 address is required.' }
$adapterAddress = Get-NetIPAddress -InterfaceAlias $InterfaceAlias -AddressFamily IPv4 | Where-Object IPAddress -eq $Address
if (-not $adapterAddress) { throw 'The chosen address is not assigned to this adapter.' }
$network = Get-NetConnectionProfile -InterfaceAlias $InterfaceAlias
if ($network.NetworkCategory -ne 'Private') { throw 'Choose a Windows Private network before enabling phone access.' }
$executable = (Resolve-Path -LiteralPath $PythonPath).Path
if ([IO.Path]::GetFileName($executable) -notin @('python.exe','pythonw.exe')) { throw 'Expected the companion Python interpreter.' }
$ruleName = 'DCSCompanion-Tablet-18787'
$existing = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
if ($existing) {
    # This tool owns only its named rule; no existing Python or DCS rule changes.
    Remove-NetFirewallRule -Name $ruleName
}
New-NetFirewallRule -Name $ruleName -DisplayName 'DCS Companion - private network phone access' `
    -Direction Inbound -Action Allow -Enabled True -Protocol TCP -LocalPort 18787 `
    -Profile Private -LocalAddress $Address -RemoteAddress LocalSubnet `
    -InterfaceAlias $InterfaceAlias -Program $executable | Out-Null
Write-Output 'DCS Companion phone access enabled for the selected private network.'
