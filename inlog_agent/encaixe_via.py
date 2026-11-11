"""Liga dois pulsos pelo caminho da malha oficial.

A corda curta, até 160 m, continua na regra já homologada. O vão de cerca
de um minuto só entra se a via tiver um caminho único, dentro da velocidade
de coleta. Atalho de quarteirão, empate entre dois caminhos ou intervalo
longo não viram trecho executado.
"""

from __future__ import annotations

import heapq
import math
from typing import Any

from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

RAIO_M = 45.0
SALTO_RETO_M = 160.0
INFERIR_MAX_S = 100.0
INFERIR_MAX_M = 520.0
FOLGA_ARCO = 1.65
RAZAO_OUTRO = 1.20
AFASTAMENTO_M = 30.0
VEL_COLETA_MAX = 28.0


class Passo:
    def __init__(self, indices: list[int], coords: list[tuple[float, float]], metros: float) -> None:
        self.indices = indices
        self.coords = coords
        self.metros = metros


class _Aresta:
    def __init__(self, a: tuple[int, int], b: tuple[int, int], comp: float, pai: int, coords: list[tuple[float, float]]) -> None:
        self.a = a
        self.b = b
        self.comp = comp
        self.pai = pai
        self.coords = coords

    def outro(self, no: tuple[int, int]) -> tuple[int, int]:
        return self.b if no == self.a else self.a

    def desde(self, no: tuple[int, int]) -> list[tuple[float, float]]:
        if no == self.a:
            return self.coords
        return list(reversed(self.coords))


class _Ancora:
    def __init__(self, eid: int, proj: float, pai: int) -> None:
        self.eid = eid
        self.proj = proj
        self.pai = pai
        self.no_a: tuple[int, int] = (0, 0)
        self.no_b: tuple[int, int] = (0, 0)
        self.custo_a = 0.0
        self.custo_b = 0.0
        self.coords_a: list[tuple[float, float]] = []
        self.coords_b: list[tuple[float, float]] = []


