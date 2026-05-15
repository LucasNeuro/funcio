
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

from dotenv import load_dotenv


def _database_url_from_env() -> str:
    """Igual à cadeia em vitual_agent_os (antes do import desse módulo)."""
    for name in ("DATABASE_URL", "SUPABASE_DATABASE_URL", "POSTGRES_URL"):
        u = (os.getenv(name) or "").strip()
        if u:
            return u
    return ""


def _load_agents_env() -> Path:
    """
    Carrega .env na mesma pasta que run.py (pasta agents/) com UTF-8 e fallback de DATABASE_URL (BOM / parser).
    """
    p = Path(__file__).resolve().parent / ".env"
    load_dotenv(p, override=True, encoding="utf-8")
    if p.is_file() and not _database_url_from_env():
        try:
            text = p.read_text(encoding="utf-8-sig")
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("DATABASE_URL="):
                    val = line.split("=", 1)[1].strip()
                    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                        val = val[1:-1]
                    if val and not val.startswith("#"):
                        os.environ["DATABASE_URL"] = val
                    break
        except OSError:
            pass
    return p


_ENV_FILE = _load_agents_env()

# Agno lê AGNO_DEBUG na importação / runtime do agente
if os.getenv("VITUAL_AGENT_DEBUG", "0").strip().lower() in ("1", "true", "yes"):
    os.environ["AGNO_DEBUG"] = "true"

try:
    import certifi

    _ca = certifi.where()
    os.environ.setdefault("SSL_CERT_FILE", _ca)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _ca)
except Exception:
    pass

def _configure_logging() -> None:
    """Logs no terminal durante pedidos HTTP e actividade Agno (tool/model)."""
    import logging

    name = (os.getenv("VITUAL_LOG_LEVEL") or "INFO").strip().upper()
    level = getattr(logging, name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
        force=True,
    )
    for logger_name in ("agno", "uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(level)


def _bind_ok(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False


if __name__ == "__main__":
    _configure_logging()
    host = os.getenv("AGNO_HOST", "0.0.0.0")
    # Render define PORT; localmente use AGNO_PORT ou 8000
    _raw = (os.getenv("AGNO_PORT") or os.getenv("PORT") or "8000").strip()
    try:
        port = int(_raw)
    except ValueError:
        print(
            f"[vitual] AGNO_PORT/PORT inválido ({_raw!r}); a usar 8000.",
            file=sys.stderr,
            flush=True,
        )
        port = 8000
    if port <= 0 or port > 65535:
        print(f"[vitual] porta fora do intervalo: {port}; a usar 8000.", file=sys.stderr, flush=True)
        port = 8000

    from vitual_agent_os import build_vitual_os

    agent_os, app, _db_tools, _db_state = build_vitual_os()

    if not _bind_ok(port):
        print(
            f"Porta {port} ocupada. Feche o outro processo ou defina AGNO_PORT no .env",
            file=sys.stderr,
        )
        sys.exit(1)
    has_url = bool(_database_url_from_env())
    print(
        f"[vitual] diagnostico .env: ficheiro={_ENV_FILE} existe={_ENV_FILE.is_file()} | "
        f"len DATABASE_URL={len((os.getenv('DATABASE_URL') or ''))} | "
        f"len SUPABASE_DATABASE_URL={len((os.getenv('SUPABASE_DATABASE_URL') or ''))} | "
        f"PostgresTools={'sim' if _db_tools else 'nao'}",
        flush=True,
    )
    if not _db_tools:
        print("\n" + "=" * 72 + "\n", flush=True)
        if _db_state == "missing_url" or not has_url:
            print(
                "AVISO: PostgresTools INATIVAS — DATABASE_URL não está definida.\n"
                "No Render: Environment → DATABASE_URL. Local: ficheiro .env junto a run.py.\n",
                flush=True,
            )
        elif _db_state.startswith("error:"):
            print(
                "AVISO: PostgresTools INATIVAS — DATABASE_URL existe mas a ligação falhou:\n"
                f"  {_db_state[6:].strip()}\n"
                "Confirma password, URI (pooler vs direct), rede e ssl. Ver stderr acima.\n",
                flush=True,
            )
        else:
            print(
                f"AVISO: PostgresTools INATIVAS — estado: {_db_state}\n",
                flush=True,
            )
        print(
            "No Agno UI: + NEW SESSION após corrigir.\n" + "=" * 72 + "\n",
            flush=True,
        )
    else:
        print("[vitual] PostgresTools ativas (DATABASE_URL OK).\n", flush=True)
    loc = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"Docs: http://{loc}:{port}/docs")
    print(f"os.agno.com (Local): http://localhost:{port}")
    _access = os.getenv("VITUAL_ACCESS_LOG", "1").strip().lower() not in ("0", "false", "no")
    _uv_level = (os.getenv("VITUAL_LOG_LEVEL") or "info").strip().lower()
    print(
        f"[vitual] logging: nivel={_uv_level.upper()} access_log={_access} | "
        "para mais detalhes: VITUAL_LOG_LEVEL=DEBUG VITUAL_AGENT_DEBUG=1 no .env\n",
        flush=True,
    )
    agent_os.serve(
        app=app,
        host=host,
        port=port,
        reload=False,
        access_log=_access,
        log_level=_uv_level,
    )
