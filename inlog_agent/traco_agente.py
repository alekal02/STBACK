"""Liga o pulso do rastreador sem inventar coleta.

O compactador manda um ponto a cada 30–90 s. Em marcha de coleta isso dá
200–500 m entre pulsos. Cortar em 95 m ou jogar fora trecho curto
quebra o nexo dentro do polígono. Aqui:

- linha contínua = pulsos reais em sequência
- linha tracejada = lacuna de sinal (só desenho, não entra no %)
- teleporte grande continua cortado
"""

from __future__ import annotations

import math
from typing import Any

from .gps import haversine_m, tracos_realistas

Ponto = tuple[float, float]
VEL_ABSURDA = 88.0
JUNTA_M = 480.0
LACUNA_M = 780.0
TELEPORTE_M = 1400.0
SALTO_RUA_M = 95.0
SALTO_MAX_M = 220.0
ANGULO_RUA = 42.0
VEL_DESLOC = 48.0
MIN_PTS_TRECHO = 2
MIN_M_TRECHO = 25.0


def aplicar_traco(rotas: list[dict[str, Any]]) -> None:
    for item in rotas or []:
        pontos = list(item.get("pontos_dia") or item.get("pontos") or [])
        limpos = limpar_pontos(pontos)
        item["pontos_traco"] = limpos
        item["trechos"] = trechos_uniformes(limpos)
        item["traco_agente"] = {
            "pontos_origem": len(pontos),
            "pontos_limpos": len(limpos),
            "trechos": len(item["trechos"]),
        }


