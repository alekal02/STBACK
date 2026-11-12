"""Um motor de trajetória para compactador e basculante.

Ordem: corte do turno, limpeza dos pulsos, caminho na malha completa.
As ruas oficiais somam à malha OSM e só então as camadas se ligam:
cruzamento entre camadas vira nó, e ponta a até 8 m cola na outra.
Entre dois pulsos o desenho segue essa malha, não só a via da rota.
Até 10 minutos o vão liga. O teto de 60 km/h usa a reta do GPS bruto.
A folga de 1,5× a reta mais 30 m vale só no vão acima de 90 s e com
velocidade baixa. Até 90 s, liga se o caminho couber no teto de 60 km/h:
a ré e a rua sem saída entram.
A malha ignora mão única: a aresta vale nos dois sentidos, e a ré entra.
Com um caminho só, ele entra inteiro. Com dois ou mais, só os trechos
comuns. O percentual credita a via oficial que essa linha percorreu.
"""

from __future__ import annotations

import heapq
import json
import math
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from shapely.geometry import LineString, MultiLineString, Point
from shapely.ops import substring

from .gps import haversine_m
from .mapa_oficial import TO_UTM, TO_WGS

SNAP_M = 45.0
JUNTA_M = 480.0
GAP_S = 600.0
VEL_ABSURDA = 110.0
VEL_BAIXA = 3.0
VEL_MAX = 60.0
FATOR_CAMINHO = 1.65
FATOR_LENTO = 1.5
FOLGA_LENTA_M = 30.0
VAO_RETA_S = 90.0
FOLGA_CAMINHO_M = 45.0
JUNTA_PULSO_S = 5.0
MARGEM_CAMINHO = 0.40
AFASTAMENTO_M = 30.0
JUNCAO_M = 12.0
NO_M = 16.0
FOLGA_CRUZAMENTO_M = 5.0

Ponto = tuple[str, float, float]


@dataclass
class _Snap:
    peca: int
    offset: float
    x: float
    y: float


@dataclass
class Rede:
    linhas: list[LineString] = field(default_factory=list)
    peca_nos: list[tuple[int, int]] = field(default_factory=list)
    adj: list[list[tuple[int, int, float]]] = field(default_factory=list)
    _tree: Any = None

    @property
    def vazia(self) -> bool:
        return not self.linhas


@dataclass
class Trajeto:
    placa: str
    tipo: str
    linhas: list[list[list[float]]]
    geom: Any
    pecas: list[list[Ponto]]
    n_pontos: int


def tipo_da_placa(placa: str) -> str:
    from .cadastro_frota import tipo_de

    return tipo_de(placa)


def entra_no_percentual(placa: str, veiculo: str = "") -> bool:
    """Compactador ou basculante. PM, trator, van e os demais ficam de fora."""
    from .dificil_acesso import eh_coleta

    return eh_coleta(placa, veiculo)


def placas_do_percentual(dia: date, turno: str) -> list[dict[str, str]]:
    """Placas do turno que entram nas vias percorridas, com o nome do veículo."""
    import sqlite3

    from .cadastro_frota import tipo_de
    from .live_db import conectar

    pedido = str(turno or "").strip().lower()
    try:
        con = conectar(leitura=True)
    except sqlite3.Error:
        return []
    try:
        linhas = con.execute(
            "SELECT placa, veiculo FROM placa WHERE data = ? AND turno = ? ORDER BY placa",
            (dia.isoformat(), pedido),
        ).fetchall()
    except sqlite3.Error:
        linhas = []
    finally:
        con.close()
    saida: list[dict[str, str]] = []
    for row in linhas:
        placa = str(row["placa"] or "")
        veiculo = str(row["veiculo"] or "")
        if not placa or not entra_no_percentual(placa, veiculo):
            continue
        saida.append(
            {
                "placa": placa,
                "veiculo": veiculo,
                "tipo": tipo_de(placa),
            }
        )
    return saida


def _hora(texto: str) -> datetime | None:
    bruto = (texto or "").strip().replace("Z", "")
    if not bruto:
        return None
    try:
        return datetime.fromisoformat(bruto[:19])
    except ValueError:
        return None


def _segundos(anterior: str, atual: str) -> float:
    a = _hora(anterior)
    b = _hora(atual)
    if a is None or b is None:
        return 0.0
    return (b - a).total_seconds()


def normalizar_pulsos(pontos: list[Any]) -> list[Ponto]:
    saida: list[Ponto] = []
    for item in pontos or []:
        if isinstance(item, dict):
            ts = str(item.get("timestamp") or item.get("ts") or "")
            lat = item.get("latitude", item.get("lat"))
            lon = item.get("longitude", item.get("lon"))
        elif isinstance(item, (list, tuple)) and len(item) >= 3:
            ts, lat, lon = item[0], item[1], item[2]
        else:
            continue
        try:
            lat_f = float(lat)
            lon_f = float(lon)
        except (TypeError, ValueError):
            continue
        if not ts or not math.isfinite(lat_f) or not math.isfinite(lon_f):
            continue
        saida.append((str(ts), lat_f, lon_f))
    saida.sort(key=lambda p: p[0])
    return saida


def cortar_turno(pontos: list[Ponto], dia: date | None, turno: str | None) -> list[Ponto]:
    """Só o relógio do turno. A data operacional do noturno é o dia em que ele começa."""
    if dia is None or not turno:
        return list(pontos or [])
    from .live_turno import fim_operacional, inicio_operacional

    inicio = inicio_operacional(dia, turno)
    fim = fim_operacional(dia, turno)
    saida: list[Ponto] = []
    for ts, lat, lon in pontos or []:
        momento = _hora(ts)
        if momento is not None and inicio <= momento <= fim:
            saida.append((ts, lat, lon))
    return saida


