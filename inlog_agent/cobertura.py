from __future__ import annotations

import math
from typing import Any, Iterable

from pyproj import Transformer
from shapely import contains_xy
from shapely.geometry import LineString, MultiPoint, Point
from shapely.ops import linemerge, unary_union

from .gps import haversine_m

TO_UTM = Transformer.from_crs(4326, 31982, always_xy=True)
TO_WGS = Transformer.from_crs(31982, 4326, always_xy=True)

RAIO_M = 45.0
PASSO_M = 40.0
SALTO_GPS_M = 160.0
FEITA_MIN = 90.0
PARCIAL_MIN = 45.0


def _xy(ponto: dict[str, Any] | tuple[float, float]) -> tuple[float, float] | None:
    if isinstance(ponto, dict) and ponto.get("_utm"):
        return ponto["_utm"]
    if isinstance(ponto, (tuple, list)):
        lat, lon = float(ponto[0]), float(ponto[1])
    else:
        cached = ponto.get("_utm") if isinstance(ponto, dict) else None
        if cached:
            return cached
        lat = ponto.get("latitude")
        lon = ponto.get("longitude")
        if lat is None or lon is None:
            return None
        lat, lon = float(lat), float(lon)
    x, y = TO_UTM.transform(lon, lat)
    if isinstance(ponto, dict):
        ponto["_utm"] = (x, y)
    return x, y


def preparar_pontos(pontos: list[dict[str, Any]]) -> list[tuple[dict[str, Any], float, float]]:
    """Converte a frota para UTM de uma vez só, para o mapa oficial não repetir isso 71 vezes."""
    faltando: list[dict[str, Any]] = []
    lons: list[float] = []
    lats: list[float] = []
    for ponto in pontos or []:
        if ponto.get("_utm"):
            continue
        try:
            lat = float(ponto.get("latitude"))
            lon = float(ponto.get("longitude"))
        except (TypeError, ValueError):
            continue
        faltando.append(ponto)
        lats.append(lat)
        lons.append(lon)
    if faltando:
        xs, ys = TO_UTM.transform(lons, lats)
        for ponto, x, y in zip(faltando, xs, ys):
            ponto["_utm"] = (float(x), float(y))
    pares = []
    for ponto in pontos or []:
        utm = ponto.get("_utm")
        if utm:
            pares.append((ponto, float(utm[0]), float(utm[1])))
    return pares


def _wgs(coords: list[tuple[float, float]]) -> list[list[float]]:
    saida = []
    for x, y in coords:
        lon, lat = TO_WGS.transform(x, y)
        saida.append([lat, lon])
    return saida


def _linhas_gps(pontos: list[dict[str, Any]], salto_m: float = SALTO_GPS_M) -> list[LineString]:
    xy: list[tuple[float, float]] = []
    for ponto in pontos or []:
        par = _xy(ponto)
        if par:
            xy.append(par)
    linhas: list[LineString] = []
    atual: list[tuple[float, float]] = []
    for par in xy:
        if atual and math.hypot(par[0] - atual[-1][0], par[1] - atual[-1][1]) > salto_m:
            if len(atual) >= 2:
                linhas.append(LineString(atual))
            atual = [par]
            continue
        if not atual or math.hypot(par[0] - atual[-1][0], par[1] - atual[-1][1]) >= 4:
            atual.append(par)
    if len(atual) >= 2:
        linhas.append(LineString(atual))
    return linhas


def recortar_pontos(pontos: list[dict[str, Any]], geom, buffer_m: float = 90) -> list[dict[str, Any]]:
    if geom is None or not pontos:
        return list(pontos or [])
    zona = geom.buffer(buffer_m)
    return _dentro_da_zona(preparar_pontos(pontos), zona)


def _dentro_da_zona(
    pares: list[tuple[dict[str, Any], float, float]],
    zona,
) -> list[dict[str, Any]]:
    if not pares or zona is None or zona.is_empty:
        return []
    minx, miny, maxx, maxy = zona.bounds
    xs: list[float] = []
    ys: list[float] = []
    candidatos: list[dict[str, Any]] = []
    for ponto, x, y in pares:
        if x < minx or x > maxx or y < miny or y > maxy:
            continue
        candidatos.append(ponto)
        xs.append(x)
        ys.append(y)
    if not candidatos:
        return []
    mask = contains_xy(zona, xs, ys)
    return [ponto for ponto, fica in zip(candidatos, mask) if fica]


