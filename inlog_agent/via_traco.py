"""Encaixa o GPS nas vias oficiais do setor.

O rastreador pinta uma reta entre pulsos. No mapa isso atravessa
quarteirão e o diretor lê como linha sem lógica. Aqui o traço só
existe se o pulso encosta na malha oficial e o caminho entre dois
pulsos também é rua da rota. Não inventa coleta: só o desenho.
"""

from __future__ import annotations

import heapq
import math
from typing import Any

from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

from .gps import _datetime_do_timestamp, haversine_m
from .mapa_oficial import TO_UTM, TO_WGS

SNAP_M = 38.0
PASSO_M = 16.0
JUNCAO_M = 8.0
GAP_S = 150.0
MAX_CAMINHO_M = 380.0
MAX_FATOR = 3.4


def tracar_nas_vias(
    pontos: list[dict[str, Any]] | list[list[float]] | None,
    ruas: list[Any] | None,
) -> list[list[list[float]]]:
    malha = MalhaVias(ruas or [])
    if not malha.ok:
        return []
    return malha.tracar(pontos or [])


class MalhaVias:
    def __init__(self, ruas: list[Any]):
        linhas = [ln for ln in ruas if ln is not None and getattr(ln, "length", 0) >= 8]
        self.ok = bool(linhas)
        self._nodes: list[tuple[float, float]] = []
        self._adj: list[list[tuple[int, float]]] = []
        self._tree: STRtree | None = None
        self._linhas = linhas
        if self.ok:
            self._montar(linhas)

    def tracar(self, pontos: list[Any]) -> list[list[list[float]]]:
        snaps: list[tuple[int, float, str]] = []
        for ponto in _ordenar(pontos):
            xy = _xy(ponto)
            if not xy:
                continue
            nodo = self._snap(xy[0], xy[1])
            if nodo is None:
                continue
            snaps.append((nodo, math.hypot(xy[0] - self._nodes[nodo][0], xy[1] - self._nodes[nodo][1]), _ts(ponto)))
        if len(snaps) < 2:
            return []
        trechos: list[list[list[float]]] = []
        atual: list[list[float]] = []
        prev_i, _d0, ts0 = snaps[0]
        atual.extend(self._wgs_nodes([prev_i]))
        for nodo, _dist, ts in snaps[1:]:
            if nodo == prev_i:
                ts0 = ts or ts0
                continue
            dt = _dt_s(ts0, ts)
            euclid = math.hypot(
                self._nodes[nodo][0] - self._nodes[prev_i][0],
                self._nodes[nodo][1] - self._nodes[prev_i][1],
            )
            if dt is not None and dt > GAP_S and euclid > 40:
                if len(atual) >= 2:
                    trechos.append(atual)
                atual = self._wgs_nodes([nodo])
                prev_i, ts0 = nodo, ts
                continue
            caminho = self._dijkstra(prev_i, nodo)
            if not caminho:
                if len(atual) >= 2:
                    trechos.append(atual)
                atual = self._wgs_nodes([nodo])
                prev_i, ts0 = nodo, ts
                continue
            metros = sum(
                math.hypot(
                    self._nodes[a][0] - self._nodes[b][0],
                    self._nodes[a][1] - self._nodes[b][1],
                )
                for a, b in zip(caminho, caminho[1:])
            )
            if metros > MAX_CAMINHO_M or (euclid >= 25 and metros > MAX_FATOR * euclid):
                if len(atual) >= 2:
                    trechos.append(atual)
                atual = self._wgs_nodes([nodo])
                prev_i, ts0 = nodo, ts
                continue
            pts = self._wgs_nodes(caminho)
            if atual and pts:
                atual.extend(pts[1:] if _quase(atual[-1], pts[0]) else pts)
            else:
                atual.extend(pts)
            prev_i, ts0 = nodo, ts or ts0
        if len(atual) >= 2:
            trechos.append(atual)
        return [_limpar(t) for t in trechos if len(t) >= 2]

    def _montar(self, linhas: list[LineString]) -> None:
        bruto: list[tuple[float, float]] = []
        sequencias: list[list[int]] = []
        for linha in linhas:
            seq: list[int] = []
            for x, y in _densificar(linha, PASSO_M):
                bruto.append((float(x), float(y)))
                seq.append(len(bruto) - 1)
            if len(seq) >= 2:
                sequencias.append(seq)
        grupos = _agrupar(bruto, JUNCAO_M)
        self._nodes = []
        mapa: list[int] = [0] * len(bruto)
        centro: dict[int, list[tuple[float, float]]] = {}
        for i, g in enumerate(grupos):
            centro.setdefault(g, []).append(bruto[i])
        grupo_nodo: dict[int, int] = {}
        for g, pts in centro.items():
            mx = sum(p[0] for p in pts) / len(pts)
            my = sum(p[1] for p in pts) / len(pts)
            grupo_nodo[g] = len(self._nodes)
            self._nodes.append((mx, my))
        for i, g in enumerate(grupos):
            mapa[i] = grupo_nodo[g]
        n = len(self._nodes)
        self._adj = [[] for _ in range(n)]
        vistos: set[tuple[int, int]] = set()
        for seq in sequencias:
            for a, b in zip(seq, seq[1:]):
                i, j = mapa[a], mapa[b]
                if i == j:
                    continue
                chave = (min(i, j), max(i, j))
                if chave in vistos:
                    continue
                vistos.add(chave)
                dist = math.hypot(self._nodes[i][0] - self._nodes[j][0], self._nodes[i][1] - self._nodes[j][1])
                if dist < 0.4:
                    continue
                self._adj[i].append((j, dist))
                self._adj[j].append((i, dist))
        self._tree = STRtree(linhas) if linhas else None

    def _snap(self, x: float, y: float) -> int | None:
        if not self._linhas or self._tree is None:
            return None
        pt = Point(x, y)
        try:
            idxs = list(self._tree.query(pt.buffer(SNAP_M)))
        except TypeError:
            idxs = list(range(len(self._linhas)))
        melhor_d = SNAP_M + 1
        proj = None
        for idx in idxs:
            linha = idx if hasattr(idx, "distance") else self._linhas[int(idx)]
            d = linha.distance(pt)
            if d < melhor_d:
                melhor_d = d
                proj = linha.interpolate(linha.project(pt))
        if proj is None or melhor_d > SNAP_M:
            return None
        px, py = float(proj.x), float(proj.y)
        melhor_i = 0
        melhor_n = 1e18
        for i, (nx, ny) in enumerate(self._nodes):
            d = (nx - px) ** 2 + (ny - py) ** 2
            if d < melhor_n:
                melhor_n = d
                melhor_i = i
        if math.sqrt(melhor_n) > SNAP_M + PASSO_M:
            return None
        return melhor_i

    def _dijkstra(self, origem: int, destino: int) -> list[int] | None:
        if origem == destino:
            return [origem]
        inf = MAX_CAMINHO_M + 1
        dist = [inf] * len(self._nodes)
        dist[origem] = 0.0
        prev = [-1] * len(self._nodes)
        heap = [(0.0, origem)]
        while heap:
            custo, u = heapq.heappop(heap)
            if custo > dist[u] or custo > MAX_CAMINHO_M:
                continue
            if u == destino:
                break
            for v, w in self._adj[u]:
                nw = custo + w
                if nw < dist[v] and nw <= MAX_CAMINHO_M:
                    dist[v] = nw
                    prev[v] = u
                    heapq.heappush(heap, (nw, v))
        if dist[destino] >= inf:
            return None
        caminho = [destino]
        cur = destino
        while cur != origem:
            cur = prev[cur]
            if cur < 0:
                return None
            caminho.append(cur)
        caminho.reverse()
        return caminho

    def _wgs_nodes(self, idxs: list[int]) -> list[list[float]]:
        saida = []
        for i in idxs:
            x, y = self._nodes[i]
            lon, lat = TO_WGS.transform(x, y)
            saida.append([float(lat), float(lon)])
        return saida


