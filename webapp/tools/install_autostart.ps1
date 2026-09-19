param([switch]$Uninstall, [switch]$StartNow)
$ErrorActionPreference = 'Stop'
$appDirectory = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pythonWindowless = Join-Path $appDirectory '.venv\Scripts\pythonw.exe'
$pythonConsole = Join-Path $appDirectory '.venv\Scripts\python.exe'
$watcherScript = Join-Path $appDirectory 'autostart.py'
$startupDirectory = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startupDirectory 'DCS Companion Auto Start.lnk'
$arguments = '"' + $watcherScript + '" watch'
$shellObject = New-Object -ComObject WScript.Shell
if (Test-Path -LiteralPath $shortcutPath) {
    $existingShortcut = $shellObject.CreateShortcut($shortcutPath)
    if ($existingShortcut.TargetPath -ne $pythonWindowless -or $existingShortcut.Arguments -ne $arguments) {
        throw 'A different shortcut uses the DCS Companion Auto Start name. It was left unchanged.'
    }
}
if ($Uninstall) {
    if (Test-Path -LiteralPath $shortcutPath) { Remove-Item -LiteralPath $shortcutPath }
    if (Test-Path -LiteralPath $pythonConsole) { & $pythonConsole $watcherScript stop }
    Write-Host 'Auto Start disabled. The dashboard, DCS, and other Startup items were left alone.'
    exit 0
}
if (!(Test-Path -LiteralPath $pythonWindowless) -or !(Test-Path -LiteralPath $watcherScript)) {
    throw 'Run Setup Dashboard.cmd before enabling Auto Start.'
}
$shortcut = $shellObject.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonWindowless
$shortcut.Arguments = $arguments
$shortcut.WorkingDirectory = Split-Path -Parent $appDirectory
$shortcut.Description = 'Starts the local DCS Companion when DCS runs. Dry-run controls by default.'
$shortcut.WindowStyle = 7
$shortcut.Save()
Write-Host ('Auto Start enabled for your Windows account: ' + $shortcutPath)
if ($StartNow) {
    Start-Process -FilePath $pythonWindowless -ArgumentList $arguments -WorkingDirectory $shortcut.WorkingDirectory -WindowStyle Hidden
    Write-Host 'The hidden watcher is running. It will wait for DCS.'
}
