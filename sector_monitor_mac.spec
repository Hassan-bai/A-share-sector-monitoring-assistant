# -*- mode: python ; coding: utf-8 -*-
"""
板块异动监控 PyInstaller 打包配置 —— macOS 版
生成可双击运行的 .app 应用包
"""

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        # 把 static 目录打包进去
        ('static', 'static'),
    ],
    hiddenimports=[
        'flask',
        'werkzeug',
        'werkzeug.serving',
        'werkzeug.routing',
        'jinja2',
        'click',
        'itsdangerous',
        'requests',
        'urllib3',
        'charset_normalizer',
        'certifi',
        'idna',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'pandas', 'scipy'],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='板块异动监控',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,           # 保留终端窗口，方便看日志
    disable_windowed_traceback=False,
    argv_emulation=True,    # macOS 专用：处理文件关联参数
    target_arch=None,       # None = 自动匹配当前 Mac 架构（Intel/Apple Silicon 均可）
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

# ── macOS 专用：生成 .app 应用包 ─────────────────────────────
app = BUNDLE(
    exe,
    name='板块异动监控.app',
    icon=None,
    bundle_identifier='com.sector.monitor',
    info_plist={
        'CFBundleName':             '板块异动监控',
        'CFBundleDisplayName':      '板块异动监控',
        'CFBundleVersion':          '1.0.0',
        'CFBundleShortVersionString': '1.0.0',
        'NSHighResolutionCapable':  True,
        # 允许程序访问网络（抓行情数据必需）
        'NSAppTransportSecurity': {
            'NSAllowsArbitraryLoads': True,
        },
    },
)
