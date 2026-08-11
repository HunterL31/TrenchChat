# Vendor the trenchchat/core subset that identity.py and storage.py need
# into python/app/trenchchat/, so serious_python can package it as part of
# the app bundle. Re-run after pulling upstream changes to those files.

$SpikeDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $SpikeDir "..\..")
$Dest = Join-Path $SpikeDir "python\app\trenchchat"

if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
New-Item -ItemType Directory -Force -Path (Join-Path $Dest "core") | Out-Null

Copy-Item (Join-Path $RepoRoot "trenchchat\__init__.py") (Join-Path $Dest "__init__.py")
Copy-Item (Join-Path $RepoRoot "trenchchat\config.py") (Join-Path $Dest "config.py")
Copy-Item (Join-Path $RepoRoot "trenchchat\core\__init__.py") (Join-Path $Dest "core\__init__.py")
Copy-Item (Join-Path $RepoRoot "trenchchat\core\fileutils.py") (Join-Path $Dest "core\fileutils.py")
Copy-Item (Join-Path $RepoRoot "trenchchat\core\lockbox.py") (Join-Path $Dest "core\lockbox.py")
Copy-Item (Join-Path $RepoRoot "trenchchat\core\permissions.py") (Join-Path $Dest "core\permissions.py")
Copy-Item (Join-Path $RepoRoot "trenchchat\core\identity.py") (Join-Path $Dest "core\identity.py")
Copy-Item (Join-Path $RepoRoot "trenchchat\core\storage.py") (Join-Path $Dest "core\storage.py")

Write-Host "Vendored trenchchat core subset into $Dest"
