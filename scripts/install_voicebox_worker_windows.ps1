param(
    [string]$HubUrl = "https://voicebox.complexelite.com",
    [string]$Name = "Threadripper RTX 2060 Super",
    [int]$LocalPort = 17493
)

$ErrorActionPreference = "Stop"
$base = Join-Path $env:LOCALAPPDATA "Complex Elite\Voicebox Remote Worker"
$configDir = Join-Path $env:USERPROFILE ".config\voicebox"
$configPath = Join-Path $configDir "remote-worker.json"
$agentPath = Join-Path $base "voicebox_remote_worker.py"
$venvPython = Join-Path $base "venv\Scripts\python.exe"
$runnerPath = Join-Path $base "run-worker.cmd"
$logPath = Join-Path $base "worker.log"
$taskName = "Complex Elite Voicebox GPU Worker"

function Set-UserOnlyAcl([string]$Path, [bool]$Directory) {
    $acl = New-Object System.Security.AccessControl.DirectorySecurity
    if (-not $Directory) { $acl = New-Object System.Security.AccessControl.FileSecurity }
    $acl.SetAccessRuleProtection($true, $false)
    $currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $inheritance = if ($Directory) { [System.Security.AccessControl.InheritanceFlags]"ContainerInherit, ObjectInherit" } else { [System.Security.AccessControl.InheritanceFlags]::None }
    $propagation = [System.Security.AccessControl.PropagationFlags]::None
    $allow = [System.Security.AccessControl.AccessControlType]::Allow
    foreach ($sid in @($currentSid, [System.Security.Principal.SecurityIdentifier]::new("S-1-5-18"))) {
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new($sid, [System.Security.AccessControl.FileSystemRights]::FullControl, $inheritance, $propagation, $allow)
        [void]$acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Find-Python {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($version in @("3.14", "3.13", "3.12")) {
            & $launcher.Source "-$version" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,12) else 1)" *> $null
            if ($LASTEXITCODE -eq 0) {
                return ((& $launcher.Source "-$version" -c "import sys; print(sys.executable)").Trim())
            }
        }
    }
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        & $pythonCommand.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3,12) else 1)" *> $null
        if ($LASTEXITCODE -eq 0) { return $pythonCommand.Source }
    }
    return $null
}

New-Item -ItemType Directory -Path $base, $configDir -Force | Out-Null
Set-UserOnlyAcl $base $true
Set-UserOnlyAcl $configDir $true

$healthUrl = "http://127.0.0.1:$LocalPort/health"
try {
    $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 8
} catch {
    throw "Voicebox is not responding at $healthUrl. Install/open Voicebox for Windows, wait for its local server to start, then run this installer again. The worker API must remain bound to 127.0.0.1."
}
if ($health.status -ne "healthy") { throw "The local Voicebox server did not report healthy status." }
Write-Host "Local Voicebox: healthy; GPU=$($health.gpu_type); backend=$($health.backend_type)"
if (-not $health.gpu_available) { Write-Warning "Voicebox does not currently report a usable GPU. Install/enable the CUDA backend in Voicebox before expecting RTX acceleration." }

$python = Find-Python
if (-not $python) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) { throw "Python 3.12+ with pip is required. Install Python 3.12 or newer, then rerun this installer." }
    Write-Host "Installing per-user Python 3.12..."
    & $winget.Source install --exact --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "Python installation failed. Install Python 3.12 from python.org and rerun." }
    $env:Path = "$env:LOCALAPPDATA\Programs\Python\Python312;$env:LOCALAPPDATA\Programs\Python\Python312\Scripts;$env:LOCALAPPDATA\Programs\Python\Launcher;$env:Path"
    $python = Find-Python
    if (-not $python) { throw "Python installed, but the Python launcher could not find it. Open a new PowerShell window and rerun." }
}

if (-not (Test-Path $venvPython)) {
    & $python -m venv (Join-Path $base "venv")
    if ($LASTEXITCODE -ne 0) { throw "Could not create the worker Python environment." }
}
& $venvPython -m pip install --disable-pip-version-check --upgrade httpx
if ($LASTEXITCODE -ne 0) { throw "Could not install the worker's HTTP client." }

$agentUri = "https://raw.githubusercontent.com/complexelite/voicebox/main/scripts/voicebox_remote_worker.py"
Invoke-WebRequest -Uri $agentUri -OutFile $agentPath

if (Test-Path $configPath) {
    $existing = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    if ($existing.hub_url -ne $HubUrl.TrimEnd('/')) { throw "An existing worker credential is registered to a different Voicebox hub. Revoke it before enrolling this PC to another hub." }
    Write-Host "Reusing this PC's existing scoped worker credential."
} else {
    $secureKey = Read-Host "Voicebox admin API key (used only to enroll this PC; it will not be saved)" -AsSecureString
    $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    try { $apiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer) }
    if ($apiKey.Length -lt 32) { throw "The API key is invalid or too short." }
    try {
        $enrollment = Invoke-RestMethod -Method Post -Uri ($HubUrl.TrimEnd('/') + "/workers/enroll") `
            -Headers @{ Authorization = "Bearer $apiKey" } -ContentType "application/json" `
            -Body (@{ name = $Name } | ConvertTo-Json -Compress) -TimeoutSec 30
    } catch { throw "Worker enrollment failed. Check the Voicebox admin API key and hub URL; the key was not saved." }
    finally { $apiKey = $null; $secureKey.Dispose() }

    $credential = @{
        hub_url = $HubUrl.TrimEnd('/'); name = $Name; worker_id = $enrollment.id; token = $enrollment.token
    } | ConvertTo-Json
    Set-Content -LiteralPath $configPath -Value $credential -Encoding UTF8
    Set-UserOnlyAcl $configPath $false
    Write-Host "This PC enrolled with a device-scoped credential. The credential file is restricted to your Windows user and SYSTEM."
}

$quotedPython = '"' + $venvPython + '"'
$quotedAgent = '"' + $agentPath + '"'
$quotedConfig = '"' + $configPath + '"'
$quotedState = '"' + (Join-Path $base "state") + '"'
$quotedLog = '"' + $logPath + '"'
$runner = @"
@echo off
$quotedPython $quotedAgent --credential-file $quotedConfig --local-api http://127.0.0.1:$LocalPort --state-dir $quotedState >> $quotedLog 2>&1
"@
Set-Content -LiteralPath $runnerPath -Value $runner -Encoding ASCII

$action = New-ScheduledTaskAction -Execute $env:ComSpec -Argument "/d /c `"`"$runnerPath`"`""
$taskUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $taskUser
$principal = New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "Outbound-only Voicebox inference worker for this PC's local GPU." -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host "Installed and started '$taskName'. The worker connects outbound to $HubUrl; the local Voicebox API stays on 127.0.0.1:$LocalPort."
Write-Host "Worker log: $logPath"
Write-Host "Download the desired TTS models in local Voicebox. Only downloaded models are advertised to the hub."
