"""Repassa a empresa nas chamadas do Processor ao Core e ao CRM (cabeçalho ``X-Evo-Tenant-Id``).

As ~25 chamadas ao Core montam seus cabeçalhos cada uma por conta própria; em vez de editar todas, o
``httpx`` (que todas usam) ganha um passo central. Só vale para os hosts informados (Core e CRM):
a empresa nunca vai para APIs externas (OpenAI, Google, GitHub...). Idempotente.
"""

from __future__ import annotations

from typing import Iterable
from urllib.parse import urlparse

import httpx

from .contexto import CABECALHO, empresa_atual

_hosts: set[str] = set()
_instalado = False


def hosts_de(urls: Iterable[str]) -> set[str]:
    return {h for h in (urlparse(u).hostname for u in urls if u) if h}


def _acrescentar(request: httpx.Request) -> None:
    empresa = empresa_atual()
    if empresa and request.url.host in _hosts and CABECALHO not in request.headers:
        request.headers[CABECALHO] = empresa


def instalar(urls: Iterable[str]) -> None:
    global _instalado
    _hosts.update(hosts_de(urls))
    if _instalado:
        return
    _instalado = True

    enviar_async = httpx.AsyncClient.send
    enviar_sync = httpx.Client.send

    async def send_async(self, request, *args, **kwargs):
        _acrescentar(request)
        return await enviar_async(self, request, *args, **kwargs)

    def send_sync(self, request, *args, **kwargs):
        _acrescentar(request)
        return enviar_sync(self, request, *args, **kwargs)

    httpx.AsyncClient.send = send_async  # type: ignore[method-assign]
    httpx.Client.send = send_sync  # type: ignore[method-assign]


def limpar_para_testes() -> None:
    _hosts.clear()
