# Fork 改动说明

这个 fork 面向“电脑运行 Virtual TCU，iPad / iPhone / Mac / 其他局域网设备只看遥测面板”的使用方式。

## 主要改动

- 将 `http://<电脑局域网 IP>:8765` 默认页面替换为中文只读遥测仪表盘。
- 绕过原 Vue/Vite 浏览器仪表盘在远程设备上可能黑屏的问题。
- 删除远程端用不上的 `Drive Mode` 和顶部 `Session` 面板，让车辆学习、驾驶风格、动力区间更靠上。
- 保留核心显示数据：挡位、速度、转速、RPM LED、功率、扭矩、油门、刹车、离合、TCU 状态、实时曲线、G 值、抓地、车辆学习、统计和换挡历史。
- 远程页面保持只读；调模式、改参数、网络设置、输出模式和记录控制仍在 Windows 上的 Electron Settings 窗口里完成。
- 页面脚本对缺失面板更稳健，隐藏或删除某些可选 DOM 元素不会让整页停止更新。

## 默认访问方式

在 Virtual TCU 设置里把 **Web bind address** 设置为：

```text
0.0.0.0
```

然后局域网设备直接打开：

```text
http://<电脑局域网 IP>:8765
```

例如：

```text
http://192.168.18.58:8765
```

不再需要 `?v=4` 这类缓存参数；正式构建后默认首页就是中文远程仪表盘。

## 安装方式保持不变

保留原项目的发布方式：

- `VirtualTCU-*-win64.exe`：推荐安装包，包含 Electron 托盘应用、Settings、HUD、后端和自动更新。
- `VirtualTCU-*-win64.zip`：Electron 便携包。
- `VirtualTCU-Backend-*-win64.zip`：无 Electron 的 backend-only 便携包。
- 源码运行：按 README 安装 Python / Node / pnpm 后运行。

GitHub Actions 的 release workflow 会在推送 `v*` tag 时自动构建这些产物。

## 建议 release 文案

标题：

```text
v13.1.2 - 中文局域网只读仪表盘
```

说明：

```text
这个版本保留 Virtual TCU 原有换挡逻辑、Electron 设置窗口、HUD、安装包和便携包发布方式，同时把浏览器 Dashboard 改成适合 iPad/iPhone/Mac 局域网查看的中文只读遥测面板。修复远程浏览器只显示黑屏的问题；远程设备直接打开 http://电脑IP:8765 即可查看挡位、RPM、速度、踏板、TCU 状态、实时曲线、G 值、学习状态、统计和换挡历史。
```
