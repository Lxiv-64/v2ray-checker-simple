# Сборка многоразмерной иконки ico.ico из одного изображения.
#
#     powershell -ExecutionPolicy Bypass -File scripts\make-ico.ps1
#
# Зачем: Windows просит разные размеры значка (16 — список файлов, 32 — панель
# задач, 48 — крупные значки, 256 — плитки). Если в .ico есть только один
# размер, проводник нередко показывает вместо иконки стандартную заглушку.
#
# Скрипт берёт исходное изображение (по умолчанию ico.png или ico.ico), делает
# из него кадры 16/24/32/48/64/128/256 и собирает корректный ICO-контейнер.
# Все кадры сохраняются как DIB (32 бита + маска прозрачности): так иконку
# понимают и GDI+ (System.Drawing), и PyInstaller, и проводник Windows.

param(
    [string]$Source = "",
    [string]$Target = "ico.ico",
    [int[]]$Sizes = @(16, 24, 32, 48, 64, 128, 256)
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing

if (-not $Source) {
    foreach ($candidate in @("ico.png", "ico.ico")) {
        if (Test-Path $candidate) { $Source = $candidate; break }
    }
}
if (-not $Source -or -not (Test-Path $Source)) {
    throw "Не найдено исходное изображение (ожидался ico.png или ico.ico)"
}
Write-Host "Исходник: $Source"

# Загружаем источник максимального размера, чтобы не терять качество.
if ([System.IO.Path]::GetExtension($Source) -ieq ".ico") {
    $sourceBitmap = (New-Object System.Drawing.Icon((Resolve-Path $Source))).ToBitmap()
} else {
    $sourceBitmap = [System.Drawing.Image]::FromFile((Resolve-Path $Source))
}

function ConvertTo-IcoDib {
    param([System.Drawing.Bitmap]$Bitmap)
    $size = $Bitmap.Width
    $stride = $size * 4
    $andStride = [int]([math]::Floor(($size + 31) / 32) * 4)
    $andSize = $andStride * $size
    $stream = New-Object System.IO.MemoryStream
    $writer = New-Object System.IO.BinaryWriter($stream)
    # BITMAPINFOHEADER: высота удвоена (пиксели + маска прозрачности)
    $writer.Write([int]40); $writer.Write([int]$size); $writer.Write([int]($size * 2))
    $writer.Write([int16]1); $writer.Write([int16]32); $writer.Write([int]0)
    $writer.Write([int]($stride * $size)); $writer.Write([int]0); $writer.Write([int]0)
    $writer.Write([int]0); $writer.Write([int]0)
    # Пиксели BGRA снизу вверх
    for ($y = $size - 1; $y -ge 0; $y--) {
        for ($x = 0; $x -lt $size; $x++) {
            $pixel = $Bitmap.GetPixel($x, $y)
            $writer.Write([byte]$pixel.B); $writer.Write([byte]$pixel.G)
            $writer.Write([byte]$pixel.R); $writer.Write([byte]$pixel.A)
        }
    }
    # AND-маска: нули означают «непрозрачно» (прозрачность задаёт альфа-канал)
    $writer.Write((New-Object byte[] $andSize))
    $writer.Flush()
    $bytes = $stream.ToArray()
    $writer.Dispose(); $stream.Dispose()
    return , $bytes
}

$frames = @()
foreach ($size in $Sizes) {
    $bitmap = New-Object System.Drawing.Bitmap($size, $size,
        [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
    $graphics.DrawImage($sourceBitmap, 0, 0, $size, $size)
    $graphics.Dispose()
    $frames += , @{ Size = $size; Data = (ConvertTo-IcoDib -Bitmap $bitmap) }
    $bitmap.Dispose()
    Write-Host ("  кадр {0}x{0}: {1} байт" -f $size, $frames[-1].Data.Length)
}
$sourceBitmap.Dispose()

# ICONDIR + ICONDIRENTRY + данные кадров
$result = New-Object System.IO.MemoryStream
$writer = New-Object System.IO.BinaryWriter($result)
$writer.Write([int16]0); $writer.Write([int16]1); $writer.Write([int16]$frames.Count)
$offset = 6 + 16 * $frames.Count
foreach ($frame in $frames) {
    $dimension = if ($frame.Size -ge 256) { 0 } else { $frame.Size }
    $writer.Write([byte]$dimension); $writer.Write([byte]$dimension)
    $writer.Write([byte]0); $writer.Write([byte]0)
    $writer.Write([int16]1); $writer.Write([int16]32)
    $writer.Write([int]$frame.Data.Length); $writer.Write([int]$offset)
    $offset += $frame.Data.Length
}
foreach ($frame in $frames) { $writer.Write($frame.Data) }
$writer.Flush()

$targetPath = [System.IO.Path]::GetFullPath($Target)
[System.IO.File]::WriteAllBytes($targetPath, $result.ToArray())
$writer.Dispose(); $result.Dispose()
Write-Host ("Готово: {0} ({1} кадров, {2} байт)" -f $targetPath, $frames.Count,
            (Get-Item $targetPath).Length)
