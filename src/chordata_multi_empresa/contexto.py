"""Empresa corrente da requisição (multi-empresa Chordata, Caminho C).

Fica em um ``ContextVar``: cada requisição (e cada tarefa criada a partir dela) enxerga só a sua.
Tudo é desligado por padrão: sem ``MULTI_EMPRESA=on`` o Processor se comporta como o Evo comunitário.
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

CABECALHO = "X-Evo-Tenant-Id"

_UUID = re.compile(r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
_LIGADO = {"on", "1", "true", "sim", "yes"}

_empresa: ContextVar[Optional[str]] = ContextVar("chordata_empresa_id", default=None)


def ligado() -> bool:
    """True quando ``MULTI_EMPRESA`` está ligada (lido a cada chamada, para os testes poderem alterar)."""
    return os.getenv("MULTI_EMPRESA", "").strip().lower() in _LIGADO


def uuid_valido(valor: object) -> bool:
    return isinstance(valor, str) and bool(_UUID.match(valor))


def empresa_atual() -> Optional[str]:
    return _empresa.get()


@contextmanager
def com_empresa(empresa_id: Optional[str]) -> Iterator[None]:
    """Fixa a empresa durante o bloco. ``None`` limpa (útil para descartar um valor ainda não verificado)."""
    if empresa_id is not None and not uuid_valido(empresa_id):
        raise ValueError("empresa_id inválido (esperado um UUID)")
    marca = _empresa.set(empresa_id.lower() if empresa_id else None)
    try:
        yield
    finally:
        _empresa.reset(marca)