def limpar_pontos(pontos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordenados = sorted(pontos or [], key=_chave_tempo)
    saida: list[dict[str, Any]] = []
    for i, ponto in enumerate(ordenados):
        par = _xy(ponto)
        if not par:
            continue
        if saida:
            anterior = _xy(saida[-1])
            if anterior and haversine_m(anterior, par) < 2.5:
                continue
        if 0 < i < len(ordenados) - 1:
            prev = _xy(ordenados[i - 1])
            nxt = _xy(ordenados[i + 1])
            if prev and nxt:
                d_prev = haversine_m(prev, par)
                d_next = haversine_m(par, nxt)
                d_viz = haversine_m(prev, nxt)
                if d_prev > TELEPORTE_M and d_next > TELEPORTE_M:
                    continue
                if d_prev > 250 and d_next > 250 and d_viz < min(d_prev, d_next) * 0.55:
                    continue
        if _velocidade(ponto) > VEL_ABSURDA:
            continue
        saida.append(ponto)
    return saida


def montar_traco_continuo(pontos: list[dict[str, Any]] | None) -> dict[str, list]:
    """Monta o traço pelos pulsos na ordem da hora. Não inventa ponto no meio."""
    itens = list(pontos or [])
    if itens and isinstance(itens[0], dict):
        itens = sorted(itens, key=_chave_tempo)
    pares: list[Ponto] = []
    for ponto in itens:
        par = _xy(ponto) if isinstance(ponto, dict) else _par_lista(ponto)
        if par:
            pares.append(par)
    trechos, lacunas = _partir_pulsos(pares)
    return {"trechos": trechos, "lacunas": lacunas}


def trechos_uniformes(pontos: list[dict[str, Any]]) -> list[list[list[float]]]:
    return montar_traco_continuo(pontos)["trechos"]


def _par_lista(par: Any) -> Ponto | None:
    if not par or not isinstance(par, (list, tuple)) or len(par) < 2:
        return None
    try:
        return (float(par[0]), float(par[1]))
    except (TypeError, ValueError):
        return None


def _partir_pulsos(pares: list[Ponto]) -> tuple[list[list[list[float]]], list[list[list[float]]]]:
    if len(pares) < 2:
        if len(pares) == 1:
            pt = [pares[0][0], pares[0][1]]
            return [[pt, pt]], []
        return [], []

    from .gps import _rdp

    brutos: list[list[Ponto]] = []
    lacunas: list[list[list[float]]] = []
    atual = [pares[0]]
    for cur in pares[1:]:
        dist = haversine_m(atual[-1], cur)
        if dist <= JUNTA_M:
            if dist >= 2.0:
                atual.append(cur)
            continue
        if dist <= LACUNA_M:
            if len(atual) >= 2:
                brutos.append(atual)
            lacunas.append([[atual[-1][0], atual[-1][1]], [cur[0], cur[1]]])
            atual = [cur]
            continue
        if len(atual) >= 2:
            brutos.append(atual)
        atual = [cur]
    if len(atual) >= 2:
        brutos.append(atual)
    elif atual and brutos and haversine_m(brutos[-1][-1], atual[0]) <= LACUNA_M:
        lacunas.append(
            [[brutos[-1][-1][0], brutos[-1][-1][1]], [atual[0][0], atual[0][1]]]
        )
        brutos[-1].append(atual[0])

    if not brutos:
        return tracos_realistas(
            [{"latitude": p[0], "longitude": p[1]} for p in pares],
            salto_max_m=JUNTA_M,
            tolerancia_m=4,
        ), lacunas

    trechos: list[list[list[float]]] = []
    for trecho in brutos:
        metros = sum(haversine_m(a, b) for a, b in zip(trecho, trecho[1:]))
        if len(trecho) < 2 or metros < MIN_M_TRECHO:
            if len(trecho) >= 2:
                trechos.append([[p[0], p[1]] for p in trecho])
            continue
        limpo = _rdp(trecho, 4.0)
        if len(limpo) < 2:
            limpo = [trecho[0], trecho[-1]]
        trechos.append([[lat, lon] for lat, lon in limpo])
    return trechos, lacunas


def ligar_trechos(
    trechos: list[list[list[float]]] | None,
) -> dict[str, list]:
    """Reata fragmentos vizinhos sem inventar vértice: junta ou marca lacuna."""
    limpos = [t for t in (trechos or []) if t and len(t) >= 2]
    if not limpos:
        return {"trechos": [], "lacunas": []}
    juntos: list[list[list[float]]] = [list(limpos[0])]
    lacunas: list[list[list[float]]] = []
    for trecho in limpos[1:]:
        a = juntos[-1][-1]
        b = trecho[0]
        try:
            dist = haversine_m((float(a[0]), float(a[1])), (float(b[0]), float(b[1])))
        except (TypeError, ValueError, IndexError):
            juntos.append(list(trecho))
            continue
        if dist <= JUNTA_M:
            juntos[-1].extend(trecho)
        elif dist <= LACUNA_M:
            lacunas.append([a, b])
            juntos.append(list(trecho))
        else:
            juntos.append(list(trecho))
    return {"trechos": juntos, "lacunas": lacunas}


def recortar_trechos_ao_setor(
    trechos: list[list[list[float]]],
    aneis: list[list[list[float]]],
    buffer_m: float = 40,
) -> list[list[list[float]]]:
    """Mantém o GPS do setor. Saiu do polígono ou salto grande: corta, não inventa reta."""
    if not trechos:
        return []
    if not aneis:
        return [t for t in trechos if t and len(t) >= 2]
    from shapely.geometry import Point, Polygon

    from .mapa_oficial import TO_UTM

    raio = max(0.0, float(buffer_m))
    zonas = []
    for anel in aneis:
        xy = []
        for par in anel or []:
            if not par or len(par) < 2:
                continue
            x, y = TO_UTM.transform(float(par[1]), float(par[0]))
            xy.append((x, y))
        if len(xy) >= 4:
            zonas.append(Polygon(xy).buffer(raio))
    if not zonas:
        return [t for t in trechos if t and len(t) >= 2]

    def _dentro(par: list[float]) -> bool:
        x, y = TO_UTM.transform(float(par[1]), float(par[0]))
        return any(zona.covers(Point(x, y)) for zona in zonas)

    saida: list[list[list[float]]] = []
    for trecho in trechos:
        pts = [par for par in trecho if par and len(par) >= 2]
        if not pts:
            continue
        atual: list[list[float]] = []
        for par in pts:
            try:
                ok = _dentro(par)
            except (TypeError, ValueError):
                ok = False
            if not ok:
                if len(atual) >= 2:
                    saida.append(atual)
                atual = []
                continue
            if atual:
                try:
                    dist = haversine_m(
                        (float(atual[-1][0]), float(atual[-1][1])),
                        (float(par[0]), float(par[1])),
                    )
                except (TypeError, ValueError):
                    dist = JUNTA_M + 1
                if dist > TELEPORTE_M:
                    if len(atual) >= 2:
                        saida.append(atual)
                    atual = [par]
                    continue
            atual.append(par)
        if len(atual) >= 2:
            saida.append(atual)
    return saida


def _xy(ponto: dict[str, Any]) -> Ponto | None:
    try:
        return (float(ponto["latitude"]), float(ponto["longitude"]))
    except (KeyError, TypeError, ValueError):
        return None


def _chave_tempo(ponto: dict[str, Any]) -> str:
    ts = str(ponto.get("timestamp") or "")
    if "/" in ts and len(ts) >= 10:
        dia, resto = ts[:10], ts[10:]
        partes = dia.split("/")
        if len(partes) == 3:
            return f"{partes[2]}-{partes[1]}-{partes[0]}{resto}"
    return ts


def _velocidade(ponto: dict[str, Any]) -> float:
    bruto = ponto.get("velocidade")
    try:
        return float(str(bruto).replace(",", ".").replace("km/h", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _rumo(a: Ponto, b: Ponto) -> float:
    return math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))


def _mesmo_rumo(a: Ponto, b: Ponto, c: Ponto) -> bool:
    if haversine_m(a, b) < 8 or haversine_m(b, c) < 8:
        return True
    d = abs(_rumo(a, b) - _rumo(b, c)) % 360
    d = min(d, 360 - d)
    return d <= ANGULO_RUA
