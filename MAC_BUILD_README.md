# 在 Mac 上打包「板块异动监控」

> 本文档适合帮忙打包的朋友参考，全程只需执行几条命令。

---

## 前置要求

- macOS 10.15 (Catalina) 或更新版本
- Python 3.9+（没装的话看下面安装方式）

### 安装 Python（如果没有）

方式一：官网下载（推荐）
https://www.python.org/downloads/macos/

方式二：Homebrew
```bash
brew install python
```

---

## 打包步骤

### 第一步：把项目文件夹拷贝到 Mac

将整个 `sector_monitor` 文件夹（包含 `main.py`、`build_mac.sh` 等所有文件）拷贝到 Mac 上任意位置，比如桌面。

### 第二步：打开终端

按 `Command + 空格`，搜索"终端"（Terminal），打开。

### 第三步：进入项目目录

```bash
cd ~/Desktop/sector_monitor
```
（根据你放的位置调整路径）

### 第四步：一键打包

```bash
bash build_mac.sh
```

脚本会自动：
1. 检查 Python 版本
2. 创建虚拟环境
3. 安装所有依赖
4. 执行 PyInstaller 打包
5. 提示产物路径

**首次运行约需 2-5 分钟**，请耐心等待。

---

## 打包完成后

打包成功后，`dist/` 目录里会出现：

```
dist/
└── 板块异动监控.app   ← 双击这个运行
```

**双击启动**，然后在浏览器访问：
```
http://127.0.0.1:18888
```

---

## 常见问题

### ❓ 双击 .app 提示「无法打开，因为无法验证开发者」

这是 macOS Gatekeeper 安全机制，解决方法：

**方法一（推荐）**：右键点击 `.app` → 选「打开」→ 弹窗里点「打开」

**方法二**：系统偏好设置 → 安全性与隐私 → 仍然打开

**方法三**（终端）：
```bash
xattr -cr dist/板块异动监控.app
```

---

### ❓ 打包报错 `ModuleNotFoundError`

在虚拟环境里手动安装缺失的包：
```bash
source venv_mac/bin/activate
pip install <缺失的包名>
python3 -m PyInstaller sector_monitor_mac.spec --clean --noconfirm
```

---

### ❓ Apple Silicon（M1/M2/M3）兼容性

脚本自动检测当前 Mac 的 CPU 架构打包，无需手动配置。
在 M 系列 Mac 上打出来的 `.app` 只能在 M 系列上运行；Intel Mac 同理。

如需兼容两种架构（Universal Binary），需安装 Rosetta 并执行：
```bash
arch -x86_64 python3 -m PyInstaller sector_monitor_mac.spec --clean --noconfirm
```

---

## 发给使用者

最终只需发送 `dist/板块异动监控.app` 这一个文件即可。
使用者双击打开，浏览器访问 `http://127.0.0.1:18888`，无需安装任何其他软件。
