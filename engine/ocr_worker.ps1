# OCR worker (shared with altshot): Windows' built-in OCR (Windows.Media.Ocr) behind a tiny stdin/stdout protocol.
# Runs under Windows PowerShell 5.1 (it can reach WinRT with nothing installed). Kept alive by the watcher so the
# engine loads once.
#
# Protocol, repeated:
#   in : one ASCII header line "<width> <height> <byteCount>\n", then exactly byteCount bytes of Gray8 pixels
#        (row-major, one byte per pixel). The image only ever lives in memory: nothing is written to disk.
#   out: one UTF-8 JSON line: {"ok":true,"lines":[[[text,x,y,w,h],...],...]} or {"ok":false,"error":"..."}
# Startup prints {"ready":true,"lang":"..."} once the engine is loaded.
param([string]$Language = 'en-US')
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
Add-Type -AssemblyName System.Web
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]

$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
$asTaskOcr = $asTask.MakeGenericMethod([Windows.Media.Ocr.OcrResult])

function Send([string]$s) { [Console]::Out.WriteLine($s); [Console]::Out.Flush() }
function Esc([string]$s) { [System.Web.HttpUtility]::JavaScriptStringEncode($s) }

try {
    $lang = New-Object Windows.Globalization.Language($Language)
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
    if ($null -eq $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
    if ($null -eq $engine) { Send '{"ready":false,"error":"no OCR language installed"}'; exit 2 }
} catch { Send ('{"ready":false,"error":"' + (Esc $_.Exception.Message) + '"}'); exit 2 }
Send ('{"ready":true,"lang":"' + (Esc $engine.RecognizerLanguage.LanguageTag) + '"}')

$stdin = [Console]::OpenStandardInput()
function ReadExactly([byte[]]$buf, [int]$n) {
    $off = 0
    while ($off -lt $n) {
        $r = $stdin.Read($buf, $off, $n - $off)
        if ($r -le 0) { return $false }
        $off += $r
    }
    return $true
}
function ReadHeader() {
    $sb = New-Object System.Text.StringBuilder
    while ($true) {
        $b = $stdin.ReadByte()
        if ($b -lt 0) { return $null }
        if ($b -eq 10) { return $sb.ToString() }
        [void]$sb.Append([char]$b)
        if ($sb.Length -gt 64) { return '' }
    }
}

while ($true) {
    $hdr = ReadHeader
    if ($null -eq $hdr) { break }
    try {
        $p = $hdr.Trim().Split(' ')
        $w = [int]$p[0]; $h = [int]$p[1]; $n = [int]$p[2]
        if ($w -le 0 -or $h -le 0 -or $n -ne $w * $h) { throw "bad header '$hdr'" }
        $buf = New-Object byte[] $n
        if (-not (ReadExactly $buf $n)) { break }
        $ibuf = [System.Runtime.InteropServices.WindowsRuntime.WindowsRuntimeBufferExtensions]::AsBuffer($buf)
        $bmp = [Windows.Graphics.Imaging.SoftwareBitmap]::CreateCopyFromBuffer($ibuf,
            [Windows.Graphics.Imaging.BitmapPixelFormat]::Gray8, $w, $h)
        $task = $asTaskOcr.Invoke($null, @($engine.RecognizeAsync($bmp)))
        [void]$task.Wait(-1)
        $res = $task.Result
        $bmp.Dispose()
        $buf = $null
        $out = New-Object System.Text.StringBuilder
        [void]$out.Append('{"ok":true,"lines":[')
        $firstLine = $true
        foreach ($line in $res.Lines) {
            if (-not $firstLine) { [void]$out.Append(',') }; $firstLine = $false
            [void]$out.Append('[')
            $firstWord = $true
            foreach ($word in $line.Words) {
                if (-not $firstWord) { [void]$out.Append(',') }; $firstWord = $false
                $r = $word.BoundingRect
                [void]$out.Append('["').Append((Esc $word.Text)).Append('",').Append([int]$r.X).Append(',').Append([int]$r.Y).Append(',').Append([int][Math]::Ceiling($r.Width)).Append(',').Append([int][Math]::Ceiling($r.Height)).Append(']')
            }
            [void]$out.Append(']')
        }
        [void]$out.Append(']}')
        Send $out.ToString()
    } catch {
        Send ('{"ok":false,"error":"' + (Esc $_.Exception.Message) + '"}')
    }
}