DENTRO_M = 12.0
BORDA_M = 90.0


def cobertura_espacial(
    pontos: list[dict[str, Any]],
    geom,
    raio_m: float = 80.0,
) -> dict[str, Any]:
    """Olha o mapa: quanto do polígono o GPS realmente pintou, não o km andado."""
    if geom is None or geom.is_empty:
        return {"percentual": 0.0, "area_pct": 0.0, "metodo": "mapa"}
    visto: set[tuple[int, int]] = set()
    coords: list[tuple[float, float]] = []
    for ponto in pontos or []:
        par = ponto.get("_utm") if isinstance(ponto, dict) else None
        if not par:
            par = _xy(ponto)
        if not par:
            continue
        chave = (int(par[0] / 25), int(par[1] / 25))
        if chave in visto:
            continue
        visto.add(chave)
        coords.append(par)
    if not coords:
        return {"percentual": 0.0, "area_pct": 0.0, "metodo": "mapa"}
    pintado = MultiPoint(coords).buffer(raio_m).intersection(geom)
    area_pct = 100.0 * pintado.area / geom.area if geom.area else 0.0
    area_pct = round(min(100.0, area_pct), 1)
    return {
        "percentual": area_pct,
        "area_pct": area_pct,
        "metodo": "mapa",
        "km2_pintado": round(pintado.area / 1e6, 4),
        "km2_setor": round(geom.area / 1e6, 4),
    }


