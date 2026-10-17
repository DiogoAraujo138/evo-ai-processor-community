"""Limite de execucoes de agente em paralelo: nunca passa do maximo, o excesso espera e depois recebe 503."""

from __future__ import annotations

import asyncio

from src.chordata_multi_empresa.limite import LimiteDeConcorrencia, e_execucao_pesada

A2A = "/api/v1/a2a/c9cbd608-ee35-49fe-8200-fadaaa682d87"


def test_so_as_execucoes_pesadas_entram_na_fila():
    assert e_execucao_pesada("POST", A2A)
    assert e_execucao_pesada("POST", "/api/v1/chat/x")
    assert not e_execucao_pesada("GET", A2A)
    assert not e_execucao_pesada("POST", A2A + "/conversation/history")
    assert not e_execucao_pesada("GET", "/api/v1/sessions/account")


def _escopo(caminho=A2A, metodo="POST"):
    return {"type": "http", "method": metodo, "path": caminho}


def test_nunca_passa_do_maximo_e_todas_terminam():
    ativas = {"agora": 0, "pico": 0}

    async def app(scope, receive, send):
        ativas["agora"] += 1
        ativas["pico"] = max(ativas["pico"], ativas["agora"])
        await asyncio.sleep(0.05)
        ativas["agora"] -= 1
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def roda():
        mw = LimiteDeConcorrencia(app, maximo=3, espera_maxima=5)
        enviadas = []

        async def send(m):
            enviadas.append(m)

        await asyncio.gather(*[mw(_escopo(), None, send) for _ in range(20)])
        return enviadas

    enviadas = asyncio.run(roda())
    assert ativas["pico"] == 3
    assert sum(1 for m in enviadas if m["status"] == 200) == 20


def test_fila_cheia_responde_503_com_retry_after():
    async def app(scope, receive, send):
        await asyncio.sleep(0.5)
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def roda():
        mw = LimiteDeConcorrencia(app, maximo=1, espera_maxima=0.05)
        respostas = []

        async def send(m):
            if m["type"] == "http.response.start":
                respostas.append(m)

        await asyncio.gather(*[mw(_escopo(), None, send) for _ in range(3)])
        return respostas

    respostas = asyncio.run(roda())
    status = sorted(r["status"] for r in respostas)
    assert status == [200, 503, 503]
    assert any((b"retry-after", b"5") in r["headers"] for r in respostas if r["status"] == 503)


def test_outras_rotas_passam_direto():
    chamadas = []

    async def app(scope, receive, send):
        chamadas.append(scope["path"])

    async def roda():
        mw = LimiteDeConcorrencia(app, maximo=1, espera_maxima=0.01)
        await asyncio.gather(*[mw(_escopo("/api/v1/sessions/account", "GET"), None, None) for _ in range(5)])

    asyncio.run(roda())
    assert len(chamadas) == 5
