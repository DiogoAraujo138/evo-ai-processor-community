"""Liga o multi-empresa ao FastAPI do Processor sem reescrever o núcleo.

Peças:
  - ``ContextoChordata``: implementa o ponto de extensão ``runtime_context``. O que ele devolve em
    ``current_context_id`` já é repassado pelo Processor no cabeçalho ``X-Evo-Tenant-Id`` ao buscar
    credenciais no CRM.
  - ``EmpresaPorCabecalho`` (mais externo, antes do login): fixa a empresa pedida no cabeçalho só para o
    login conseguir achar a chave de API do agente (que mora em tabela isolada). **Não é confiável**: o
    valor é descartado logo depois do login, a menos que seja verificado.
  - ``EmpresaDaRequisicao`` (logo depois do login): decide a empresa de verdade (``requisicao.resolver``),
    recusa com 400/403 quando não dá, e fixa a empresa para o resto da requisição.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Optional

import anyio
from sqlalchemy import text
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import banco
from .contexto import CABECALHO, com_empresa, empresa_atual, ligado, uuid_valido
from .requisicao import Consulta, Recusada, resolver

logger = logging.getLogger(__name__)


class ConsultaBanco:
    """Lê ``empresas``/``empresa_usuarios`` (tabelas sem RLS) pela sessão normal do Processor."""

    def __init__(self, fabrica_sessao: Callable[[], Any]):
        self._fabrica = fabrica_sessao

    def vinculadas(self, user_id: str) -> set[str]:
        with self._fabrica() as db:
            linhas = db.execute(text(
                "SELECT eu.empresa_id::text FROM empresa_usuarios eu JOIN empresas e ON e.id = eu.empresa_id "
                "WHERE eu.user_id = CAST(:u AS uuid) AND e.ativa"
            ), {"u": user_id}).all()
        return {linha[0] for linha in linhas}

    def empresa_ativa(self, empresa_id: str) -> bool:
        with self._fabrica() as db:
            return db.execute(
                text("SELECT 1 FROM empresas WHERE id = CAST(:i AS uuid) AND ativa"), {"i": empresa_id}
            ).first() is not None


class ContextoChordata:
    """Implementação do ponto de extensão ``runtime_context``."""

    def current_context_id(self, source: Any) -> Optional[str]:
        verificada = getattr(getattr(source, "state", None), "empresa_id", None)
        return verificada or empresa_atual()

    def with_context(self, context_id: str, fn: Callable[[], Any]) -> Any:
        with com_empresa(context_id):
            return fn()

    @asynccontextmanager
    async def bind_context(self, context_id: str) -> AsyncIterator[None]:
        with com_empresa(context_id):
            yield


class EmpresaPorCabecalho:
    """ASGI puro (não BaseHTTPMiddleware: o ContextVar precisa seguir na mesma tarefa)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not ligado():
            await self.app(scope, receive, send)
            return
        candidata = Request(scope, receive).headers.get(CABECALHO, "").strip()
        with com_empresa(candidata.lower() if uuid_valido(candidata) else None):
            await self.app(scope, receive, send)


class EmpresaDaRequisicao:
    def __init__(self, app, consulta: Optional[Consulta] = None):
        self.app = app
        self._consulta = consulta

    def consulta(self) -> Consulta:
        if self._consulta is None:
            from src.config.database import SessionLocal

            self._consulta = ConsultaBanco(SessionLocal)
        return self._consulta

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not ligado():
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        contexto = getattr(request.state, "user_context", None)
        try:
            empresa = await anyio.to_thread.run_sync(
                resolver, contexto, request.headers.get(CABECALHO), self.consulta()
            )
        except Recusada as e:
            await _recusar(scope, receive, send, e.status, e.mensagem, "ERR_EMPRESA")
            return
        except Exception:  # noqa: BLE001 - nunca segue "sem escopo" se não deu para decidir
            logger.exception("Multi-empresa: falha ao resolver a empresa")
            await _recusar(scope, receive, send, 503, "Não foi possível resolver a empresa", "ERR_SERVICE_UNAVAILABLE")
            return

        # None (rota sem login) limpa a empresa candidata do cabeçalho: valor não verificado não passa daqui.
        if empresa:
            request.state.empresa_id = empresa
        with com_empresa(empresa):
            await self.app(scope, receive, send)


async def _recusar(scope, receive, send, status: int, mensagem: str, codigo: str) -> None:
    resposta = JSONResponse(
        status_code=status,
        content={"error": "Forbidden" if status == 403 else "Bad Request" if status == 400 else "Service Unavailable",
                 "code": codigo, "message": mensagem},
    )
    await resposta(scope, receive, send)


def instalar_se_ligado(app) -> bool:
    """Chamada única no fim do ``main.py``. Sem ``MULTI_EMPRESA=on`` não faz nada."""
    if not ligado():
        return False

    from src import evo_extension_points as pontos
    from src.config.database import engine

    nomes = [m.cls.__name__ for m in app.user_middleware]
    if "EvoAuthMiddleware" not in nomes:
        # Sem a ordem certa (login antes da empresa) é melhor não subir do que subir sem isolamento.
        raise RuntimeError("Multi-empresa: EvoAuthMiddleware não encontrado; não dá para ordenar os middlewares.")
    if getattr(app, "middleware_stack", None) is not None:
        raise RuntimeError("Multi-empresa: instalar antes de a aplicação começar a atender.")

    with engine.connect() as conn:
        from .esquema import diagnosticar

        problemas = diagnosticar(conn)
    for aviso in problemas["avisos"]:
        logger.warning("Multi-empresa: %s", aviso)
    if problemas["erros"]:
        for erro in problemas["erros"]:
            logger.critical("Multi-empresa: %s", erro)
        raise RuntimeError("Multi-empresa ligada, mas o banco não está pronto: " + " | ".join(problemas["erros"]))

    from src.config.settings import settings

    from . import repasse

    repasse.instalar([settings.CORE_SERVICE_URL, settings.EVO_AI_CRM_URL])
    banco.registrar_gancho()
    pontos.replace("runtime_context", ContextoChordata())
    # app.user_middleware: do mais externo para o mais interno.
    app.user_middleware.insert(0, Middleware(EmpresaPorCabecalho))
    app.user_middleware.insert(nomes.index("EvoAuthMiddleware") + 2, Middleware(EmpresaDaRequisicao))
    logger.info("Multi-empresa ligada no Processor.")
    return True
