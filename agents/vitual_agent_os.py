"""
Vitual — montagem do AgentOS: Mistral + SqliteDb (sessões) + PostgresTools (opcional) + exportação de ficheiros.

Requer variáveis de ambiente já carregadas (ex.: run.py chama load_dotenv antes do import).

agents/.env (carregado por run.py antes deste módulo):

  Mistral
  MISTRAL_API_KEY — obrigatório
  MISTRAL_MODEL — opcional (default mistral-large-latest)
  MISTRAL_CLIENT_TIMEOUT_SEC — opcional; timeout pedidos API em segundos (ex. 120)
  AGENT_TOOL_CHOICE ou MISTRAL_TOOL_CHOICE — opcional; ex. any força uso de uma tool (útil com perguntas à BD)

  Postgres (PostgresTools Agno)
  DATABASE_URL (preferido) ou SUPABASE_DATABASE_URL ou POSTGRES_URL — opcional; URI Postgres
  SUPABASE_DB_SCHEMA — opcional (default public)
  VITUAL_POSTGRES_TOOLS — opcional; subconjunto separado por vírgulas; vazio = todas as 6 funções
  VITUAL_PG_STATEMENT_TIMEOUT_MS — opcional (default 60000); cancela queries lentas (ms)

  Supabase REST (CustomApiTools), só se VITUAL_SUPABASE_REST=1
  SUPABASE_URL ou NEXT_PUBLIC_SUPABASE_URL ou VITE_SUPABASE_URL — URL do projecto
  SUPABASE_ANON_KEY ou NEXT_PUBLIC_SUPABASE_ANON_KEY — chave anon (default sem service role)
  SUPABASE_SERVICE_ROLE_KEY — usada se VITUAL_SUPABASE_USE_SERVICE_ROLE=1 (ignora RLS na REST)
  VITUAL_SUPABASE_USE_SERVICE_ROLE — 1 service role; 0 ou omitido = anon

  Outras flags Vitual
  VITUAL_FILE_EXPORT — opcional (default 1); 0 desactiva FileGenerationTools (PDF/CSV/JSON/TXT)
  VITUAL_SUPABASE_REST — 1 activa REST /rest/v1; 0 ou omitido = PostgresTools + exportação local (sem PostgREST)
  VITUAL_LOCAL_PDF — opcional; 1 força PDF local; 0 desliga; omitido = PDF ligado com FileGeneration
  VITUAL_SUPABASE_USE_SERVICE_ROLE — 1 para PostgREST com SERVICE_ROLE_KEY (ignora RLS); 0 ou omitido = anon
  VITUAL_NUM_HISTORY_RUNS — opcional (default 5); turnos recentes no contexto (1–30)
  VITUAL_AGENT_DEBUG — opcional; 1 activa debug Agno (run.py também define AGNO_DEBUG=true)

  Cliente HTTP → API Mistral (httpx)
  AGNO_HTTP_VERIFY — 0 desactiva verificação SSL (só desenvolvimento)
  AGNO_SSL_PREFER_CERTIFI — 1 força bundle Mozilla (certifi)
  AGNO_SSL_USE_TRUSTSTORE — 0 força certifi (compat. com nomes antigos); por defeito usa truststore (loja do SO)

  (Hub multi-agente / Storage Markdown em Supabase — reactivar mais tarde.)

  Servidor / logs (lidos em run.py, mesmo .env)
  AGNO_HOST — opcional (default 0.0.0.0)
  AGNO_PORT — opcional (default 8000)
  VITUAL_LOG_LEVEL — opcional (default INFO no logging; uvicorn usa a mesma variável em minúsculas)
  VITUAL_ACCESS_LOG — opcional (default 1); 0 desactiva access log uvicorn

Docs Agno (tools): https://docs.agno.com/tools/overview
Docs Agno PostgresTools: https://docs.agno.com/tools/toolkits/database/postgres
File generation: https://docs.agno.com (File Generation Tools)
"""
from __future__ import annotations

