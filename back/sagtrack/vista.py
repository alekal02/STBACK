"""Link temporário do relatório. A página abre com os dados já salvos; o PDF só nasce no download."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import Settings, pasta_dados

_HORAS = 6
_TIPOS = {"frota", "rota", "placa"}


def _destinos_chave() -> list[Path]:
    destinos = [pasta_dados() / "link.key"]
    output = os.getenv("OUTPUT_DIR", "").strip()
    if output:
        destinos.append(Path(output) / "link.key")
    return destinos


def _chave() -> bytes:
    """A mesma chave em todo reinício. Um arquivo novo a cada deploy invalidava o link (410)."""
    semente = (
        os.getenv("SAGTRACK_LINK_SECRET", "").strip()
        or os.getenv("FULLTRACK_SECRET_KEY", "").strip()
        or os.getenv("WUZAPI_ADMIN_TOKEN", "").strip()
    )
    if semente:
        chave = hashlib.sha256(b"segtrack-link-v1\n" + semente.encode()).hexdigest().encode()
    else:
        chave = b""
        for arquivo in _destinos_chave():
            if arquivo.is_file():
                salvo = arquivo.read_bytes().strip()
                if len(salvo) >= 16:
                    chave = salvo
                    break
        if not chave:
            chave = secrets.token_hex(32).encode()
    for arquivo in _destinos_chave():
        try:
            if not arquivo.is_file() or arquivo.read_bytes().strip() != chave:
                arquivo.parent.mkdir(parents=True, exist_ok=True)
                arquivo.write_bytes(chave)
        except OSError:
            continue
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


def _malha(linhas: list, limite: int = 180, max_pts: int = 40) -> list[list[list[float]]]:
    saida = []
    for linha in linhas or []:
        fina = _afinar(linha, max_pts)
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
    from .pdfmapa import (
        _aneis,
        _cor_placa,
        _eixos_da_rota,
        _ler,
        _ruas,
        _trajeto_do_caminhao,
    )
    from .relatorios import balanco_frota

    dia = date.fromisoformat(str(claims["d"]))
    pasta = settings.output_dir / dia.isoformat()
    setores = _ler(pasta, "setores.json") or []
    frota = _ler(pasta, "frota.json") or []
    resumo = _ler(pasta, "resumo.json") or {}
    chave = (codigo or str(claims.get("q") or "")).strip().upper()
    setor = next((item for item in setores if str(item.get("codigo") or "").upper() == chave), None)
    balanco = balanco_frota(setores, frota)
    row = next((item for item in balanco if str(item.get("codigo") or "").upper() == chave), None)
    feitos, faltando = _ruas(frota, str((setor or {}).get("codigo") or chave))
    mostrar_aberto = str(claims.get("t") or "") == "rota"
    aneis = _aneis(row or setor or {})
    malha = list(feitos or []) + list(faltando or [])
    if not malha:
        malha = _eixos_da_rota(dia, chave)

    gps_dia = {
        str(c.get("placa") or ""): list(c.get("gps") or [])
        for c in (resumo.get("caminhoes") or [])
    }
    placas = [str(p) for p in ((row or {}).get("placas") or []) if str(p).strip()]
    if not placas:
        # frota item plaques that covered this route
        for item in frota:
            for cob in item.get("coberturas_inlog") or []:
                if str(cob.get("codigo_rota") or "").upper() == chave:
                    placa = str(item.get("placa") or "")
                    if placa and placa not in placas:
                        placas.append(placa)

    trajetos: list[dict[str, Any]] = []
    for placa in placas:
        trechos_rota: list = []
        for item in frota:
            if str(item.get("placa") or "") != placa:
                continue
            for cob in item.get("coberturas_inlog") or []:
                if str(cob.get("codigo_rota") or "").upper() != chave:
                    continue
                trechos_rota = list(cob.get("trechos_gps") or [])
                break
        gps = trechos_rota if trechos_rota else (gps_dia.get(placa) or [])
        colado = _trajeto_do_caminhao(aneis, gps, malha or None)
        if not colado:
            continue
        r, g, b = _cor_placa(placa)
        trajetos.append(
            {
                "placa": placa,
                "cor": f"#{r:02x}{g:02x}{b:02x}",
                "tracos": _malha(colado, 80, 120),
            }
        )

    return {
        "codigo": str((setor or row or {}).get("codigo") or chave),
        "nome": str((setor or row or {}).get("nome") or ""),
        "situacao": (row or {}).get("situacao") or "",
        "percentual": (row or {}).get("percentual"),
        "poligonos": _malha((setor or {}).get("poligonos") or aneis, 8, 60),
        "feitos": _malha(feitos, 200, 60) if str(claims.get("t") or "") == "rota" else [],
        "abertas": _malha(faltando, 180, 40) if mostrar_aberto else [],
        "trajetos": trajetos,
        "mapbox_token": (
            os.getenv("MAPBOX_ACCESS_TOKEN") or os.getenv("MAPBOX_TOKEN") or ""
        ).strip(),
    }


def pdf(settings: Settings, claims: dict[str, Any]) -> tuple[int, bytes, str]:
    from .whatsapp import entrega_pdf

    dia = date.fromisoformat(str(claims["d"]))
    return entrega_pdf(settings, str(claims["t"]), str(claims.get("q") or ""), dia)


def html_pagina() -> str:
    return Path(__file__).with_name("vista.html").read_text(encoding="utf-8")
