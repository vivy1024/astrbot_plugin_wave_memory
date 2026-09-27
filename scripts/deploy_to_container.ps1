<#
把 WaveMemory 的一个已提交版本部署进 AstrBot 容器，并确认重启后跑的就是这个提交。

    [1] 本地单测（-SkipTests 跳过）
    [2] 卷外数据库快照：调用 openclaw\scripts\backup_wavememory.ps1（-SkipBackup 跳过）
    [3] 备份容器里现在的插件代码到宿主机 AstrBot-master\data\backups\wavememory_code（保留 5 份）
    [4] git archive 指定提交 → 容器内用 scripts/_deploy_apply.py 替换插件代码：
          只动代码文件（旧 .git、node_modules、data/、logs/、*.db、*.tar 不碰）；
          上次部署清单里有、这次没有的文件删除；清单外的未知文件保留并列出（-PruneUnknown 删除其中的
          .py 与旧前端产物，首次部署没有清单时用它清掉 v5 遗留模块；删掉的都在 [3] 的代码备份里）；
          清掉 __pycache__，写 _deploy_manifest.txt 与 _deploy_version.json（/api/health 与启动日志显示提交号）
    [5] docker restart（-NoRestart 跳过），等待启动日志出现本次提交号

只部署已提交的内容，工作区未提交的改动不会带上。重启会让 QQ Bot 短暂离线。
代码回滚不会回滚数据库结构：新版本启动时做过的迁移仍在，必要时用 [2] 的快照恢复数据。

用法（在 openclaw 工作区）：
    pwsh -File astrbot_plugin_wave_memory\scripts\deploy_to_container.ps1 [-Ref v6] [-DryRun]
    pwsh -File astrbot_plugin_wave_memory\scripts\deploy_to_container.ps1 -Rollback   # 恢复最近一份代码备份并重启
