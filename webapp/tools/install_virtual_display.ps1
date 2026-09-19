# Installs only the pinned, verified virtual display driver. Run as administrator.
# Packages must already be staged under webapp/runtime/virtual-display-setup.
# Does not download, reboot, alter physical screen modes, or edit DCS settings.
$ErrorActionPreference = 'Stop'
$virtualApp = Split-Path -Parent $PSScriptRoot
$virtualStage = Join-Path $virtualApp 'runtime\virtual-display-setup'
$virtualReportPath = Join-Path $virtualStage 'installation.json'
$virtualTarget = 'C:\VirtualDisplayDriver'
$virtualSource = Join-Path $virtualStage 'driver-25.7.23\VirtualDisplayDriver'
$virtualInstaller = Join-Path $virtualStage 'nefcon-1.14.0\x64\nefconw.exe'
$virtualCertificateAdded = $false
$virtualReport = [ordered]@{started_utc=[DateTime]::UtcNow.ToString('o'); status='checking'; certificate_added=$false; device_instance=$null; driver_package=$null; restart_requested=$false}
function Save-VirtualReport { $virtualReport | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $virtualReportPath -Encoding UTF8 }
function Assert-VirtualFile([string]$Path, [string]$Expected) {
    if ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -ne $Expected) { throw "Verified package file changed: $Path" }
}
try {
    $virtualIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not ([Security.Principal.WindowsPrincipal]$virtualIdentity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Windows administrator approval is required to install a display device.' }
    if (Get-Process -Name DCS -ErrorAction SilentlyContinue) { throw 'Close DCS completely before installing the virtual display.' }
    if (Test-Path -LiteralPath (Join-Path $virtualApp 'runtime\display-export\applied.json')) { throw 'Restore the physical-monitor DCS layout before installing the virtual display.' }
    if (Test-Path -LiteralPath $virtualTarget) { throw 'An existing virtual-display configuration needs review; it will not be overwritten.' }
    if (Test-Path -LiteralPath 'HKLM:\SOFTWARE\MikeTheTech\VirtualDisplayDriver') { throw 'An existing virtual-display registry configuration needs review.' }
    $virtualExisting = @(Get-PnpDevice -Class Display | Where-Object {
        $virtualHardware = (Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName DEVPKEY_Device_HardwareIds -ErrorAction SilentlyContinue).Data
        $virtualHardware -contains 'Root\MttVDD'
    })
    if ($virtualExisting.Count) { throw 'A virtual display device already exists; refusing a duplicate installation.' }
    Assert-VirtualFile (Join-Path $virtualSource 'MttVDD.inf') '550D211FE481E74DFE3F9D724ED78BE48B3A9113405965D683D9373E8D672F5D'
    Assert-VirtualFile (Join-Path $virtualSource 'mttvdd.cat') '08A0093FC9B2E32B287A6F8A77CA4DE0A31830D29FC33D2B13A918DC859468F6'
    Assert-VirtualFile (Join-Path $virtualSource 'MttVDD.dll') 'C9CA837F57A98FBD43BC416A7F535A95843626E7759EAF85CF0CD7CE334DBB05'
    Assert-VirtualFile (Join-Path $virtualSource 'vdd_settings.xml') 'EDB2501D6D5DA17F66D15D4B97A6F4A3F0D8963165AC4A6A6259D95118288020'
    Assert-VirtualFile $virtualInstaller '4AB5D41AF3422833316BCB323BDF53F16C3C37891929D2AC8B78E99136A95AF5'
    $virtualSignature = Get-AuthenticodeSignature -LiteralPath (Join-Path $virtualSource 'mttvdd.cat')
    $virtualHelperSignature = Get-AuthenticodeSignature -LiteralPath $virtualInstaller
    if ($virtualSignature.Status -ne 'Valid' -or $virtualSignature.SignerCertificate.Thumbprint -ne '3CF8CF26D8BA266C3A483AB7D26D4A818E317D76' -or $virtualHelperSignature.Status -ne 'Valid') { throw 'The signed driver or device installer could not be verified.' }
    $virtualBefore = Get-Content -LiteralPath (Join-Path $virtualStage 'before.json') -Raw | ConvertFrom-Json
    $virtualPrimary = @($virtualBefore.monitors | Where-Object primary)
    if ($virtualPrimary.Count -ne 1 -or $virtualPrimary[0].width -lt 1536 -or $virtualPrimary[0].width -gt 7680) { throw 'The measured primary screen cannot use this three-panel virtual layout.' }
    [xml]$virtualXml = Get-Content -LiteralPath (Join-Path $virtualSource 'vdd_settings.xml') -Raw
    $virtualXml.vdd_settings.monitors.count = '1'
    $virtualXml.vdd_settings.gpu.friendlyname = 'default'
    if ($virtualXml.vdd_settings.global) { [void]$virtualXml.vdd_settings.RemoveChild($virtualXml.vdd_settings.global) }
    $virtualResolutions = $virtualXml.SelectSingleNode('/vdd_settings/resolutions')
    $virtualResolutions.RemoveAll()
    $virtualResolution = $virtualXml.CreateElement('resolution')
    foreach ($virtualEntry in @(@('width',[string]$virtualPrimary[0].width),@('height','512'),@('refresh_rate','60'))) {
        $virtualElement = $virtualXml.CreateElement($virtualEntry[0]); $virtualElement.InnerText = $virtualEntry[1]; [void]$virtualResolution.AppendChild($virtualElement)
    }
    [void]$virtualResolutions.AppendChild($virtualResolution)
    New-Item -ItemType Directory -Path $virtualTarget | Out-Null
    $virtualXml.Save((Join-Path $virtualTarget 'vdd_settings.xml'))
    $virtualReport.config_path = Join-Path $virtualTarget 'vdd_settings.xml'
    $virtualReport.config_sha256 = (Get-FileHash -LiteralPath $virtualReport.config_path -Algorithm SHA256).Hash
    $virtualReport.width = $virtualPrimary[0].width; $virtualReport.height = 512
    $virtualCertificatePath = 'Cert:\LocalMachine\TrustedPublisher\'+$virtualSignature.SignerCertificate.Thumbprint
    if (-not (Test-Path -LiteralPath $virtualCertificatePath)) {
        $virtualStore = [Security.Cryptography.X509Certificates.X509Store]::new('TrustedPublisher','LocalMachine')
        try { $virtualStore.Open('ReadWrite'); $virtualStore.Add($virtualSignature.SignerCertificate); $virtualCertificateAdded = $true } finally { $virtualStore.Close() }
    }
    $virtualReport.certificate_added = $virtualCertificateAdded
    $virtualReport.certificate_thumbprint = $virtualSignature.SignerCertificate.Thumbprint
    $virtualReport.status = 'installing'; Save-VirtualReport
    if (Get-Process -Name DCS -ErrorAction SilentlyContinue) { throw 'DCS started during setup; installation stopped.' }
    # nefconw is a GUI-subsystem executable: PowerShell's invocation operator
    # does not reliably set LASTEXITCODE. Wait on the actual child process.
    $virtualInfArgument = Join-Path $virtualSource 'MttVDD.inf'
    if ($virtualInfArgument -match '["\r\n]') { throw 'The staged INF path cannot be quoted safely.' }
    $virtualArguments = 'install "{0}" "Root\MttVDD"' -f $virtualInfArgument
    $virtualProcess = Start-Process -FilePath $virtualInstaller -ArgumentList $virtualArguments -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput (Join-Path $virtualStage 'device-install.log') -RedirectStandardError (Join-Path $virtualStage 'device-install-error.log')
    $virtualProcess.Refresh()
    $virtualReport.installer_pid = $virtualProcess.Id
    $virtualReport.installer_exit_code = $virtualProcess.ExitCode
    if ($null -eq $virtualProcess.ExitCode -or $virtualProcess.ExitCode -ne 0) { throw "The device installer returned $($virtualProcess.ExitCode). Inspect device-install.log and device-install-error.log before retrying." }
    $virtualDevices = @(Get-PnpDevice -Class Display | Where-Object {
        $virtualHardware = (Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName DEVPKEY_Device_HardwareIds -ErrorAction SilentlyContinue).Data
        $virtualHardware -contains 'Root\MttVDD'
    })
    if ($virtualDevices.Count -ne 1) { throw 'Exactly one installed virtual display device could not be verified.' }
    $virtualDevice = $virtualDevices[0]
    $virtualReport.device_instance = $virtualDevice.InstanceId
    $virtualReport.device_status = $virtualDevice.Status
    $virtualReport.driver_package = (Get-PnpDeviceProperty -InstanceId $virtualDevice.InstanceId -KeyName DEVPKEY_Device_DriverInfPath).Data
    $virtualReport.driver_installed = $true
    if ($virtualDevice.Status -ne 'OK') { throw 'The driver installed but the virtual device is not healthy. Inspect the recorded device status before continuing.' }
    $virtualReport.status = 'installed'
} catch {
    $virtualReport.status = 'failed'; $virtualReport.error = $_.Exception.Message
    try {
        $virtualFailedDevices = @(Get-PnpDevice -Class Display | Where-Object {
            $virtualHardware = (Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName DEVPKEY_Device_HardwareIds -ErrorAction SilentlyContinue).Data
            $virtualHardware -contains 'Root\MttVDD'
        })
        if ($virtualFailedDevices.Count -eq 1 -and $virtualReport.config_sha256) {
            $virtualReport.device_instance = $virtualFailedDevices[0].InstanceId
            $virtualReport.device_status = $virtualFailedDevices[0].Status
            $virtualReport.driver_package = (Get-PnpDeviceProperty -InstanceId $virtualReport.device_instance -KeyName DEVPKEY_Device_DriverInfPath -ErrorAction SilentlyContinue).Data
        } elseif ($virtualFailedDevices.Count -eq 0) {
            # Remove only exact configuration/certificate objects created by this attempt.
            if ($virtualReport.config_sha256 -and (Test-Path -LiteralPath $virtualReport.config_path) -and (Get-FileHash -LiteralPath $virtualReport.config_path -Algorithm SHA256).Hash -eq $virtualReport.config_sha256) {
                Remove-Item -LiteralPath $virtualReport.config_path
                if (-not (Get-ChildItem -LiteralPath $virtualTarget -Force)) { Remove-Item -LiteralPath $virtualTarget }
                $virtualReport.config_rolled_back = $true
            }
            if ($virtualCertificateAdded -and (Test-Path -LiteralPath $virtualCertificatePath)) {
                Remove-Item -LiteralPath $virtualCertificatePath
                $virtualReport.certificate_rolled_back = $true
            }
        }
    } catch { $virtualReport.recovery_error = $_.Exception.Message }
} finally {
    $virtualReport.finished_utc = [DateTime]::UtcNow.ToString('o'); Save-VirtualReport
}
if ($virtualReport.status -ne 'installed') { exit 1 }