def separar_dentro_borda(
    pontos: list[dict[str, Any]],
    geom,
    dentro_m: float = DENTRO_M,
    borda_m: float = BORDA_M,
    pares: list[tuple[dict[str, Any], float, float]] | None = None,
    zona_geom=None,
    nucleo_geom=None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Separa GPS que entrou no polígono do que só passou ao lado (buffer)."""
    if geom is None or not pontos:
        return [], [], []
    pares = pares if pares is not None else preparar_pontos(pontos)
    zona = zona_geom if zona_geom is not None else geom.buffer(borda_m)
    nucleo = nucleo_geom if nucleo_geom is not None else geom.buffer(dentro_m)
    zona_pts = _dentro_da_zona(pares, zona)
    if not zona_pts:
        return [], [], []
    ids_zona = {id(p) for p in zona_pts}
    pares_zona = [item for item in pares if id(item[0]) in ids_zona]
    dentro = _dentro_da_zona(pares_zona, nucleo)
    ids_dentro = {id(p) for p in dentro}
    borda = [p for p in zona_pts if id(p) not in ids_dentro]
    return dentro, borda, zona_pts


def km_gps(pontos: list[dict[str, Any]], salto_m: float = 400) -> float:
    total = 0.0
    anterior = None
    for ponto in pontos or []:
        lat = ponto.get("latitude")
        lon = ponto.get("longitude")
        if lat is None or lon is None:
            continue
        atual = (float(lat), float(lon))
        if anterior is not None:
            dist = haversine_m(anterior, atual)
            if 0 < dist < salto_m:
                total += dist
        anterior = atual
    return total / 1000.0


def _cortar(linha: LineString, passo_m: float) -> list[LineString]:
    comprimento = linha.length
    if comprimento <= passo_m * 1.4:
        return [linha]
    n = max(2, int(math.ceil(comprimento / passo_m)))
    trechos = []
    for i in range(n):
        a = linha.interpolate(comprimento * i / n)
        b = linha.interpolate(comprimento * (i + 1) / n)
        if a.distance(b) >= 2:
            trechos.append(LineString([a, b]))
    return trechos or [linha]


def _iter_linhas(geom) -> Iterable:
    if geom is None or geom.is_empty:
        return
    tipo = geom.geom_type
    if tipo == "LineString":
        yield geom
        return
    if tipo in {"MultiLineString", "GeometryCollection"}:
        for parte in geom.geoms:
            yield from _iter_linhas(parte)


def _parece_contorno(linhas: list[LineString]) -> bool:
    if len(linhas) != 1:
        return False
    linha = linhas[0]
    return bool(linha.is_closed and linha.length > 2500)


def _fundir(linhas: list[LineString]) -> list[list[list[float]]]:
    if not linhas:
        return []
    try:
        merged = linemerge(unary_union(linhas))
    except (ValueError, TypeError):
        merged = unary_union(linhas)
    saida = []
    for geom in _iter_linhas(merged):
        if geom.length < 8:
            continue
        saida.append(_wgs(list(geom.coords)))
    if saida:
        return saida
    for geom in linhas:
        if geom.is_empty or geom.length < 8:
            continue
        saida.append(_wgs(list(geom.coords)))
    return saida


def _confianca(n_pts: int, km_rastreio: float, km_plano: float, n_gaps: int) -> str:
    if n_pts < 15:
        return "baixa"
    dens = n_pts / max(km_rastreio, 0.4)
    if dens >= 14 and n_gaps <= 6 and km_rastreio >= min(2.0, 0.25 * max(km_plano, 0.1)):
        return "alta"
    if dens >= 6:
        return "media"
    return "baixa"


def _veredicto(pct: float, confianca: str, n_pts: int) -> str:
    if n_pts == 0:
        return "SEM RASTREIO"
    if confianca == "baixa" and pct < FEITA_MIN:
        return "INCERTA"
    if pct >= FEITA_MIN:
        return "FEITA"
    if pct >= PARCIAL_MIN:
        return "PARCIAL"
    return "NÃO FEITA"


def _linhas_planejadas(planejada: list[Any], salto_m: float = 90) -> list[LineString]:
    xy: list[tuple[float, float]] = []
    for ponto in planejada or []:
        par = _xy(ponto)
        if par:
            xy.append(par)
    linhas: list[LineString] = []
    atual: list[tuple[float, float]] = []
    for par in xy:
        if atual and math.hypot(par[0] - atual[-1][0], par[1] - atual[-1][1]) > salto_m:
            if len(atual) >= 2:
                linhas.append(LineString(atual))
            atual = [par]
            continue
        atual.append(par)
    if len(atual) >= 2:
        linhas.append(LineString(atual))
    return linhas


def avaliar_cobertura(
    planejada: list[Any],
    gps: list[dict[str, Any]],
    geom_setor=None,
    km_oficial: float | None = None,
    ruas: list[LineString] | None = None,
) -> dict[str, Any]:
    pontos = recortar_pontos(gps, geom_setor, 90) if geom_setor is not None else list(gps or [])
    n_pts = len(pontos)
    km_rastreio = km_gps(pontos, salto_m=SALTO_GPS_M + 40)
    planejadas = list(ruas or [])
    if not planejadas:
        candidatas = _linhas_planejadas(planejada)
        if candidatas and not _parece_contorno(candidatas):
            planejadas = candidatas
    gps_linhas = _linhas_gps(pontos)
    gps_xy = [_xy(p) for p in pontos]
    gps_xy = [p for p in gps_xy if p]

    feitos: list[LineString] = []
    faltando: list[LineString] = []
    km_plano = 0.0
    km_feito = 0.0
    if planejadas:
        gps_geom = None
        if gps_linhas:
            gps_geom = unary_union(gps_linhas)
        elif gps_xy:
            gps_geom = unary_union([Point(p) for p in gps_xy])
        for linha in planejadas:
            for trecho in _cortar(linha, PASSO_M):
                km_plano += trecho.length / 1000.0
                coberto = (
                    gps_geom is not None
                    and not gps_geom.is_empty
                    and gps_geom.distance(trecho) <= RAIO_M
                )
                if coberto:
                    feitos.append(trecho)
                    km_feito += trecho.length / 1000.0
                else:
                    faltando.append(trecho)
        pct = round(min(100.0, 100.0 * km_feito / km_plano), 1) if km_plano else 0.0
        if km_oficial and km_oficial > 0:
            pct = round(min(100.0, 100.0 * km_feito / km_oficial), 1)
            km_plano = float(km_oficial)
        metodo = "ruas"
    else:
        base = km_oficial or 0.0
        km_feito = km_rastreio
        km_plano = base
        pct = round(min(100.0, 100.0 * km_feito / base), 1) if base and pontos else 0.0
        metodo = "km-setor"

    gaps = 0
    anterior = None
    for par in gps_xy:
        if anterior is not None and math.hypot(par[0] - anterior[0], par[1] - anterior[1]) > 400:
            gaps += 1
        anterior = par
    conf = _confianca(n_pts, km_rastreio, km_plano or (km_oficial or 0), gaps)
    veredicto = _veredicto(pct, conf, n_pts)
    return {
        "percentual": pct,
        "veredicto": veredicto,
        "confianca": conf,
        "metodo": metodo,
        "km_coleta": round(km_feito if metodo == "ruas" else km_rastreio, 3),
        "km_gps": round(km_rastreio, 3),
        "km_total": round(km_plano or (km_oficial or 0.0), 3),
        "km_faltando": round(max(0.0, (km_plano or 0) - km_feito), 3),
        "pontos_no_setor": n_pts,
        "trechos_feitos": _fundir(feitos),
        "trechos_faltando": _fundir(faltando),
        "trechos_planejados": _fundir(planejadas),
        "trechos_gps": [_wgs(list(linha.coords)) for linha in gps_linhas],
        "pontos_clip": pontos,
    }


def linhas_de_coords(coords: list[list[list[float]]]) -> list[LineString]:
    linhas = []
    for trecho in coords or []:
        xy = []
        for par in trecho:
            if len(par) < 2:
                continue
            ponto = _xy((par[0], par[1]))
            if ponto:
                xy.append(ponto)
        if len(xy) >= 2:
            linhas.append(LineString(xy))
    return linhas


def passagens_na_rota(
    pontos: list[dict[str, Any]],
    geom,
    dia,
) -> list[dict[str, Any]]:
    """Cada permanência dentro do polígono, sem cortar o meio do percurso.

    A hora de entrada e de saída é o cruzamento com a borda.
    O traço liga esse ponto de entrada aos pulsos reais até a saída.
    """
    if geom is None or geom.is_empty or not pontos:
        return []
    from datetime import datetime

    from .gps import _datetime_do_timestamp
    from .traco_agente import _chave_tempo

    itens: list[tuple[datetime | None, dict[str, Any], tuple[float, float], bool]] = []
    for ponto in pontos:
        xy = _xy(ponto)
        if not xy:
            continue
        dt = _datetime_do_timestamp(str(ponto.get("timestamp") or ""), dia)
        try:
            dentro = bool(geom.covers(Point(xy)))
        except Exception:
            dentro = False
        itens.append((dt, ponto, xy, dentro))
    itens.sort(key=lambda item: (_chave_tempo(item[1]), item[0] or datetime.min))
    if not itens:
        return []

    teleporte_m = 1400.0

    def _wgs(xy: tuple[float, float]) -> list[float]:
        lon, lat = TO_WGS.transform(xy[0], xy[1])
        return [round(lat, 6), round(lon, 6)]

    def _latlon(ponto: dict[str, Any]) -> list[float] | None:
        try:
            return [round(float(ponto["latitude"]), 6), round(float(ponto["longitude"]), 6)]
        except (KeyError, TypeError, ValueError):
            return None

    def _pontos_geom(inter) -> list[Point]:
        if inter is None or inter.is_empty:
            return []
        tipo = inter.geom_type
        if tipo == "Point":
            return [inter]
        if tipo == "MultiPoint":
            return list(inter.geoms)
        if tipo == "LineString":
            return [Point(inter.coords[0]), Point(inter.coords[-1])]
        if tipo == "MultiLineString":
            saida = []
            for parte in inter.geoms:
                saida.extend(_pontos_geom(parte))
            return saida
        if tipo == "GeometryCollection":
            saida = []
            for parte in inter.geoms:
                saida.extend(_pontos_geom(parte))
            return saida
        return []

    def _cruzar(a_xy: tuple[float, float], b_xy: tuple[float, float], para_dentro: bool):
        linha = LineString([a_xy, b_xy])
        if linha.length < 0.4:
            return None
        try:
            marcas = _pontos_geom(linha.intersection(geom.boundary))
        except Exception:
            return None
        if not marcas:
            return None
        marcas.sort(key=lambda pt: linha.project(pt))
        cruz = marcas[0]
        alvo = b_xy if para_dentro else a_xy
        dx, dy = alvo[0] - cruz.x, alvo[1] - cruz.y
        norma = math.hypot(dx, dy) or 1.0
        passo = min(2.0, norma * 0.35)
        interno = (cruz.x + dx / norma * passo, cruz.y + dy / norma * passo)
        frac = max(0.0, min(1.0, linha.project(cruz) / linha.length))
        return interno, frac

    def _hora(a: datetime | None, b: datetime | None, frac: float) -> datetime | None:
        if a and b:
            return a + (b - a) * frac
        return b or a

    def _txt(dt: datetime | None) -> str:
        if not dt:
            return ""
        return dt.strftime("%d/%m %H:%M")

    def _janela(ini: datetime | None, fim: datetime | None) -> str:
        if not ini or not fim:
            return _txt(ini or fim)
        if ini.date() == fim.date():
            return f"{ini.strftime('%d/%m %H:%M')}–{fim.strftime('%H:%M')}"
        return f"{ini.strftime('%d/%m %H:%M')}–{fim.strftime('%d/%m %H:%M')}"

    def _junta(linha: list[list[float]], par: list[float] | None) -> None:
        if not par:
            return
        if linha:
            try:
                if haversine_m((linha[-1][0], linha[-1][1]), (par[0], par[1])) < 4:
                    return
            except (TypeError, ValueError):
                return
        linha.append(par)

    aberta: dict[str, Any] | None = None
    saida: list[dict[str, Any]] = []

    def _fechar(xy_saida: tuple[float, float] | None, hora: datetime | None) -> None:
        nonlocal aberta
        if not aberta:
            return
        if xy_saida is not None:
            _junta(aberta["linha"], _wgs(xy_saida))
        ini = aberta.get("hora_ini")
        fim = hora or aberta.get("hora_ultimo") or ini
        linha = aberta["linha"]
        if len(linha) >= 2 and ini and fim:
            saida.append(
                {
                    "hora_inicio": _txt(ini),
                    "hora_fim": _txt(fim),
                    "horario": _janela(ini, fim),
                    "ponto_inicio": list(linha[0]),
                    "ponto_fim": list(linha[-1]),
                    "trecho": linha,
                }
            )
        aberta = None

    def _abrir(xy_entrada: tuple[float, float] | None, hora: datetime | None, ponto: dict[str, Any]) -> None:
        nonlocal aberta
        linha: list[list[float]] = []
        if xy_entrada is not None:
            _junta(linha, _wgs(xy_entrada))
        _junta(linha, _latlon(ponto))
        aberta = {"linha": linha, "hora_ini": hora, "hora_ultimo": hora}

    for i, (dt, ponto, xy, dentro) in enumerate(itens):
        if i == 0:
            if dentro:
                _abrir(xy, dt, ponto)
            continue
        dt0, ponto0, xy0, dentro0 = itens[i - 1]
        dist = math.hypot(xy[0] - xy0[0], xy[1] - xy0[1])
        if dist > teleporte_m:
            if dentro0:
                _fechar(xy0, dt0)
            if dentro:
                _abrir(xy, dt, ponto)
            continue
        if not dentro0 and dentro:
            cruz = _cruzar(xy0, xy, True)
            if cruz:
                xy_ent, frac = cruz
                _abrir(xy_ent, _hora(dt0, dt, frac), ponto)
            else:
                _abrir(xy, dt, ponto)
        elif dentro0 and dentro:
            if aberta is None:
                _abrir(xy0, dt0, ponto0)
            if aberta is not None:
                _junta(aberta["linha"], _latlon(ponto))
                if dt:
                    aberta["hora_ultimo"] = dt
        elif dentro0 and not dentro:
            cruz = _cruzar(xy0, xy, False)
            if aberta is None:
                _abrir(xy0, dt0, ponto0)
            if cruz:
                xy_sai, frac = cruz
                _fechar(xy_sai, _hora(dt0, dt, frac))
            else:
                _fechar(xy0, dt0)
    if aberta:
        _fechar(None, aberta.get("hora_ultimo"))
    return saida
