"""Multi-empresa Chordata no Postgres de verdade.

Precisa de um banco DESCARTÁVEL com o esquema do CRM já isolado (``empresas``) e de um papel comum
(NOSUPERUSER NOBYPASSRLS). Sem as variáveis abaixo o módulo é ignorado (a suíte do upstream segue verde):

    PROCESSOR_TEST_ADMIN_URL=postgresql://postgres:...@host/evo_processor_tests   (dono: instala/remove)
    PROCESSOR_TEST_APP_URL=postgresql://evo_proc_teste_app:...@host/evo_processor_tests

NUNCA aponte para o banco de uso: ``remover`` e o TRUNCATE de preparação apagam dados.
"""

import asyncio
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

ADMIN = os.getenv("PROCESSOR_TEST_ADMIN_URL")
APP = os.getenv("PROCESSOR_TEST_APP_URL")
pytestmark = pytest.mark.skipif(not (ADMIN and APP), reason="defina PROCESSOR_TEST_ADMIN_URL e PROCESSOR_TEST_APP_URL")

A = "11111111-1111-1111-1111-111111111111"
B = "22222222-2222-2222-2222-222222222222"

LIMPAR = (
    "evo_ai_agent_processor_sessions", "evo_ai_agent_processor_session_metadata",
    "evo_ai_agent_processor_execution_metrics", "evo_agent_processor_execution_metrics",
    "evo_core_agent_integrations", "evo_core_agents", "evo_core_api_keys", "evo_core_folders",
    "evo_core_agent_folders", "evo_core_folder_shares", "evo_core_custom_mcp_servers",
    "evo_core_custom_tools", "evo_core_integration_credentials",
)


@pytest.fixture(scope="module")
def admin():
    from src.chordata_multi_empresa import esquema

    engine = create_engine(ADMIN)
    with engine.begin() as c:  # dados de execuções anteriores (inclusive repetidos entre empresas) vão embora antes
        c.execute(text("TRUNCATE " + ", ".join(LIMPAR) + " CASCADE"))
    esquema.remover(engine, incluir_nucleo=True)
    with engine.begin() as c:
        c.execute(text("TRUNCATE empresas CASCADE"))
        c.execute(text("INSERT INTO empresas (nome, slug) VALUES ('Empresa padrão', 'padrao')"))
        c.execute(text("INSERT INTO empresas (id, nome, slug) VALUES (:a, 'A', 'a'), (:b, 'B', 'b')"), {"a": A, "b": B})
    esquema.instalar(engine, incluir_nucleo=True)
    with engine.begin() as c:
        c.execute(text("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO evo_proc_teste_app"))
    yield engine
    engine.dispose()


@pytest.fixture(scope="module", autouse=True)
def gancho():
    from src.chordata_multi_empresa import banco

    os.environ["MULTI_EMPRESA"] = "on"
    banco.registrar_gancho()
    yield
    banco.remover_gancho()
    os.environ.pop("MULTI_EMPRESA", None)


@pytest.fixture
def db(admin):
    # pool de UMA conexão: se a empresa vazasse entre transações, estes testes pegariam
    engine = create_engine(APP, pool_size=1, max_overflow=0)
    yield sessionmaker(bind=engine)
    engine.dispose()


def contar(db, tabela, empresa):
    from src.chordata_multi_empresa import com_empresa

    with com_empresa(empresa), db() as s:
        return s.execute(text(f"SELECT count(*) FROM {tabela}")).scalar()


def test_papel_da_aplicacao_nao_ignora_o_rls(db):
    with db() as s:
        super_, bypass = s.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")).one()
    assert not super_ and not bypass


def test_instalar_e_idempotente(admin):
    from src.chordata_multi_empresa import esquema

    r = esquema.instalar(admin, incluir_nucleo=True)
    assert "evo_ai_agent_processor_sessions" in r["isoladas"] and "evo_core_agents" in r["isoladas"]


def test_sessoes_isoladas_por_empresa_com_o_modelo_real(db):
    from src.chordata_multi_empresa import com_empresa
    from src.models.models import Session as Sessao

    with com_empresa(A), db() as s:
        s.add(Sessao(id="s-a", app_name="app", user_id="u", state={}))
        s.commit()
    with com_empresa(B), db() as s:
        s.add(Sessao(id="s-b", app_name="app", user_id="u", state={}))
        s.commit()

    for empresa, esperado in ((A, "s-a"), (B, "s-b")):
        with com_empresa(empresa), db() as s:
            assert [x.id for x in s.query(Sessao).all()] == [esperado]
    with db() as s:  # sem empresa: nada (falha segura)
        assert s.query(Sessao).count() == 0


def test_empresa_nao_vaza_entre_transacoes_do_pool(db):
    assert contar(db, "evo_ai_agent_processor_sessions", A) == 1
    assert contar(db, "evo_ai_agent_processor_sessions", None) == 0  # mesma conexão, sem empresa
    assert contar(db, "evo_ai_agent_processor_sessions", B) == 1


