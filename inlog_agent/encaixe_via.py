"""Casa a sequência de pulsos na malha oficial.

HMM (Viterbi) sobre as vias da rota: cada pulso pode cair em mais de uma rua,
e a sequência escolhe o caminho pela emissão (distância à via), pela direção
e pela velocidade de coleta. A corda curta que já corre sobre a via continua
na regra homologada. Vão sem caminho plausível — empate, atalho longo ou
intervalo acima de um minuto — não vira reta nem trecho executado.
"""

from __future__ import annotations

import heapq
import math
from datetime import datetime
from typing import Any

from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

RAIO_M = 45.0
SALTO_RETO_M = 160.0
SALTO_MALHA_M = 70.0
INFERIR_MAX_S = 100.0
INFERIR_MAX_M = 520.0
FOLGA_ARCO = 1.65
AFASTAMENTO_M = 30.0
VEL_COLETA_MAX = 28.0
FOLGA_VIA_M = 18.0
SIGMA_GPS_M = 20.0
BETA_ROTA = 0.025
MARGEM_EMPATE = 0.40
CANDIDATOS = 5
_QUEBRA_TEMPO_S = 600.0
_QUEBRA_RETA_M = 480.0


class Passo:
    def __init__(self, indices: list[int], coords: list[tuple[float, float]], metros: float) -> None:
        self.indices = indices
        self.coords = coords
        self.metros = metros


class _CordaMarca:
    """A reta entre os dois pulsos já está sobre a via."""


