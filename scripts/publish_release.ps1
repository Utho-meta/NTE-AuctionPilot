param(
    [Parameter(Mandatory=$true)][string]$AssetDirectory,
    [string]$PythonExecutable='python',
    [ValidateSet('upload','publish','verify')][string]$Mode='publish'
)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$repoDirectory=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$assetDirectory=[IO.Path]::GetFullPath($AssetDirectory)
$contractPath=Join-Path $repoDirectory 'release/contract.json'
$contract=Get-Content -LiteralPath $contractPath -Raw | ConvertFrom-Json
$receipt=Join-Path (Split-Path $assetDirectory -Parent) 'release-verification.json'
$preflight=Join-Path (Split-Path $assetDirectory -Parent) 'local-preflight.json'
$statePath=Join-Path (Split-Path $assetDirectory -Parent) 'github-release-state.json'
[IO.File]::WriteAllText($receipt,'{"passed":false,"stage":"publisher_preflight"}')
$complete=$false
if(Test-Path -LiteralPath $statePath){
    $saved=Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    $saved.release_complete=$false
    [IO.File]::WriteAllText($statePath,($saved | ConvertTo-Json -Depth 12))
}
& $PythonExecutable (Join-Path $PSScriptRoot 'release_gate.py') preflight --root $repoDirectory --contract $contractPath --assets $assetDirectory --receipt $preflight
if($LASTEXITCODE -ne 0){throw 'Privacy/consistency preflight rejected publication'}
$head=(git -C $repoDirectory rev-parse HEAD).Trim()
if($LASTEXITCODE -ne 0 -or (git -C $repoDirectory status --porcelain)){throw 'Commit all release inputs before publishing'}
$body=[IO.File]::ReadAllText((Join-Path $repoDirectory $contract.notes_file))
$api="https://api.github.com/repos/$($contract.repository)"
$env:GIT_TERMINAL_PROMPT='0'
$env:GCM_INTERACTIVE='Never'
$credentialText="protocol=https`nhost=github.com`n`n" | git -C $repoDirectory credential fill 2>$null
$releaseToken=@($credentialText | Where-Object {$_ -like 'password=*'} | ForEach-Object {$_.Substring(9)}) | Select-Object -First 1
if(-not $releaseToken){throw 'No GitHub credential available'}
$headers=@{Authorization="Bearer $releaseToken";'User-Agent'='AuctionPilot-privacy-release';Accept='application/vnd.github+json';'X-GitHub-Api-Version'='2022-11-28'}
$credentialText=$null;$releaseToken=$null
function Payload($value){return ,([Text.Encoding]::UTF8.GetBytes(($value | ConvertTo-Json -Depth 15)))}
function Save-State($release){
    $state=@{id=$release.id;tag_name=$release.tag_name;commit=$release.target_commitish;url=$release.html_url;draft=$release.draft;prerelease=$release.prerelease;release_complete=$complete;receipt=$receipt;assets=@($release.assets | Select-Object id,name,size,digest,browser_download_url)}
    [IO.File]::WriteAllText($statePath,($state | ConvertTo-Json -Depth 12),[Text.UTF8Encoding]::new($false))
}
try{
    $repo=Invoke-RestMethod -Uri $api -Headers $headers
    if($repo.private -or -not $repo.permissions.push -or $repo.full_name -ne $contract.repository -or $repo.description -cne $contract.repository_description){throw 'Repository identity/permissions/About mismatch'}
    $remote=Invoke-RestMethod -Uri "$api/git/ref/heads/$($contract.branch)" -Headers $headers
    if($remote.object.sha -ne $head){throw 'Local inputs are not remote main'}
    $latest=Invoke-RestMethod -Uri "$api/releases/latest" -Headers $headers
    if($latest.tag_name -ne $contract.stable_latest_tag){throw 'Stable release marker differs'}
    # Compare every contract-bound document and the contract itself before upload.
    $names=@($contract.documents.PSObject.Properties.Name)+@('release/contract.json','scripts/release_gate.py','scripts/privacy_gate.py')
    foreach($name in $names){
        $remoteFile=Invoke-RestMethod -Uri "$api/contents/$($name)?ref=$head" -Headers $headers
        $remoteText=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($remoteFile.content))
        $localText=[IO.File]::ReadAllText((Join-Path $repoDirectory $name))
        if($remoteText.Replace("`r`n","`n").Trim() -cne $localText.Replace("`r`n","`n").Trim()){throw "Remote release input differs: $name"}
    }
    if(Test-Path -LiteralPath $statePath){
        $saved=Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
        $release=Invoke-RestMethod -Uri "$api/releases/$($saved.id)" -Headers $headers
        if($release.tag_name -ne $contract.tag -or $release.target_commitish -ne $saved.commit){throw 'Owned release identity changed'}
    }else{
        if($Mode -eq 'verify'){throw 'No owned release state'}
        $existing=Invoke-RestMethod -Uri "$api/releases?per_page=100" -Headers $headers
        if(@($existing | Where-Object tag_name -eq $contract.tag).Count){throw 'Unowned release already exists; refusing overwrite'}
        $payload=@{tag_name=$contract.tag;target_commitish=$head;name=$contract.release_title;body=$body;draft=$true;prerelease=$contract.prerelease;make_latest='false'}
        $release=Invoke-RestMethod -Uri "$api/releases" -Method Post -Headers $headers -ContentType 'application/json; charset=utf-8' -Body (Payload $payload)
        Save-State $release
    }
    if($Mode -ne 'verify' -and $release.draft){
        $uploadBase=$release.upload_url.Split('{')[0]
        foreach($asset in $contract.assets){
            $existing=@($release.assets | Where-Object name -eq $asset.name)
            if($existing.Count){
                if($existing.Count -ne 1 -or $existing[0].size -ne $asset.size -or $existing[0].digest -ne "sha256:$($asset.sha256)"){throw 'Existing asset differs; refusing overwrite'}
                continue
            }
            $type=if($asset.name.EndsWith('.zip')){'application/zip'}else{'text/plain'}
            Write-Output "Uploading $($asset.name)"
            $uploaded=Invoke-RestMethod -Uri ($uploadBase+'?name='+[Uri]::EscapeDataString($asset.name)) -Method Post -Headers $headers -ContentType $type -InFile (Join-Path $assetDirectory $asset.name) -TimeoutSec 1200
            if($uploaded.size -ne $asset.size -or $uploaded.digest -ne "sha256:$($asset.sha256)"){throw 'Uploaded bytes differ'}
            $release=Invoke-RestMethod -Uri "$api/releases/$($release.id)" -Headers $headers
            Save-State $release
        }
    }
    if($release.assets.Count -ne $contract.assets.Count){throw 'Asset count differs'}
    foreach($asset in $contract.assets){
        $match=@($release.assets | Where-Object name -eq $asset.name)
        if($match.Count -ne 1 -or $match[0].size -ne $asset.size -or $match[0].digest -ne "sha256:$($asset.sha256)"){throw 'Release assets differ'}
    }
    if($release.body.Replace("`r`n","`n").Trim() -cne $body.Replace("`r`n","`n").Trim()){throw 'Release body differs'}
    if($Mode -eq 'publish'){
        if($release.draft){$release=Invoke-RestMethod -Uri "$api/releases/$($release.id)" -Method Patch -Headers $headers -ContentType 'application/json; charset=utf-8' -Body (Payload @{draft=$false;prerelease=$contract.prerelease;make_latest='false'})}
        Save-State $release
        # Only explicitly enumerated superseded releases may be withdrawn.
        foreach($old in $contract.retired_releases){
            $previous=Invoke-RestMethod -Uri "$api/releases/$($old.id)" -Headers $headers
            if($previous.tag_name -ne $old.tag){throw 'Retired release identity differs'}
            if(-not $previous.draft){
                $previous=Invoke-RestMethod -Uri "$api/releases/$($old.id)" -Method Patch -Headers $headers -ContentType 'application/json; charset=utf-8' -Body (Payload @{draft=$true;make_latest='false'})
                if(-not $previous.draft){throw 'Superseded release was not withdrawn'}
            }
        }
    }
    if($Mode -in @('publish','verify')){
        if($release.draft){throw 'Release is still a draft'}
        & $PythonExecutable (Join-Path $PSScriptRoot 'release_gate.py') verify --root $repoDirectory --contract $contractPath --receipt $receipt
        if($LASTEXITCODE -ne 0){throw 'Public verification failed; release incomplete'}
        $complete=$true
        Save-State $release
    }
    [pscustomobject]@{url=$release.html_url;draft=$release.draft;local_public_verification_passed=$complete;github_actions_must_also_pass=$true} | ConvertTo-Json
}finally{$headers.Clear()}
