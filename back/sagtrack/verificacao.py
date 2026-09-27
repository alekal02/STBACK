"""Agente que separa coleta real de passagem pelo polígono do setor."""

from __future__ import annotations

from typing import Any

from .cobertura import PARCIAL_MIN, km_gps

AREA_COLETA_MIN = 22.0
from .gps import haversine_m

VEREDICTOS_OK = {"PARCIAL", "FEITA"}
VEL_COLETA_MAX = 28.0
PONTOS_MIN = 80
KM_MIN = 6.0
PCT_MIN = 25.0
DENS_MIN = 8.0
FRACAO_DO_MELHOR = 0.25


def coberturas_executadas(item: dict[str, Any], *, max_n: int = 5) -> list[dict[str, Any]]:
    cobs = list(item.get("coberturas_inlog") or [])
    if not cobs:
        return []
    melhor_pts = max(int(cob.get("pontos_no_setor") or 0) for cob in cobs) or 1
    melhor_km = max(float(cob.get("km_coleta") or 0) for cob in cobs) or 0.1
    saida = []
    for cob in cobs:
        analise = analisar_execucao(
            cob,
            item,
            melhor_pts=melhor_pts,
            melhor_km=melhor_km,
        )
        cob["execucao"] = analise["execucao"]
        cob["motivo_execucao"] = analise["motivo"]
        if analise["execucao"] in VEREDICTOS_OK:
            saida.append(cob)
    saida.sort(key=lambda row: float(row.get("percentual") or 0), reverse=True)
    return saida[:max_n]


def analisar_execucao(
    cob: dict[str, Any],
    item: dict[str, Any] | None = None,
    *,
    melhor_pts: int = 1,
    melhor_km: float = 0.1,
) -> dict[str, str]:
    pct = float(cob.get("percentual") or 0)
    n_pts = int(cob.get("pontos_no_setor") or 0)
    km = float(cob.get("km_coleta") or 0)
    veredicto = str(cob.get("veredicto") or "").upper()
    dens = n_pts / max(km, 0.5)
    pontos = _pontos_do_setor(cob, item)
    lentos = _fracao_lenta(pontos)
    torto = _tortuosidade(pontos or _pts_trechos(cob))
    atravessou = _parece_atravessar(cob, dens, torto)

    n_dentro = int(cob.get("pontos_dentro") or n_pts)
    if n_dentro < 18:
        return {
            "execucao": "PASSAGEM",
            "motivo": "passou ao lado do polígono sem entrar na rota",
        }
    if str(cob.get("metodo") or "") == "mapa" and pct < AREA_COLETA_MIN:
        return {
            "execucao": "PASSAGEM",
            "motivo": (
                f"o mapa mostra só passagem: GPS pintou {pct:.0f}% do polígono "
                "(não cobre as ruas da rota)"
            ),
        }
    if pct >= 80 and n_dentro >= 18:
        return {
            "execucao": "FEITA",
            "motivo": "o mapa pintou o polígono: coleta concluída neste setor",
        }
    if pct >= PARCIAL_MIN and n_dentro >= 18:
        return {
            "execucao": "PARCIAL",
            "motivo": "o mapa confirma coleta nas ruas do polígono",
        }
    if veredicto in VEREDICTOS_OK and n_pts >= 40 and km >= 4 and pct >= AREA_COLETA_MIN:
        return {
            "execucao": "FEITA" if veredicto == "FEITA" or pct >= 80 else "PARCIAL",
            "motivo": "o mapa confirma coleta nas ruas do polígono",
        }
    if pct >= 80 and n_pts >= PONTOS_MIN and km >= KM_MIN:
        return {"execucao": "FEITA", "motivo": "80% ou mais do km oficial com GPS no setor"}
    if pct >= PARCIAL_MIN and n_pts >= PONTOS_MIN and km >= KM_MIN:
        return {"execucao": "PARCIAL", "motivo": "coleta parcial: volume de GPS compatível com trabalho no setor"}

    if n_pts < PONTOS_MIN or km < KM_MIN or pct < PCT_MIN:
        return {
            "execucao": "PASSAGEM",
            "motivo": "passou pelo polígono sem volume de coleta (poucos pontos, km ou %)",
        }
    if n_pts < FRACAO_DO_MELHOR * melhor_pts and km < FRACAO_DO_MELHOR * melhor_km:
        return {
            "execucao": "PASSAGEM",
            "motivo": "GPS residual do setor vizinho, não é a rota trabalhada no dia",
        }
    if atravessou and pct < PARCIAL_MIN:
        return {
            "execucao": "PASSAGEM",
            "motivo": "trajeto atravessa o setor sem desenho de coleta nas ruas",
        }
    if dens < DENS_MIN and lentos < 0.35:
        return {
            "execucao": "PASSAGEM",
            "motivo": "deslocamento rápido; sem pontos de coleta no setor",
        }
    if pct >= PCT_MIN and dens >= DENS_MIN and n_pts >= 0.25 * melhor_pts:
        return {
            "execucao": "PARCIAL",
            "motivo": "coleta parcial confirmada por densidade de GPS e tempo no setor",
        }
    return {
        "execucao": "PASSAGEM",
        "motivo": "presença no polígono sem evidência de coleta",
    }


def _pontos_do_setor(cob: dict[str, Any], item: dict[str, Any] | None) -> list[dict[str, Any]]:
    clip = cob.get("pontos_clip") or []
    if clip:
        return clip
    return _pts_trechos(cob)


def _pts_trechos(cob: dict[str, Any]) -> list[dict[str, Any]]:
    saida = []
    for trecho in cob.get("trechos_gps") or []:
        for par in trecho or []:
            if not par or len(par) < 2:
                continue
            saida.append({"latitude": par[0], "longitude": par[1]})
    return saida


def _fracao_lenta(pontos: list[dict[str, Any]]) -> float:
    vels = []
    for ponto in pontos or []:
        bruto = ponto.get("velocidade")
        try:
            vel = float(str(bruto).replace(",", ".").replace("km/h", "").strip())
        except (TypeError, ValueError):
            continue
        if vel >= 0:
            vels.append(vel)
    if len(vels) < 8:
        return 0.0
    return sum(1 for vel in vels if vel <= VEL_COLETA_MAX) / len(vels)


def _tortuosidade(pontos: list[dict[str, Any]]) -> float:
    coords = []
    for ponto in pontos or []:
        try:
            coords.append((float(ponto["latitude"]), float(ponto["longitude"])))
        except (KeyError, TypeError, ValueError):
            continue
    if len(coords) < 6:
        return 1.0
    caminho = 0.0
    for a, b in zip(coords, coords[1:]):
        dist = haversine_m(a, b)
        if 0 < dist < 400:
            caminho += dist
    lats = [p[0] for p in coords]
    lons = [p[1] for p in coords]
    diag = haversine_m((min(lats), min(lons)), (max(lats), max(lons)))
    if diag < 80:
        return 1.0
    return caminho / diag


def _parece_atravessar(cob: dict[str, Any], dens: float, torto: float) -> bool:
    trechos = [t for t in (cob.get("trechos_gps") or []) if t and len(t) >= 2]
    if len(trechos) <= 1 and torto < 1.55 and dens < 20:
        return True
    if torto < 1.35 and dens < 12:
        return True
    return False


def km_gps_clip(pontos: list[dict[str, Any]]) -> float:
    return km_gps(pontos)
