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
from agno.tools import Toolkit
from agno.tools.file_generation import FileGenerationTools

_ROOT = Path(__file__).resolve().parent
_EXPORTS = _ROOT / "exports"

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


def _fetch_playbook_markdown(url: str | None, *, max_bytes: int) -> str | None:
    u = (url or "").strip()
    if not u:
        return None
    try:
        with httpx.Client(timeout=60.0, verify=_httpx_verify_ssl()) as client:
            r = client.get(
                u,
                headers={"Accept": "text/markdown, text/plain, application/json, */*"},
            )
        if r.status_code != 200:
            print(
                f"Hub playbook HTTP {r.status_code} para {u[:120]}…",
                file=sys.stderr,
            )
            return None
        body = r.content
        if len(body) > max_bytes:
            print(
                f"Hub playbook excede {max_bytes} bytes ({len(body)}); truncando.",
                file=sys.stderr,
            )
            body = body[:max_bytes]
        return body.decode("utf-8", errors="replace")
    except Exception as e:
        print(f"Hub playbook: falha ao ler URL: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def _load_hub_agents_rows() -> list[dict]:
    """
    Lê agentes do Hub para um AgentOS multi-agente (id Agno = agente_slug).

    Controlos (``agents/.env``):
      VITUAL_AGENTOS_FROM_HUB — 0 desliga (só agente ``chat`` legacy).
      VITUAL_AGENTOS_TENANT_ID ou DEFAULT_TENANT_ID — filtra ``tenant_id`` (uuid).
      VITUAL_AGENTOS_SLUGS — lista separada por vírgulas (subconjunto).
    """
    if os.getenv("VITUAL_AGENTOS_FROM_HUB", "1").strip().lower() in ("0", "false", "no"):
        return []
    dsn = _postgres_dsn_normalized()
    if not dsn:
        return []
    try:
        import psycopg
        from psycopg import errors as pg_errors
        from psycopg.rows import dict_row
    except ImportError:
        print("Hub agentes: instale psycopg (pip install 'psycopg[binary]')", file=sys.stderr)
        return []

    schema, st_ms = _pg_schema_and_timeout_ms()
    opts = f"-c search_path={schema} -c statement_timeout={st_ms}"

    tenant = (os.getenv("VITUAL_AGENTOS_TENANT_ID") or os.getenv("DEFAULT_TENANT_ID") or "").strip()
    slugs_raw = (os.getenv("VITUAL_AGENTOS_SLUGS") or "").strip()
    wanted = {s.strip() for s in slugs_raw.split(",") if s.strip()} if slugs_raw else None

    def _run_sql(
        sql: str,
        params: tuple | list | None,
    ) -> list[dict]:
        with psycopg.connect(
            dsn,
            row_factory=dict_row,
            connect_timeout=30,
            options=opts,
        ) as conn:
            conn.read_only = True
            cur = conn.execute(sql, params or ())
            return list(cur.fetchall())

    sql_with_arch = """
        SELECT agente_slug, nome, system_prompt_base, playbook_public_url, modo_operacao
        FROM hub_agente_identidade
        WHERE ativo IS NOT FALSE
          AND arquivado_em IS NULL
    """
    params: list = []
    if tenant:
        sql_with_arch += " AND tenant_id = %s::uuid"
        params.append(tenant)
    sql_with_arch += " ORDER BY nivel NULLS LAST, nome NULLS LAST, agente_slug"

    sql_min = """
        SELECT agente_slug, nome, system_prompt_base, playbook_public_url, modo_operacao
        FROM hub_agente_identidade
        WHERE ativo IS NOT FALSE
        ORDER BY nivel NULLS LAST, nome NULLS LAST, agente_slug
    """
    sql_arch_no_tenant = """
        SELECT agente_slug, nome, system_prompt_base, playbook_public_url, modo_operacao
        FROM hub_agente_identidade
        WHERE ativo IS NOT FALSE
          AND arquivado_em IS NULL
        ORDER BY nivel NULLS LAST, nome NULLS LAST, agente_slug
    """
    rows: list[dict] = []
    try:
        rows = _run_sql(sql_with_arch, params)
    except pg_errors.UndefinedColumn:
        try:
            rows = _run_sql(sql_arch_no_tenant, ())
        except pg_errors.UndefinedColumn:
            try:
                rows = _run_sql(sql_min, ())
            except Exception as e:
                print(f"Hub agentes: {type(e).__name__}: {e}", file=sys.stderr)
                return []
        except Exception as e:
            print(f"Hub agentes: {type(e).__name__}: {e}", file=sys.stderr)
            return []
    except Exception as e:
        print(f"Hub agentes: {type(e).__name__}: {e}", file=sys.stderr)
        return []

    if wanted:
        rows = [r for r in rows if (r.get("agente_slug") or "") in wanted]

    if not rows:
        print("Hub agentes: nenhuma linha em hub_agente_identidade (filtros activos).", flush=True)
    else:
        print(f"Hub agentes: {len(rows)} agente(s) para AgentOS.", flush=True)
    return rows


def _hub_playbook_max_bytes() -> int:
    try:
        n = int((os.getenv("VITUAL_AGENTOS_PLAYBOOK_MAX_BYTES") or "500000").strip())
        return max(8_192, min(n, 5_000_000))
    except ValueError:
        return 500_000


def _instructions_hub_agent(
    base_instructions: str,
    *,
    agente_slug: str,
    nome: str,
    modo_operacao: str | None,
    playbook_md: str | None,
    system_prompt_base: str | None,
) -> str:
    modo = (modo_operacao or "").strip()
    block = (
        f"\n\n## Contexto CRM (Hub)\n"
        f"- **agente_slug:** `{agente_slug}`\n"
        f"- **nome:** {nome.strip() or agente_slug}\n"
    )
    if modo:
        block += f"- **modo_operacao:** `{modo}`\n"
    parts = [base_instructions, block]
    if playbook_md and playbook_md.strip():
        parts.append("\n---\n\n# Playbook (Markdown do Hub)\n\n")
        parts.append(playbook_md.strip())
    elif system_prompt_base and str(system_prompt_base).strip():
        parts.append("\n---\n\n# Prompt base (Hub)\n\n")
        parts.append(str(system_prompt_base).strip())
    else:
        parts.append(
            "\n\n_(Este agente não tem playbook_public_url nem system_prompt_base preenchido no Hub; "
            "segue só as regras globais e ferramentas.)_"
        )
    return "".join(parts)


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


def _httpx_verify_ssl() -> bool | str | ssl.SSLContext:
    """Verificação SSL para clientes httpx síncronos (alinhado com mistral_http_clients)."""
    if os.getenv("AGNO_HTTP_VERIFY", "1").strip().lower() in ("0", "false", "no"):
        return False
    prefer_certifi = os.getenv("AGNO_SSL_PREFER_CERTIFI", "0").strip().lower() in ("1", "true", "yes")
    legacy_certifi = os.getenv("AGNO_SSL_USE_TRUSTSTORE", "").strip().lower() in ("0", "false", "no")
    prefer_certifi = prefer_certifi or legacy_certifi
    if not prefer_certifi:
        try:
            import truststore

            return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception:
            prefer_certifi = True
    if prefer_certifi:
        import certifi

        cafile = certifi.where()
        os.environ.setdefault("SSL_CERT_FILE", cafile)
        os.environ.setdefault("REQUESTS_CA_BUNDLE", cafile)
        return cafile
    return True


class VitualSupabaseStorageToolkit(Toolkit):
    """Relatórios Markdown → Supabase Storage (POST /storage/v1/object/...)."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        bucket: str,
        path_prefix: str = "",
        httpx_verify: bool | str | ssl.SSLContext = True,
        public_read_hint: bool = False,
    ):
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._bucket = bucket.strip()
        self._prefix = path_prefix.strip().strip("/")
        self._httpx_verify = httpx_verify
        self._public_read_hint = public_read_hint
        super().__init__(
            name="vitual_supabase_storage",
            tools=[self.upload_markdown_report],
        )

    def _object_key(self, filename: str) -> tuple[str | None, str | None]:
        raw = (filename or "").strip()
        if not raw:
            return None, "filename vazio"
        base_name = Path(raw).name
        if not base_name or base_name in (".", ".."):
            return None, "filename inválido"
        safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in base_name)
        if not safe.lower().endswith(".md"):
            safe += ".md"
        if self._prefix:
            return f"{self._prefix}/{safe}", None
        return safe, None

    def upload_markdown_report(self, markdown: str, filename: str) -> str:
        """
        Grava um relatório em Markdown no bucket Supabase configurado.

        Args:
            markdown: Texto completo do relatório (títulos #, listas, tabelas Markdown).
            filename: Nome do ficheiro, ex. relatorio_leads_maio.md (só o basename é usado; é sanitizado).
        """
        key, err = self._object_key(filename)
        if not key:
            return f"Error: {err}"
        if not (markdown and str(markdown).strip()):
            return "Error: markdown vazio"
        url = f"{self._base}/storage/v1/object/{self._bucket}/{key}"
        headers = {
            "Authorization": f"Bearer {self._key}",
            "apikey": self._key,
            "Content-Type": "text/markdown; charset=utf-8",
        }
        try:
            with httpx.Client(timeout=120.0, verify=self._httpx_verify) as client:
                r = client.post(
                    url,
                    headers=headers,
                    content=str(markdown).encode("utf-8"),
                    params={"upsert": "true"},
                )
        except Exception as e:
            return f"Error: pedido HTTP falhou: {type(e).__name__}: {e}"
        if r.status_code not in (200, 201):
            return f"Error: Storage HTTP {r.status_code}: {r.text[:800]}"
        extra = ""
        if self._public_read_hint:
            extra = (
                f" | URL pública (se o bucket for público): "
                f"{self._base}/storage/v1/object/public/{self._bucket}/{key}"
            )
        return f"OK: Markdown gravado no Storage em {self._bucket}/{key}.{extra}"


def vitual_supabase_storage_tools() -> list:
    """Upload de relatórios .md para Supabase Storage.

    Activo se ``VITUAL_SUPABASE_STORAGE_BUCKET`` estiver definido e
    ``VITUAL_SUPABASE_STORAGE`` não for 0/false/no.
    """
    if os.getenv("VITUAL_SUPABASE_STORAGE", "").strip().lower() in ("0", "false", "no"):
        return []
    bucket = (os.getenv("VITUAL_SUPABASE_STORAGE_BUCKET") or "").strip()
    if not bucket:
        return []
    base = _env_first("SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_URL", "VITE_SUPABASE_URL").rstrip("/")
    if not base:
        print(
            "Supabase Storage: falta SUPABASE_URL ou NEXT_PUBLIC_SUPABASE_URL",
            file=sys.stderr,
        )
        return []
    use_svc = os.getenv("VITUAL_SUPABASE_STORAGE_USE_SERVICE_ROLE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if not use_svc:
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
        print("Supabase Storage: falta SUPABASE_SERVICE_ROLE_KEY ou SUPABASE_ANON_KEY", file=sys.stderr)
        return []
    prefix = (os.getenv("VITUAL_SUPABASE_STORAGE_PREFIX") or "relatorios").strip().strip("/")
    public_hint = os.getenv("VITUAL_SUPABASE_STORAGE_PUBLIC_READ", "0").strip().lower() in ("1", "true", "yes")
    t = VitualSupabaseStorageToolkit(
        base_url=base,
        api_key=key,
        bucket=bucket,
        path_prefix=prefix,
        httpx_verify=_httpx_verify_ssl(),
        public_read_hint=public_hint,
    )
    print(
        f"Supabase Storage: bucket={bucket} prefix={prefix or '(raiz)'} auth={'service_role' if use_svc else 'anon'}",
        flush=True,
    )
    return [t]


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
    """PDF local: desligado por defeito quando há bucket Storage configurado (e Storage não está explicitamente off)."""
    bucket_cfg = (os.getenv("VITUAL_SUPABASE_STORAGE_BUCKET") or "").strip()
    storage_explicit_off = os.getenv("VITUAL_SUPABASE_STORAGE", "").strip().lower() in ("0", "false", "no")
    local_pdf_raw = (os.getenv("VITUAL_LOCAL_PDF") or "").strip().lower()
    if local_pdf_raw in ("1", "true", "yes"):
        return True
    if local_pdf_raw in ("0", "false", "no"):
        return False
    if storage_explicit_off:
        return True
    return not bool(bucket_cfg)


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

    storage_tools = vitual_supabase_storage_tools()
    file_tools = vitual_file_generation_tools()
    rest_tools = vitual_supabase_rest_tools()

    if storage_tools:
        base_instructions += (
            " REGRA OBRIGATÓRIA — relatórios na nuvem: tens a ferramenta upload_markdown_report (bucket Supabase). "
            "Sempre que o utilizador pedir relatório, exportação, sumário guardado ou guardar no bucket, "
            "compõe o relatório em Markdown completo (# títulos, listas, tabelas em MD) e chama "
            "upload_markdown_report com o texto completo e filename terminado em .md (ex.: relatorio_leads_maio.md). "
            "NÃO uses generate_pdf_file para esse pedido; PDF local só se o utilizador pedir explicitamente PDF. "
            "Na resposta ao utilizador, menciona o caminho no bucket que a tool devolver (linha OK:)."
        )

    if file_tools:
        pdf_avail = _local_pdf_enabled()
        if pdf_avail:
            base_instructions += (
                " Ficheiros locais (pasta exports): generate_pdf_file (PDF), generate_csv_file, generate_json_file, "
                "generate_text_file. Se o Storage Markdown estiver disponível, preferência para relatórios é "
                "upload_markdown_report, não PDF."
            )
        else:
            base_instructions += (
                " Ficheiros locais (pasta exports): generate_csv_file, generate_json_file, generate_text_file. "
                "PDF local está desligado; relatórios vão em Markdown com upload_markdown_report."
            )

    if rest_tools:
        base_instructions += (
            " Tens também make_request sobre a API REST PostgREST do Supabase (base /rest/v1): "
            "usa GET com endpoint tipo nome_tabela?select=*&limit=20. Cabeçalhos já incluem autenticação. "
            "Com chave anon respeita-se RLS; não faças PATCH/DELETE sem o utilizador pedir explicitamente."
        )

    if storage_tools:
        base_instructions += (
            " O caminho no Supabase usa o prefixo do servidor; não digas que gravaste em C:... para arquivo na nuvem."
        )

    all_tools = [*db_tools, *file_tools, *rest_tools, *storage_tools]

    try:
        _hr = int((os.getenv("VITUAL_NUM_HISTORY_RUNS") or "5").strip())
        num_history_runs = max(1, min(_hr, 30))
    except ValueError:
        num_history_runs = 5

    hub_rows = _load_hub_agents_rows()
    playbook_max = _hub_playbook_max_bytes()
    sessions_dir = _ROOT / "agno_sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    agents_list: list[Agent] = []
    first_hub_instructions: str | None = None

    if hub_rows:
        seen_ids: set[str] = set()
        for row in hub_rows:
            slug = str(row.get("agente_slug") or "").strip()
            if not slug or slug in seen_ids:
                continue
            seen_ids.add(slug)
            nome = str(row.get("nome") or slug).strip() or slug
            display_name = nome[:120] if len(nome) > 120 else nome
            url = row.get("playbook_public_url")
            playbook = _fetch_playbook_markdown(
                str(url) if url else None,
                max_bytes=playbook_max,
            )
            instr = _instructions_hub_agent(
                base_instructions,
                agente_slug=slug,
                nome=nome,
                modo_operacao=row.get("modo_operacao"),
                playbook_md=playbook,
                system_prompt_base=row.get("system_prompt_base"),
            )
            if first_hub_instructions is None:
                first_hub_instructions = instr
            db_path = sessions_dir / f"{slug}.db"
            agents_list.append(
                Agent(
                    id=slug,
                    name=display_name,
                    model=MistralChat(**_m_kw),
                    instructions=instr,
                    tools=all_tools,
                    tool_choice=tool_choice,
                    markdown=True,
                    add_history_to_context=True,
                    num_history_runs=num_history_runs,
                    db=SqliteDb(db_file=str(db_path)),
                    debug_mode=_debug,
                )
            )

    legacy_chat = os.getenv("VITUAL_AGENTOS_LEGACY_CHAT", "1").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if agents_list and legacy_chat and first_hub_instructions:
        if not any(a.id == "chat" for a in agents_list):
            agents_list.append(
                Agent(
                    id="chat",
                    name="Chat",
                    model=MistralChat(**_m_kw),
                    instructions=first_hub_instructions,
                    tools=all_tools,
                    tool_choice=tool_choice,
                    markdown=True,
                    add_history_to_context=True,
                    num_history_runs=num_history_runs,
                    db=SqliteDb(db_file=str(_ROOT / "agno.db")),
                    debug_mode=_debug,
                )
            )

    if not agents_list:
        agents_list = [
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
        f"Vitual AgentOS — {len(agents_list)} agente(s): "
        + ", ".join(a.id for a in agents_list)
        + ". Postgres + ficheiros + opcional REST + Markdown → Storage."
    )

    agent_os = AgentOS(
        name="vitual",
        description=os_description,
        agents=agents_list,
        tracing=False,
    )
    app = agent_os.get_app()
    return agent_os, app, db_tools, db_tools_state
