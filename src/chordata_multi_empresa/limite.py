"""Limite de execuções de agente em paralelo (fila no lugar de espera circular entre pools de banco).

Cada execução de agente segura conexões de DOIS pools ao mesmo tempo: o síncrono (``Depends(get_db)`` das rotas) e o assíncrono
do ADK (``DatabaseSessionService``, 5+10 por padrão). Com mais execuções simultâneas do que conexões, cada requisição segura uma
de um pool esperando uma do outro até estourar o tempo de 30 s do pool (achado no teste de carga com 40 mensagens em rajada:
dezenas de conexões "idle in transaction", 39 timeouts de IA). Com este limite o excesso espera numa fila em memória; passada a
espera máxima responde 503 com ``Retry-After`` (o chamador tenta de novo).

``MAX_CONCURRENT_AGENT_RUNS`` (padrão 0 = desligado, comportamento do upstream), ``AGENT_RUN_QUEUE_TIMEOUT`` (segundos, padrão 60).
Recomendado: um valor abaixo do menor pool (ex.: 8).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

logger = logging.getLogger(__name__)

# POST /api/v1/a2a/<uuid>  (execução do agente) e POST /api/v1/chat/...
_PESADA = re.compile(r"^/api/v1/(a2a/[0-9a-fA-F-]{36}|chat(/.*)?)$")


def e_execucao_pesada(metodo: str, caminho: str) -> bool:
    return metodo == "POST" and bool(_PESADA.match(caminho))


class LimiteDeConcorrencia:
    """ASGI puro: não toca no corpo nem na resposta; só decide quando a requisição pode começar."""

    def __init__(self, app, maximo: int, espera_maxima: float = 60.0):
        self.app = app
        self.maximo = maximo
        self.espera_maxima = espera_maxima
        self._semaforo = asyncio.Semaphore(maximo)
        self.em_fila = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not e_execucao_pesada(scope.get("method", ""), scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        self.em_fila += 1
        try:
            await asyncio.wait_for(self._semaforo.acquire(), timeout=self.espera_maxima)
        except asyncio.TimeoutError:
            logger.warning("limite de execuções: fila cheia por %.0fs (máximo %d); respondendo 503", self.espera_maxima, self.maximo)
            await send({"type": "http.response.start", "status": 503,
                        "headers": [(b"content-type", b"application/json"), (b"retry-after", b"5")]})
            await send({"type": "http.response.body",
                        "body": b'{"error":"Processor ocupado","code":"ERR_BUSY","message":"Muitas execucoes em andamento; tente de novo"}'})
            return
        finally:
            self.em_fila -= 1
        try:
            await self.app(scope, receive, send)
        finally:
            self._semaforo.release()


def instalar_limite(app) -> bool:
    """Liga o limite se ``MAX_CONCURRENT_AGENT_RUNS`` > 0. Chamar DEPOIS dos demais ``add_middleware`` (fica o mais externo:
    a fila vem antes de qualquer uso do banco)."""
    maximo = int(os.getenv("MAX_CONCURRENT_AGENT_RUNS", "0") or 0)
    if maximo <= 0:
        return False
    app.add_middleware(LimiteDeConcorrencia, maximo=maximo, espera_maxima=float(os.getenv("AGENT_RUN_QUEUE_TIMEOUT", "60")))
    logger.info("limite de execuções de agente em paralelo: %d (fila até %ss)", maximo, os.getenv("AGENT_RUN_QUEUE_TIMEOUT", "60"))
    return True