def limpar_pulsos(pontos: list[Ponto]) -> list[Ponto]:
    """Tira pulso duplicado, pico de teleporte e velocidade absurda."""
    saida: list[Ponto] = []
    ordenados = list(pontos or [])
    for i, atual in enumerate(ordenados):
        _ts, lat, lon = atual
        if saida and haversine_m((saida[-1][1], saida[-1][2]), (lat, lon)) < 2.5:
            continue
        if 0 < i < len(ordenados) - 1:
            prev = ordenados[i - 1]
            nxt = ordenados[i + 1]
            d_prev = haversine_m((prev[1], prev[2]), (lat, lon))
            d_next = haversine_m((lat, lon), (nxt[1], nxt[2]))
            d_viz = haversine_m((prev[1], prev[2]), (nxt[1], nxt[2]))
            if d_prev > 1400 and d_next > 1400:
                continue
            if d_prev > 250 and d_next > 250 and d_viz < min(d_prev, d_next) * 0.55:
                continue
        if saida:
            ant = saida[-1]
            dist = haversine_m((ant[1], ant[2]), (lat, lon))
            dt = _segundos(ant[0], atual[0])
            if dt > 0 and dist > 200 and dist / dt * 3.6 > VEL_ABSURDA:
                continue
        saida.append(atual)
    return _juntar_pulsos(saida)


def _juntar_pulsos(pontos: list[Ponto], minimo_s: float = JUNTA_PULSO_S) -> list[Ponto]:
    """Pulsos com menos de 5 s viram um só. Fica o último do grupo."""
    saida: list[Ponto] = []
    for atual in pontos or []:
        if saida and 0 <= _segundos(saida[-1][0], atual[0]) < minimo_s:
            saida[-1] = atual
            continue
        saida.append(atual)
    return saida


def _delta_rumo(a: float, b: float) -> float:
    delta = abs(a - b) % math.pi
    if delta > math.pi / 2:
        delta = math.pi - delta
    return delta


def _rumo_linha(linha: LineString, offset: float) -> float | None:
    if linha.length < 1:
        return None
    antes = linha.interpolate(max(0.0, offset - 8.0))
    depois = linha.interpolate(min(linha.length, offset + 8.0))
    dx = depois.x - antes.x
    dy = depois.y - antes.y
    if math.hypot(dx, dy) < 0.5:
        return None
    return math.atan2(dy, dx)


def _linhas_de(geom) -> list[LineString]:
    if geom is None or geom.is_empty:
        return []
    tipo = geom.geom_type
    if tipo == "LineString":
        return [geom] if len(geom.coords) >= 2 and geom.length >= 4 else []
    if tipo in {"MultiLineString", "GeometryCollection"}:
        saida: list[LineString] = []
        for parte in geom.geoms:
            saida.extend(_linhas_de(parte))
        return saida
    return []


def _inverter(linha: LineString) -> LineString:
    return LineString([(float(x), float(y)) for x, y in reversed(list(linha.coords))])


def _trecho(linha: LineString, a: float, b: float) -> LineString | None:
    """Trecho de a até b, no sentido pedido. a e b são distâncias ao longo da linha."""
    ini, fim = (a, b) if a <= b else (b, a)
    if fim - ini < 0.4:
        return None
    try:
        pedaco = substring(linha, max(0.0, ini), min(linha.length, fim))
    except Exception:
        return None
    if pedaco.is_empty or pedaco.geom_type != "LineString" or pedaco.length < 0.4:
        return None
    return pedaco if a <= b else _inverter(pedaco)


def _no_comprimento(coords: list[tuple[float, float]], acum: list[float], dist: float) -> tuple[float, float]:
    if dist <= 0:
        return coords[0]
    if dist >= acum[-1]:
        return coords[-1]
    lo, hi = 0, len(acum) - 1
    while lo + 1 < hi:
        meio = (lo + hi) // 2
        if acum[meio] < dist:
            lo = meio
        else:
            hi = meio
    span = acum[hi] - acum[lo] or 1.0
    t = (dist - acum[lo]) / span
    x0, y0 = coords[lo]
    x1, y1 = coords[hi]
    return (x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)


def _fatiar(coords: list[tuple[float, float]], cortes: list[float]) -> list[list[tuple[float, float]]]:
    acum = [0.0]
    for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
        acum.append(acum[-1] + math.hypot(x1 - x0, y1 - y0))
    comp = acum[-1]
    if comp < 8:
        return []
    marcas = [0.0, comp]
    for dist in cortes:
        if 5.0 < dist < comp - 5.0:
            marcas.append(dist)
    if len(marcas) == 2:
        return [coords]
    ordenados = sorted({round(item, 2) for item in marcas})
    saida: list[list[tuple[float, float]]] = []
    for a, b in zip(ordenados, ordenados[1:]):
        if b - a < 4:
            continue
        pedaco = [_no_comprimento(coords, acum, a)]
        for ponto, dist in zip(coords, acum):
            if a + 0.4 < dist < b - 0.4:
                pedaco.append(ponto)
        pedaco.append(_no_comprimento(coords, acum, b))
        limpo = [pedaco[0]]
        for ponto in pedaco[1:]:
            if math.hypot(ponto[0] - limpo[-1][0], ponto[1] - limpo[-1][1]) >= 0.4:
                limpo.append(ponto)
        if len(limpo) >= 2:
            saida.append(limpo)
    return saida


