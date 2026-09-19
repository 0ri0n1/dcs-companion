# Remove only the device/package/configuration recorded by this Companion install.
$ErrorActionPreference = 'Stop'
$virtualApp = Split-Path -Parent $PSScriptRoot
$virtualStage = Join-Path $virtualApp 'runtime\virtual-display-setup'
$virtualRecord = Get-Content -LiteralPath (Join-Path $virtualStage 'installation.json') -Raw | ConvertFrom-Json
$virtualIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not ([Security.Principal.WindowsPrincipal]$virtualIdentity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Windows administrator approval is required.' }
if (Get-Process -Name DCS -ErrorAction SilentlyContinue) { throw 'Close DCS before removing the virtual screen.' }
if (Test-Path -LiteralPath (Join-Path $virtualApp 'runtime\display-export\applied.json')) { throw 'Restore the DCS display layout in the companion before uninstalling the virtual screen.' }
if (-not $virtualRecord.device_instance -or $virtualRecord.driver_package -notmatch '^oem[0-9]+\.inf$') { throw 'A verified install record is required. Inspect the installation report before manual recovery.' }
$virtualPublishedInf = Join-Path $env:WINDIR ('INF\'+$virtualRecord.driver_package)
if (-not (Test-Path -LiteralPath $virtualPublishedInf) -or (Get-FileHash -LiteralPath $virtualPublishedInf -Algorithm SHA256).Hash -ne '550D211FE481E74DFE3F9D724ED78BE48B3A9113405965D683D9373E8D672F5D') { throw 'The recorded driver package is no longer the pinned MttVDD package; nothing was removed.' }
$virtualDevice = Get-PnpDevice -InstanceId $virtualRecord.device_instance -ErrorAction SilentlyContinue
if ($virtualDevice) {
    $virtualHardware = (Get-PnpDeviceProperty -InstanceId $virtualDevice.InstanceId -KeyName DEVPKEY_Device_HardwareIds).Data
    $virtualPackage = (Get-PnpDeviceProperty -InstanceId $virtualDevice.InstanceId -KeyName DEVPKEY_Device_DriverInfPath).Data
    if ($virtualHardware -notcontains 'Root\MttVDD' -or $virtualPackage -ne $virtualRecord.driver_package) { throw 'The recorded device identity changed; no device removed.' }
    & pnputil.exe /remove-device $virtualDevice.InstanceId *> (Join-Path $virtualStage 'device-remove.log')
    if ($LASTEXITCODE -ne 0) { throw 'Windows could not remove the recorded virtual display. Inspect device-remove.log.' }
}
$virtualRemaining = @(Get-CimInstance Win32_PnPSignedDriver | Where-Object InfName -eq $virtualRecord.driver_package)
if ($virtualRemaining.Count) { throw 'Another device still uses this driver package; it was retained.' }
& pnputil.exe /delete-driver $virtualRecord.driver_package *> (Join-Path $virtualStage 'package-remove.log')
if ($LASTEXITCODE -ne 0) { throw 'Windows retained the driver package. Inspect package-remove.log.' }
if ($virtualRecord.config_path -eq 'C:\VirtualDisplayDriver\vdd_settings.xml' -and (Test-Path -LiteralPath $virtualRecord.config_path)) {
    if ((Get-FileHash -LiteralPath $virtualRecord.config_path -Algorithm SHA256).Hash -eq $virtualRecord.config_sha256) {
        Remove-Item -LiteralPath $virtualRecord.config_path
        if (-not (Get-ChildItem -LiteralPath 'C:\VirtualDisplayDriver' -Force)) { Remove-Item -LiteralPath 'C:\VirtualDisplayDriver' }
    }
}
if ($virtualRecord.certificate_added -and $virtualRecord.certificate_thumbprint -eq '3CF8CF26D8BA266C3A483AB7D26D4A818E317D76') {
    $virtualCertificatePath = 'Cert:\LocalMachine\TrustedPublisher\'+$virtualRecord.certificate_thumbprint
    if (Test-Path -LiteralPath $virtualCertificatePath) { Remove-Item -LiteralPath $virtualCertificatePath }
}
@{status='uninstalled'; completed_utc=[DateTime]::UtcNow.ToString('o'); device_instance=$virtualRecord.device_instance; driver_package=$virtualRecord.driver_package} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $virtualStage 'uninstallation.json') -Encoding UTF8
