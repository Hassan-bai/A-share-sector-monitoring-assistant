# 板块异动监控

A股板块异动实时监控工具，帮助快速捕捉热点板块的连板梯队、龙头股及跟风股。

## 功能特性

- **实时板块扫描**：自动监控全市场板块异动，涨幅排名实时更新
- **连板梯队**：基于近7日真实K线数据，准确计算连板天数（首板 / 2连板 / N连板）
- **龙头识别**：综合涨幅、主力净流入、连板情况自动标注板块龙头
- **跟风小弟**：识别微红跟风股，展示题材活跃度
- **成分股总览**：点击板块查看所有成分股的涨幅、近5日、主力净流入、连板数
- **周报生成**：一键生成本周板块异动周报

## 数据来源

- 板块行情：东方财富
- 个股K线：东方财富日K接口

## 使用方法

### 直接运行（推荐）

下载 `dist/板块异动监控.exe`，双击运行即可，无需安装 Python。

### 源码运行

```bash
pip install -r requirements.txt
python main.py
```

浏览器访问 `http://localhost:5000` 查看监控界面。

## 项目结构

```
sector_monitor/
├── main.py            # 程序入口
├── sector_data.py     # 数据获取与分析核心
├── web_server.py      # Web 服务
├── detector.py        # 异动检测
├── news_fetcher.py    # 资讯获取
├── weekly_report.py   # 周报生成
├── static/            # 前端静态文件
│   └── index.html
├── requirements.txt
└── 启动监控.bat       # Windows 快捷启动
```

## 环境要求

- Python 3.10+
- Windows 10/11（exe 版本）

## License

MIT
