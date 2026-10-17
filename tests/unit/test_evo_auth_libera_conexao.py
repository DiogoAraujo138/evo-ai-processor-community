"""Chordata: o middleware de autenticacao tem de liberar a conexao do banco ANTES de executar a requisicao.

Antes (upstream), a sessao ficava aberta durante o `call_next` inteiro: cada execucao de agente (segundos de IA) segurava uma
conexao do pool; com ~15 execucoes simultaneas o pool esgotava e a chamada de IA caia. Achado no teste de carga.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.middleware import evo_auth
from src.middleware.evo_auth import EvoAuthMiddleware

AGENT_ID = "c9cbd608-ee35-49fe-8200-fadaaa682d87"


def test_sync_route_fecha_a_sessao_antes_do_call_next():
    db = MagicMock()
    estado = {}

    async def call_next(_request):
        estado["fechada_ao_executar"] = db.close.called
        return "resposta"

    request = SimpleNamespace(
        method="POST",
        url=SimpleNamespace(path=f"/api/v1/a2a/{AGENT_ID}/sync/x"),
        headers={"x-api-key": "k"},
        state=SimpleNamespace(),
    )
    mw = EvoAuthMiddleware(app=MagicMock())
    with patch.object(mw, "_should_skip", return_value=False), \
         patch.object(mw, "_extract_agent_id_from_sync_path", return_value=AGENT_ID), \
         patch.object(evo_auth, "get_db", return_value=iter([db])), \
         patch.object(evo_auth.agent_service, "validate_agent_api_key",
                      new=AsyncMock(return_value={"valid": True, "agent_id": AGENT_ID, "agent_name": "a"})):
        resultado = asyncio.run(mw.dispatch(request, call_next))

    assert resultado == "resposta"
    assert estado["fechada_ao_executar"] is True, "a conexao do banco ainda estava aberta durante a execucao do agente"
