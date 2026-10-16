"""Linha de comando do instalador.

    python -m src.chordata_multi_empresa instalar [--nucleo]
    python -m src.chordata_multi_empresa remover  [--nucleo]
    python -m src.chordata_multi_empresa diagnosticar
    python -m src.chordata_multi_empresa conceder --papel NOME
    python -m src.chordata_multi_empresa criar-tabelas
    python -m src.chordata_multi_empresa migrar-alembic

A conexão vem de ``MULTI_EMPRESA_ADMIN_URL`` (dono das tabelas) ou, na falta, de ``POSTGRES_CONNECTION_STRING``.
"""

from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import create_engine

from . import esquema


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="chordata_multi_empresa")
    p.add_argument("comando", choices=["instalar", "remover", "diagnosticar", "conceder", "criar-tabelas", "migrar-alembic"])
    p.add_argument("--nucleo", action="store_true", help="inclui as tabelas evo_core_* (só depois que o Core souber a empresa)")
    p.add_argument("--papel", help="papel da aplicação (conceder)")
    a = p.parse_args(argv)

    url = os.getenv("MULTI_EMPRESA_ADMIN_URL") or os.getenv("POSTGRES_CONNECTION_STRING")
    if not url:
        print("Defina MULTI_EMPRESA_ADMIN_URL ou POSTGRES_CONNECTION_STRING.", file=sys.stderr)
        return 2
    engine = create_engine(url)

    if a.comando == "instalar":
        r = esquema.instalar(engine, incluir_nucleo=a.nucleo)
        print("Isoladas:", ", ".join(r["isoladas"]) or "-")
        if r["ausentes"]:
            print("Ausentes (rode de novo depois do primeiro uso):", ", ".join(r["ausentes"]))
    elif a.comando == "criar-tabelas":
        # O Processor cria as próprias tabelas na subida (src/main.py, create_all). Em produção a aplicação roda sem
        # DDL, então o dono as cria ANTES, aqui, com a mesma regra (a tabela `users` é do Auth e fica de fora).
        from src.models.models import Base

        tabelas = [t for nome, t in Base.metadata.tables.items() if nome != "users"]
        Base.metadata.create_all(bind=engine, tables=tabelas, checkfirst=True)
        print("Tabelas do Processor criadas/conferidas:", len(tabelas))
    elif a.comando == "migrar-alembic":
        # `alembic upgrade head` falha com DuplicateTable num banco que já tinha as tabelas (criadas pelo create_all do
        # Processor) e `alembic_version` vazia. O compose antigo engolia esse erro. Aqui a decisão é explícita:
        # banco existente sem versão => `stamp head` (marca como atual); banco novo ou versionado => `upgrade head`.
        from alembic import command
        from alembic.config import Config
        from sqlalchemy import inspect, text

        cfg = Config("alembic.ini")
        insp = inspect(engine)
        com_versao = False
        if insp.has_table("alembic_version"):
            with engine.connect() as conn:
                com_versao = conn.execute(text("SELECT count(*) FROM alembic_version")).scalar() > 0
        if not com_versao and insp.has_table("evo_agent_processor_execution_metrics"):
            command.stamp(cfg, "head")
            print("Banco já tinha as tabelas do Processor e nenhuma versão do alembic: marcado como atual (stamp head).")
        else:
            command.upgrade(cfg, "head")
            print("alembic upgrade head concluído.")
    elif a.comando == "remover":
        r = esquema.remover(engine, incluir_nucleo=a.nucleo)
        print("Revertidas:", ", ".join(r["removidas"]) or "-")
    elif a.comando == "conceder":
        if not a.papel:
            print("Informe --papel.", file=sys.stderr)
            return 2
        esquema.conceder(engine, a.papel)
        print("Permissões concedidas a", a.papel)
    else:
        with engine.connect() as conn:
            r = esquema.diagnosticar(conn)
        for e in r["erros"]:
            print("ERRO :", e)
        for w in r["avisos"]:
            print("AVISO:", w)
        if not r["erros"] and not r["avisos"]:
            print("Tudo certo.")
        return 1 if r["erros"] else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
