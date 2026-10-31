param([switch]$Uninstall)

$here     = Split-Path -Parent $MyInvocation.MyCommand.Path
$script   = Join-Path $here 'hyperx_battery_logger.py'
$startup  = [Environment]::GetFolderPath('Startup')
$launcher = Join-Path $startup 'HyperX Battery Logger.vbs'
$oldLink  = Join-Path $startup 'HyperX Battery Logger.lnk'

$pythonw = python -c "import sys,os,hid; print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"
if (-not $pythonw -or -not (Test-Path $pythonw)) {
    Write-Error "Couldn't find a Python with hidapi. Run: python -m pip install hidapi"
    exit 1
}

if ($Uninstall) {
    Remove-Item $launcher, $oldLink -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -like '*hyperx_battery_logger.py*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Write-Host 'Autostart removed and logger stopped.'
    exit 0
}

$vbs = "CreateObject(""WScript.Shell"").Run """"""$pythonw"""" """"$script"""""", 0, False"
Set-Content -Path $launcher -Value $vbs -Encoding ASCII
Remove-Item $oldLink -ErrorAction SilentlyContinue

$running = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like '*hyperx_battery_logger.py*' }
if (-not $running) { Start-Process $pythonw -ArgumentList "`"$script`"" -WorkingDirectory $here }

Write-Host "Installed: $launcher"
Write-Host "Logging to $(Join-Path $here 'data\battery_log.json')"
Write-Host "Open $(Join-Path $here 'battery_dashboard.html') to see the charts."
