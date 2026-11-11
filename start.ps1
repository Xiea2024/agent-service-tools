$ErrorActionPreference = 'Stop'
$launcher = Join-Path $PSScriptRoot 'scripts/start.py'
if (Get-Command python -ErrorAction SilentlyContinue) {
    & python $launcher @args
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 $launcher @args
} else {
    throw 'Python 3 is required. Install Python, then run this script again.'
}
exit $LASTEXITCODE
