"""Multi-empresa Chordata: decisão da empresa e middlewares (sem banco)."""

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from src.chordata_multi_empresa.contexto import com_empresa, empresa_atual, ligado, uuid_valido
from src.chordata_multi_empresa.extensao import ContextoChordata, EmpresaDaRequisicao, EmpresaPorCabecalho
from src.chordata_multi_empresa.requisicao import chave_do_papel

A = "11111111-1111-1111-1111-111111111111"
B = "22222222-2222-2222-2222-222222222222"
INATIVA = "33333333-3333-3333-3333-333333333333"
USER_UM = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER_DOIS = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
USER_NENHUM = "cccccccc-cccc-cccc-cccc-cccccccccccc"


class ConsultaFalsa:
    ativas = {A, B}
    vinculos = {USER_UM: {A}, USER_DOIS: {A, B}}

    def vinculadas(self, user_id):
        return set(self.vinculos.get(user_id, set()))

    def empresa_ativa(self, empresa_id):
        return empresa_id in self.ativas


class ConsultaQuebrada:
    def vinculadas(self, user_id):
        raise RuntimeError("banco fora do ar")

    empresa_ativa = vinculadas


class FalsoLogin:
    """Faz o papel do EvoAuth: preenche request.state.user_context (None = rota pública)."""

    contexto = None
    vista_no_login = None

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        FalsoLogin.vista_no_login = empresa_atual()
        if FalsoLogin.contexto is not None:
            Request(scope, receive).state.user_context = FalsoLogin.contexto
        await self.app(scope, receive, send)


async def endpoint(request):
    return JSONResponse({"empresa": empresa_atual(), "ep": ContextoChordata().current_context_id(request)})


def cliente(consulta=None):
    app = Starlette(
        routes=[Route("/x", endpoint)],
        middleware=[
            Middleware(EmpresaPorCabecalho),
            Middleware(FalsoLogin),
            Middleware(EmpresaDaRequisicao, consulta=consulta or ConsultaFalsa()),
        ],
    )
    return TestClient(app)


@pytest.fixture(autouse=True)
def ligar(monkeypatch):
    monkeypatch.setenv("MULTI_EMPRESA", "on")
    FalsoLogin.contexto = None
    FalsoLogin.vista_no_login = None


def usuario(user_id, role=None):
    return {"user_id": user_id, "is_agent_bot": False, "role": role}


def test_ligado_so_com_variavel(monkeypatch):
    monkeypatch.delenv("MULTI_EMPRESA", raising=False)
    assert not ligado()
    monkeypatch.setenv("MULTI_EMPRESA", "on")
    assert ligado()


def test_uuid_e_papel():
    assert uuid_valido(A) and not uuid_valido("x") and not uuid_valido(None)
    assert chave_do_papel({"key": "super_admin"}) == "super_admin"
    assert chave_do_papel("agent") == "agent" and chave_do_papel(None) is None


def test_com_empresa_restaura_e_rejeita_invalido():
    assert empresa_atual() is None
    with com_empresa(A):
        assert empresa_atual() == A
        with com_empresa(None):
            assert empresa_atual() is None
        assert empresa_atual() == A
    assert empresa_atual() is None
    with pytest.raises(ValueError):
        with com_empresa("'; drop table x; --"):
            pass


def test_rota_publica_descarta_empresa_do_cabecalho_mas_o_login_a_ve():
    r = cliente().get("/x", headers={"X-Evo-Tenant-Id": B})
    assert r.status_code == 200
    assert FalsoLogin.vista_no_login == B  # o login precisa dela para achar a chave do agente
    assert r.json() == {"empresa": None, "ep": None}  # mas valor não verificado não passa do login


def test_usuario_com_uma_empresa_nao_precisa_do_cabecalho():
    FalsoLogin.contexto = usuario(USER_UM)
    r = cliente().get("/x")
    assert r.status_code == 200 and r.json() == {"empresa": A, "ep": A}


