<#
.SYNOPSIS
  OCR one PNG with the Windows built-in engine (Windows.Media.Ocr) and print one line per recognised text line.

.DESCRIPTION
  Development tool used by tools/build_ocr_cache.py to create ghostaudit/data/screenshot_ocr.json. Nothing is
  installed and nothing leaves the machine: the engine ships with Windows. Output format, one line each:
      <x>|<y>|<text>
  where x,y are the top-left pixel of the line's first word. Run: powershell -File ocr_windows.ps1 -Path img.png
#>
param([Parameter(Mandatory = $true)][string]$Path, [string]$Lang = 'en-US')
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $type) {
    $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    $t.Wait(-1) | Out-Null
    $t.Result
}
$full = (Resolve-Path $Path).Path
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($full)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bmp = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new($Lang))
if ($null -eq $engine) { throw "OCR language $Lang is not available" }
$res = Await ($engine.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])
foreach ($line in $res.Lines) {
    $ws = $line.Words
    $x = ($ws | ForEach-Object { $_.BoundingRect.X } | Measure-Object -Minimum).Minimum
    $y = ($ws | ForEach-Object { $_.BoundingRect.Y } | Measure-Object -Minimum).Minimum
    '{0:0}|{1:0}|{2}' -f $x, $y, $line.Text
}