def construir_rede(linhas: list[Any] | None) -> Rede:
    from shapely import points as shp_points
    from shapely.strtree import STRtree

    brutas: list[LineString] = []
    for linha in linhas or []:
        brutas.extend(_linhas_de(linha))
    if not brutas:
        return Rede()
    arvore = STRtree(brutas)
    cortes: list[list[float]] = [[] for _ in brutas]
    xs: list[float] = []
    ys: list[float] = []
    dono_ponta: list[int] = []
    for i, linha in enumerate(brutas):
        xs.append(float(linha.coords[0][0]))
        ys.append(float(linha.coords[0][1]))
        dono_ponta.append(i)
        xs.append(float(linha.coords[-1][0]))
        ys.append(float(linha.coords[-1][1]))
        dono_ponta.append(i)
    try:
        pares = arvore.query(shp_points(xs, ys), predicate="dwithin", distance=JUNCAO_M)
        entradas = pares[0]
        vias = pares[1]
    except TypeError:
        entradas, vias = [], []
    for entrada, via in zip(entradas, vias):
        i = dono_ponta[int(entrada)]
        j = int(via)
        if i == j:
            continue
        ponta = Point(xs[int(entrada)], ys[int(entrada)])
        outra = brutas[j]
        if outra.distance(ponta) > JUNCAO_M:
            continue
        cortes[j].append(float(outra.project(ponta)))
    pecas: list[LineString] = []
    for linha, marcas in zip(brutas, cortes):
        if not marcas:
            if linha.length >= 8:
                pecas.append(linha)
            continue
        coords = [(float(x), float(y)) for x, y, *resto in linha.coords]
        for pedaco in _fatiar(coords, marcas):
            pecas.append(LineString(pedaco))
    if not pecas:
        return Rede()
    extremos: list[tuple[float, float]] = []
    dono: list[int] = []
    for i, peca in enumerate(pecas):
        extremos.append((float(peca.coords[0][0]), float(peca.coords[0][1])))
        dono.append(i)
        extremos.append((float(peca.coords[-1][0]), float(peca.coords[-1][1])))
        dono.append(i)
    pai = list(range(len(extremos)))

    def achar(i: int) -> int:
        while pai[i] != i:
            pai[i] = pai[pai[i]]
            i = pai[i]
        return i

    def unir(a: int, b: int) -> None:
        ra, rb = achar(a), achar(b)
        if ra != rb:
            pai[rb] = ra

    baldes: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, (x, y) in enumerate(extremos):
        baldes[(int(math.floor(x / NO_M)), int(math.floor(y / NO_M)))].append(i)
    for i, (x, y) in enumerate(extremos):
        ix = int(math.floor(x / NO_M))
        iy = int(math.floor(y / NO_M))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in baldes.get((ix + dx, iy + dy), ()):
                    if j <= i or dono[i] == dono[j]:
                        continue
                    ox, oy = extremos[j]
                    if math.hypot(x - ox, y - oy) <= NO_M:
                        unir(i, j)
    raiz_id: dict[int, int] = {}
    peca_nos: list[tuple[int, int]] = []
    for i in range(len(pecas)):
        par = []
        for ponta in (i * 2, i * 2 + 1):
            raiz = achar(ponta)
            if raiz not in raiz_id:
                raiz_id[raiz] = len(raiz_id)
            par.append(raiz_id[raiz])
        peca_nos.append((par[0], par[1]))
    adj: list[list[tuple[int, int, float]]] = [[] for _ in range(len(raiz_id))]
    for i, (n0, n1) in enumerate(peca_nos):
        if n0 == n1:
            continue
        comp = float(pecas[i].length)
        # Mão única não entra: a via vale nos dois sentidos, e a ré usa o mesmo arco.
        adj[n0].append((n1, i, comp))
        adj[n1].append((n0, i, comp))
    rede = Rede(linhas=pecas, peca_nos=peca_nos, adj=adj)
    rede._tree = STRtree(pecas)
    return rede


_REDE: Rede | None = None


def _arquivo_osm() -> Path | None:
    raiz = Path(__file__).resolve().parents[1] / "output"
    forçado = (os.getenv("SAGTRACK_OSM") or "").strip()
    candidatos = [Path(forçado)] if forçado else []
    if raiz.is_dir():
        candidatos.extend(raiz.rglob("osm_wgs.json"))
    validos = [caminho for caminho in candidatos if caminho.is_file() and caminho.stat().st_size > 1000]
    if not validos:
        return None
    return max(validos, key=lambda caminho: caminho.stat().st_size)


def _linhas_osm() -> list[LineString]:
    """Vias do OSM já em UTM. Some o arquivo e a malha fica só com o GPKG."""
    caminho = _arquivo_osm()
    if caminho is None:
        return []
    try:
        bruto = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    saida: list[LineString] = []
    for coords in bruto if isinstance(bruto, list) else []:
        utm = []
        for par in coords:
            if not isinstance(par, (list, tuple)) or len(par) < 2:
                continue
            try:
                x, y = TO_UTM.transform(float(par[1]), float(par[0]))
            except (TypeError, ValueError):
                continue
            utm.append((float(x), float(y)))
        if len(utm) < 2:
            continue
        linha = LineString(utm)
        if linha.length >= 8:
            saida.append(linha)
    return saida


def _dists_no_miolo(linha: LineString, geom) -> list[float]:
    """Distâncias do cruzamento no meio da linha. Ponta encostada não corta."""
    if geom is None or geom.is_empty:
        return []
    tipo = geom.geom_type
    if tipo in {"MultiLineString", "GeometryCollection", "MultiPoint"}:
        saida: list[float] = []
        for parte in geom.geoms:
            saida.extend(_dists_no_miolo(linha, parte))
        return saida
    if tipo == "LineString":
        pontos = [Point(geom.coords[0]), Point(geom.coords[-1])]
    elif tipo == "Point":
        pontos = [geom]
    else:
        return []
    comp = float(linha.length)
    saida = []
    for ponto in pontos:
        dist = float(linha.project(ponto))
        if 1.0 < dist < comp - 1.0:
            saida.append(dist)
    return saida


