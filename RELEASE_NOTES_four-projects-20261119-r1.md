# AuctionPilot 四项目合集 · 浮窗拖动修复 · 2026-11-19

**[下载最新四项目完整包（约 305 MB）](https://github.com/Utho-meta/NTE-AuctionPilot/releases/download/four-projects-20261119-r1/AuctionPilot-FourProjects-20261119.zip)**

[下载 SHA-256 校验文件](https://github.com/Utho-meta/NTE-AuctionPilot/releases/download/four-projects-20261119-r1/AuctionPilot-FourProjects-20261119.zip.sha256.txt)

本期为预发布合集。发布页附带的 `Source code` 是文档快照，不包含四项目运行文件。主程序稳定版仍为 v0.12.10。

- 01 AuctionPilot 主程序 v0.12.12 Preview2（11 月 19 日修订），运行 `01-主程序/AuctionPilot.exe`。
- 02 Arena 模拟器 v0.4.0 Preview14，运行 `02-Arena模拟器/AuctionPilotArena.exe`。
- 03 展柜搭配实验室 v0.4 Preview3，运行 `03-展柜搭配实验室/展柜搭配实验室.exe`，也可打开同目录的 HTML。
- 04 版本分析报告 v1.0，打开 `04-版本报告/index.html`。

## 本次变化

- 修复屏幕识别开始后浮窗回跳，以及局内拖动时在两个位置之间闪动的问题。手动移动后，识别刷新会保留松手位置。
- 主程序、Arena 模拟器和展柜启动入口统一使用小吱图标。
- Windows 可双击 `展柜搭配实验室.exe`，由默认浏览器打开同目录的 HTML。原 HTML 入口继续可用。

先关闭旧主程序，然后完整解压到新文件夹并打开 `项目索引.html`。旧设置和录像可留在旧目录。需要沿用主程序设置时，可复制旧目录的 `auction-pilot.json` 到新主程序目录。

浮窗修复在 Windows 11 单显示器上完成 30 项检查，并已实机确认识别前后都能拖动、松手后位置可保留。本包沿用经实机确认的主程序 DLL。核对了三个 Windows 启动图标，Arena 通过运行检查，展柜通过目录迁移和离线页面检查。这些检查不等于长期实战验证。

报告保留 2026-11-13 的 Arena Preview5 冻结模型结果。之后修正的 M1000 尺寸和均价取整尚未用于重跑角色定榜。本期调整浮窗拖动和启动入口，沿用既有研究视频与排名。

ZIP 为 `AuctionPilot-FourProjects-20261119.zip`，共 **305,394,179 字节**。

SHA-256：

```text
9754a5b4ecc6544d4d0713d7892c7524ea42a372c1eec9aad55b7fc03c0c31ec
```