import os
import ssl
import sys
from pathlib import Path

import httpx

from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.models.mistral import MistralChat
from agno.os import AgentOS
from agno.tools.file_generation import FileGenerationTools

_ROOT = Path(__file__).resolve().parent
_EXPORTS = _ROOT / "exports"
_debug = os.getenv("VITUAL_AGENT_DEBUG", "0").strip().lower() in ("1", "true", "yes")

try:
    import certifi as _certifi_boot

    _boot_ca = _certifi_boot.where()
    os.environ.setdefault("SSL_CERT_FILE", _boot_ca)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _boot_ca)
except Exception:
    pass

# Funções expostas pelo PostgresTools (cookbook Agno) — grupo analítico completo
_POSTGRES_TOOL_NAMES_ALL: tuple[str, ...] = (
    "show_tables",
    "describe_table",
    "summarize_table",
    "inspect_query",
    "run_query",
    "export_table_to_path",
)


def _env_first(*names: str) -> str:
    """Primeiro ``os.getenv`` não vazio (aliases entre Vitual, Supabase e Next/Vite)."""
    for name in names:
        out = (os.getenv(name) or "").strip()
        if out:
            return out
    return ""


def _postgres_dsn_normalized() -> str:
    """
    Mesma normalização de URI que PostgresTools (read-only).
    Devolve string vazia se não houver DATABASE_URL / aliases.
    """
    raw = _env_first("DATABASE_URL", "SUPABASE_DATABASE_URL", "POSTGRES_URL")
    if not raw:
        return ""
    for prefix in ("postgresql+psycopg://", "postgres+psycopg://"):
        if raw.startswith(prefix):
            raw = "postgresql://" + raw[len(prefix) :]
            break
    if raw.startswith("postgres://"):
        raw = "postgresql://" + raw[len("postgres://") :]
    if "supabase.co" in raw and "sslmode=" not in raw:
        raw = f'{raw}{"&" if "?" in raw else "?"}sslmode=require'
    return raw


def _pg_schema_and_timeout_ms() -> tuple[str, int]:
    schema = (os.getenv("SUPABASE_DB_SCHEMA") or "public").strip()
    if not schema.replace("_", "").replace(" ", "").isalnum():
        schema = "public"
    try:
        st_ms = int((os.getenv("VITUAL_PG_STATEMENT_TIMEOUT_MS") or "60000").strip())
        st_ms = max(1000, min(st_ms, 600_000))
    except ValueError:
        st_ms = 60000
    return schema, st_ms


def mistral_http_clients() -> tuple[httpx.Client, httpx.AsyncClient]:
    """
    Clientes HTTP para o SDK Mistral (sync + async/stream).
    Por defeito usa a loja de certificados do sistema (pacote ``truststore``) — necessário em muitos
    Windows com proxies / raízes empresariais que o bundle certifi não inclui.
    Forçar só Mozilla CA (certifi): AGNO_SSL_PREFER_CERTIFI=1
    Desactivar verificação SSL (só dev): AGNO_HTTP_VERIFY=0
    """
    insecure = os.getenv("AGNO_HTTP_VERIFY", "1").strip().lower() in ("0", "false", "no")
    timeout = httpx.Timeout(120.0, connect=30.0)
    if insecure:
        verify: bool | str | ssl.SSLContext = False
    else:
        prefer_certifi = os.getenv("AGNO_SSL_PREFER_CERTIFI", "0").strip().lower() in ("1", "true", "yes")
        # Compat: antes era opt-in com AGNO_SSL_USE_TRUSTSTORE=1; agora truststore é o default.
        legacy_certifi = os.getenv("AGNO_SSL_USE_TRUSTSTORE", "").strip().lower() in ("0", "false", "no")
        prefer_certifi = prefer_certifi or legacy_certifi

        verify: bool | str | ssl.SSLContext
        if not prefer_certifi:
            try:
                import truststore

                verify = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            except Exception:
                prefer_certifi = True

        if prefer_certifi:
            import certifi

            cafile = certifi.where()
            os.environ.setdefault("SSL_CERT_FILE", cafile)
            os.environ.setdefault("REQUESTS_CA_BUNDLE", cafile)
            verify = cafile
    return (
        httpx.Client(verify=verify, timeout=timeout),
        httpx.AsyncClient(verify=verify, timeout=timeout),
    )


