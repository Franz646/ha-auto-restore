# Saves the Home Assistant backup encryption key encrypted for the current Windows user (DPAPI).
# Run it once:  powershell -ExecutionPolicy Bypass -File .\save_backup_key.ps1 [-Path <file>]
# The key is in HA: Settings > System > Backups, card "Encryption key" > Show my encryption key
# (or the emergency kit you downloaded).
param([string]$Path = (Join-Path $env:USERPROFILE '.ha-auto-restore\backup_key.dpapi'))
New-Item -ItemType Directory -Force (Split-Path $Path) | Out-Null
$s = Read-Host -AsSecureString 'Home Assistant backup encryption key'
if ($s.Length -eq 0) { Write-Host 'Nothing saved.'; exit 1 }
$s | ConvertFrom-SecureString | Set-Content -LiteralPath $Path -Encoding ascii
Write-Host "Saved to $Path (readable only by this user on this PC)."
