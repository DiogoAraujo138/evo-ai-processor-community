"""Decide QUAL empresa vale em uma requisição autenticada (mesmas regras do CRM).

A empresa nunca vem "de graça" do cliente:
  - usuário comum: só empresas em que tem vínculo (``empresa_usuarios``, no banco compartilhado);
  - super_admin do grupo: qualquer empresa ativa, mas precisa dizer qual;
  - agente (chave de API de agente, Bot Runtime): precisa dizer a empresa; o RLS garante que o
    agente só existe dentro dela (chave de outra empresa não acha o agente).
Sem empresa resolvida a requisição é recusada: nunca roda "sem escopo".
"""

from __future__ import annotations

from typing import Optional, Protocol

from .contexto import CABECALHO, uuid_valido


class Recusada(Exception):
    def __init__(self, mensagem: str, status: int):
        super().__init__(mensagem)
        self.mensagem = mensagem
        self.status = status


class Consulta(Protocol):
    def vinculadas(self, user_id: str) -> set[str]: ...

    def empresa_ativa(self, empresa_id: str) -> bool: ...


def chave_do_papel(papel: object) -> Optional[str]:
    """O Auth devolve o papel como ``{"key": "super_admin", ...}``; aceita também texto puro."""
    if isinstance(papel, dict):
        papel = papel.get("key")
    return str(papel) if papel else None


def _pedida(cabecalho: Optional[str]) -> Optional[str]:
    valor = (cabecalho or "").strip()
    if not valor:
        return None
    if not uuid_valido(valor):
        raise Recusada("Empresa inválida", 400)
    return valor.lower()


def _exigir_ativa(consulta: Consulta, empresa_id: str) -> str:
    if not consulta.empresa_ativa(empresa_id):
        raise Recusada("Empresa inexistente ou inativa", 403)
    return empresa_id


def resolver(user_context: Optional[dict], cabecalho: Optional[str], consulta: Consulta) -> Optional[str]:
    """Devolve o id da empresa, ``None`` (rota sem autenticação) ou levanta ``Recusada``."""
    if not user_context:
        return None

    pedida = _pedida(cabecalho)
    falta = Recusada(f"Informe a empresa no cabeçalho {CABECALHO}", 400)

    if user_context.get("is_agent_bot"):
        if not pedida:
            raise falta
        return _exigir_ativa(consulta, pedida)

    if chave_do_papel(user_context.get("role")) == "super_admin":
        if not pedida:
            raise falta
        return _exigir_ativa(consulta, pedida)

    user_id = user_context.get("user_id")
    vinculadas = consulta.vinculadas(str(user_id).lower()) if uuid_valido(str(user_id)) else set()
    if not vinculadas:
        raise Recusada("Usuário sem empresa", 403)
    if pedida:
        if pedida not in vinculadas:
            raise Recusada("Sem acesso a esta empresa", 403)
        return pedida
    if len(vinculadas) == 1:
        return next(iter(vinculadas))
    raise falta