def crm_postgres_tools() -> tuple[list, str]:
    """
    PostgresTools (Agno) em modo leitura sobre o Postgres do Supabase.
    Devolve (tools, estado): estado é "active", "missing_url" ou "error:<mensagem>".

    Usa DATABASE_URL (URI completa do dashboard). O anon/service role JWT não serve aqui.

    Nota: o Agno corre ferramentas síncronas em ``asyncio.to_thread``. Uma ligação ``psycopg``
    partilhada entre o thread principal e o worker não é segura e pode bloquear ou falhar em
    silêncio — por isso validamos com uma ligação temporária e cada toolkit abre a sua no thread
    que executa a tool (ver :class:`VitualPostgresTools`).
    """
    raw = _postgres_dsn_normalized()
    if not raw:
        return [], "missing_url"

    schema, st_ms = _pg_schema_and_timeout_ms()

    try:
        import psycopg
        from agno.tools.postgres import PostgresTools
        from psycopg.rows import dict_row
    except ImportError:
        print("Instale: pip install 'psycopg[binary]'", file=sys.stderr)
        return [], "error: falta pacote psycopg (pip install 'psycopg[binary]')"

    # search_path + statement_timeout via libpq options (evita prepared $1 em SET)
    _pg_opts = f"-c search_path={schema} -c statement_timeout={st_ms}"

    try:
        with psycopg.connect(
            raw,
            row_factory=dict_row,
            connect_timeout=30,
            options=_pg_opts,
        ) as conn:
            conn.read_only = True
            conn.execute("SELECT 1")
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        print(f"Postgres (DATABASE_URL): {err}", file=sys.stderr)
        return [], f"error:{err}"

    include_raw = (os.getenv("VITUAL_POSTGRES_TOOLS") or "").strip()
    if include_raw:
        requested = {x.strip() for x in include_raw.split(",") if x.strip()}
        unknown = requested - set(_POSTGRES_TOOL_NAMES_ALL)
        if unknown:
            print(
                f"PostgresTools: nomes desconhecidos em VITUAL_POSTGRES_TOOLS (ignorados): {unknown}",
                file=sys.stderr,
            )
        include_tools = sorted(requested & set(_POSTGRES_TOOL_NAMES_ALL))
        if not include_tools:
            include_tools = list(_POSTGRES_TOOL_NAMES_ALL)
    else:
        include_tools = list(_POSTGRES_TOOL_NAMES_ALL)

    class VitualPostgresTools(PostgresTools):
        """Ligação criada no thread que executa a tool (compatível com Agno async + to_thread)."""

        def __init__(
            self,
            *,
            dsn: str,
            table_schema: str,
            include_tools: list[str],
            statement_timeout_ms: int,
        ):
            self._dsn = dsn
            self._statement_timeout_ms = max(1000, min(int(statement_timeout_ms), 600_000))
            super().__init__(
                connection=None,
                table_schema=table_schema,
                include_tools=include_tools,
            )

        def connect(self):
            if self._connection is not None and not self._connection.closed:
                return self._connection
            st = self._statement_timeout_ms
            opts = f"-c search_path={self.table_schema} -c statement_timeout={st}"
            self._connection = psycopg.connect(
                self._dsn,
                row_factory=dict_row,
                connect_timeout=30,
                options=opts,
            )
            self._connection.read_only = True
            return self._connection

    print(
        f"PostgresTools: schema={schema} (read-only), tools={include_tools}",
        flush=True,
    )
    return [
        VitualPostgresTools(
            dsn=raw,
            table_schema=schema,
            include_tools=include_tools,
            statement_timeout_ms=st_ms,
        )
    ], "active"


