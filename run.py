"""
Entrada na raiz do repo para hosts que arrancam em /src (ex.: Render com ``python run.py``).

Muda o cwd para ``agents/`` e executa o ``run.py`` real — assim o .env, imports e ``vitual_agent_os``
continuam corretos.
"""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_AGENTS = _ROOT / "agents"
_MAIN = _AGENTS / "run.py"


if __name__ == "__main__":
    if not _MAIN.is_file():
        print(f"[vitual] Não encontrei {_MAIN}", file=sys.stderr, flush=True)
        sys.exit(2)
    os.chdir(_AGENTS)
    runpy.run_path(str(_MAIN), run_name="__main__")