def _unir_pontas(linhas: list[LineString], folga_m: float) -> list[LineString]:
    """Pontas a até 5 m viram o mesmo ponto. As duas pontas da mesma linha não se unem."""
    pontas: list[tuple[int, int, float, float]] = []
    for i, linha in enumerate(linhas):
        c0, c1 = linha.coords[0], linha.coords[-1]
        pontas.append((i, 0, float(c0[0]), float(c0[1])))
        pontas.append((i, 1, float(c1[0]), float(c1[1])))
    pai = list(range(len(pontas)))

    def achar(i: int) -> int:
        while pai[i] != i:
            pai[i] = pai[pai[i]]
            i = pai[i]
        return i

    def unir(a: int, b: int) -> None:
        ra, rb = achar(a), achar(b)
        if ra != rb:
            pai[rb] = ra

    baldes: dict[tuple[int, int], list[int]] = defaultdict(list)
    for k, (_i, _qual, x, y) in enumerate(pontas):
        baldes[(int(math.floor(x / folga_m)), int(math.floor(y / folga_m)))].append(k)
    for k, (i, _qual, x, y) in enumerate(pontas):
        ix = int(math.floor(x / folga_m))
        iy = int(math.floor(y / folga_m))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in baldes.get((ix + dx, iy + dy), ()):
                    if j <= k or pontas[j][0] == i:
                        continue
                    if math.hypot(x - pontas[j][2], y - pontas[j][3]) <= folga_m:
                        unir(k, j)
    grupos: dict[int, list[int]] = defaultdict(list)
    for k in range(len(pontas)):
        grupos[achar(k)].append(k)
    mover: dict[tuple[int, int], tuple[float, float]] = {}
    for membros in grupos.values():
        if len(membros) < 2:
            continue
        cx = sum(pontas[k][2] for k in membros) / len(membros)
        cy = sum(pontas[k][3] for k in membros) / len(membros)
        for k in membros:
            mover[(pontas[k][0], pontas[k][1])] = (cx, cy)
    saida: list[LineString] = []
    for i, linha in enumerate(linhas):
        coords = [(float(x), float(y)) for x, y, *_resto in linha.coords]
        novo0 = mover.get((i, 0), coords[0])
        novo1 = mover.get((i, 1), coords[-1])
        if math.hypot(novo0[0] - novo1[0], novo0[1] - novo1[1]) >= 0.4:
            coords[0] = novo0
            coords[-1] = novo1
        limpo = [coords[0]]
        for ponto in coords[1:]:
            if math.hypot(ponto[0] - limpo[-1][0], ponto[1] - limpo[-1][1]) >= 0.4:
                limpo.append(ponto)
        if len(limpo) >= 2:
            saida.append(LineString(limpo))
    return saida


def _ligar_cruzamentos(linhas: list[Any], folga_m: float = FOLGA_CRUZAMENTO_M) -> list[LineString]:
    """Quebra a linha onde ela cruza outra e une pontas a até 5 m."""
    from shapely.strtree import STRtree

    base = []
    for linha in linhas or []:
        base.extend(_linhas_de(linha))
    if len(base) < 2:
        return base
    arvore = STRtree(base)
    cortes: list[list[float]] = [[] for _ in base]
    for i, linha in enumerate(base):
        try:
            hits = arvore.query(linha, predicate="intersects")
        except TypeError:
            hits = []
        for bruto in hits:
            j = int(bruto)
            if j <= i:
                continue
            outra = base[j]
            try:
                cruz = linha.intersection(outra)
            except Exception:
                continue
            cortes[i].extend(_dists_no_miolo(linha, cruz))
            cortes[j].extend(_dists_no_miolo(outra, cruz))
    partidos: list[LineString] = []
    for linha, marcas in zip(base, cortes):
        if not marcas:
            partidos.append(linha)
            continue
        coords = [(float(x), float(y)) for x, y, *_resto in linha.coords]
        for pedaco in _quebrar_no_cruzamento(coords, marcas):
            partidos.append(LineString(pedaco))
    return _unir_pontas(partidos, folga_m)


def _quebrar_no_cruzamento(coords: list[tuple[float, float]], cortes: list[float]) -> list[list[tuple[float, float]]]:
    """Corta no cruzamento, inclusive perto da ponta. O miolo de 5 m do _fatiar não serve aqui."""
    acum = [0.0]
    for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
        acum.append(acum[-1] + math.hypot(x1 - x0, y1 - y0))
    comp = acum[-1]
    if comp < 0.4:
        return []
    marcas = [0.0, comp]
    for dist in cortes:
        if 0.4 < dist < comp - 0.4:
            marcas.append(dist)
    ordenados = sorted({round(item, 2) for item in marcas})
    if len(ordenados) == 2:
        return [coords]
    saida: list[list[tuple[float, float]]] = []
    for a, b in zip(ordenados, ordenados[1:]):
        if b - a < 0.4:
            continue
        pedaco = [_no_comprimento(coords, acum, a)]
        for ponto, dist in zip(coords, acum):
            if a + 0.2 < dist < b - 0.2:
                pedaco.append(ponto)
        pedaco.append(_no_comprimento(coords, acum, b))
        limpo = [pedaco[0]]
        for ponto in pedaco[1:]:
            if math.hypot(ponto[0] - limpo[-1][0], ponto[1] - limpo[-1][1]) >= 0.2:
                limpo.append(ponto)
        if len(limpo) >= 2:
            saida.append(limpo)
    return saida or [coords]


def rede_oficial() -> Rede:
    global _REDE
    if _REDE is not None:
        return _REDE
    from .mapa_oficial import carregar_vias

    camadas = list(_linhas_osm())
    camadas.extend(carregar_vias())
    _REDE = construir_rede(_ligar_cruzamentos(camadas, folga_m=8.0))
    return _REDE


def _rede_de(rede: Any) -> Rede:
    if isinstance(rede, Rede):
        return rede
    if rede is None:
        return rede_oficial()
    return construir_rede(list(rede))


def _projetar(rede: Rede, x: float, y: float, rumo: float | None, peca_atual: int | None) -> _Snap | None:
    if rede.vazia or rede._tree is None:
        return None
    ponto = Point(x, y)
    try:
        indices = rede._tree.query(ponto.buffer(SNAP_M))
    except Exception:
        return None
    melhor: _Snap | None = None
    melhor_custo = 1e18
    for bruto in indices:
        indice = int(bruto)
        linha = rede.linhas[indice]
        dist = float(linha.distance(ponto))
        if dist > SNAP_M:
            continue
        offset = float(linha.project(ponto))
        custo = dist - (12.0 if indice == peca_atual else 0.0)
        if rumo is not None:
            rumo_via = _rumo_linha(linha, offset)
            if rumo_via is not None:
                delta = _delta_rumo(rumo, rumo_via)
                if delta > math.radians(42):
                    custo += (delta - math.radians(42)) * 40.0
        if custo < melhor_custo:
            proj = linha.interpolate(max(0.0, min(linha.length, offset)))
            melhor = _Snap(indice, offset, float(proj.x), float(proj.y))
            melhor_custo = custo
    return melhor


def _afasta(base: LineString, outra: LineString) -> float:
    maior = 0.0
    dist = 0.0
    while dist <= outra.length:
        maior = max(maior, float(base.distance(outra.interpolate(dist))))
        dist += 15.0
    return maior


