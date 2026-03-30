param(
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$BackendDir = Join-Path $RepoRoot 'backend'
$LogPath = Join-Path $PSScriptRoot 'startup.log'
$BackendOutLogPath = Join-Path $PSScriptRoot 'backend.out.log'
$BackendErrLogPath = Join-Path $PSScriptRoot 'backend.err.log'

function Write-StartupLog {
    param([string]$Message)
    $line = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '  ' + $Message
    Add-Content -Path $LogPath -Value $line -Encoding ASCII
    if (-not $Quiet) { Write-Host $Message }
}

function Test-PortListening {
    param([int]$Port)
    try {
        Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Resolve-PythonExe {
    $candidates = @()
    try {
        $cmd = Get-Command python -ErrorAction Stop
        if ($cmd -and $cmd.Source) { $candidates += $cmd.Source }
    } catch {}
    $candidates += @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python310\python.exe'),
        'C:\Python313\python.exe',
        'C:\Python312\python.exe',
        'C:\Python311\python.exe',
        'C:\Python310\python.exe'
    )
    foreach ($candidate in $candidates | Select-Object -Unique) {
        if ($candidate -and (Test-Path $candidate)) { return $candidate }
    }
    return $null
}

try {
    Write-StartupLog 'Starting background startup check...'
    if (-not (Test-PortListening -Port 8000)) {
        $pythonExe = Resolve-PythonExe
        if (-not $pythonExe) {
            Write-StartupLog 'ERROR: python.exe not found in task context.'
            throw 'python.exe not found'
        }
        Write-StartupLog ('Using Python: ' + $pythonExe)
        if (Test-Path $BackendOutLogPath) { Remove-Item $BackendOutLogPath -Force -ErrorAction SilentlyContinue }
        if (Test-Path $BackendErrLogPath) { Remove-Item $BackendErrLogPath -Force -ErrorAction SilentlyContinue }

        $ffmpegBin = $null
        $wingetPkgs = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
        if (Test-Path $wingetPkgs) {
            $ffmpegExe = Get-ChildItem $wingetPkgs -Recurse -Filter 'ffmpeg.exe' -ErrorAction SilentlyContinue |
                Select-Object -First 1 -ExpandProperty FullName
            if ($ffmpegExe) { $ffmpegBin = Split-Path $ffmpegExe -Parent }
        }
        if ($ffmpegBin) {
            $env:PATH = $ffmpegBin + ';' + $env:PATH
            Write-StartupLog ('ffmpeg found at: ' + $ffmpegBin)
        } else {
            Write-StartupLog 'ffmpeg not found - clip trimming will fall back to full video'
        }

        $env:PYTHONUNBUFFERED = '1'
        Start-Process -FilePath $pythonExe `
            -WorkingDirectory $BackendDir `
            -ArgumentList @('-u', '-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', '8000') `
            -WindowStyle Hidden `
            -RedirectStandardOutput $BackendOutLogPath `
            -RedirectStandardError $BackendErrLogPath

        $started = $false
        for ($i = 0; $i -lt 20; $i++) {
            Start-Sleep -Milliseconds 500
            if (Test-PortListening -Port 8000) {
                $started = $true
                break
            }
        }
        if ($started) {
            Write-StartupLog 'Backend started on http://127.0.0.1:8000'
        } else {
            Write-StartupLog 'ERROR: Backend did not open port 8000 after waiting.'
            if (Test-Path $BackendErrLogPath) {
                $errLines = Get-Content $BackendErrLogPath -Tail 20 -ErrorAction SilentlyContinue
                $errTail = $errLines -join ' / '
                if ($errTail) { Write-StartupLog ('Backend stderr: ' + $errTail) }
            }
            if (Test-Path $BackendOutLogPath) {
                $outLines = Get-Content $BackendOutLogPath -Tail 20 -ErrorAction SilentlyContinue
                $outTail = $outLines -join ' / '
                if ($outTail) { Write-StartupLog ('Backend stdout: ' + $outTail) }
            }
        }
    } else {
        Write-StartupLog 'Backend already running on port 8000'
    }
    Write-StartupLog 'Single app URL: http://127.0.0.1:8000'
} catch {
    $msg = $_.Exception.Message
    Write-StartupLog ('Startup failure: ' + $msg)
    throw
}
