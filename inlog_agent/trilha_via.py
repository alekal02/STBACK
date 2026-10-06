"""Ajuste visual da trajetoria as vias oficiais.

Nao calcula cobertura, percentual nem status. So decide quais pedacos
da linha de GPS podem aparecer no PDF sem cortar quadra.
"""

from __future__ import annotations

import math
from typing import Any

# Ponto aceito se cair perto de uma via oficial.
RAIO_M = 40.0
# Corda longa demais, mesmo que roce uma via no meio.
MAX_CORDA_M = 280.0
# A corda inteira precisa continuar em cima da malha.
FOLGA_M = 18.0


def vias_de(*grupos: list) -> list[list[tuple[float, float]]]:
    """Junta trechos (lon, lat) ja gravados. Nao le o banco."""
    malha: list[list[tuple[float, float]]] = []
    for grupo in grupos:
        for linha in grupo or []:
            seq = []
            for par in linha:
                if isinstance(par, (list, tuple)) and len(par) >= 2:
                    seq.append((float(par[0]), float(par[1])))
            if len(seq) >= 2:
                malha.append(seq)
    return malha


def trilhas_na_via(linhas: list, malha: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    """Devolve pedacos (lon, lat) colados na malha. Descarta o que sai da rota."""
    segmentos = _segmentos(malha)
    if not segmentos:
        return []
    grade = _grade(segmentos)
    saida: list[list[tuple[float, float]]] = []
    for linha in linhas or []:
        bruto = [p for p in (_par(ponto) for ponto in _afinar(linha)) if p is not None]
        ajustados = [_projetar(p, grade) for p in bruto]
        pedaco: list[tuple[float, float]] = []
        anterior: tuple[float, float] | None = None
        for ponto in ajustados:
            if ponto is None:
                if len(pedaco) >= 2:
                    saida.append(pedaco)
                pedaco = []
                anterior = None
                continue
            if anterior is not None and not _corda_na_via(anterior, ponto, grade):
                if len(pedaco) >= 2:
                    saida.append(pedaco)
                pedaco = [ponto]
            elif not pedaco or _metros(pedaco[-1], ponto) >= 4:
                pedaco.append(ponto)
            anterior = ponto
        if len(pedaco) >= 2:
            saida.append(pedaco)
    return saida


def _par(ponto: Any) -> tuple[float, float] | None:
    if isinstance(ponto, (list, tuple)) and len(ponto) >= 2:
        return float(ponto[1]), float(ponto[0])
    return None


def _afinar(linha: list) -> list:
    """Tira pontos quase parados. Conserva a ordem e as curvas."""
    if len(linha) <= 2:
        return list(linha)
    saida = [linha[0]]
    ultimo = _par(linha[0])
    for ponto in linha[1:-1]:
        atual = _par(ponto)
        if ultimo is None or atual is None or _metros(ultimo, atual) >= 12:
            saida.append(ponto)
            ultimo = atual or ultimo
    saida.append(linha[-1])
    return saida


def _segmentos(malha: list[list[tuple[float, float]]]) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    segs = []
    for linha in malha:
        for a, b in zip(linha, linha[1:]):
            if _metros(a, b) >= 0.5:
                segs.append((a, b))
    return segs


def _grade(segmentos: list) -> dict[tuple[int, int], list]:
    grade: dict[tuple[int, int], list] = {}
    for seg in segmentos:
        a, b = seg
        chaves = {_chave(a), _chave(b)}
        passos = max(1, int(_metros(a, b) / 70))
        for i in range(passos + 1):
            t = i / passos
            chaves.add(_chave((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)))
        for chave in chaves:
            grade.setdefault(chave, []).append(seg)
    return grade


def _chave(ponto: tuple[float, float]) -> tuple[int, int]:
    lon, lat = ponto
    return int(math.floor(lon / 0.0008)), int(math.floor(lat / 0.0008))


def _vizinhos(ponto: tuple[float, float], grade: dict) -> list:
    cx, cy = _chave(ponto)
    vistos = []
    marca = set()
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for seg in grade.get((cx + dx, cy + dy), ()):
                ident = id(seg)
                if ident not in marca:
                    marca.add(ident)
                    vistos.append(seg)
    return vistos


def _projetar(ponto: tuple[float, float], grade: dict) -> tuple[float, float] | None:
    melhor = None
    dist = RAIO_M + 1
    for a, b in _vizinhos(ponto, grade):
        proj, d = _no_segmento(ponto, a, b)
        if d < dist:
            dist = d
            melhor = proj
    if melhor is None or dist > RAIO_M:
        return None
    return melhor


def _corda_na_via(a: tuple[float, float], b: tuple[float, float], grade: dict) -> bool:
    if _metros(a, b) > MAX_CORDA_M:
        return False
    for fracao in (0.25, 0.5, 0.75):
        meio = (a[0] + (b[0] - a[0]) * fracao, a[1] + (b[1] - a[1]) * fracao)
        if _distancia_malha(meio, grade) > FOLGA_M:
            return False
    return True


def _distancia_malha(ponto: tuple[float, float], grade: dict) -> float:
    segs = _vizinhos(ponto, grade)
    if not segs:
        return 1e9
    return min(_no_segmento(ponto, a, b)[1] for a, b in segs)


def _no_segmento(
    ponto: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
) -> tuple[tuple[float, float], float]:
    lon0, lat0 = ponto
    ax, ay = _xy(a, lon0, lat0)
    bx, by = _xy(b, lon0, lat0)
    dx, dy = bx - ax, by - ay
    base = dx * dx + dy * dy
    t = 0.0 if base <= 1e-9 else max(0.0, min(1.0, (-ax * dx - ay * dy) / base))
    # ponto esta na origem local
    qx, qy = ax + t * dx, ay + t * dy
    lon = a[0] + (b[0] - a[0]) * t
    lat = a[1] + (b[1] - a[1]) * t
    return (lon, lat), math.hypot(qx, qy)


def _xy(ponto: tuple[float, float], lon0: float, lat0: float) -> tuple[float, float]:
    lon, lat = ponto
    x = (lon - lon0) * 111_320.0 * math.cos(math.radians(lat0))
    y = (lat - lat0) * 110_540.0
    return x, y


def _metros(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(*_xy(b, a[0], a[1]))
