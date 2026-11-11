"""Tipo de cada placa. O código não decide pelo modelo do veículo.

O arquivo data/frota_tipos.json é a base. O administrador grava por cima
na tabela frota_tipo. Placa nova do rastreador entra como sem tipo até
alguém classificar.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

TIPOS = ("compactador", "basculante", "apoio", "trator", "outro")
SEM_TIPO = "sem_tipo"
_CACHE: tuple[str, dict[str, str]] | None = None


def _norm(placa: str) -> str:
    return "".join(ch for ch in str(placa or "").upper() if ch.isalnum())


def _fmt(placa: str) -> str:
    chave = _norm(placa)
    if len(chave) == 7:
        return f"{chave[:3]}-{chave[3:]}"
    return chave


def _arquivo():
    from .config import ROOT

    return ROOT / "data" / "frota_tipos.json"


def _base() -> dict[str, str]:
    try:
        bruto = json.loads(_arquivo().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(bruto, dict):
        return {}
    saida: dict[str, str] = {}
    for placa, tipo in bruto.items():
        chave = _norm(str(placa))
        valor = str(tipo or "").strip().lower()
        if chave and valor in TIPOS:
            saida[chave] = valor
    return saida


def _ajustes() -> dict[str, str]:
    from .live_db import caminho_banco, conectar

    try:
        if not caminho_banco().is_file():
            return {}
        con = conectar(leitura=True)
    except (OSError, sqlite3.Error, RuntimeError):
        return {}
    try:
        rows = con.execute("SELECT placa, tipo FROM frota_tipo").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        con.close()
    saida: dict[str, str] = {}
    for row in rows:
        chave = _norm(str(row["placa"] or ""))
        valor = str(row["tipo"] or "").strip().lower()
        if chave and valor in TIPOS:
            saida[chave] = valor
    return saida


def invalidar() -> None:
    global _CACHE
    _CACHE = None


def _mapa() -> dict[str, str]:
    global _CACHE
    from .live_db import caminho_banco

    chave = str(caminho_banco())
    if _CACHE is None or _CACHE[0] != chave:
        mapa = _base()
        mapa.update(_ajustes())
        _CACHE = (chave, mapa)
    return _CACHE[1]


def tipo_de(placa: str) -> str:
    return _mapa().get(_norm(placa), SEM_TIPO)


def _nomes() -> dict[str, str]:
    from .live_db import caminho_banco, conectar

    try:
        if not caminho_banco().is_file():
            return {}
        con = conectar(leitura=True)
    except (OSError, sqlite3.Error, RuntimeError):
        return {}
    try:
        rows = con.execute(
            "SELECT placa, veiculo FROM placa WHERE veiculo != '' ORDER BY data, turno"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        con.close()
    nomes: dict[str, str] = {}
    for row in rows:
        chave = _norm(str(row["placa"] or ""))
        nome = str(row["veiculo"] or "").strip()
        if chave and nome:
            nomes[chave] = nome
    return nomes


def _placas_do_rastreador() -> set[str]:
    """Placas que o rastreador já gravou. Ainda sem tipo até o admin classificar."""
    from .live_db import caminho_banco, conectar

    try:
        if not caminho_banco().is_file():
            return set()
        con = conectar(leitura=True)
    except (OSError, sqlite3.Error, RuntimeError):
        return set()
    try:
        rows = con.execute("SELECT DISTINCT placa FROM placa").fetchall()
    except sqlite3.Error:
        return set()
    finally:
        con.close()
    return {_norm(str(row["placa"] or "")) for row in rows if _norm(str(row["placa"] or ""))}


def listar() -> list[dict[str, str]]:
    mapa = dict(_mapa())
    for chave in _placas_do_rastreador():
        mapa.setdefault(chave, SEM_TIPO)
    nomes = _nomes()
    chaves = sorted(mapa, key=lambda chave: (mapa[chave] != SEM_TIPO, chave))
    return [
        {"placa": _fmt(chave), "tipo": mapa[chave], "veiculo": nomes.get(chave, "")}
        for chave in chaves
    ]


def definir(placa: str, tipo: str) -> dict[str, Any]:
    chave = _norm(placa)
    valor = str(tipo or "").strip().lower()
    if not chave:
        raise ValueError("Informe a placa.")
    if valor not in TIPOS:
        raise ValueError("Tipo inválido. Use compactador, basculante, apoio, trator ou outro.")
    from .live_db import conectar

    con = conectar()
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS frota_tipo (
                placa TEXT PRIMARY KEY,
                tipo TEXT NOT NULL
            )
            """
        )
        con.execute(
            "INSERT INTO frota_tipo (placa, tipo) VALUES (?, ?) "
            "ON CONFLICT(placa) DO UPDATE SET tipo = excluded.tipo",
            (_fmt(chave), valor),
        )
        con.commit()
    finally:
        con.close()
    invalidar()
    return {"ok": True, "placa": _fmt(chave), "tipo": valor}
