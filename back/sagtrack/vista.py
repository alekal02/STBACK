"""Link temporário do relatório. A página abre com os dados já salvos; o PDF só nasce no download."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import date
from typing import Any
from urllib.parse import urlparse

from .config import Settings, pasta_dados

_HORAS = 12
_TIPOS = {"frota", "rota", "placa"}


def _chave() -> bytes:
    arquivo = pasta_dados() / "link.key"
    if arquivo.is_file():
        salvo = arquivo.read_bytes().strip()
        if len(salvo) >= 16:
            return salvo
    chave = secrets.token_hex(32).encode()
    arquivo.write_bytes(chave)
    return chave


def _base(settings: Settings) -> str:
    url = (settings.webhook_url or "").strip()
    if url.startswith("http"):
        parte = urlparse(url)
        host = parte.hostname or ""
        if parte.netloc and host not in {"localhost", "127.0.0.1"}:
            return f"{parte.scheme}://{parte.netloc}"
    return "https://sagtrack-backend.onrender.com"


def emitir(settings: Settings, tipo: str, consulta: str, dia: date) -> str:
    corpo = json.dumps(
        {
            "t": tipo if tipo in _TIPOS else "frota",
            "q": (consulta or "").strip().upper()[:40],
            "d": dia.isoformat(),
            "e": int(time.time()) + _HORAS * 3600,
        },
        separators=(",", ":"),
    )
    blob = base64.urlsafe_b64encode(corpo.encode()).decode().rstrip("=")
    mac = hmac.new(_chave(), blob.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{_base(settings)}/r/{blob}.{mac}"


def abrir(token: str) -> dict[str, Any] | None:
    bruto = (token or "").strip()
    if "." not in bruto or len(bruto) > 800:
        return None
    blob, mac = bruto.rsplit(".", 1)
    esperado = hmac.new(_chave(), blob.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(esperado, mac):
        return None
    try:
        padded = blob + "=" * (-len(blob) % 4)
        dados = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(dados, dict):
        return None
    if int(dados.get("e") or 0) < int(time.time()):
        return None
    tipo = str(dados.get("t") or "")
    if tipo not in _TIPOS:
        return None
    try:
        date.fromisoformat(str(dados.get("d") or ""))
    except ValueError:
        return None
    return dados


def _afinar(linha: list, max_pts: int = 40) -> list[list[float]]:
    pontos = [p for p in linha or [] if isinstance(p, (list, tuple)) and len(p) >= 2]
    if len(pontos) > max_pts:
        passo = max(1, len(pontos) // max_pts)
        pontos = pontos[::passo]
        if pontos[-1] != linha[-1]:
            pontos.append(linha[-1])
    saida = []
    for ponto in pontos:
        try:
            saida.append([round(float(ponto[0]), 5), round(float(ponto[1]), 5)])
        except (TypeError, ValueError):
            continue
    return saida


def _malha(linhas: list, limite: int = 180) -> list[list[list[float]]]:
    saida = []
    for linha in linhas or []:
        fina = _afinar(linha)
        if len(fina) >= 2:
            saida.append(fina)
        if len(saida) >= limite:
            break
    return saida


def dados(settings: Settings, claims: dict[str, Any]) -> dict[str, Any]:
    from .pdfmapa import _ler
    from .relatorios import montar

    dia = date.fromisoformat(str(claims["d"]))
    tipo = str(claims["t"])
    consulta = str(claims.get("q") or "")
    pasta = settings.output_dir / dia.isoformat()
    resumo = _ler(pasta, "resumo.json") or {}
    if tipo == "frota":
        from .relatorios import _texto_decisao, balanco_frota

        setores = _ler(pasta, "setores.json") or []
        frota = _ler(pasta, "frota.json") or []
        balanco = balanco_frota(setores, frota)
        leve = [
            {k: v for k, v in row.items() if k not in {"pontos", "poligonos", "placas"}}
            for row in balanco
        ]
        return {
            "tipo": "frota",
            "titulo": "Frota do dia",
            "data": dia.isoformat(),
            "consulta": "",
            "codigos": [],
            "expira": int(claims["e"]),
            "congelado_em": resumo.get("congelado_em") or "",
            "texto": _texto_decisao(dia, balanco),
            "balanco": leve,
            "percentual": None,
            "conclusao": "",
        }
    rel = montar(settings.output_dir, dia, tipo, consulta)
    codigos = []
    if tipo == "rota" and consulta:
        codigos = [consulta]
    elif tipo == "placa":
        codigos = [str(item.get("codigo") or "") for item in rel.get("setores") or [] if item.get("codigo")]
    return {
        "tipo": tipo,
        "titulo": rel.get("titulo") or "Relatório",
        "data": dia.isoformat(),
        "consulta": consulta,
        "codigos": codigos,
        "expira": int(claims["e"]),
        "congelado_em": resumo.get("congelado_em") or "",
        "texto": rel.get("texto") or [],
        "balanco": rel.get("balanco") or [],
        "percentual": rel.get("percentual"),
        "conclusao": rel.get("conclusao") or "",
    }


def mapa(settings: Settings, claims: dict[str, Any], codigo: str) -> dict[str, Any]:
    from .pdfmapa import _ler, _ruas
    from .relatorios import balanco_frota

    dia = date.fromisoformat(str(claims["d"]))
    pasta = settings.output_dir / dia.isoformat()
    setores = _ler(pasta, "setores.json") or []
    frota = _ler(pasta, "frota.json") or []
    chave = (codigo or str(claims.get("q") or "")).strip().upper()
    setor = next((item for item in setores if str(item.get("codigo") or "").upper() == chave), None)
    balanco = balanco_frota(setores, frota)
    row = next((item for item in balanco if str(item.get("codigo") or "").upper() == chave), None)
    feitos, faltando = _ruas(frota, str((setor or {}).get("codigo") or chave))
    mostrar_aberto = str(claims.get("t") or "") == "rota"
    paradas = []
    for ponto in (row or {}).get("pontos") or []:
        if isinstance(ponto, dict):
            try:
                paradas.append([round(float(ponto["latitude"]), 5), round(float(ponto["longitude"]), 5)])
            except (KeyError, TypeError, ValueError):
                continue
        elif isinstance(ponto, (list, tuple)) and len(ponto) >= 2:
            fina = _afinar([ponto], 1)
            paradas.extend(fina)
    return {
        "codigo": str((setor or row or {}).get("codigo") or chave),
        "nome": str((setor or row or {}).get("nome") or ""),
        "situacao": (row or {}).get("situacao") or "",
        "percentual": (row or {}).get("percentual"),
        "poligonos": _malha((setor or {}).get("poligonos") or [], 8),
        "feitos": _malha(feitos),
        "abertas": _malha(faltando) if mostrar_aberto else [],
        "paradas": paradas[:80],
    }


def pdf(settings: Settings, claims: dict[str, Any]) -> tuple[bytes, str]:
    from .whatsapp import _gerar

    dia = date.fromisoformat(str(claims["d"]))
    return _gerar(settings, str(claims["t"]), str(claims.get("q") or ""), dia)


def html_pagina() -> str:
    from pathlib import Path

    return Path(__file__).with_name("vista.html").read_text(encoding="utf-8")
