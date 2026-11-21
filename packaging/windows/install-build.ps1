# Puts a fresh build into the program folder (dist\VideoMerger\) without touching anything else in it: ffmpeg.exe,
# ffprobe.exe, asfbin.exe, a "bin" folder, videos and whatever else was put there stay as they are. Only what the
# build makes is replaced: VideoMerger.exe, _internal, LICENSE.txt and README.txt (and anything else the new build has
# at its top level). build.ps1 runs it; it can also be run on its own:
#
#   powershell -ExecutionPolicy Bypass -File packaging\windows\install-build.ps1 -From <build folder> -To <program folder>
param(
    [Parameter(Mandatory = $true)][string]$From,
    [Parameter(Mandatory = $true)][string]$To
)
$ErrorActionPreference = "Stop"

# What builds make (removed even if this build no longer makes one of them), and what is never the build's.
$built = @("VideoMerger.exe", "_internal", "LICENSE.txt", "THIRD-PARTY-NOTICES.txt", "README.txt")
$never = @("ffmpeg.exe", "ffprobe.exe", "ffplay.exe", "asfbin.exe", "bin")

$new = @(Get-ChildItem -LiteralPath $From -Force | ForEach-Object { $_.Name })
if (-not $new) { throw "The build in $From is empty." }
$theirs = @($new | Where-Object { $_ -in $never })
if ($theirs) { throw "The build in $From contains $($theirs -join ', '): never the build's, nothing replaced." }

New-Item -ItemType Directory -Force -Path $To | Out-Null
foreach ($name in @($built + $new | Sort-Object -Unique)) {
    $path = Join-Path $To $name
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
}
foreach ($name in $new) {
    Copy-Item -LiteralPath (Join-Path $From $name) -Destination $To -Recurse -Force
}
Write-Host "Program folder updated: $To (only the build's own files were replaced)"
