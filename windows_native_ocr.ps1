param([Parameter(Mandatory=$true)][string]$ImagePath)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
function Write-AsciiJson([object]$value) {
  # ConvertTo-Json + PowerShell 5.1 stdout can use the legacy system page.
  # Emit only ASCII so Python can always parse UTF-8 JSON without loss.
  $serialized=ConvertTo-Json -InputObject $value -Compress -Depth 3
  $builder=New-Object System.Text.StringBuilder
  foreach($ch in $serialized.ToCharArray()) {
    if([int]$ch -gt 127) {
      [void]$builder.Append('\u')
      [void]$builder.Append(([int]$ch).ToString('x4'))
    } else {
      [void]$builder.Append($ch)
    }
  }
  [Console]::Out.WriteLine($builder.ToString())
}
[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)
try {
  Add-Type -AssemblyName System.Runtime.WindowsRuntime
  $asTask=([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq "AsTask" -and $_.IsGenericMethodDefinition -and
    $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name.Contains("IAsyncOperation")
  } | Select-Object -First 1)
  if ($null -eq $asTask) {throw "Windows Runtime AsTask is not accessible"}
  function Await-Op($op,[Type]$type) {
    $t=$asTask.MakeGenericMethod($type).Invoke($null,@($op))
    $t.GetAwaiter().GetResult()
  }
  $FileType=[Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
  $StreamType=[Windows.Storage.Streams.IRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime]
  $DecoderType=[Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
  $BitmapType=[Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
  $LanguageType=[Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime]
  $EngineType=[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
  $ResultType=[Windows.Media.Ocr.OcrResult,Windows.Foundation,ContentType=WindowsRuntime]

  $resolved=[IO.Path]::GetFullPath($ImagePath)
  if (-not [IO.File]::Exists($resolved)) {throw "Image missing"}
  $file=Await-Op $FileType::GetFileFromPathAsync($resolved) $FileType
  $stream=Await-Op $file.OpenAsync([Windows.Storage.FileAccessMode]::Read) $StreamType
  try {
    $decoder=Await-Op $DecoderType::CreateAsync($stream) $DecoderType
    $bitmap=Await-Op $decoder.GetSoftwareBitmapAsync() $BitmapType
    try {
      if ($bitmap.PixelWidth -gt $EngineType::MaxImageDimension -or
          $bitmap.PixelHeight -gt $EngineType::MaxImageDimension) {
        throw "Image exceeds native Windows OCR limits"
      }
      $preferredLang="ru"
      $language=[Windows.Globalization.Language]::new($preferredLang)
      $engine=$EngineType::TryCreateFromLanguage($language)
      if ($null -eq $engine) {
        $preferredLang="en-US"
        $language=[Windows.Globalization.Language]::new($preferredLang)
        $engine=$EngineType::TryCreateFromLanguage($language)
      }
      if ($null -eq $engine) {throw "Windows OCR requires Russian or English language recognition feature"}
      $result=Await-Op $engine.RecognizeAsync($bitmap) $ResultType
      $chosen=$result.Text
      $lines=@($result.Lines).Count
      # Fallback to English recognition for Latin-only technical documents.
      $cyr=([regex]::Matches($chosen,'[А-Яа-яЁё]')).Count
      if ($cyr -lt 5 -and $preferredLang -eq "ru") {
        $english=$EngineType::TryCreateFromLanguage([Windows.Globalization.Language]::new("en-US"))
        if ($null -ne $english) {
          $alternate=Await-Op $english.RecognizeAsync($bitmap) $ResultType
          if ($alternate.Text.Length -ge [Math]::Max(14,$chosen.Length*0.65)) {
            $chosen=$alternate.Text
            $lines=@($alternate.Lines).Count
            $preferredLang="en-US"
          }
        }
      }
      Write-AsciiJson ([pscustomobject]@{ok=$true;text=$chosen;lines=$lines;language=$preferredLang;engine="Windows.Media.Ocr"})
    } finally {if ($null -ne $bitmap) {$bitmap.Dispose()}}
  } finally {if ($null -ne $stream) {$stream.Dispose()}}
} catch {
  Write-AsciiJson ([pscustomobject]@{ok=$false;error=$_.Exception.Message})
  exit 1
}
