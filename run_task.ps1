# 排程器啟動腳本：run_task.ps1 -Mode preview|final
# 由 Windows 工作排程器呼叫，輸出記錄到 screener_task.log
param([string]$Mode = "final")

$base = Split-Path -Parent $MyInvocation.MyCommand.Path
$py   = Join-Path $base "daily_screener.py"
$log  = Join-Path $base "screener_task.log"

$env:PYTHONIOENCODING = "utf-8"
Add-Content -Path $log -Value "`n=== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') --$Mode ===" -Encoding utf8
cmd /c "chcp 65001 >nul & python `"$py`" --$Mode >> `"$log`" 2>&1"