class Malha:
    """Grafo das vias já em metros. O mesmo objeto serve vários pulsos."""

    def __init__(self, linhas: list[Any] | None) -> None:
        self.linhas = []
        self.origem: list[int] = []
        for indice, linha in enumerate(linhas or []):
            if _util(linha):
                self.linhas.append(linha)
                self.origem.append(indice)
        self.arestas: list[_Aresta] = []
        self.adj: dict[tuple[int, int], list[int]] = {}
        self.geoms: list[LineString] = []
        self.arvore: STRtree | None = None
        if self.linhas:
            self._montar()

    def inferir(
        self,
        a: tuple[float, float],
        b: tuple[float, float],
        segundos: float,
        max_kmh: float = VEL_COLETA_MAX,
    ) -> Passo | None:
        if not self.arestas or segundos < 1 or segundos > INFERIR_MAX_S:
            return None
        reta = math.hypot(b[0] - a[0], b[1] - a[1])
        if reta <= SALTO_RETO_M or reta > INFERIR_MAX_M:
            return None
        ancora_a = self._ancorar(a)
        ancora_b = self._ancorar(b)
        if ancora_a is None or ancora_b is None:
            return None
        if ancora_a.eid == ancora_b.eid:
            return self._na_mesma(ancora_a, ancora_b, reta, segundos, max_kmh)
        achado = self._caminho(ancora_a, ancora_b, set())
        if achado is None:
            return None
        metros, coords, eids, comprimentos = achado
        if not self._cabe(metros, reta, segundos, max_kmh):
            return None
        proibir = set(eids) - {ancora_a.eid, ancora_b.eid}
        if proibir:
            outro = self._caminho(ancora_a, ancora_b, proibir)
            if outro is not None and self._empata(metros, coords, outro[0], outro[1]):
                return None
        indices = self._indices(comprimentos)
        if not indices:
            return None
        return Passo(indices, coords, metros)

    def trilha(
        self,
        a_ll: tuple[float, float],
        b_ll: tuple[float, float],
        segundos: float,
    ) -> list[tuple[float, float]] | None:
        """(lat, lon) do pulso até o seguinte, colado na via. None se o vão não cabe."""
        from .cobertura import TO_UTM, TO_WGS

        ax, ay = TO_UTM.transform(float(a_ll[1]), float(a_ll[0]))
        bx, by = TO_UTM.transform(float(b_ll[1]), float(b_ll[0]))
        passo = self.inferir((ax, ay), (bx, by), segundos)
        if passo is None:
            return None
        saida: list[tuple[float, float]] = []
        for x, y in ((ax, ay), *passo.coords, (bx, by)):
            lon, lat = TO_WGS.transform(x, y)
            par = (float(lat), float(lon))
            if not saida or abs(saida[-1][0] - par[0]) > 1e-7 or abs(saida[-1][1] - par[1]) > 1e-7:
                saida.append(par)
        return saida if len(saida) >= 2 else None

    def _montar(self) -> None:
        cortes: list[list[float]] = [[0.0, float(linha.length)] for linha in self.linhas]
        arvore = STRtree(self.linhas)
        for i, linha in enumerate(self.linhas):
            try:
                vizinhos = [int(item) for item in arvore.query(linha)]
            except Exception:
                vizinhos = []
            for j in vizinhos:
                if j == i:
                    continue
                outra = self.linhas[j]
                try:
                    cruz = linha.intersection(outra)
                except Exception:
                    continue
                for pt in _pontos(cruz):
                    cortes[i].append(_proj(linha, pt))
                    cortes[j].append(_proj(outra, pt))
            for extremo in (Point(linha.coords[0]), Point(linha.coords[-1])):
                try:
                    perto = [int(item) for item in arvore.query(extremo.buffer(2.5))]
                except Exception:
                    perto = []
                for j in perto:
                    if j == i:
                        continue
                    outra = self.linhas[j]
                    if outra.distance(extremo) > 2.5:
                        continue
                    cortes[j].append(_proj(outra, extremo))
        for i, linha in enumerate(self.linhas):
            marcas = sorted({round(min(linha.length, max(0.0, d)), 2) for d in cortes[i]})
            for d0, d1 in zip(marcas, marcas[1:]):
                if d1 - d0 < 0.5:
                    continue
                coords = _fatia(linha, d0, d1)
                if len(coords) < 2:
                    continue
                na, nb = _no(coords[0]), _no(coords[-1])
                if na == nb:
                    continue
                eid = len(self.arestas)
                self.arestas.append(_Aresta(na, nb, d1 - d0, i, coords))
                self.adj.setdefault(na, []).append(eid)
                self.adj.setdefault(nb, []).append(eid)
                self.geoms.append(LineString(coords))
        if self.geoms:
            self.arvore = STRtree(self.geoms)

    def _ancorar(self, ponto: tuple[float, float]) -> _Ancora | None:
        if self.arvore is None:
            return None
        pt = Point(ponto)
        try:
            ids = [int(item) for item in self.arvore.query(pt.buffer(RAIO_M))]
        except Exception:
            ids = []
        medidos = []
        for eid in ids:
            dist = self.geoms[eid].distance(pt)
            if dist <= RAIO_M:
                medidos.append((dist, eid))
        if not medidos:
            return None
        medidos.sort()
        _dist, eid = medidos[0]
        pai = self.arestas[eid].pai
        for dist2, eid2 in medidos[1:]:
            if dist2 > _dist * 2.0 + 0.5:
                break
            pai2 = self.arestas[eid2].pai
            if pai2 == pai or self.linhas[pai].distance(self.linhas[pai2]) <= 1.0:
                continue
            return None
        geom = self.geoms[eid]
        proj = _proj(geom, pt)
        ancora = _Ancora(eid, proj, pai)
        aresta = self.arestas[eid]
        ancora.no_a = aresta.a
        ancora.no_b = aresta.b
        ancora.custo_a = proj
        ancora.custo_b = max(0.0, aresta.comp - proj)
        ancora.coords_a = _fatia(geom, proj, 0.0)
        ancora.coords_b = _fatia(geom, proj, aresta.comp)
        return ancora

    def _na_mesma(
        self,
        a: _Ancora,
        b: _Ancora,
        reta: float,
        segundos: float,
        max_kmh: float,
    ) -> Passo | None:
        along = abs(a.proj - b.proj)
        if not self._cabe(along, reta, segundos, max_kmh):
            return None
        geom = self.geoms[a.eid]
        coords = _fatia(geom, a.proj, b.proj)
        volta = self._caminho(a, b, {a.eid})
        if volta is not None and self._empata(along, coords, volta[0], volta[1]):
            return None
        comprimentos = {a.pai: along}
        indices = self._indices(comprimentos)
        if not indices:
            return None
        return Passo(indices, coords, along)

    def _caminho(
        self,
        a: _Ancora,
        b: _Ancora,
        proibir: set[int],
    ) -> tuple[float, list[tuple[float, float]], list[int], dict[int, float]] | None:
        inicio = ("ini", id(a), id(b), len(proibir))
        dist: dict[Any, float] = {}
        prev: dict[Any, tuple[Any, list[tuple[float, float]], int, int | None]] = {}
        heap: list[tuple[float, int, Any]] = []
        seq = 0
        chegada = {
            b.no_a: (b.custo_a, list(reversed(b.coords_a)), b.pai),
            b.no_b: (b.custo_b, list(reversed(b.coords_b)), b.pai),
        }
        for no, custo, coords, pai in (
            (a.no_a, a.custo_a, a.coords_a, a.pai),
            (a.no_b, a.custo_b, a.coords_b, a.pai),
        ):
            if custo < dist.get(no, 1e18):
                dist[no] = custo
                prev[no] = (inicio, coords, pai, a.eid if custo > 0 else None)
                seq += 1
                heapq.heappush(heap, (custo, seq, no))
        melhor: tuple[float, Any, list[tuple[float, float]], int] | None = None
        vistos = 0
        while heap:
            custo, _ordem, no = heapq.heappop(heap)
            if custo != dist.get(no):
                continue
            vistos += 1
            if vistos > 4000 or custo > INFERIR_MAX_M:
                break
            if no in chegada:
                extra, pedaco, pai = chegada[no]
                total = custo + extra
                if melhor is None or total < melhor[0]:
                    melhor = (total, no, pedaco, pai)
                if custo >= (melhor[0] if melhor else total):
                    continue
            if melhor is not None and custo > melhor[0]:
                break
            for eid in self.adj.get(no, ()):
                if eid in proibir:
                    continue
                aresta = self.arestas[eid]
                nxt = aresta.outro(no)
                novo = custo + aresta.comp
                if novo >= dist.get(nxt, 1e18) or novo > INFERIR_MAX_M:
                    continue
                dist[nxt] = novo
                prev[nxt] = (no, aresta.desde(no), aresta.pai, eid)
                seq += 1
                heapq.heappush(heap, (novo, seq, nxt))
        if melhor is None or melhor[0] > INFERIR_MAX_M:
            return None
        total, no, pedaco_fim, pai_fim = melhor
        coords: list[tuple[float, float]] = []
        eids: list[int] = []
        comprimentos: dict[int, float] = {}
        cadeia = []
        atual: Any = no
        while atual != inicio:
            anterior, pedaco, pai, eid = prev[atual]
            cadeia.append((pedaco, pai, eid))
            atual = anterior
        for pedaco, pai, eid in reversed(cadeia):
            if eid is not None:
                eids.append(eid)
            comprimentos[pai] = comprimentos.get(pai, 0.0) + _comp(pedaco)
            _juntar(coords, pedaco)
        comprimentos[pai_fim] = comprimentos.get(pai_fim, 0.0) + _comp(pedaco_fim)
        _juntar(coords, pedaco_fim)
        return total, coords, eids, comprimentos

    def _cabe(self, metros: float, reta: float, segundos: float, max_kmh: float) -> bool:
        if metros < 8 or metros > INFERIR_MAX_M or reta <= 0:
            return False
        if metros / reta > FOLGA_ARCO:
            return False
        return metros / segundos * 3.6 <= max_kmh

    def _empata(
        self,
        primeiro: float,
        coords: list[tuple[float, float]],
        segundo: float,
        outros: list[tuple[float, float]],
    ) -> bool:
        if segundo > primeiro * RAZAO_OUTRO or len(coords) < 2 or len(outros) < 2:
            return False
        base = LineString(coords)
        return max(base.distance(Point(ponto)) for ponto in outros) > AFASTAMENTO_M

    def _indices(self, comprimentos: dict[int, float]) -> list[int]:
        saida = []
        for pai, comp in comprimentos.items():
            linha = self.linhas[pai]
            limite = 8.0 if linha.length >= 16 else max(2.0, linha.length * 0.5)
            if comp >= limite - 0.1:
                saida.append(self.origem[pai])
        return saida


