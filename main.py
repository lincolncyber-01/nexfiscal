"""NexFiscal — ponto de entrada. Sem argumentos abre a interface gráfica."""
import os
import sys
from pathlib import Path

# In the packaged Windows build, keep the Playwright browser beside the app.
# The build workflow places Chromium in <app directory>/ms-playwright.
if getattr(sys, "frozen", False):
    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH",
        str(Path(sys.executable).resolve().parent / "ms-playwright"),
    )

from nexfiscal.cli import main

if __name__ == "__main__":
    sys.exit(main())