def _parecido(primeiro: LineString, segundo: LineString) -> bool:
    curto = min(float(primeiro.length), float(segundo.length))
    longo = max(float(primeiro.length), float(segundo.length))
    if curto < 1 or longo > curto * (1.0 + MARGEM_CAMINHO) + 8:
        return False
    return _afasta(primeiro, segundo) > AFASTAMENTO_M


def _caminho(
    rede: Rede,
    origem: _Snap,
    destino: _Snap,
    limite: float,
    proibir: set[int],
    desvio: bool,
) -> tuple[LineString, set[int]] | None:
    if limite < 0.4:
        return None
    linha_a = rede.linhas[origem.peca]
    if origem.peca == destino.peca and not desvio and origem.peca not in proibir:
        direto = _trecho(linha_a, origem.offset, destino.offset)
        if direto is None or direto.length > limite + 1:
            return None
        return direto, {origem.peca}
    n0, n1 = rede.peca_nos[origem.peca]
    alvo0, alvo1 = rede.peca_nos[destino.peca]
    alvos = {alvo0, alvo1}
    entrada: dict[int, LineString | None] = {}
    semente: dict[int, float] = {}
    melhor: dict[int, float] = {}
    prev: dict[int, tuple[int, int]] = {}
    heap: list[tuple[float, int, int]] = []

    def entrar(no: int, custo: float, geom: LineString | None) -> None:
        if custo > limite or custo >= semente.get(no, 1e18):
            return
        if geom is None and custo > 0.5:
            return
        entrada[no] = geom
        semente[no] = custo
        heapq.heappush(heap, (custo, no, 0))

    def relaxar(no: int, viz: int, peca: int, custo: float) -> None:
        if peca in proibir or custo > limite or custo >= melhor.get(viz, 1e18):
            return
        melhor[viz] = custo
        prev[viz] = (no, peca)
        heapq.heappush(heap, (custo, viz, 1))

    entrar(n0, origem.offset, _trecho(linha_a, origem.offset, 0.0))
    entrar(n1, max(0.0, linha_a.length - origem.offset), _trecho(linha_a, origem.offset, linha_a.length))
    linha_b = rede.linhas[destino.peca]

    def extra_de(no: int) -> float:
        if no == alvo0:
            return float(destino.offset)
        return max(0.0, float(linha_b.length) - float(destino.offset))

    achado: tuple[int, float, int] | None = None
    melhor_total = 1e18
    passos = 0
    while heap and passos < 2500:
        custo, no, flag = heapq.heappop(heap)
        passos += 1
        if custo >= melhor_total:
            continue
        if flag == 0:
            if custo > semente.get(no, 1e18) + 0.1:
                continue
        elif custo > melhor.get(no, 1e18) + 0.1:
            continue
        if no in alvos and (flag == 1 or not desvio):
            total = custo + extra_de(no)
            if total < melhor_total and total <= limite + 1:
                achado = (no, custo, flag)
                melhor_total = total
            continue
        for viz, peca, comp in rede.adj[no]:
            relaxar(no, viz, peca, custo + comp)
    if achado is None:
        return None
    no_fim, custo_fim, flag_fim = achado
    if no_fim == alvo0:
        extra = destino.offset
        saida = _trecho(linha_b, 0.0, destino.offset)
    else:
        extra = max(0.0, linha_b.length - destino.offset)
        saida = _trecho(linha_b, linha_b.length, destino.offset)
    if custo_fim + extra > limite + 1:
        return None
    cadeia: list[tuple[int, int, int]] = []
    usadas = {origem.peca, destino.peca}
    cursor = no_fim
    if flag_fim == 1 and cursor in prev:
        visto: set[int] = set()
        while cursor in prev and cursor not in visto and len(cadeia) < 400:
            visto.add(cursor)
            anterior, peca = prev[cursor]
            cadeia.append((anterior, cursor, peca))
            usadas.add(peca)
            cursor = anterior
            if cursor in entrada:
                break
    if cursor not in entrada:
        return None
    coords: list[tuple[float, float]] = []

    def juntar(geom: LineString | None) -> None:
        if geom is None or geom.is_empty or geom.geom_type != "LineString":
            return
        for x, y in geom.coords:
            if coords and math.hypot(x - coords[-1][0], y - coords[-1][1]) < 0.4:
                continue
            coords.append((float(x), float(y)))

    juntar(entrada.get(cursor))
    for anterior, _atual, peca in reversed(cadeia):
        na, nb = rede.peca_nos[peca]
        linha = rede.linhas[peca]
        if anterior == na and _atual == nb:
            juntar(linha)
        elif anterior == nb and _atual == na:
            juntar(_inverter(linha))
    juntar(saida)
    if len(coords) < 2:
        if extra < 0.4 and cursor in entrada and entrada[cursor] is not None:
            return entrada[cursor], usadas
        return None
    return LineString(coords), usadas


def _linhas_de_geom(geom: Any) -> list[LineString]:
    if geom is None or getattr(geom, "is_empty", True):
        return []
    tipo = geom.geom_type
    if tipo == "LineString":
        return [geom] if geom.length >= 8 else []
    if tipo == "MultiLineString":
        return [parte for parte in geom.geoms if parte.length >= 8]
    if tipo == "GeometryCollection":
        saida: list[LineString] = []
        for parte in geom.geoms:
            saida.extend(_linhas_de_geom(parte))
        return saida
    return []


def _usa_reta_lenta(dt: float, vel_gps: float) -> bool:
    """1,5× a reta + 30 m só no vão longo e lento. Até 90 s vale o teto de 60 km/h."""
    return dt > VAO_RETA_S and vel_gps < VEL_BAIXA


def _limite_do_vao(dt: float, dist_gps: float, salto: float, teto_m: float, vel_gps: float) -> float:
    if _usa_reta_lenta(dt, vel_gps):
        return dist_gps * FATOR_LENTO + FOLGA_LENTA_M
    if dt > 0:
        return teto_m
    return max(salto * FATOR_CAMINHO, salto + FOLGA_CAMINHO_M)