def _supabase_rest_enabled() -> bool:
    return os.getenv("VITUAL_SUPABASE_REST", "0").strip().lower() in ("1", "true", "yes")


def vitual_supabase_rest_tools() -> list:
    """
    PostgREST do Supabase via CustomApiTools (make_request).
    Anon: respeita RLS. Service role: só com VITUAL_SUPABASE_USE_SERVICE_ROLE=1.
    """
    if not _supabase_rest_enabled():
        return []
    base = _env_first("SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_URL", "VITE_SUPABASE_URL").rstrip("/")
    if not base:
        return []
    use_svc = os.getenv("VITUAL_SUPABASE_USE_SERVICE_ROLE", "0").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    key = (
        _env_first("SUPABASE_SERVICE_ROLE_KEY")
        if use_svc
        else _env_first("SUPABASE_ANON_KEY", "NEXT_PUBLIC_SUPABASE_ANON_KEY")
    )
    if not key:
        key = _env_first("SUPABASE_SERVICE_ROLE_KEY")
    if not key:
        return []
    hdrs = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    try:
        from agno.tools.api import CustomApiTools

        t = CustomApiTools(
            base_url=f"{base}/rest/v1",
            headers=hdrs,
            enable_make_request=True,
            verify_ssl=True,
            timeout=60,
        )
        mode = "service_role" if use_svc else "anon"
        print(f"CustomApiTools: Supabase PostgREST ({mode}) -> {base}/rest/v1", flush=True)
        return [t]
    except ImportError:
        print("CustomApiTools: instale requests (pip install requests)", file=sys.stderr)
        return []
    except Exception as e:
        print(f"CustomApiTools: {e}", file=sys.stderr)
        return []


def _local_pdf_enabled() -> bool:
    """PDF local (FileGenerationTools): VITUAL_LOCAL_PDF=0 desliga; omitido ou 1 = ligado."""
    local_pdf_raw = (os.getenv("VITUAL_LOCAL_PDF") or "").strip().lower()
    if local_pdf_raw in ("0", "false", "no"):
        return False
    return True


def vitual_file_generation_tools() -> list:
    """PDF/CSV/JSON/TXT via Agno FileGenerationTools; ficheiros em agents/exports/."""
    off = os.getenv("VITUAL_FILE_EXPORT", "1").strip().lower() in ("0", "false", "no")
    if off:
        return []
    pdf_on = _local_pdf_enabled()
    try:
        _EXPORTS.mkdir(parents=True, exist_ok=True)
        tools = FileGenerationTools(
            output_directory=str(_EXPORTS),
            enable_json_generation=True,
            enable_csv_generation=True,
            enable_pdf_generation=pdf_on,
            enable_txt_generation=True,
        )
        print(
            f"FileGenerationTools: JSON/CSV/TXT{' + PDF' if pdf_on else ' (PDF local desligado)'} -> {_EXPORTS}",
            flush=True,
        )
        return [tools]
    except Exception as e:
        print(f"FileGenerationTools: {e}", file=sys.stderr)
        return []