def test_usuario_nao_escolhe_empresa_alheia():
    FalsoLogin.contexto = usuario(USER_UM)
    r = cliente().get("/x", headers={"X-Evo-Tenant-Id": B})
    assert r.status_code == 403 and r.json()["code"] == "ERR_EMPRESA"


def test_usuario_com_duas_empresas_precisa_escolher():
    FalsoLogin.contexto = usuario(USER_DOIS)
    assert cliente().get("/x").status_code == 400
    r = cliente().get("/x", headers={"X-Evo-Tenant-Id": B})
    assert r.status_code == 200 and r.json()["empresa"] == B


def test_usuario_sem_vinculo_e_recusado():
    FalsoLogin.contexto = usuario(USER_NENHUM)
    assert cliente().get("/x").status_code == 403


def test_super_admin_precisa_dizer_a_empresa_e_ela_deve_estar_ativa():
    FalsoLogin.contexto = usuario(USER_NENHUM, role={"key": "super_admin"})
    assert cliente().get("/x").status_code == 400
    assert cliente().get("/x", headers={"X-Evo-Tenant-Id": INATIVA}).status_code == 403
    r = cliente().get("/x", headers={"X-Evo-Tenant-Id": B})
    assert r.status_code == 200 and r.json()["empresa"] == B


def test_agente_precisa_de_empresa_ativa():
    FalsoLogin.contexto = {"agent_id": "x", "is_agent_bot": True}
    assert cliente().get("/x").status_code == 400
    assert cliente().get("/x", headers={"X-Evo-Tenant-Id": INATIVA}).status_code == 403
    assert cliente().get("/x", headers={"X-Evo-Tenant-Id": A}).json()["empresa"] == A


def test_cabecalho_malformado_e_400():
    FalsoLogin.contexto = usuario(USER_UM)
    assert cliente().get("/x", headers={"X-Evo-Tenant-Id": "abc"}).status_code == 400


def test_falha_na_consulta_nunca_segue_sem_escopo():
    FalsoLogin.contexto = usuario(USER_UM)
    r = cliente(ConsultaQuebrada()).get("/x")
    assert r.status_code == 503


def test_desligado_nao_muda_nada(monkeypatch):
    monkeypatch.delenv("MULTI_EMPRESA", raising=False)
    FalsoLogin.contexto = usuario(USER_NENHUM)  # sem vínculo: ligado seria 403
    r = cliente().get("/x", headers={"X-Evo-Tenant-Id": B})
    assert r.status_code == 200 and r.json() == {"empresa": None, "ep": None}


def test_repasse_so_vai_para_core_e_crm():
    import asyncio

    import httpx

    from src.chordata_multi_empresa import repasse

    repasse.instalar(["http://evo-core:5555/api/v1", "http://evo-crm:3000"])
    vistos = {}

    def responder(request):
        vistos[request.url.host] = request.headers.get("x-evo-tenant-id")
        return httpx.Response(200)

    async def chamar():
        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as c:
            for url in ("http://evo-core:5555/x", "http://evo-crm:3000/y", "https://api.openai.com/z"):
                await c.get(url)
            await c.get("http://evo-core:5555/x", headers={"X-Evo-Tenant-Id": B})

    with com_empresa(A):
        asyncio.run(chamar())
    assert vistos["evo-core"] == B  # o cabeçalho explícito não é sobrescrito
    assert vistos["evo-crm"] == A
    assert vistos["api.openai.com"] is None  # empresa nunca vai para terceiros

    vistos.clear()
    asyncio.run(chamar())  # sem empresa fixada: nada é acrescentado
    assert vistos["evo-crm"] is None

    with com_empresa(A):  # cliente síncrono também
        with httpx.Client(transport=httpx.MockTransport(responder)) as c:
            c.get("http://evo-crm:3000/y")
    assert vistos["evo-crm"] == A


def test_pontos_de_extensao_aceitam_a_implementacao():
    from src import evo_extension_points as pontos

    pontos.replace("runtime_context", ContextoChordata())
    try:
        with com_empresa(A):
            assert pontos.runtime_context.current_context_id(None) == A
    finally:
        pontos.reset("runtime_context")
