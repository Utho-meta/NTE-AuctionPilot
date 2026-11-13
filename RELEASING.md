# 发布完成检查

上传成功不等于发布完成。每次更新以本期交付包的 `版本与验证.json` 为依据，同时更新首页、About、发布说明与下载包；只有本次自动检查全部通过，才能向用户报告完成。百度网盘由用户接管，不属于此流程。

## 固定顺序

1. 完成本期四项目包、报告与视频及必要验证。先清理整个公开交付：内部记录、个人工作路径、账户信息、私人链接和凭据、参考声音原件、个人设置库存及私人录像截图不得进入公开包。递归处理嵌套 ZIP、程序内嵌归档、文档/图片/视频元数据和调试信息；必要第三方许可、供应商签名及不含个人身份的公共构建元数据保留。清理仅在交付副本进行，保留本地原件。
2. 使用 Python 3.13 和 FFmpeg/ffprobe 执行 `python scripts/privacy_gate.py <最终ZIP> --report <本地检查记录>`。同时人工审阅公开文本数据、所有唯一图片、视频抽帧、讲稿字幕与授权成品音频，核对二进制元数据和许可。把本次输出的 `payload_inventory_sha256` 与各项真实审阅结论写入 `release/privacy-review.json`。该文件只记录公开摘要及方法，不写被删除的隐私值；禁止在未审阅时自动勾选。任何成员改变均须重新审阅。扫描失败、解析失败或不支持的格式都必须解决后才能继续。
3. 冻结 ZIP，更新 README 的最新下载、四项目版本和入口、更新范围与预发布标记。旧下载放入历史区域。同步 About、PRIVACY 及本期 `RELEASE_NOTES_<tag>.md`；后者必须与 GitHub Release 正文一致。
4. 检查 `release/requirements.json` 中本期标题、交付文件与需要停止提供的旧版本。根据最终 ZIP 和已经审阅的文档生成新清单；不要手填摘要或沿用上期清单。

```powershell
python scripts/release_gate.py prepare --root . --contract release/contract.json --requirements release/requirements.json --repository Utho-meta/NTE-AuctionPilot --tag <本期标签> --archive <最终ZIP路径> --checksum <SHA256文件路径> --notes RELEASE_NOTES_<本期标签>.md --prerelease --stable-latest <仍应保留的稳定版标签>
```

`--archive` 的文件名必须与即将上传的英文资产名一致。校验文件格式为 `SHA256`、两个空格、ZIP 文件名。正式稳定版移除 `--prerelease`，并把 `--stable-latest` 设为本期稳定版。准备步骤只生成清单，不能据此报告发布完成。

5. 提交并推送文档、清单、人工审阅摘要及检查程序，使用统一发布脚本：

```powershell
./scripts/publish_release.ps1 -AssetDirectory <资产目录> -PythonExecutable <Python3.13路径> -Mode publish
```

脚本在任何上传/发布前重新执行本地 `preflight`（包含完整隐私扫描）；再核对远端首页文档，上传草稿、核对资产、发布及撤回清单明确列出的旧版本。凭据仅从现有 Git 凭据管理器读取，不写入磁盘或日志。不能使用旧日期脚本绕过隐私检查。

文档先于资产发布时，自动检查会因资产尚不存在而失败；发布事件和本地发布收尾会再次检查。这段时间只能称“发布中”。
6. 发布脚本自动执行下列命令；退出码非零时立即停止，不能显示完成、继续宣布交付或使用上次记录。GitHub 也会在 main 更新、Release 发布或编辑后运行同一检查。

```powershell
python scripts/release_gate.py verify --root . --contract release/contract.json --receipt release-verification.json
```

7. 确认本次结果 `passed: true` 且 `privacy_verified: true`，标签、远端提交和清单摘要均对应当前版本，且 GitHub Actions 的 **Release consistency** 成功。之后才提供下载链接并报告完成。失败时根据具体失败项修正文档、元数据或重新准备正确版本；不能继续分发已知有隐私问题的旧包。撤回采用草稿状态保留维护者存档，同时检查旧直链已不能匿名下载。

## 自动核对范围

- GitHub 默认分支、About 简介、远端已提交的 README、隐私说明和本期发布说明与审阅清单相同。
- README 最新下载排在首位，四项目版本与入口对应压缩包清单；历史链接只能放在历史区。Release 同样具有本期版本、直链、精确字节数、SHA-256 和预发布标记。
- 仓库发布说明与 Release 正文相同；说明 `Source code` 文档快照不是运行包；稳定版 latest 符合本期计划。
- 实际首页和发布页显示本期版本与正确下载链接；README/Release 的链接能够访问。
- 每次从匿名公开下载直链重新下载所有资产，核对大小和 SHA-256，不读取缓存 ZIP 或旧的检查结果。验证 ZIP 全部 CRC、四项目清单和入口、报告视频及索引。
- 对实际下载包再次递归执行隐私扫描，并验证内容清单与已提交的人工审阅摘要完全一致。包括嵌套 ZIP、PyInstaller/PYZ、PE 调试路径、UTF-16/JSON 转义内容、HTML 内嵌图片、图片 EXIF/文本块、视频各流及章节元数据。旧的结构一致性检查或人工审阅不能替代本期内容检查。
- 精确列入撤回清单的旧 Release 与下载链接必须返回 404/410，不能只从 README 隐藏入口。
- 下载前后再比对远端提交、简介、发布正文与资产身份；中途变化则失败，避免混用两个版本。

结果写入 `release-verification.json`，包含本次运行标识、时间、提交、清单与检查程序摘要、下载摘要及 ZIP 检查结果。开始即覆盖旧成功状态，失败保存原因并返回非零。GitHub Actions 保留该结果 90 天；长期交付记录另存入本地本期发布目录。任何后续修改都使此前结果失去对当前版本的证明效力，需要重新检查。

检查只证明发布内容一致，不能代替功能测试、长期实战或游戏机制研究。GitHub 的 About 单独编辑或资产单独增删不一定触发上述事件；必须通过发布脚本收尾检查，或在 Actions 手动运行本流程，不能继续引用修改前的成功记录。