def test_gravacao_sem_empresa_ou_em_empresa_alheia_falha(db):
    from src.chordata_multi_empresa import com_empresa

    with db() as s, pytest.raises(DBAPIError):
        s.execute(text("INSERT INTO evo_ai_agent_processor_sessions (id, app_name, user_id) VALUES ('x','a','u')"))
        s.commit()
    with com_empresa(A), db() as s, pytest.raises(DBAPIError):
        s.execute(
            text("INSERT INTO evo_ai_agent_processor_sessions (id, app_name, user_id, empresa_id) VALUES ('y','a','u', :b)"),
            {"b": B},
        )
        s.commit()


def test_nao_altera_nem_apaga_linha_de_outra_empresa(db):
    from src.chordata_multi_empresa import com_empresa

    with com_empresa(B), db() as s:
        r = s.execute(text("UPDATE evo_ai_agent_processor_sessions SET app_name = 'hack' WHERE id = 's-a'"))
        s.commit()
        assert r.rowcount == 0
        r = s.execute(text("DELETE FROM evo_ai_agent_processor_sessions WHERE id = 's-a'"))
        s.commit()
        assert r.rowcount == 0
    assert contar(db, "evo_ai_agent_processor_sessions", A) == 1


def test_agentes_isolados_e_nome_unico_por_empresa(db):
    from src.chordata_multi_empresa import com_empresa

    novo = "INSERT INTO evo_core_agents (name, type, card_url) VALUES (:n, 'llm', 'http://x')"
    for empresa in (A, B):  # o mesmo nome em duas empresas é permitido
        with com_empresa(empresa), db() as s:
            s.execute(text(novo), {"n": "atendente"})
            s.commit()
    with com_empresa(A), db() as s, pytest.raises(IntegrityError):  # mas não repetido na mesma
        s.execute(text(novo), {"n": "atendente"})
        s.commit()
    assert contar(db, "evo_core_agents", A) == 1 and contar(db, "evo_core_agents", B) == 1
    assert contar(db, "evo_core_agents", None) == 0


def test_credenciais_e_chaves_de_api_isoladas(db):
    from src.chordata_multi_empresa import com_empresa

    with com_empresa(A), db() as s:
        s.execute(text(
            "INSERT INTO evo_core_integration_credentials (name, provider, kind, value, scope) "
            "VALUES ('chave', 'openai', 'static', 'segredo', 'account')"
        ))
        s.commit()
    assert contar(db, "evo_core_integration_credentials", A) == 1
    assert contar(db, "evo_core_integration_credentials", B) == 0


def test_motor_assincrono_do_adk_tambem_recebe_a_empresa(admin):
    from src.chordata_multi_empresa import com_empresa

    async def rodar():
        engine = create_async_engine(APP.replace("postgresql://", "postgresql+asyncpg://", 1), pool_size=1, max_overflow=0)
        try:
            resultado = {}
            for nome, empresa in (("a", A), ("b", B), ("nenhuma", None), ("a2", A)):
                with com_empresa(empresa):
                    async with AsyncSession(engine) as s:
                        resultado[nome] = (await s.execute(
                            text("SELECT id FROM evo_ai_agent_processor_sessions ORDER BY id")
                        )).scalars().all()
            return resultado
        finally:
            await engine.dispose()

    r = asyncio.run(rodar())
    assert r == {"a": ["s-a"], "b": ["s-b"], "nenhuma": [], "a2": ["s-a"]}


def test_diagnostico(admin, db):
    from src.chordata_multi_empresa import esquema

    with db() as s:
        r = esquema.diagnosticar(s.connection())
    assert r["erros"] == []
    with admin.connect() as c:  # o dono é superusuário: o RLS não vale para ele
        assert any("superusuário" in e for e in esquema.diagnosticar(c)["erros"])


def test_zz_remover_devolve_o_banco_ao_estado_original(admin):
    from src.chordata_multi_empresa import esquema

    # "atendente" existe nas duas empresas: voltar ao único global é impossível e nada pode mudar
    with pytest.raises(RuntimeError, match="evo_core_agents"):
        esquema.remover(admin, incluir_nucleo=True)
    with admin.connect() as c:
        assert c.execute(text("SELECT relrowsecurity FROM pg_class WHERE relname = 'evo_core_agents'")).scalar()

    with admin.begin() as c:
        c.execute(text("DELETE FROM evo_core_agents WHERE empresa_id = :b"), {"b": B})
    r = esquema.remover(admin, incluir_nucleo=True)
    assert "evo_core_agents" in r["removidas"]
    with admin.connect() as c:
        colunas = c.execute(text(
            "SELECT count(*) FROM information_schema.columns WHERE column_name = 'empresa_id' "
            "AND table_name IN ('evo_core_agents', 'evo_ai_agent_processor_sessions')"
        )).scalar()
        indices = {i[0] for i in c.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'evo_core_agents'"))}
        rls = c.execute(text("SELECT relrowsecurity FROM pg_class WHERE relname = 'evo_core_agents'")).scalar()
    assert colunas == 0 and not rls
    assert "idx_evo_core_agents_name_unique" in indices and "idx_evo_core_agents_name_unique_empresa" not in indices
    esquema.instalar(admin, incluir_nucleo=True)  # e instala de novo sem erro
