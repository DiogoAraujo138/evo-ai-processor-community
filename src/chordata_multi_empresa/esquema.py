"""Instala/remove o isolamento por empresa nas tabelas do Processor (e, à parte, nas do Core).

Mesma receita do CRM (ADR-0007): ``empresa_id`` NOT NULL com FK, ``DEFAULT chordata_empresa_atual()``
(o banco carimba a empresa na criação), RLS forçado e uma política única. Roda em uma transação e é
idempotente. Pressupõe que o instalador do CRM já criou ``empresas`` e ``empresa_usuarios``
(o banco é o mesmo, ``evo_community``).

Dois grupos, de propósito:
  - PROCESSADOR: sessões, metadados, métricas e as tabelas do ADK. Só o Processor as usa.
  - NUCLEO: agentes, pastas, chaves e credenciais (``evo_core_*``). São do **Core (Go)**, que ainda não
    sabe informar a empresa. Instalar este grupo antes do Core estar pronto faz o Core parar de enxergar
    seus próprios dados. Por isso só entra com ``incluir_nucleo=True`` (``--nucleo`` na linha de comando).
"""

from __future__ import annotations

from typing import Iterable

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

SLUG_PADRAO = "padrao"
POLITICA = "isolamento_por_empresa"

PROCESSADOR = (
    "evo_ai_agent_processor_sessions",
    "evo_ai_agent_processor_session_metadata",
    "evo_ai_agent_processor_execution_metrics",
    "evo_agent_processor_execution_metrics",  # versão antiga da mesma tabela (ainda existe em bancos antigos)
)

# Tabelas do ADK (DatabaseSessionService): o histórico das conversas. ``app_states`` fica de fora: a chave
# é só o nome do app (a PK colidiria entre empresas) e guarda estado do app, não conversa.
ADK = ("sessions", "events", "user_states")

NUCLEO = (
    "evo_core_folders",
    "evo_core_agent_folders",
    "evo_core_folder_shares",
    "evo_core_agents",
    "evo_core_agent_integrations",
    "evo_core_api_keys",
    "evo_core_custom_mcp_servers",
    "evo_core_custom_tools",
    "evo_core_integration_credentials",
)
# evo_core_mcp_servers é o catálogo de servidores MCP pré-cadastrados (sementes do Core): continua global.

# Únicos que NÃO viram "por empresa": já são relativos a um registro que tem empresa.
UNICOS_FIXOS = {"unique_agent_integration"}


def tabelas(incluir_nucleo: bool = False) -> tuple[str, ...]:
    return PROCESSADOR + ADK + (NUCLEO if incluir_nucleo else ())


def _q(conn: Connection, nome: str) -> str:
    return conn.dialect.identifier_preparer.quote(nome)