#>
param(
    [string]$Ref = 'HEAD',
    [string]$Container = 'astrbot',
    [switch]$SkipTests,
    [switch]$SkipBackup,
    [switch]$NoRestart,
    [switch]$DryRun,
    [switch]$Rollback,
    [switch]$PruneUnknown,
    [ValidateRange(1, 20)][int]$KeepCodeBackups = 5,
    [ValidateRange(30, 900)][int]$StartTimeoutSeconds = 240,
    [string]$BackupScript = (Join-Path $PSScriptRoot '..\..\scripts\backup_wavememory.ps1')
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pluginsRoot = '/AstrBot/data/plugins'
$pluginName = 'astrbot_plugin_wave_memory'
$pluginDir = "$pluginsRoot/$pluginName"
$codeBackupDir = '/AstrBot/data/backups_host/wavememory_code'   # 宿主机 AstrBot-master/data/backups/wavememory_code
$ts = Get-Date -Format 'yyyyMMdd_HHmmss'
# 代码备份不带旧 git 检出、依赖目录与历史归档（它们不归部署管，也不会被替换）
$tarExcludes = "--exclude=__pycache__ --exclude=$pluginName/.git --exclude=$pluginName/node_modules --exclude='$pluginName/*.tar'"

function Invoke-Checked([string]$what, [scriptblock]$block) {
    & $block
    if ($LASTEXITCODE -ne 0) { throw "$what 失败（退出码 $LASTEXITCODE）" }
}

function Assert-ContainerRunning {
    $state = docker inspect $Container --format '{{.State.Status}}' 2>$null
    if ($LASTEXITCODE -ne 0) { throw "找不到容器 $Container（Docker Desktop 没开？）" }
    if ($state -ne 'running') { throw "$Container 容器状态是 $state，不是 running" }
}

# 把归档套到插件目录（部署与回滚共用 scripts/_deploy_apply.py，只动代码文件）
function Invoke-Apply([string]$archive, [string]$stamp) {
    $remoteApply = "/tmp/wm_apply_$ts.py"
    Invoke-Checked 'docker cp 套用脚本' { docker cp (Join-Path $PSScriptRoot '_deploy_apply.py') "${Container}:$remoteApply" | Out-Null }
    $applyArgs = @($remoteApply, $pluginDir, $archive)
    if ($stamp) { $applyArgs += @('--stamp', $stamp) }
    if ($PruneUnknown) { $applyArgs += '--prune-unknown' }
    try {
        $out = docker exec $Container python3 @applyArgs 2>&1
        if ($LASTEXITCODE -ne 0) { throw "容器内套用代码失败：$($out | Out-String)" }
    }
    finally {
        docker exec $Container rm -f $remoteApply $archive $(if ($stamp) { $stamp }) | Out-Null
    }
    return ($out | Select-Object -Last 1 | ConvertFrom-Json)
}

# 读容器最近的日志（去掉颜色码）。不用 docker logs --since：Docker Desktop 上它对本容器始终返回空。
function Get-RecentLog {
    return (docker logs $Container --tail 3000 2>&1 | Out-String) -replace "\x1b\[[0-9;]*m", ''
}

function Get-InitLine([string]$log) {
    return ($log -split "`n" | Where-Object { $_ -match '\[WaveMemory\] Fully initialized' } | Select-Object -Last 1)
}

function Restart-AndWait([string]$expectCommit) {
    # 重启前的最后一行日志作为分界：只看它之后的日志，旧进程的报错与启动行不算数。
    # （不能用上一条启动完成行：上一次启动失败时它指向更早的成功启动，中间那次失败会被误算进来）
    $marker = ((Get-RecentLog) -split "`n" | Where-Object { $_.Trim() } | Select-Object -Last 1)
    Write-Host "  docker restart $Container（QQ Bot 会短暂离线）"
    Invoke-Checked 'docker restart' { docker restart $Container | Out-Null }
    $deadline = (Get-Date).AddSeconds($StartTimeoutSeconds)
    $short = if ($expectCommit) { $expectCommit.Substring(0, 10) } else { '' }
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 5
        $log = Get-RecentLog
        if ($marker) {
            $at = $log.LastIndexOf($marker)
            if ($at -ge 0) { $log = $log.Substring($at + $marker.Length) }
        }
        $line = Get-InitLine $log
        if ($line) {
            if ($short -and $line -notmatch [regex]::Escape($short)) { throw "插件启动了，但日志里的提交不是 ${short}: $line" }
            Write-Host "  $($line.Trim())"
            return
        }
        $failed = ($log -split "`n" | Where-Object { $_ -match 'wave_memory.*(Traceback|加载失败|failed to load)|插件 astrbot_plugin_wave_memory.*失败' } | Select-Object -First 3)
        if ($failed) { throw "插件加载报错：`n$($failed -join "`n")`n（可用 -Rollback 恢复上一版代码）" }
    }
    throw "等了 $StartTimeoutSeconds 秒没看到 'Fully initialized'，请看 docker logs $Container（可用 -Rollback 恢复）"
}

# ---------- 回滚 ----------
if ($Rollback) {
    Assert-ContainerRunning
    $latest = docker exec $Container sh -c "ls -1t $codeBackupDir/wm_code_*.tar.gz 2>/dev/null | head -n 1"
    if (-not $latest) { throw "$codeBackupDir 里没有代码备份" }
    Write-Host "回滚到 $latest"
    if ($DryRun) { Write-Host '(DryRun) 不做改动'; return }
    # 先把当前代码也留一份，回滚本身可以再撤销
    Invoke-Checked '备份当前代码' { docker exec $Container sh -c "tar -C $pluginsRoot $tarExcludes -czf $codeBackupDir/wm_code_${ts}_before_rollback.tar.gz $pluginName" }
    $remoteCopy = "/tmp/wm_rollback_$ts.tar.gz"
    Invoke-Checked '准备回滚归档' { docker exec $Container cp $latest $remoteCopy }
    $result = Invoke-Apply $remoteCopy ''
    Write-Host "  恢复 $($result.written) 个文件，删除 $($result.removed.Count) 个新版本引入的文件"
    if (-not $NoRestart) { Restart-AndWait '' }
    Write-Host '回滚完成（数据库未回滚）'
    return
}

# ---------- [0] 要部署的提交 ----------
$commit = (git -C $repo rev-parse --verify "$Ref^{commit}").Trim()
if ($LASTEXITCODE -ne 0) { throw "找不到提交 $Ref" }
$branch = if ($Ref -eq 'HEAD') { (git -C $repo rev-parse --abbrev-ref HEAD).Trim() } else { $Ref }
$subject = (git -C $repo log -1 --format=%s $commit).Trim()
$dirty = [bool](git -C $repo status --porcelain --untracked-files=no)
Write-Host "部署 $($commit.Substring(0, 10)) ($branch) $subject"
if ($dirty) { Write-Warning '工作区有未提交改动，这些改动不会部署' }

# ---------- [1] 本地单测 ----------
if (-not $SkipTests) {
    Write-Host '[1/5] 本地单测'
    Push-Location $repo
    try {
        # 测的是工作区；工作区与要部署的提交不同时提醒
        if ($dirty -or $commit -ne (git rev-parse HEAD).Trim()) { Write-Warning '单测跑在工作区上，与要部署的提交不完全一致' }
        Invoke-Checked 'pytest' { python -m pytest tests -q -x -p no:cacheprovider | Select-Object -Last 3 | Write-Host }
    }
    finally { Pop-Location }
} else { Write-Host '[1/5] 跳过单测' }

Assert-ContainerRunning

$tmpTar = Join-Path ([IO.Path]::GetTempPath()) "wm_deploy_$ts.tar"
$tmpStamp = Join-Path ([IO.Path]::GetTempPath()) "wm_deploy_$ts.json"
$remoteTar = "/tmp/wm_deploy_$ts.tar"
$remoteStamp = "/tmp/wm_deploy_$ts.json"
Invoke-Checked 'git archive' { git -C $repo archive --format=tar -o $tmpTar $commit }
[ordered]@{
    commit      = $commit
    branch      = $branch
    subject     = $subject
    deployed_at = (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss')
    dirty       = $dirty
} | ConvertTo-Json | Set-Content -Path $tmpStamp -Encoding utf8NoBOM

if ($DryRun) {
    $count = (tar -tf $tmpTar | Where-Object { $_ -notmatch '/$' }).Count
    Write-Host "(DryRun) 归档 $count 个文件；将备份数据库与代码、替换 $pluginDir$(if (-not $NoRestart) { '、重启容器' })"
    Remove-Item $tmpTar, $tmpStamp -Force
    return
}

try {
    # ---------- [2] 数据库快照 ----------
    if (-not $SkipBackup) {
        Write-Host '[2/5] 卷外数据库快照'
        if (-not (Test-Path $BackupScript)) { throw "找不到备份脚本 $BackupScript（或用 -SkipBackup）" }
        & $BackupScript
    } else { Write-Host '[2/5] 跳过数据库快照' }

    # ---------- [3] 代码备份 ----------
    Write-Host "[3/5] 备份容器内现有代码 → $codeBackupDir"
    Invoke-Checked '代码备份' {
        docker exec $Container sh -c "mkdir -p $codeBackupDir && if [ -d $pluginDir ]; then tar -C $pluginsRoot $tarExcludes -czf $codeBackupDir/wm_code_$ts.tar.gz $pluginName; fi"
    }
    docker exec $Container sh -c "ls -1t $codeBackupDir/wm_code_*.tar.gz 2>/dev/null | tail -n +$($KeepCodeBackups + 1) | xargs -r rm -f" | Out-Null

    # ---------- [4] 替换代码 ----------
    Write-Host "[4/5] 替换 $pluginDir"
    Invoke-Checked 'docker cp 归档' { docker cp $tmpTar "${Container}:$remoteTar" }
    Invoke-Checked 'docker cp 版本戳' { docker cp $tmpStamp "${Container}:$remoteStamp" }
    $out = Invoke-Apply $remoteTar $remoteStamp
    $result = $out
    Write-Host "  写入 $($result.written) 个文件，删除 $($result.removed.Count) 个不在本次版本里的文件"
    $result.removed | Select-Object -First 50 | ForEach-Object { Write-Host "    - $_" }
    if ($result.unknown.Count) {
        Write-Host "  部署清单外的文件保留 $($result.unknown.Count) 个（首次部署或手工放入；-PruneUnknown 可删除）："
        $result.unknown | Select-Object -First 30 | ForEach-Object { Write-Host "    ? $_" }
    }
}
finally {
    Remove-Item $tmpTar, $tmpStamp -Force -ErrorAction SilentlyContinue
}

# ---------- [5] 重启 ----------
if ($NoRestart) {
    Write-Host '[5/5] 跳过重启；代码下次 AstrBot 重启或插件重载后生效'
} else {
    Write-Host '[5/5] 重启并等待插件启动'
    Restart-AndWait $commit
}
Write-Host "部署完成：$($commit.Substring(0, 10)) ($branch)"