CORDA = _CordaMarca()


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
        self.dist = 0.0


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
        achado = self.casar([(a, 0.0), (b, float(segundos))], max_kmh=max_kmh)
        item = achado[0] if achado else None
        return item if isinstance(item, Passo) else None

    def casar(
        self,
        pontos: list[tuple[tuple[float, float], float]],
        max_kmh: float = VEL_COLETA_MAX,
    ) -> list[Any]:
        """Um item por vão: Passo na via, CORDA se a reta já corre na malha, None se não cabe."""
        n = len(pontos)
        saida: list[Any] = [None] * max(0, n - 1)
        if n < 2 or not self.arestas:
            return saida
        rumo: float | None = None
        i = 0
        while i < n - 1:
            tipo = self._classificar(pontos[i][0], pontos[i + 1][0], _dt(pontos[i][1], pontos[i + 1][1]), max_kmh)
            if tipo == "corda":
                saida[i] = CORDA
                rumo = _rumo_gps(pontos[i][0], pontos[i + 1][0]) or rumo
                i += 1
                continue
            if tipo == "quebra":
                saida[i] = None
                rumo = None
                i += 1
                continue
            j = i
            while j < n - 1 and self._classificar(pontos[j][0], pontos[j + 1][0], _dt(pontos[j][1], pontos[j + 1][1]), max_kmh) == "match":
                j += 1
            for k, passo in enumerate(self._viterbi(pontos[i : j + 1], rumo, max_kmh)):
                saida[i + k] = passo
                if isinstance(passo, Passo) and passo.coords:
                    rumo = _rumo_final(passo.coords, 30.0)
                else:
                    rumo = None
            i = j
        return saida

    def desenhar(self, brutos: list[tuple[str, float, float]]) -> list[list[tuple[float, float]]]:
        """Polilinhas (lat, lon) já casadas. Vão sem caminho não entra como reta."""
        from .cobertura import TO_UTM, TO_WGS

        marcas: list[tuple[tuple[float, float], float, float, float]] = []
        for ts, lat, lon in brutos:
            try:
                x, y = TO_UTM.transform(float(lon), float(lat))
            except (TypeError, ValueError):
                continue
            marcas.append(((float(x), float(y)), _segundos_de(ts), float(lat), float(lon)))
        if len(marcas) < 2:
            return []
        casados = self.casar([(xy, seg) for xy, seg, _lat, _lon in marcas])
        grupos: list[list[tuple[float, float]]] = []
        atual: list[tuple[float, float]] = [(marcas[0][2], marcas[0][3])]
        for i, item in enumerate(casados):
            lat, lon = marcas[i + 1][2], marcas[i + 1][3]
            reta = math.hypot(marcas[i + 1][0][0] - marcas[i][0][0], marcas[i + 1][0][1] - marcas[i][0][1])
            dt = _dt(marcas[i][1], marcas[i + 1][1])
            if item is CORDA and dt <= _QUEBRA_TEMPO_S and reta <= _QUEBRA_RETA_M:
                _por(atual, (lat, lon))
                continue
            if isinstance(item, Passo) and len(item.coords) >= 2:
                if len(atual) == 1 and math.hypot(marcas[i][0][0] - item.coords[0][0], marcas[i][0][1] - item.coords[0][1]) > FOLGA_VIA_M:
                    lon0, lat0 = TO_WGS.transform(item.coords[0][0], item.coords[0][1])
                    atual = [(float(lat0), float(lon0))]
                for x, y in item.coords:
                    lon_p, lat_p = TO_WGS.transform(x, y)
                    _por(atual, (float(lat_p), float(lon_p)))
                if math.hypot(marcas[i + 1][0][0] - item.coords[-1][0], marcas[i + 1][0][1] - item.coords[-1][1]) <= FOLGA_VIA_M:
                    _por(atual, (lat, lon))
                continue
            if len(atual) >= 2:
                grupos.append(atual)
            atual = [(lat, lon)]
        if len(atual) >= 2:
            grupos.append(atual)
        return grupos

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

    def _classificar(self, a: tuple[float, float], b: tuple[float, float], dt: float, max_kmh: float) -> str:
        reta = math.hypot(b[0] - a[0], b[1] - a[1])
        if reta < 8:
            return "corda"
        if reta <= SALTO_RETO_M and (dt < 1 or self._sobre_a_via(a, b)):
            return "corda"
        if dt < 1 or dt > INFERIR_MAX_S or reta > INFERIR_MAX_M:
            return "quebra"
        if reta / dt * 3.6 > max_kmh:
            return "quebra"
        return "match"

    def _sobre_a_via(self, a: tuple[float, float], b: tuple[float, float]) -> bool:
        if self.arvore is None:
            return False
        for passo in range(5):
            t = passo / 4.0
            pt = Point(a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
            try:
                ids = [int(item) for item in self.arvore.query(pt.buffer(FOLGA_VIA_M))]
            except Exception:
                return False
            if not any(self.geoms[eid].distance(pt) <= FOLGA_VIA_M for eid in ids):
                return False
        return True

    def _candidatos(self, ponto: tuple[float, float]) -> list[_Ancora]:
        if self.arvore is None:
            return []
        pt = Point(ponto)
        try:
            ids = [int(item) for item in self.arvore.query(pt.buffer(RAIO_M))]
        except Exception:
            ids = []
        por_pai: dict[int, tuple[float, int]] = {}
        for eid in ids:
            dist = float(self.geoms[eid].distance(pt))
            if dist > RAIO_M:
                continue
            pai = self.arestas[eid].pai
            anterior = por_pai.get(pai)
            if anterior is None or dist < anterior[0]:
                por_pai[pai] = (dist, eid)
        saida: list[_Ancora] = []
        for dist, eid in sorted(por_pai.values())[:CANDIDATOS]:
            geom = self.geoms[eid]
            proj = _proj(geom, pt)
            aresta = self.arestas[eid]
            ancora = _Ancora(eid, proj, aresta.pai)
            ancora.no_a = aresta.a
            ancora.no_b = aresta.b
            ancora.custo_a = proj
            ancora.custo_b = max(0.0, aresta.comp - proj)
            ancora.coords_a = _fatia(geom, proj, 0.0)
            ancora.coords_b = _fatia(geom, proj, aresta.comp)
            ancora.dist = dist
            saida.append(ancora)
        return saida

    def _viterbi(
        self,
        pontos: list[tuple[tuple[float, float], float]],
        rumo_antes: float | None,
        max_kmh: float,
    ) -> list[Passo | None]:
        cands = [self._candidatos(par[0]) for par in pontos]
        dp: list[list[tuple[float, int, float | None, Passo | None]]] = []
        for t, lista in enumerate(cands):
            if not lista:
                dp.append([])
                continue
            if t == 0:
                dp.append([(-0.5 * (cand.dist / SIGMA_GPS_M) ** 2, -1, rumo_antes, None) for cand in lista])
                continue
            dt = _dt(pontos[t - 1][1], pontos[t][1])
            origem = pontos[t - 1][0]
            destino = pontos[t][0]
            anteriores = dp[t - 1]
            coluna: list[tuple[float, int, float | None, Passo | None]] = []
            for cand in lista:
                emit = -0.5 * (cand.dist / SIGMA_GPS_M) ** 2
                ligacoes: list[tuple[float, int, float | None, Passo]] = []
                for pi, prev in enumerate(cands[t - 1]):
                    if not anteriores or pi >= len(anteriores):
                        continue
                    base, _prev, rumo_prev, _passo = anteriores[pi]
                    if base < -1e8:
                        continue
                    lig = self._ligar(prev, cand, origem, destino, dt, max_kmh, rumo_prev)
                    if lig is None:
                        continue
                    nota, bearing, passo = lig
                    ligacoes.append((base + emit + nota, pi, bearing, passo))
                if not ligacoes:
                    coluna.append((emit, -1, None, None))
                    continue
                ligacoes.sort(key=lambda item: -item[0])
                melhor = ligacoes[0]
                ambiguo = False
                for outra in ligacoes[1:]:
                    if melhor[0] - outra[0] > MARGEM_EMPATE:
                        break
                    if _diverge(melhor[3].coords, outra[3].coords):
                        ambiguo = True
                        break
                coluna.append((emit, -1, None, None) if ambiguo else melhor)
            dp.append(self._fechar_coluna(coluna, lista))
        return self._reconstruir(dp)

    def _fechar_coluna(
        self,
        coluna: list[tuple[float, int, float | None, Passo | None]],
        lista: list[_Ancora],
    ) -> list[tuple[float, int, float | None, Passo | None]]:
        ligados = [i for i, item in enumerate(coluna) if item[1] >= 0 and item[3] is not None]
        if not ligados:
            return coluna
        ligados.sort(key=lambda i: -coluna[i][0])
        melhor = coluna[ligados[0]][3]
        ambiguo = False
        for indice in ligados[1:]:
            if coluna[ligados[0]][0] - coluna[indice][0] > MARGEM_EMPATE:
                break
            outro = coluna[indice][3]
            if melhor is not None and outro is not None and _diverge(melhor.coords, outro.coords):
                ambiguo = True
                break
        if ambiguo:
            return [(-0.5 * (cand.dist / SIGMA_GPS_M) ** 2, -1, None, None) for cand in lista]
        return [item if item[1] >= 0 else (-1e9, -1, None, None) for item in coluna]

    def _reconstruir(self, dp: list[list[tuple[float, int, float | None, Passo | None]]]) -> list[Passo | None]:
        n = len(dp)
        passos: list[Passo | None] = [None] * max(0, n - 1)
        t = n - 1
        while t >= 0 and not dp[t]:
            t -= 1
        if t < 0:
            return passos
        ci = max(range(len(dp[t])), key=lambda i: dp[t][i][0])
        while t >= 0:
            if not dp[t]:
                t -= 1
                if t >= 0 and dp[t]:
                    ci = max(range(len(dp[t])), key=lambda i: dp[t][i][0])
                continue
            _score, prev, _rumo, passo = dp[t][ci]
            if t > 0 and prev >= 0 and isinstance(passo, Passo):
                passos[t - 1] = passo
                ci = prev
                t -= 1
                continue
            t -= 1
            if t >= 0 and dp[t]:
                ci = max(range(len(dp[t])), key=lambda i: dp[t][i][0])
        return passos

    def _ligar(
        self,
        a: _Ancora,
        b: _Ancora,
        origem: tuple[float, float],
        destino: tuple[float, float],
        dt: float,
        max_kmh: float,
        rumo_antes: float | None,
    ) -> tuple[float, float | None, Passo] | None:
        dx = destino[0] - origem[0]
        dy = destino[1] - origem[1]
        reta = math.hypot(dx, dy)
        if reta < 8 or dt < 1:
            return None
        opcoes: list[tuple[float, list[tuple[float, float]], list[int], float]] = []
        if a.eid == b.eid:
            along = abs(a.proj - b.proj)
            coords = _fatia(self.geoms[a.eid], a.proj, b.proj)
            item = self._embalar(along, coords, {a.pai: along}, reta, dx, dy, dt, max_kmh, rumo_antes)
            if item is not None:
                opcoes.append(item)
            volta = self._caminho(a, b, {a.eid})
            if volta is not None:
                item = self._embalar(volta[0], volta[1], volta[3], reta, dx, dy, dt, max_kmh, rumo_antes)
                if item is not None:
                    opcoes.append(item)
        else:
            primeiro = self._caminho(a, b, set())
            if primeiro is not None:
                item = self._embalar(primeiro[0], primeiro[1], primeiro[3], reta, dx, dy, dt, max_kmh, rumo_antes)
                if item is not None:
                    opcoes.append(item)
                if primeiro[0] / reta >= 1.05:
                    proibir = set(primeiro[2]) - {a.eid, b.eid}
                    if proibir:
                        outro = self._caminho(a, b, proibir)
                        if outro is not None:
                            item = self._embalar(outro[0], outro[1], outro[3], reta, dx, dy, dt, max_kmh, rumo_antes)
                            if item is not None:
                                opcoes.append(item)
        if not opcoes:
            return None
        opcoes.sort(key=lambda item: -item[0])
        melhor = opcoes[0]
        if len(opcoes) > 1 and melhor[0] - opcoes[1][0] <= MARGEM_EMPATE and _diverge(melhor[1], opcoes[1][1]):
            return None
        _nota, coords, indices, metros = melhor
        return melhor[0], _rumo_final(coords, 30.0), Passo(indices, coords, metros)

    def _embalar(
        self,
        metros: float,
        coords: list[tuple[float, float]],
        comprimentos: dict[int, float],
        reta: float,
        dx: float,
        dy: float,
        dt: float,
        max_kmh: float,
        rumo_antes: float | None,
    ) -> tuple[float, list[tuple[float, float]], list[int], float] | None:
        nota = self._pontuar(metros, coords, reta, dx, dy, dt, max_kmh, rumo_antes)
        if nota is None or len(coords) < 2:
            return None
        return nota, coords, self._indices(comprimentos), metros

    def _pontuar(
        self,
        metros: float,
        coords: list[tuple[float, float]],
        reta: float,
        dx: float,
        dy: float,
        dt: float,
        max_kmh: float,
        rumo_antes: float | None,
    ) -> float | None:
        if not self._cabe(metros, reta, dt, max_kmh):
            return None
        vel = metros / dt * 3.6
        return (
            -BETA_ROTA * abs(metros - reta)
            + _nota_rumo(coords, dx, dy)
            + _nota_continuidade(coords, rumo_antes)
            - 0.03 * max(0.0, vel - 20.0)
        )

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

    def _indices(self, comprimentos: dict[int, float]) -> list[int]:
        saida = []
        for pai, comp in comprimentos.items():
            linha = self.linhas[pai]
            limite = 8.0 if linha.length >= 16 else max(2.0, linha.length * 0.5)
            if comp >= limite - 0.1:
                saida.append(self.origem[pai])
        return saida


def _dt(a: float, b: float) -> float:
    return abs(float(b) - float(a))


def _segundos_de(texto: str) -> float:
    bruto = (texto or "").strip().replace("Z", "")
    if not bruto:
        return 0.0
    try:
        return datetime.fromisoformat(bruto[:19]).timestamp()
    except ValueError:
        return 0.0


def _rumo_gps(a: tuple[float, float], b: tuple[float, float]) -> float | None:
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    if math.hypot(dx, dy) < 15:
        return None
    return math.atan2(dy, dx)


def _angulo(a: float, b: float) -> float:
    delta = abs(a - b) % (2 * math.pi)
    if delta > math.pi:
        delta = 2 * math.pi - delta
    return delta


def _rumo_inicial(coords: list[tuple[float, float]], metros: float) -> float | None:
    if len(coords) < 2:
        return None
    acc = 0.0
    for i in range(1, len(coords)):
        acc += math.hypot(coords[i][0] - coords[i - 1][0], coords[i][1] - coords[i - 1][1])
        if acc >= metros or i == len(coords) - 1:
            dx = coords[i][0] - coords[0][0]
            dy = coords[i][1] - coords[0][1]
            if math.hypot(dx, dy) >= 1:
                return math.atan2(dy, dx)
    return None


def _rumo_final(coords: list[tuple[float, float]], metros: float) -> float | None:
    if len(coords) < 2:
        return None
    acc = 0.0
    ultimo = len(coords) - 1
    for i in range(ultimo - 1, -1, -1):
        acc += math.hypot(coords[i + 1][0] - coords[i][0], coords[i + 1][1] - coords[i][1])
        if acc >= metros or i == 0:
            dx = coords[ultimo][0] - coords[i][0]
            dy = coords[ultimo][1] - coords[i][1]
            if math.hypot(dx, dy) >= 1:
                return math.atan2(dy, dx)
    return None


def _nota_rumo(coords: list[tuple[float, float]], dx: float, dy: float) -> float:
    if len(coords) < 2 or math.hypot(dx, dy) < 8:
        return 0.0
    rumo = _rumo_inicial(coords, 30.0)
    if rumo is None:
        return 0.0
    delta = _angulo(math.atan2(dy, dx), rumo)
    meio = math.pi / 2
    if delta <= meio:
        return -1.6 * (delta / meio) ** 2
    return -1.6 - 2.0 * ((delta - meio) / meio)


def _nota_continuidade(coords: list[tuple[float, float]], rumo_antes: float | None) -> float:
    if rumo_antes is None:
        return 0.0
    rumo = _rumo_inicial(coords, 30.0)
    if rumo is None:
        return 0.0
    delta = _angulo(rumo, rumo_antes)
    if delta <= math.radians(35):
        return 0.85
    if delta >= math.radians(80):
        return -0.55
    return 0.0


def _diverge(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> bool:
    if len(a) < 2 or len(b) < 2:
        return False
    base = LineString(a)
    return max(base.distance(Point(ponto)) for ponto in b) > AFASTAMENTO_M


def _por(destino: list[tuple[float, float]], ponto: tuple[float, float]) -> None:
    if destino and abs(destino[-1][0] - ponto[0]) <= 1e-7 and abs(destino[-1][1] - ponto[1]) <= 1e-7:
        return
    destino.append(ponto)


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
