# Build "Metadata Viewer.exe" on Windows.
#
# Must run ON Windows: PyInstaller does not cross-compile, so a .exe cannot be
# produced from macOS or Linux. Requires Python 3.10+ and PowerShell.
#
#   .\build_windows.ps1                 # downloads exiftool and bundles it
#   .\build_windows.ps1 -SkipExifTool   # build without it (uses PATH at runtime)

param([switch]$SkipExifTool)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "==> afhankelijkheden"
python -m pip install --quiet --upgrade pyinstaller pillow mutagen

if (-not $SkipExifTool) {
    Write-Host "==> exiftool ophalen"
    # exiftool.org only ever hosts the current version, so read the version off
    # the page instead of pinning one. The Windows build lives on SourceForge.
    $page = Invoke-WebRequest -Uri "https://exiftool.org/" -UseBasicParsing
    if ($page.Content -notmatch 'exiftool-([\d.]+)_64\.zip') {
        throw "Kon de actuele exiftool-versie niet vinden op exiftool.org"
    }
    $version = $Matches[1]
    Write-Host "    versie $version"
    $url = "https://sourceforge.net/projects/exiftool/files/exiftool-${version}_64.zip/download"
    $zip = Join-Path $env:TEMP "exiftool.zip"
    $work = Join-Path $env:TEMP "exiftool-extract"

    # SourceForge redirects to a mirror and is flaky in CI. Use curl.exe (ships
    # with Windows 10+) with retries instead of Invoke-WebRequest, which throws
    # an opaque "OperationStopped" on transient failures.
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        & curl.exe -L --fail --retry 5 --retry-delay 3 --retry-all-errors `
            -o $zip $url
        if ($LASTEXITCODE -ne 0) {
            throw "curl.exe download van exiftool mislukt (exit $LASTEXITCODE)"
        }
    } else {
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    }
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
    Expand-Archive -Path $zip -DestinationPath $work

    New-Item -ItemType Directory -Force -Path tools | Out-Null
    # The archive ships it as "exiftool(-k).exe"; the (-k) makes it pause for a
    # keypress, which would hang our subprocess calls. Renaming disables that.
    $found = Get-ChildItem -Recurse -Path $work -Filter "exiftool*.exe" | Select-Object -First 1
    Copy-Item $found.FullName "tools\exiftool.exe" -Force
    $libDir = Join-Path $found.DirectoryName "exiftool_files"
    if (Test-Path $libDir) { Copy-Item -Recurse -Force $libDir "tools\exiftool_files" }
    Write-Host "    exiftool $($found.Name) meeverpakt"
}

Write-Host "==> app bouwen"
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue

$args = @(
    "--noconfirm", "--clean", "--windowed", "--onefile",
    "--name", "Metadata Viewer",
    "--icon", "icon.ico",
    "--hidden-import", "mutagen",
    "--hidden-import", "PIL._tkinter_finder"
)
if (Test-Path "tools") { $args += @("--add-data", "tools;tools") }
$args += "metadata_viewer.py"

python -m PyInstaller @args

# Ensure deep_inspection.py lands next to the unpacked executable.
$exeDir = Join-Path $PSScriptRoot "dist"
if (Test-Path $exeDir) {
    Copy-Item deep_inspection.py $exeDir -Force
}

Write-Host ""
Write-Host "Klaar: dist\Metadata Viewer.exe"
