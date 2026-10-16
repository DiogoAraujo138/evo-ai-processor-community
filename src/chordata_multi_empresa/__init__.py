"""Multi-empresa Chordata para o Processor (ADR-0007, Caminho C). Desligado por padrão.

Ligar: ``MULTI_EMPRESA=on`` + banco instalado (``python -m src.chordata_multi_empresa instalar``).
Estado e riscos: vault/01_Arquitetura/Multi-empresa - Estado e Como Ligar.md
"""

from .contexto import CABECALHO, com_empresa, empresa_atual, ligado
from .extensao import instalar_se_ligado

__all__ = ["CABECALHO", "com_empresa", "empresa_atual", "instalar_se_ligado", "ligado"]
