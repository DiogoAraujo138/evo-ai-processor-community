"""Entrega a empresa ao Postgres: ``app.empresa_id`` em toda transação (RLS).

Um único ouvinte no ``sqlalchemy.orm.Session`` cobre o motor do Processor **e** o do ADK
(``DatabaseSessionService`` usa ``AsyncSession``, que envolve um ``Session`` comum).

Usa ``set_config(..., is_local => true)``: vale só até o fim da transação, então uma conexão
devolvida ao pool nunca carrega a empresa anterior. Sem empresa definida grava vazio, e a política
do banco (``empresa_id = chordata_empresa_atual()``) não enxerga nenhuma linha: falha segura.
"""

from __future__ import annotations

from sqlalchemy import event, text
from sqlalchemy.orm import Session

from .contexto import empresa_atual, ligado


def definir_empresa_na_transacao(session, transaction, connection) -> None:
    if not ligado() or connection.dialect.name != "postgresql":
        return
    connection.execute(
        text("SELECT set_config('app.empresa_id', :v, true)"),
        {"v": empresa_atual() or ""},
    )


def registrar_gancho() -> None:
    """Idempotente."""
    if not event.contains(Session, "after_begin", definir_empresa_na_transacao):
        event.listen(Session, "after_begin", definir_empresa_na_transacao)


def remover_gancho() -> None:
    if event.contains(Session, "after_begin", definir_empresa_na_transacao):
        event.remove(Session, "after_begin", definir_empresa_na_transacao)
