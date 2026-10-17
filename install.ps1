# Install nimcode from its latest GitHub Release (Windows x64).
#   irm https://raw.githubusercontent.com/PrathamPai-2007/nimcode/main/install.ps1 | iex
# $env:NIMCODE_VERSION = "v0.7.0rc1" installs a specific tag (incl. pre-releases).
$ErrorActionPreference = "Stop"
$repo = "PrathamPai-2007/nimcode"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    irm https://astral.sh/uv/install.ps1 | iex
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

$api = if ($env:NIMCODE_VERSION) {
    "https://api.github.com/repos/$repo/releases/tags/$($env:NIMCODE_VERSION)"
} else {
    "https://api.github.com/repos/$repo/releases/latest"
}
$asset = (Invoke-RestMethod $api).assets | Where-Object { $_.name -like "*win_amd64.whl" } | Select-Object -First 1
if (-not $asset) { throw "No Windows wheel found in release ($api)." }

uv tool install --force --python 3.12 $asset.browser_download_url
Write-Host ""
Write-Host "Installed. Set NVIDIA_API_KEY (or put it in ~/.nimcode/.env), then run: nimcode"
