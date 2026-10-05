"""Agente que acha o setor oficial coberto pelo GPS, inclusive rotas DIÁRIO fora da grade do dia."""

from __future__ import annotations

from datetime import date
from typing import Any

from .cobertura import avaliar_cobertura, km_gps, recortar_pontos
from .mapa_oficial import _freq_do_dia, carregar_setores


EXTRA_PTS = 500
EXTRA_KM = 12.0
EXTRA_PCT = 40.0


def preparar_setores(dia: date, rotas: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Grade do dia (com DIÁRIO) + setores de outra frequência com coleta densa no GPS."""
    todos = carregar_setores(None)
    if not todos:
        return []
    do_dia = [setor for setor in todos if _freq_do_dia(str(setor.get("frequencia") or ""), dia)]
    vistos = {str(setor.get("codigo") or "") for setor in do_dia}
    extras: list[dict[str, Any]] = []
    for item in rotas or []:
        pontos = item.get("pontos_dia") or item.get("pontos") or []
        if len(pontos) < EXTRA_PTS:
            continue
        for setor in todos:
            codigo = str(setor.get("codigo") or "")
            if not codigo or codigo in vistos:
                continue
            analise = _analisar(setor, pontos)
            if not analise["incluir"]:
                continue
            copiado = dict(setor)
            copiado["detectado_fora_da_grade"] = True
            copiado["motivo_agente"] = analise["motivo"]
            extras.append(copiado)
            vistos.add(codigo)
    return do_dia + extras


def _analisar(setor: dict[str, Any], pontos: list[dict[str, Any]]) -> dict[str, Any]:
    clip = recortar_pontos(pontos, setor["geom"], 90)
    n_pts = len(clip)
    km = km_gps(clip, salto_m=180)
    if n_pts < EXTRA_PTS or km < EXTRA_KM:
        return {"incluir": False, "motivo": "GPS residual, não é coleta neste polígono"}
    cob = avaliar_cobertura(
        [],
        pontos,
        geom_setor=setor["geom"],
        km_oficial=setor.get("km_oficial"),
        ruas=setor.get("ruas") or [],
    )
    pct = float(cob.get("percentual") or 0)
    if pct < EXTRA_PCT:
        return {"incluir": False, "motivo": f"só {pct:.0f}% do km oficial — passagem"}
    return {
        "incluir": True,
        "motivo": (
            f"GPS denso fora da grade do dia: {n_pts} pontos, {km:.1f} km, "
            f"{pct:.0f}% em {setor.get('codigo')} ({setor.get('frequencia')})"
        ),
        "percentual": pct,
        "pontos": n_pts,
        "km": km,
    }
