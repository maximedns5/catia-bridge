# CATIA bridge: installs the link to the app on this Windows PC, for this user.
#
# The app serves this script with its address and a pairing code filled in,
# and shows the user one line to paste in PowerShell:
#     irm "https://APP/catia-link/install.ps1?code=XXXXXXXX" | iex
#
# It needs no administrator rights. It:
#   1. finds Python 3.10 or later (or installs Python 3.12 for this user);
#   2. makes a private environment in %LOCALAPPDATA%\catia-bridge\venv and
#      installs the bridge there;
#   3. pairs this PC with the app (the token stays in %APPDATA%\catia-bridge);
#   4. starts the link now and at every login, with no window.
# Running it again upgrades the bridge and pairs again.

& {
# Native commands are checked by exit code (pip's warnings on stderr are not errors).
$ErrorActionPreference = 'Continue'
$Server = '__SERVER__'
$Code = '__CODE__'
$Package = '__PACKAGE__'
$PythonVersion = '3.12.7'
$Root = Join-Path $env:LOCALAPPDATA 'catia-bridge'
$Venv = Join-Path $Root 'venv'
$VenvPython = Join-Path $Venv 'Scripts\python.exe'

function Say([string]$Message) { Write-Host "[catia-bridge] $Message" }
function Stop-Install([string]$Message) {
    Write-Host "[catia-bridge] $Message" -ForegroundColor Red
    throw "CATIA bridge installation stopped: $Message"
}
function Invoke-Checked([string]$What, [scriptblock]$Command) {
    & $Command
    if ($LASTEXITCODE -ne 0) { Stop-Install "$What failed (exit code $LASTEXITCODE)." }
}

if ($Code -notmatch '^[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{8}$') {
    Stop-Install 'This installer has no valid pairing code. Ask the app for a new install command.'
}
if ($Server -notmatch '^https://[^\s''"]+$' -and $Server -notmatch '^http://(127\.0\.0\.1|localhost)(:\d+)?(/[^\s''"]*)?$') {
    Stop-Install "This installer has no valid server address ($Server)."
}

# 1. Python
function Find-Python {
    $candidates = @(
        @{ Exe = 'py'; Args = @('-3') },
        @{ Exe = 'python'; Args = @() },
        @{ Exe = (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'); Args = @() }
    )
    foreach ($c in $candidates) {
        # The Microsoft Store's "python" alias answers with an error code: skipped below.
        if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
        $exe = $c.Exe
        $pre = $c.Args
        $out = & $exe @pre -c 'import sys; print(sys.version_info >= (3, 10) and sys.maxsize > 2**32)' 2>$null
        if ($LASTEXITCODE -eq 0 -and "$out".Trim() -eq 'True') { return $c }
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Say "Python 3.10 or later was not found: installing Python $PythonVersion for this user..."
    $installer = Join-Path $env:TEMP "python-$PythonVersion-amd64.exe"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing -ErrorAction Stop -Uri "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe" -OutFile $installer
    $process = Start-Process -FilePath $installer -Wait -PassThru -ErrorAction Stop -ArgumentList @(
        '/quiet', 'InstallAllUsers=0', 'PrependPath=0', 'Include_launcher=0',
        'Include_test=0', 'Include_doc=0', 'Shortcuts=0')
    Remove-Item $installer -ErrorAction SilentlyContinue
    if ($process.ExitCode -ne 0) { Stop-Install "The Python installer failed (exit code $($process.ExitCode))." }
    $python = Find-Python
    if (-not $python) { Stop-Install 'Python was installed but cannot be found. Open a new PowerShell and try again.' }
}
$pythonExe = $python.Exe
$pythonArgs = $python.Args
Say "Python: $(& $pythonExe @pythonArgs -c 'import sys; print(sys.executable)')"

# 2. The bridge, in its own environment
if (Test-Path $VenvPython) {
    # An older link may be running from this environment: stop it before upgrading.
    & $VenvPython -m catia_mcp.link stop 2>$null | Out-Null
} else {
    Say "Creating the bridge's environment in $Venv..."
    New-Item -ItemType Directory -Force -Path $Root -ErrorAction Stop | Out-Null
    Invoke-Checked 'Creating the environment' { & $pythonExe @pythonArgs -m venv $Venv }
}
Say 'Installing the bridge (a minute or two)...'
Invoke-Checked 'Updating pip' { & $VenvPython -m pip install --quiet --disable-pip-version-check --upgrade pip }
Invoke-Checked 'Installing the bridge' { & $VenvPython -m pip install --quiet --disable-pip-version-check --upgrade $Package }

# 3. Pairing
Invoke-Checked 'Pairing with the app' { & $VenvPython -m catia_mcp.link pair --server $Server --code $Code }

# 4. Start now and at every login
Invoke-Checked 'Registering the start at login' { & $VenvPython -m catia_mcp.link autostart on }
Invoke-Checked 'Starting the link' { & $VenvPython -m catia_mcp.link start }

Say 'Done. The link runs in the background and starts with Windows.'
Say 'Open CATIA (on 3DEXPERIENCE, log in) and send a model from the app.'
Say "Status: & '$VenvPython' -m catia_mcp.link status"
}
