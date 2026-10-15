"""NexFiscal — ponto de entrada. Sem argumentos abre a interface gráfica."""
import sys

from nexfiscal.cli import main

if __name__ == "__main__":
    sys.exit(main())