def _util(linha: Any) -> bool:
    return getattr(linha, "geom_type", "") == "LineString" and not linha.is_empty and linha.length >= 2 and len(linha.coords) >= 2


def _no(ponto: tuple[float, float]) -> tuple[int, int]:
    return (int(round(ponto[0])), int(round(ponto[1])))


def _proj(linha: LineString, ponto: Point) -> float:
    return min(float(linha.length), max(0.0, float(linha.project(ponto))))


def _pontos(geom: Any) -> list[Point]:
    if geom is None or geom.is_empty:
        return []
    tipo = geom.geom_type
    if tipo == "Point":
        return [geom]
    if tipo == "MultiPoint":
        return list(geom.geoms)
    if tipo == "LineString":
        return [Point(geom.coords[0]), Point(geom.coords[-1])]
    if tipo in {"MultiLineString", "GeometryCollection"}:
        saida: list[Point] = []
        for parte in geom.geoms:
            saida.extend(_pontos(parte))
        return saida
    return []


def _fatia(linha: LineString, d0: float, d1: float) -> list[tuple[float, float]]:
    inverter = d1 < d0
    ini, fim = (d1, d0) if inverter else (d0, d1)
    ini = min(linha.length, max(0.0, ini))
    fim = min(linha.length, max(0.0, fim))
    coords = [tuple(linha.interpolate(ini).coords[0][:2])]
    for x, y in list(linha.coords)[1:-1]:
        marca = float(linha.project(Point(x, y)))
        if ini + 0.2 < marca < fim - 0.2:
            coords.append((float(x), float(y)))
    coords.append(tuple(linha.interpolate(fim).coords[0][:2]))
    if inverter:
        coords.reverse()
    return _limpos(coords)


def _comp(coords: list[tuple[float, float]]) -> float:
    total = 0.0
    for a, b in zip(coords, coords[1:]):
        total += math.hypot(b[0] - a[0], b[1] - a[1])
    return total


def _juntar(destino: list[tuple[float, float]], coords: list[tuple[float, float]]) -> None:
    for ponto in coords:
        if destino and math.hypot(ponto[0] - destino[-1][0], ponto[1] - destino[-1][1]) < 0.4:
            continue
        destino.append(ponto)


def _limpos(coords: list[tuple[float, float]]) -> list[tuple[float, float]]:
    saida: list[tuple[float, float]] = []
    for ponto in coords:
        if saida and math.hypot(ponto[0] - saida[-1][0], ponto[1] - saida[-1][1]) < 0.4:
            continue
        saida.append((float(ponto[0]), float(ponto[1])))
    return saida