def _ligar(rede: Rede, origem: _Snap, destino: _Snap, limite: float) -> tuple[LineString, list[LineString]] | None:
    """Caminho na malha. Vários caminhos viáveis devolvem só o trecho comum."""
    achado = _caminho(rede, origem, destino, limite, set(), False)
    if achado is None:
        return None
    geom, pecas = achado
    if origem.peca == destino.peca and destino.offset + 1 < origem.offset:
        return geom, [geom] if geom.length >= 8 else []
    if geom.length < 12 or not pecas:
        return geom, [geom] if geom.length >= 8 else []
    proibir = set(pecas)
    outros: list[LineString] = []
    for _ in range(3):
        outro = _caminho(rede, origem, destino, limite, proibir, True)
        if outro is None or _afasta(geom, outro[0]) <= AFASTAMENTO_M:
            break
        outros.append(outro[0])
        proibir |= set(outro[1])
    if not outros:
        return geom, [geom]
    try:
        faixa = outros[0].buffer(6.0)
        for extra in outros[1:]:
            faixa = faixa.intersection(extra.buffer(6.0))
        comum = geom.intersection(faixa)
    except Exception:
        return geom, [geom]
    return geom, _linhas_de_geom(comum)


def _utm(lat: float, lon: float) -> tuple[float, float]:
    x, y = TO_UTM.transform(lon, lat)
    return float(x), float(y)


def _wgs(x: float, y: float) -> tuple[float, float]:
    lon, lat = TO_WGS.transform(x, y)
    return round(float(lat), 6), round(float(lon), 6)


def _coords_de(geom: LineString) -> list[tuple[float, float]]:
    return [(float(x), float(y)) for x, y in geom.coords]


