# -*- mode: python ; coding: utf-8 -*-
# Empacotamento do NexFiscal com PyInstaller (modo pasta).
import os
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
for pkg in ("playwright", "reportlab"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h

a = Analysis(
    ["main.py"],
    pathex=["."],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports + ["nexfiscal.ui.main_window", "nexfiscal.controller"],
    excludes=["tkinter", "matplotlib", "numpy", "scipy", "pytest", "IPython",
              "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
              "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtMultimedia", "PySide6.QtCharts"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="NexFiscal",
    console=bool(os.environ.get("NEXFISCAL_CONSOLE")),
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="NexFiscal", upx=False)