def build_vitual_os() -> tuple[AgentOS, object, list, str]:
    """
    Cria Agent, AgentOS e a app ASGI.
    Devolve (agent_os, app, db_tools, db_tools_state) com db_tools_state em active|missing_url|error:...
    Sai com código 1 se MISTRAL_API_KEY estiver em falta.
    """
    key = os.getenv("MISTRAL_API_KEY", "").strip()
    if not key:
        print(
            "MISTRAL_API_KEY em falta. No Render: Dashboard → Environment → adicionar MISTRAL_API_KEY. "
            "Local: ficheiro .env na mesma pasta que run.py.",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)

    model_id = os.getenv("MISTRAL_MODEL", "mistral-large-latest").strip()
    sync, async_ = mistral_http_clients()
    db_tools, db_tools_state = crm_postgres_tools()

    _m_kw: dict = {
        "id": model_id,
        "api_key": key,
        "client_params": {"client": sync, "async_client": async_},
    }
    _tsec = (os.getenv("MISTRAL_CLIENT_TIMEOUT_SEC") or "").strip()
    if _tsec:
        try:
            _m_kw["timeout"] = max(30, int(_tsec))
        except ValueError:
            pass
    tool_choice: str | None = (os.getenv("AGENT_TOOL_CHOICE") or os.getenv("MISTRAL_TOOL_CHOICE") or "").strip() or None
    if not db_tools:
        tool_choice = None

    base_instructions = (
        "Assistente Vitual; responde em português quando o utilizador falar português. "
        "Em conversas gerais (saudações, explicações, opiniões, ideias) responde direto em texto "
        "sem chamar ferramentas. "
        "Se o utilizador perguntar por tabelas, dados, SQL, esquema, contagens ou 'o que existe na BD', "
        "tens de usar as ferramentas PostgreSQL (começa por show_tables); não inventes nomes de tabelas. "
        "Usa ferramentas também quando precisares de dados da base/API ou de gerar ficheiros a pedido explícito."
    )
    if db_tools:
        base_instructions += (
            " Tens acesso à base de dados do projeto via ferramentas PostgreSQL (só leitura): "
            "show_tables, describe_table, summarize_table, inspect_query (EXPLAIN), run_query (SELECT), "
            "export_table_to_path (exporta tabela para CSV — usa caminho dentro da pasta exports do projeto, "
            f"ex.: {_EXPORTS.as_posix()}/nome_tabela.csv). "
            "NUNCA digas que não tens acesso à BD quando estas ferramentas existem. "
            "Para perguntas sobre dados: começa por show_tables; run_query só SELECT com LIMIT em tabelas grandes."
        )

    file_tools = vitual_file_generation_tools()
    rest_tools = vitual_supabase_rest_tools()

    if file_tools:
        pdf_avail = _local_pdf_enabled()
        if pdf_avail:
            base_instructions += (
                " Ficheiros locais (pasta exports): generate_pdf_file (PDF), generate_csv_file, generate_json_file, "
                "generate_text_file."
            )
        else:
            base_instructions += (
                " Ficheiros locais (pasta exports): generate_csv_file, generate_json_file, generate_text_file. "
                "PDF local está desligado (VITUAL_LOCAL_PDF=0)."
            )

    if rest_tools:
        base_instructions += (
            " Tens também make_request sobre a API REST PostgREST do Supabase (base /rest/v1): "
            "usa GET com endpoint tipo nome_tabela?select=*&limit=20. Cabeçalhos já incluem autenticação. "
            "Com chave anon respeita-se RLS; não faças PATCH/DELETE sem o utilizador pedir explicitamente."
        )

    all_tools = [*db_tools, *file_tools, *rest_tools]

    try:
        _hr = int((os.getenv("VITUAL_NUM_HISTORY_RUNS") or "5").strip())
        num_history_runs = max(1, min(_hr, 30))
    except ValueError:
        num_history_runs = 5

    agents_list: list[Agent] = [
        Agent(
            id="chat",
            name="Chat",
            model=MistralChat(**_m_kw),
            instructions=base_instructions,
            tools=all_tools,
            tool_choice=tool_choice,
            markdown=True,
            add_history_to_context=True,
            num_history_runs=num_history_runs,
            db=SqliteDb(db_file=str(_ROOT / "agno.db")),
            debug_mode=_debug,
        )
    ]

    os_description = (
        "Vitual AgentOS — agente único (chat): PostgresTools (opcional) + exportação de ficheiros + "
        "PostgREST (opcional, VITUAL_SUPABASE_REST=1)."
    )

    agent_os = AgentOS(
        name="vitual",
        description=os_description,
        agents=agents_list,
        tracing=False,
    )
    app = agent_os.get_app()
    return agent_os, app, db_tools, db_tools_state