def _densificar(linha: LineString, passo: float) -> list[tuple[float, float]]:
    coords = list(linha.coords)
    if len(coords) < 2:
        return coords
    if linha.length <= passo:
        return [(float(coords[0][0]), float(coords[0][1])), (float(coords[-1][0]), float(coords[-1][1]))]
    n = max(2, int(linha.length / passo) + 1)
    return [(float(p.x), float(p.y)) for p in (linha.interpolate(i / (n - 1), normalized=True) for i in range(n))]


def _agrupar(pts: list[tuple[float, float]], raio: float) -> list[int]:
    n = len(pts)
    pai = list(range(n))

    def acha(i: int) -> int:
        while pai[i] != i:
            pai[i] = pai[pai[i]]
            i = pai[i]
        return i

    cell = max(raio, 1.0)
    buckets: dict[tuple[int, int], list[int]] = {}
    for i, (x, y) in enumerate(pts):
        buckets.setdefault((int(x // cell), int(y // cell)), []).append(i)
    r2 = raio * raio
    for (cx, cy), idxs in buckets.items():
        viz: list[int] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                viz.extend(buckets.get((cx + dx, cy + dy), ()))
        for i in idxs:
            x1, y1 = pts[i]
            ai = acha(i)
            for j in viz:
                if j <= i:
                    continue
                x2, y2 = pts[j]
                if (x1 - x2) ** 2 + (y1 - y2) ** 2 <= r2:
                    aj = acha(j)
                    if ai != aj:
                        pai[aj] = ai
                        ai = acha(i)
    return [acha(i) for i in range(n)]


def _xy(ponto: Any) -> tuple[float, float] | None:
    if isinstance(ponto, dict):
        utm = ponto.get("_utm")
        if utm and len(utm) >= 2:
            return float(utm[0]), float(utm[1])
        try:
            lat, lon = float(ponto["latitude"]), float(ponto["longitude"])
        except (KeyError, TypeError, ValueError):
            return None
        x, y = TO_UTM.transform(lon, lat)
        return float(x), float(y)
    if isinstance(ponto, (list, tuple)) and len(ponto) >= 2:
        try:
            lat, lon = float(ponto[0]), float(ponto[1])
        except (TypeError, ValueError):
            return None
        x, y = TO_UTM.transform(lon, lat)
        return float(x), float(y)
    return None


def _ts(ponto: Any) -> str:
    if isinstance(ponto, dict):
        return str(ponto.get("timestamp") or "")
    return ""


def _ordenar(pontos: list[Any]) -> list[Any]:
    dicts = [p for p in pontos or [] if isinstance(p, dict)]
    if len(dicts) == len(list(pontos or [])):
        return sorted(dicts, key=lambda p: str(p.get("timestamp") or ""))
    return list(pontos or [])


def _dt_s(a: str, b: str) -> float | None:
    if not a or not b:
        return None
    from datetime import date

    da = _datetime_do_timestamp(a, date.today())
    db = _datetime_do_timestamp(b, date.today())
    if da is None or db is None:
        return None
    return abs((db - da).total_seconds())


def _quase(a: list[float], b: list[float]) -> bool:
    try:
        return haversine_m((float(a[0]), float(a[1])), (float(b[0]), float(b[1]))) < 4
    except (TypeError, ValueError, IndexError):
        return False


def _limpar(pts: list[list[float]]) -> list[list[float]]:
    saida: list[list[float]] = []
    for par in pts:
        if saida and _quase(saida[-1], par):
            continue
        saida.append(par)
    return saida