def casar_pulsos(
    pontos: list[Any],
    rede: Any = None,
    dia: date | None = None,
    turno: str | None = None,
) -> Trajeto:
    """Corte, limpeza e casamento. `rede` None usa a malha completa da cidade."""
    malha = _rede_de(rede)
    brutos = cortar_turno(normalizar_pulsos(pontos), dia, turno)
    limpos = limpar_pulsos(brutos)
    linhas_utm: list[LineString] = []
    pecas: list[list[Ponto]] = []
    coords: list[tuple[float, float]] = []
    marcas: list[str] = []
    anterior: _Snap | None = None
    ts_anterior = ""
    xy_anterior: tuple[float, float] | None = None

    def _gravar_trecho(parte: LineString, inicio: str, fim: str) -> None:
        if parte is None or parte.is_empty or parte.length < 8:
            return
        linhas_utm.append(parte)
        pulsos: list[Ponto] = []
        total = float(parte.length) or 1.0
        base = _hora(inicio)
        duracao = max(0.0, _segundos(inicio, fim))
        percorrido = 0.0
        anterior_xy: tuple[float, float] | None = None
        for x, y in parte.coords:
            if anterior_xy is not None:
                percorrido += math.hypot(x - anterior_xy[0], y - anterior_xy[1])
            anterior_xy = (float(x), float(y))
            if base is None or duracao <= 0:
                marca = fim
            else:
                from datetime import timedelta

                marca = (base + timedelta(seconds=duracao * percorrido / total)).isoformat(timespec="seconds")
            lat, lon = _wgs(float(x), float(y))
            if pulsos and pulsos[-1][1] == lat and pulsos[-1][2] == lon:
                continue
            pulsos.append((marca, lat, lon))
        if len(pulsos) >= 2:
            pulsos[-1] = (fim, pulsos[-1][1], pulsos[-1][2])
            pecas.append(pulsos)

    def fechar() -> None:
        nonlocal coords, marcas
        if len(coords) >= 2:
            linhas_utm.append(LineString(coords))
            pulsos: list[Ponto] = []
            for (x, y), ts in zip(coords, marcas):
                lat, lon = _wgs(x, y)
                if pulsos and pulsos[-1][1] == lat and pulsos[-1][2] == lon:
                    continue
                pulsos.append((ts, lat, lon))
            if len(pulsos) >= 2:
                pecas.append(pulsos)
        coords = []
        marcas = []

    for ts, lat, lon in limpos:
        x, y = _utm(lat, lon)
        rumo = None
        dist_gps = 0.0
        if xy_anterior is not None:
            dx = x - xy_anterior[0]
            dy = y - xy_anterior[1]
            dist_gps = math.hypot(dx, dy)
            if dist_gps >= 8:
                rumo = math.atan2(dy, dx)
        snap = _projetar(malha, x, y, rumo, anterior.peca if anterior else None)
        xy_anterior = (x, y)
        if snap is None:
            fechar()
            anterior = None
            ts_anterior = ""
            continue
        if anterior is None:
            fechar()
            coords = [(snap.x, snap.y)]
            marcas = [ts]
            anterior = snap
            ts_anterior = ts
            continue
        salto = math.hypot(snap.x - anterior.x, snap.y - anterior.y)
        dt = _segundos(ts_anterior, ts)
        if dt > GAP_S or (dt <= 0 and salto > FOLGA_CAMINHO_M):
            fechar()
            coords = [(snap.x, snap.y)]
            marcas = [ts]
            anterior = snap
            ts_anterior = ts
            continue
        teto_m = (VEL_MAX / 3.6 * dt) if dt > 0 else 0.0
        vel_gps = dist_gps / dt * 3.6 if dt > 0 else VEL_MAX
        if dt > 0 and dist_gps > teto_m:
            fechar()
            coords = [(snap.x, snap.y)]
            marcas = [ts]
            anterior = snap
            ts_anterior = ts
            continue
        limite = _limite_do_vao(dt, dist_gps, salto, teto_m, vel_gps)
        ligado = _ligar(malha, anterior, snap, limite)
        if ligado is None:
            if salto < 2:
                anterior = snap
                ts_anterior = ts
                continue
            fechar()
            coords = [(snap.x, snap.y)]
            marcas = [ts]
            anterior = snap
            ts_anterior = ts
            continue
        cheio, partes = ligado
        if _usa_reta_lenta(dt, vel_gps):
            if cheio.length > dist_gps * FATOR_LENTO + FOLGA_LENTA_M + 1:
                fechar()
                coords = [(snap.x, snap.y)]
                marcas = [ts]
                anterior = snap
                ts_anterior = ts
                continue
            partes = [cheio] if cheio.length >= 8 else partes
        elif dt > 0 and vel_gps < VEL_BAIXA:
            partes = [cheio] if cheio.length >= 8 else partes
        elif dt > 0 and cheio.length > teto_m + 1:
            fechar()
            coords = [(snap.x, snap.y)]
            marcas = [ts]
            anterior = snap
            ts_anterior = ts
            continue
        if not partes:
            fechar()
            coords = [(snap.x, snap.y)]
            marcas = [ts]
            anterior = snap
            ts_anterior = ts
            continue
        if len(partes) == 1:
            geom = partes[0]
        else:
            for parte in partes:
                fechar()
                _gravar_trecho(parte, ts_anterior, ts)
            fechar()
            coords = [(snap.x, snap.y)]
            marcas = [ts]
            anterior = snap
            ts_anterior = ts
            continue
        novos = _coords_de(geom)
        if coords and novos and math.hypot(novos[0][0] - coords[-1][0], novos[0][1] - coords[-1][1]) < 1:
            novos = novos[1:]
        if novos and coords:
            from datetime import timedelta

            percurso = [coords[-1], *novos]
            distancias = [0.0]
            for a, b in zip(percurso, percurso[1:]):
                distancias.append(distancias[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
            total = distancias[-1] or 1.0
            base = _hora(ts_anterior)
            dt = max(0.0, _segundos(ts_anterior, ts))
            for indice, (px, py) in enumerate(novos, start=1):
                if base is None or dt <= 0:
                    marca = ts
                else:
                    marca = (base + timedelta(seconds=dt * distancias[indice] / total)).isoformat(timespec="seconds")
                coords.append((px, py))
                marcas.append(marca)
            marcas[-1] = ts
        anterior = snap
        ts_anterior = ts
    fechar()
    geom_final = None
    if len(linhas_utm) == 1:
        geom_final = linhas_utm[0]
    elif linhas_utm:
        geom_final = MultiLineString(linhas_utm)
    linhas = []
    for peca in pecas:
        linha = [[lat, lon] for _ts, lat, lon in peca]
        if len(linha) >= 2:
            linhas.append(linha)
    return Trajeto("", "", linhas, geom_final, pecas, len(limpos))


def aproximar_na_via(lat: float, lon: float) -> tuple[float, float]:
    """Projeção do pulso na via. Sem via perto, devolve o próprio GPS."""
    try:
        malha = rede_oficial()
    except Exception:
        return lat, lon
    if malha.vazia:
        return lat, lon
    x, y = _utm(lat, lon)
    snap = _projetar(malha, x, y, None, None)
    if snap is None:
        return lat, lon
    lat2, lon2 = _wgs(snap.x, snap.y)
    return lat2, lon2


_CACHE: dict[tuple, dict[str, Trajeto]] = {}


def trajetos_do_turno(dia: date, turno: str) -> dict[str, Trajeto]:
    """Uma passagem pelo banco. Tracks e difícil acesso leem este dicionário."""
    from .live_db import caminho_banco, conectar

    con = conectar(leitura=True)
    try:
        rows = con.execute(
            """
            SELECT placa, ts, lat, lon FROM ponto
            WHERE data = ? AND turno = ?
            ORDER BY placa, ts
            """,
            (dia.isoformat(), turno),
        ).fetchall()
    finally:
        con.close()
    ultimo = ""
    por_placa: dict[str, list[Ponto]] = {}
    for row in rows:
        placa = str(row["placa"] or "")
        ts = str(row["ts"] or "")
        por_placa.setdefault(placa, []).append((ts, float(row["lat"]), float(row["lon"])))
        if ts > ultimo:
            ultimo = ts
    chave = (str(caminho_banco()), dia.isoformat(), turno, len(rows), ultimo)
    guardado = _CACHE.get(chave)
    if guardado is not None:
        return guardado
    malha = rede_oficial()
    saida: dict[str, Trajeto] = {}
    for placa, serie in por_placa.items():
        trajeto = casar_pulsos(serie, malha, dia, turno)
        trajeto.placa = placa
        trajeto.tipo = tipo_da_placa(placa)
        saida[placa] = trajeto
    if len(_CACHE) > 6:
        _CACHE.clear()
    _CACHE[chave] = saida
    return saida


def percentual_das_rotas(dia: date, turno: str, codigos: list[str]) -> dict[str, float]:
    """Percentual pela linha casada. Só compactador e basculante entram na conta."""
    from shapely.ops import unary_union
    from shapely.strtree import STRtree

    from .dificil_acesso import norm_placa
    from .mapa_oficial import carregar_setores
    from .unificado import _casa_turno

    nomes = {item["placa"]: item["veiculo"] for item in placas_do_percentual(dia, turno)}
    liberadas = {norm_placa(placa) for placa in nomes}
    pecas: list[LineString] = []
    for placa, trajeto in trajetos_do_turno(dia, turno).items():
        if norm_placa(placa) not in liberadas and not entra_no_percentual(placa, ""):
            continue
        geom = trajeto.geom
        if geom is None or geom.is_empty:
            continue
        partes = [geom] if geom.geom_type == "LineString" else list(getattr(geom, "geoms", ()))
        for parte in partes:
            if parte is None or parte.is_empty:
                continue
            try:
                simples = parte.simplify(2.0, preserve_topology=True)
            except Exception:
                simples = parte
            if simples is not None and not simples.is_empty:
                pecas.append(simples)
    if not pecas:
        return {}
    arvore = STRtree(pecas)
    setores = {str(item["codigo"]).upper(): item for item in carregar_setores(dia)}
    saida: dict[str, float] = {}
    for codigo in codigos:
        chave = str(codigo or "").strip().upper()
        setor = setores.get(chave)
        geom = None if setor is None else setor.get("geom")
        ruas = [] if setor is None else (setor.get("ruas") or [])
        if geom is None or not ruas:
            continue
        if not _casa_turno(str(setor.get("frequencia") or ""), turno):
            continue
        try:
            indices = list(arvore.query(geom.buffer(6), predicate="intersects"))
        except TypeError:
            indices = []
        pedacos = [pecas[int(i)] for i in indices]
        if not pedacos:
            saida[chave] = 0.0
            continue
        try:
            uniao = unary_union(pedacos)
        except Exception:
            continue
        valor = percentual_da_linha(uniao, ruas, geom, setor.get("nomes") or [])
        if valor is not None:
            saida[chave] = float(valor)
    return saida


PISTA_M = 40.0
PISTA_MIN_M = 3.0


def _nome_via(texto: str) -> str:
    return " ".join(str(texto or "").strip().casefold().split())


def _segmentos_nomeados(ruas: list[Any], nomes: list[str] | None) -> tuple[list[LineString], list[str]]:
    from .cobertura import PASSO_M, _cortar

    segmentos: list[LineString] = []
    rotulos: list[str] = []
    for indice, rua in enumerate(ruas or []):
        nome = _nome_via(nomes[indice]) if nomes and indice < len(nomes) else ""
        if rua is None or getattr(rua, "is_empty", True):
            continue
        tipo = getattr(rua, "geom_type", "")
        partes = list(rua.geoms) if tipo == "MultiLineString" else [rua]
        for parte in partes:
            if getattr(parte, "geom_type", "") != "LineString" or parte.length < 2:
                continue
            for pedaco in _cortar(parte, PASSO_M):
                segmentos.append(pedaco)
                rotulos.append(nome)
    return segmentos, rotulos


def _paralela(origem: LineString, outra: LineString) -> bool:
    a0, a1 = origem.coords[0], origem.coords[-1]
    b0, b1 = outra.coords[0], outra.coords[-1]
    rumo_a = math.atan2(a1[1] - a0[1], a1[0] - a0[0])
    rumo_b = math.atan2(b1[1] - b0[1], b1[0] - b0[0])
    delta = abs(rumo_a - rumo_b) % math.pi
    delta = min(delta, math.pi - delta)
    return delta <= math.radians(25)


def _outra_pista(origem: LineString, outra: LineString) -> bool:
    """A outra faixa da mesma via, ao lado, até 40 m. A continuação da mesma faixa não entra."""
    if origem.length < 2 or outra.length < 2 or not _paralela(origem, outra):
        return False
    projecoes = []
    laterais = []
    for x, y in (outra.coords[0], outra.coords[-1]):
        ponto = Point(x, y)
        along = float(origem.project(ponto))
        projecoes.append(along)
        encostado = min(max(along, 0.0), origem.length)
        laterais.append(float(ponto.distance(origem.interpolate(encostado))))
    sobre = min(max(projecoes), origem.length) - max(min(projecoes), 0.0)
    if sobre < 8:
        return False
    lateral = min(laterais)
    return PISTA_MIN_M <= lateral <= PISTA_M


def _creditar_pista_dupla(segmentos: list[LineString], nomes: list[str], cobertos: set[int]) -> None:
    por_nome: dict[str, list[int]] = defaultdict(list)
    for indice, nome in enumerate(nomes):
        if nome:
            por_nome[nome].append(indice)
    extras: set[int] = set()
    for indice in cobertos:
        nome = nomes[indice] if indice < len(nomes) else ""
        if not nome:
            continue
        for outro in por_nome.get(nome, []):
            if outro == indice or outro in cobertos or outro in extras:
                continue
            if _outra_pista(segmentos[indice], segmentos[outro]):
                extras.add(outro)
    cobertos.update(extras)


def percentual_da_linha(geom, ruas: list[Any], setor, nomes: list[str] | None = None) -> float | None:
    """Metros da via oficial que o caminho já casado percorreu, dentro do polígono.

    A linha vem da malha completa. Só conta o trecho oficial que coincide com ela.
    Sem vias não há percentual. A via de contorno só entra se a linha passou por ela.
    """
    if not ruas or setor is None or geom is None or getattr(geom, "is_empty", True):
        return 0.0 if ruas and setor is not None else None
    from .cobertura import _mascara_contorno, percentual_das_vias

    try:
        recorte = geom.intersection(setor.buffer(6))
    except Exception:
        return 0.0
    if recorte.is_empty:
        return 0.0
    segmentos, rotulos = _segmentos_nomeados(ruas, nomes)
    if not segmentos:
        return None
    try:
        faixa = recorte.buffer(5.0)
    except Exception:
        return 0.0
    cobertos: set[int] = set()
    for indice, segmento in enumerate(segmentos):
        try:
            trecho = segmento.intersection(faixa)
        except Exception:
            continue
        comp = 0.0 if trecho.is_empty else float(trecho.length)
        limite = 8.0 if segmento.length >= 16 else max(2.0, segmento.length * 0.5)
        if comp >= limite and comp >= segmento.length * 0.45:
            cobertos.add(indice)
    _creditar_pista_dupla(segmentos, rotulos, cobertos)
    contorno = _mascara_contorno(segmentos, setor)
    feitos = sum(segmentos[i].length for i in cobertos)
    faltando = sum(
        segmentos[i].length
        for i in range(len(segmentos))
        if i not in cobertos and not contorno[i]
    )
    return percentual_das_vias(feitos, faltando)


def atividade_da_linha(geom, ruas: list[Any], setor) -> bool:
    """Há linha casada sobre via interna do polígono. Contorno sozinho não conta."""
    if geom is None or getattr(geom, "is_empty", True) or not ruas or setor is None:
        return False
    from .cobertura import _mascara_interna, _segmentos_oficiais

    try:
        recorte = geom.intersection(setor.buffer(6))
    except Exception:
        return False
    if recorte.is_empty:
        return False
    segmentos = _segmentos_oficiais(ruas)
    if not segmentos:
        return False
    try:
        faixa = recorte.buffer(8.0)
    except Exception:
        return False
    cobertos: set[int] = set()
    for indice, segmento in enumerate(segmentos):
        try:
            trecho = segmento.intersection(faixa)
        except Exception:
            continue
        comp = 0.0 if trecho.is_empty else float(trecho.length)
        limite = 8.0 if segmento.length >= 16 else max(2.0, segmento.length * 0.5)
        if comp >= limite and comp >= segmento.length * 0.45:
            cobertos.add(indice)
    if not cobertos:
        return False
    interna = _mascara_interna(segmentos, setor)
    if any(interna):
        metros = sum(segmentos[i].length for i in cobertos if interna[i])
        return metros >= 30.0
    metros = sum(segmentos[i].length for i in cobertos)
    return metros >= 30.0