def _existe(conn: Connection, tabela: str) -> bool:
    return conn.execute(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": f"public.{tabela}"}).scalar()


def _criar_tabelas_do_adk(conn: Connection) -> None:
    """O ADK cria as tabelas só no primeiro uso; cria agora para não deixar nenhuma sem proteção."""
    try:
        from google.adk.sessions import database_session_service as adk

        adk.Base.metadata.create_all(conn, checkfirst=True)
    except Exception:  # noqa: BLE001 - sem o ADK instalado (ex.: testes) as tabelas simplesmente não existem
        pass


def _exigir_base_do_crm(conn: Connection) -> None:
    faltando = [t for t in ("empresas", "empresa_usuarios") if not _existe(conn, t)]
    if faltando:
        raise RuntimeError(
            "Faltam as tabelas " + ", ".join(faltando) + ". Rode antes o instalador do CRM "
            "(rake chordata:multi_empresa:instalar): ele cria as empresas e o vínculo dos usuários."
        )
    padrao = conn.execute(text("SELECT id FROM empresas WHERE slug = :s"), {"s": SLUG_PADRAO}).scalar()
    if padrao is None:
        raise RuntimeError(f"Empresa padrão (slug '{SLUG_PADRAO}') não encontrada.")


def instalar(engine: Engine, incluir_nucleo: bool = False) -> dict:
    """Devolve ``{"isoladas": [...], "ausentes": [...]}``."""
    isoladas: list[str] = []
    ausentes: list[str] = []
    with engine.begin() as conn:
        _exigir_base_do_crm(conn)
        _criar_tabelas_do_adk(conn)
        conn.execute(text(
            "CREATE OR REPLACE FUNCTION chordata_empresa_atual() RETURNS uuid LANGUAGE sql STABLE "
            "AS $$ SELECT NULLIF(current_setting('app.empresa_id', true), '')::uuid $$"
        ))
        for tabela in tabelas(incluir_nucleo):
            if not _existe(conn, tabela):
                ausentes.append(tabela)
                continue
            _isolar(conn, tabela)
            isoladas.append(tabela)
        _converter_unicos(conn, isoladas)
    return {"isoladas": isoladas, "ausentes": ausentes}


def remover(engine: Engine, incluir_nucleo: bool = False) -> dict:
    removidas: list[str] = []
    with engine.begin() as conn:
        existentes = [t for t in tabelas(incluir_nucleo) if _existe(conn, t)]
        _restaurar_unicos(conn, existentes)
        for tabela in existentes:
            t = _q(conn, tabela)
            conn.execute(text(f"DROP POLICY IF EXISTS {POLITICA} ON {t}"))
            conn.execute(text(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY"))
            conn.execute(text(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY"))
            conn.execute(text(f"ALTER TABLE {t} DROP COLUMN IF EXISTS empresa_id CASCADE"))
            removidas.append(tabela)
    return {"removidas": removidas}


def conceder(engine: Engine, papel: str) -> None:
    """Dá ao papel da aplicação (sem superusuário) acesso às tabelas existentes e às futuras."""
    with engine.begin() as conn:
        p = _q(conn, papel)
        conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {p}"))
        conn.execute(text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {p}"))
        conn.execute(text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {p}"))


def _isolar(conn: Connection, tabela: str) -> None:
    t = _q(conn, tabela)
    conn.execute(text(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS empresa_id uuid"))
    conn.execute(text(
        f"UPDATE {t} SET empresa_id = (SELECT id FROM empresas WHERE slug = :s) WHERE empresa_id IS NULL"
    ), {"s": SLUG_PADRAO})
    conn.execute(text(f"ALTER TABLE {t} ALTER COLUMN empresa_id SET NOT NULL"))
    conn.execute(text(f"ALTER TABLE {t} ALTER COLUMN empresa_id SET DEFAULT chordata_empresa_atual()"))
    conn.execute(text(f"CREATE INDEX IF NOT EXISTS {_q(conn, f'index_{tabela}_on_empresa_id')} ON {t} (empresa_id)"))
    fk = f"fk_{tabela}_empresa"
    if not conn.execute(text("SELECT 1 FROM pg_constraint WHERE conname = :n"), {"n": fk}).scalar():
        conn.execute(text(f"ALTER TABLE {t} ADD CONSTRAINT {_q(conn, fk)} FOREIGN KEY (empresa_id) REFERENCES empresas(id)"))
    conn.execute(text(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY"))
    conn.execute(text(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY"))
    conn.execute(text(f"DROP POLICY IF EXISTS {POLITICA} ON {t}"))
    conn.execute(text(
        f"CREATE POLICY {POLITICA} ON {t} USING (empresa_id = chordata_empresa_atual()) "
        "WITH CHECK (empresa_id = chordata_empresa_atual())"
    ))


def _unicos(conn: Connection, tabela: str):
    """Índices únicos (fora da PK) da tabela: (nome, definição, constraint que o sustenta ou None)."""
    return conn.execute(text("""
        SELECT i.relname, pg_get_indexdef(ix.indexrelid), c.conname
        FROM pg_index ix
        JOIN pg_class t ON t.oid = ix.indrelid
        JOIN pg_class i ON i.oid = ix.indexrelid
        LEFT JOIN pg_constraint c ON c.conindid = ix.indexrelid AND c.contype = 'u'
        WHERE t.relname = :t AND t.relnamespace = 'public'::regnamespace
          AND ix.indisunique AND NOT ix.indisprimary
    """), {"t": tabela}).all()


def _converter_unicos(conn: Connection, isoladas: Iterable[str]) -> None:
    """Unicidade de negócio (ex.: nome do agente) passa a valer POR EMPRESA."""
    for tabela in isoladas:
        for nome, definicao, constraint in _unicos(conn, tabela):
            if nome in UNICOS_FIXOS or nome.endswith("_empresa"):
                continue
            novo = definicao.replace("USING btree (", "USING btree (empresa_id, ", 1)
            novo = novo.replace(f"INDEX {nome} ON", f"INDEX {_q(conn, nome + '_empresa')} ON", 1)
            if constraint:
                conn.execute(text(f"ALTER TABLE {_q(conn, tabela)} DROP CONSTRAINT {_q(conn, constraint)}"))
            else:
                conn.execute(text(f"DROP INDEX IF EXISTS {_q(conn, nome)}"))
            conn.execute(text(novo))


def _restaurar_unicos(conn: Connection, existentes: Iterable[str]) -> None:
    for tabela in existentes:
        for nome, definicao, _ in _unicos(conn, tabela):
            if not nome.endswith("_empresa"):
                continue
            original = nome[: -len("_empresa")]
            antigo = definicao.replace("USING btree (empresa_id, ", "USING btree (", 1)
            antigo = antigo.replace(f"INDEX {nome} ON", f"INDEX {_q(conn, original)} ON", 1)
            conn.execute(text(f"DROP INDEX IF EXISTS {_q(conn, nome)}"))
            try:
                with conn.begin_nested():
                    conn.execute(text(antigo))
            except IntegrityError as e:
                raise RuntimeError(
                    f"Não dá para voltar ao único global de '{tabela}' ({original}): empresas diferentes já usam "
                    "o mesmo valor. Resolva os repetidos e rode de novo (nada foi alterado)."
                ) from e


def diagnosticar(conn: Connection) -> dict:
    """``erros`` impedem ligar o multi-empresa; ``avisos`` são lacunas conhecidas."""
    erros: list[str] = []
    avisos: list[str] = []

    papel = conn.execute(text(
        "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
    )).one()
    if papel.rolsuper or papel.rolbypassrls:
        erros.append(
            f"O papel '{papel.rolname}' é superusuário ou ignora o RLS: o isolamento não vale. "
            "Conecte a aplicação com um papel comum (NOSUPERUSER NOBYPASSRLS)."
        )

    def protegida(tabela: str) -> bool:
        linha = conn.execute(text("""
            SELECT c.relrowsecurity, c.relforcerowsecurity,
                   EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attname = 'empresa_id' AND NOT a.attisdropped),
                   EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid AND p.polname = :p)
            FROM pg_class c WHERE c.oid = to_regclass(:t)
        """), {"t": f"public.{tabela}", "p": POLITICA}).one()
        return all(linha)

    for tabela in PROCESSADOR + ADK:
        if not _existe(conn, tabela):
            if tabela in ADK:
                avisos.append(f"Tabela do ADK '{tabela}' ainda não existe; rode o instalador de novo depois do primeiro uso.")
            continue
        if not protegida(tabela):
            erros.append(f"'{tabela}' sem isolamento por empresa (rode o instalador).")

    for tabela in NUCLEO:
        if _existe(conn, tabela) and not protegida(tabela):
            avisos.append(f"'{tabela}' (Core) sem isolamento: agentes/credenciais são visíveis a todas as empresas.")
    return {"erros": erros, "avisos": avisos}
