from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any, Iterable

from pyproj import Transformer
from shapely import contains_xy, covers, points
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
    pintado = MultiPoint(coords).buffer(raio_m, quad_segs=3).intersection(geom)
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


def percentual_das_vias(metros_feitos: float, metros_faltando: float) -> float:
    """Metros verdes / metros de vias desenhadas. O raio continua o de avaliar_cobertura.

    100 só quando não há trecho faltante. 99,x não vira 100.
    """
    feitos = max(0.0, float(metros_feitos or 0))
    faltando = max(0.0, float(metros_faltando or 0))
    total = feitos + faltando
    if total <= 0 or feitos <= 0:
        return 0.0
    if faltando <= 0:
        return 100.0
    pct = round(100.0 * feitos / total, 1)
    if pct >= 100.0:
        return 99.9
    if pct <= 0.0:
        return 0.1
    return pct


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
    """Cada permanência dentro do polígono.

    A hora de entrada e de saída pode usar o cruzamento com a borda.
    O traço desenhado usa só pulsos GPS persistidos: sem vértice sintético
    na borda e sem corda quando o salto passa de JUNTA_M.
    """
    if geom is None or geom.is_empty or not pontos:
        return []
    from datetime import datetime

    from .gps import _datetime_do_timestamp
    from .traco_agente import JUNTA_M, _chave_tempo

    bruto: list[tuple[datetime | None, dict[str, Any], tuple[float, float]]] = []
    for ponto in pontos:
        xy = _xy(ponto)
        if not xy:
            continue
        dt = _datetime_do_timestamp(str(ponto.get("timestamp") or ""), dia)
        bruto.append((dt, ponto, xy))
    if not bruto:
        return []
    try:
        mascara = covers(
            geom,
            points([xy[0] for _, _, xy in bruto], [xy[1] for _, _, xy in bruto]),
        )
    except Exception:
        mascara = [False] * len(bruto)
    itens: list[tuple[datetime | None, dict[str, Any], tuple[float, float], bool]] = [
        (dt, ponto, xy, bool(flag)) for (dt, ponto, xy), flag in zip(bruto, mascara)
    ]
    itens.sort(key=lambda item: (_chave_tempo(item[1]), item[0] or datetime.min))
    if not itens:
        return []

    gap_m = JUNTA_M

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

    def _fechar(hora: datetime | None) -> None:
        nonlocal aberta
        if not aberta:
            return
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

    def _abrir(hora: datetime | None, ponto: dict[str, Any]) -> None:
        nonlocal aberta
        linha: list[list[float]] = []
        _junta(linha, _latlon(ponto))
        aberta = {"linha": linha, "hora_ini": hora, "hora_ultimo": hora}

    for i, (dt, ponto, xy, dentro) in enumerate(itens):
        if i == 0:
            if dentro:
                _abrir(dt, ponto)
            continue
        dt0, ponto0, xy0, dentro0 = itens[i - 1]
        dist = math.hypot(xy[0] - xy0[0], xy[1] - xy0[1])
        if dist > gap_m:
            if dentro0:
                if aberta is None:
                    _abrir(dt0, ponto0)
                _fechar(dt0)
            if dentro:
                _abrir(dt, ponto)
            continue
        if not dentro0 and dentro:
            cruz = _cruzar(xy0, xy, True)
            if cruz:
                _xy_ent, frac = cruz
                _abrir(_hora(dt0, dt, frac), ponto)
            else:
                _abrir(dt, ponto)
        elif dentro0 and dentro:
            if aberta is None:
                _abrir(dt0, ponto0)
            if aberta is not None:
                _junta(aberta["linha"], _latlon(ponto))
                if dt:
                    aberta["hora_ultimo"] = dt
        elif dentro0 and not dentro:
            cruz = _cruzar(xy0, xy, False)
            if aberta is None:
                _abrir(dt0, ponto0)
            if cruz:
                _xy_sai, frac = cruz
                _fechar(_hora(dt0, dt, frac))
            else:
                _fechar(dt0)
    if aberta:
        _fechar(aberta.get("hora_ultimo"))
    return saida


SEM_APOIO = "Sem apoio identificado"


def _instante(ponto: dict[str, Any]) -> datetime | None:
    texto = str(ponto.get("timestamp") or ponto.get("ts") or "").strip().replace("Z", "")
    if not texto:
        return None
    try:
        return datetime.fromisoformat(texto[:19])
    except ValueError:
        return None


def _pontos_com_xy(pontos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [ponto for ponto in pontos or [] if _xy(ponto)]


def _amostras_velocidade(pontos: list[dict[str, Any]]) -> list[float]:
    marcas: list[tuple[datetime, tuple[float, float]]] = []
    for ponto in pontos or []:
        par = _xy(ponto)
        momento = _instante(ponto)
        if par and momento is not None:
            marcas.append((momento, par))
    marcas.sort(key=lambda item: item[0])
    velocidades: list[float] = []
    for (t0, p0), (t1, p1) in zip(marcas, marcas[1:]):
        segundos = (t1 - t0).total_seconds()
        if segundos < 1 or segundos > 180:
            continue
        dist = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
        if dist <= 0 or dist > SALTO_GPS_M:
            continue
        velocidades.append(dist / segundos * 3.6)
    return velocidades


def _velocidade_mediana(pontos: list[dict[str, Any]]) -> float | None:
    velocidades = _amostras_velocidade(pontos)
    if not velocidades:
        return None
    velocidades.sort()
    meio = len(velocidades) // 2
    if len(velocidades) % 2:
        return velocidades[meio]
    return (velocidades[meio - 1] + velocidades[meio]) / 2.0


def _fracao_lenta(pontos: list[dict[str, Any]], teto_kmh: float) -> float:
    velocidades = _amostras_velocidade(pontos)
    if len(velocidades) < 3:
        return 0.0
    return sum(1 for vel in velocidades if vel <= teto_kmh) / len(velocidades)


def _alinhado_as_vias(
    pontos: list[dict[str, Any]],
    segmentos: list[LineString],
    indices: set[int],
) -> bool | None:
    """True quando o GPS segue a via. False quando só a cruza. None sem passos comparáveis."""
    from shapely.ops import nearest_points

    escolhidos = [segmentos[i] for i in indices if 0 <= i < len(segmentos)]
    if not escolhidos:
        return None
    malha = unary_union(escolhidos)
    if malha.is_empty or malha.length < 8:
        return None
    xy = [par for ponto in pontos or [] if (par := _xy(ponto))]
    concordam = 0
    total = 0
    for a, b in zip(xy, xy[1:]):
        dist = math.hypot(b[0] - a[0], b[1] - a[1])
        if dist < 8 or dist > SALTO_GPS_M:
            continue
        meio = Point(((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0))
        perto = nearest_points(malha, meio)[0]
        proj = float(malha.project(perto))
        antes = malha.interpolate(max(0.0, proj - 8.0))
        depois = malha.interpolate(min(malha.length, proj + 8.0))
        dx = depois.x - antes.x
        dy = depois.y - antes.y
        if math.hypot(dx, dy) < 1:
            continue
        delta = abs(math.atan2(b[1] - a[1], b[0] - a[0]) - math.atan2(dy, dx)) % math.pi
        if delta > math.pi / 2:
            delta = math.pi - delta
        total += 1
        if delta <= math.pi / 4:
            concordam += 1
    if total == 0:
        return None
    return concordam / total >= 0.5


def _indices_com_progressao(
    pontos: list[dict[str, Any]],
    segmentos: list[LineString],
    indices: set[int],
) -> set[int]:
    """Trechos em que o GPS anda ao longo da via, na velocidade já homologada.

    Um pulso no limite do raio ou um salto transversal credita a via na união,
    mas não conta como contribuição exclusiva de coleta. O corte é o passo já
    usado para ler a direção (8 m) e o teto de 28 km/h, não um mínimo de rua.
    """
    from shapely.ops import nearest_points

    from .verificacao import VEL_COLETA_MAX

    if not indices:
        return set()
    escolhidos = [(i, segmentos[i]) for i in indices if 0 <= i < len(segmentos)]
    marcas: list[tuple[datetime, tuple[float, float]]] = []
    for ponto in pontos or []:
        par = _xy(ponto)
        instante = _instante(ponto)
        if par and instante is not None:
            marcas.append((instante, par))
    marcas.sort(key=lambda item: item[0])
    comprovados: set[int] = set()
    for (t0, p0), (t1, p1) in zip(marcas, marcas[1:]):
        segundos = (t1 - t0).total_seconds()
        if segundos < 1 or segundos > 180:
            continue
        dist = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
        if dist < 8 or dist > SALTO_GPS_M:
            continue
        if dist / segundos * 3.6 > VEL_COLETA_MAX:
            continue
        rumo = math.atan2(p1[1] - p0[1], p1[0] - p0[0])
        meio = Point(((p0[0] + p1[0]) / 2.0, (p0[1] + p1[1]) / 2.0))
        for indice, segmento in escolhidos:
            if (
                segmento.distance(meio) > RAIO_M
                and segmento.distance(Point(p0)) > RAIO_M
                and segmento.distance(Point(p1)) > RAIO_M
            ):
                continue
            proj = float(segmento.project(nearest_points(segmento, meio)[0]))
            antes = segmento.interpolate(max(0.0, proj - 8.0))
            depois = segmento.interpolate(min(segmento.length, proj + 8.0))
            dx = depois.x - antes.x
            dy = depois.y - antes.y
            if math.hypot(dx, dy) < 1:
                continue
            delta = abs(rumo - math.atan2(dy, dx)) % math.pi
            if delta > math.pi / 2:
                delta = math.pi - delta
            if delta > math.pi / 4:
                continue
            along = abs(segmento.project(Point(p1)) - segmento.project(Point(p0)))
            if along < 8 or along < dist * 0.5:
                continue
            comprovados.add(indice)
    return comprovados


def _classe_presenca(
    pontos: list[dict[str, Any]],
    segmentos: list[LineString],
    indices: set[int],
    execucao: str,
) -> tuple[str, str]:
    """Executor, passagem ou não confirmado. Reusa o raio, o salto e a velocidade já homologados.

    FEITA e PARCIAL entram com os trechos oficiais que o GPS alcança. Os demais
    só entram com progressão ao longo da via e velocidade de coleta. Atalho,
    cruzamento e pulso isolado ficam de fora da união.
    """
    from .verificacao import VEL_COLETA_MAX

    if str(execucao or "").upper() in {"FEITA", "PARCIAL"}:
        if indices:
            return "executor", "coleta homologada com trecho oficial no raio de 45 m"
        return "nao_confirmado", "coleta homologada sem trecho oficial associado"
    if not indices:
        return "ausente", "nenhum trecho oficial a 45 m do GPS"
    if not _linhas_gps(pontos):
        return (
            "nao_confirmado",
            "pulsos junto da via sem sequência contínua; salto acima de 160 m não comprova o intervalo",
        )
    alinhado = _alinhado_as_vias(pontos, segmentos, indices)
    if alinhado is False:
        return "passagem", "o GPS cruza as vias oficiais sem progredir ao longo delas"
    vel = _velocidade_mediana(pontos)
    if vel is not None and vel > VEL_COLETA_MAX and _fracao_lenta(pontos, VEL_COLETA_MAX) < 0.35:
        return "passagem", "velocidade acima do limite homologado de coleta (28 km/h), sem permanência lenta"
    if alinhado is None or vel is None:
        return "nao_confirmado", "há GPS na via, sem direção ou velocidade suficientes para confirmar coleta"
    return "executor", "progressão ao longo das vias oficiais em velocidade compatível com coleta"


def _segmentos_oficiais(ruas: list[Any]) -> list[LineString]:
    segmentos: list[LineString] = []
    for rua in ruas or []:
        if rua is None or getattr(rua, "is_empty", True):
            continue
        tipo = getattr(rua, "geom_type", "")
        partes = list(rua.geoms) if tipo == "MultiLineString" else [rua]
        for parte in partes:
            if getattr(parte, "geom_type", "") != "LineString" or parte.length < 2:
                continue
            segmentos.extend(_cortar(parte, PASSO_M))
    return segmentos


def _perto_das_vias(pontos: list[dict[str, Any]], segmentos: list[LineString]) -> list[dict[str, Any]]:
    """Só o GPS junto da via oficial. Trabalho em outra rota não vira serviço aqui."""
    if not pontos or not segmentos:
        return []
    from shapely import dwithin
    from shapely.geometry import Point

    malha = unary_union(segmentos)
    pares: list[dict[str, Any]] = []
    geometrias: list[Point] = []
    for ponto in pontos:
        xy = _xy(ponto)
        if not xy:
            continue
        pares.append(ponto)
        geometrias.append(Point(xy))
    if not pares:
        return []
    mask = dwithin(geometrias, malha, RAIO_M)
    return [ponto for ponto, fica in zip(pares, mask) if bool(fica)]


def _indices_cobertos(segmentos: list[LineString], pontos: list[dict[str, Any]]) -> set[int]:
    """Mesma prova de avaliar_cobertura: linha do GPS ou, sem linha, o próprio pulso."""
    if not segmentos:
        return set()
    linhas = _linhas_gps(pontos)
    if linhas:
        gps = unary_union(linhas)
    else:
        xy = [par for ponto in pontos or [] if (par := _xy(ponto))]
        gps = unary_union([Point(par) for par in xy]) if xy else None
    if gps is None or gps.is_empty:
        return set()
    from shapely import dwithin

    mask = dwithin(gps, segmentos, RAIO_M)
    return {i for i, fica in enumerate(mask) if bool(fica)}


def _metros_indices(segmentos: list[LineString], indices: set[int]) -> float:
    return sum(segmentos[i].length for i in indices if 0 <= i < len(segmentos))


def _metros_wgs(linhas: list[Any]) -> float:
    total = 0.0
    for linha in linhas or []:
        if not isinstance(linha, list):
            continue
        xy: list[tuple[float, float]] = []
        for par in linha:
            if not isinstance(par, (list, tuple)) or len(par) < 2:
                continue
            ponto = _xy((par[0], par[1]))
            if not ponto:
                continue
            if xy and math.hypot(ponto[0] - xy[-1][0], ponto[1] - xy[-1][1]) < 0.4:
                continue
            xy.append(ponto)
        if len(xy) >= 2:
            total += LineString(xy).length
    return total


def _pct_parte(metros: float, total: float) -> float:
    if total <= 0 or metros <= 0:
        return 0.0
    if metros >= total - 0.5:
        return 100.0
    pct = round(100.0 * metros / total, 1)
    if pct >= 100.0:
        return 99.9
    if pct <= 0.0:
        return 0.1
    return pct


def _janela_participacao(pontos: list[dict[str, Any]]) -> dict[str, Any]:
    marcas = [(marca, ponto) for ponto in pontos or [] if (marca := _instante(ponto)) is not None]
    if not marcas:
        return {
            "hora_inicio": "",
            "hora_fim": "",
            "inicio": "",
            "fim": "",
            "horario": "",
            "ponto_inicio": [],
            "ponto_fim": [],
        }
    marcas.sort(key=lambda item: item[0])
    ini, ponto_ini = marcas[0]
    fim, ponto_fim = marcas[-1]

    def par(ponto: dict[str, Any]) -> list[float]:
        try:
            return [round(float(ponto["latitude"]), 6), round(float(ponto["longitude"]), 6)]
        except (KeyError, TypeError, ValueError):
            return []

    hora_ini = ini.strftime("%H:%M")
    hora_fim = fim.strftime("%H:%M")
    return {
        "hora_inicio": hora_ini,
        "hora_fim": hora_fim,
        "inicio": ini.strftime("%d/%m %H:%M"),
        "fim": fim.strftime("%d/%m %H:%M"),
        "horario": f"{hora_ini}–{hora_fim}",
        "ponto_inicio": par(ponto_ini),
        "ponto_fim": par(ponto_fim),
    }


def _trechos_gps(pontos: list[dict[str, Any]]) -> list[list[list[float]]]:
    return [_wgs(list(linha.coords)) for linha in _linhas_gps(pontos)]


def _cor_placa(placa: str) -> str:
    from .pdf_mapa import cor_da_placa

    return cor_da_placa(placa)


def _texto_observados(passagens: list[str] | None, nao_confirmados: list[str] | None) -> str:
    partes = []
    if passagens:
        partes.append("Passagem: " + ", ".join(passagens))
    if nao_confirmados:
        partes.append("Não confirmado: " + ", ".join(nao_confirmados))
    return " · ".join(partes)


def resumo_executores(
    executores: list[dict[str, Any]] | None,
    apoio: str = "",
    percentual: float | None = None,
    passagens: list[str] | None = None,
    nao_confirmados: list[str] | None = None,
) -> str:
    """Texto único da rota: principal, apoios, percentuais e total."""
    itens = [item for item in executores or [] if isinstance(item, dict) and str(item.get("placa") or "").strip()]
    if not itens:
        return apoio or ""
    partes = []
    for item in itens:
        if str(item.get("papel") or "") == "sobreposta":
            continue
        papel = "Principal" if str(item.get("papel") or "") == "principal" else "Apoio efetivo"
        horario = str(item.get("horario") or "").strip()
        texto = (
            f"{papel} {item.get('placa')} "
            f"{float(item.get('percentual') or 0):.0f}% "
            f"(exclusivo {float(item.get('contribuicao_exclusiva') or 0):.0f}%)"
        )
        if horario:
            texto += f" {horario}"
        partes.append(texto)
    if percentual is not None:
        partes.append(f"Total {float(percentual):.0f}%")
    if apoio == SEM_APOIO:
        partes.append(SEM_APOIO)
    observado = _texto_observados(passagens, nao_confirmados)
    if observado:
        partes.append(observado)
    return " · ".join(partes)


def ler_executores_json(texto: str | None) -> dict[str, Any]:
    """Lê o snapshot gravado. Não recalcula cobertura."""
    vazio = {
        "calculado": False,
        "percentual": None,
        "placa_principal": "",
        "veiculo_principal": "",
        "apoio": "",
        "placas_apoio": [],
        "sobrepostos": [],
        "cobertura_sobreposta": "",
        "executores": [],
        "passagens": [],
        "nao_confirmados": [],
        "auditoria": [],
        "diagnostico": {},
        "resumo_veiculos": "",
    }
    bruto = str(texto or "").strip()
    if not bruto:
        return vazio
    try:
        dados = json.loads(bruto)
    except json.JSONDecodeError:
        return vazio
    if not isinstance(dados, dict):
        return vazio
    executores = [item for item in dados.get("executores") or [] if isinstance(item, dict)]
    apoios = [str(item.get("placa") or "") for item in executores if str(item.get("papel") or "") == "apoio" and item.get("placa")]
    sobrepostos = [str(item.get("placa") or "") for item in executores if str(item.get("papel") or "") == "sobreposta" and item.get("placa")]
    apoio = str(dados.get("apoio") or "")
    percentual = dados.get("percentual")
    passagens = [str(item) for item in dados.get("passagens") or [] if str(item or "").strip()]
    nao_confirmados = [str(item) for item in dados.get("nao_confirmados") or [] if str(item or "").strip()]
    return {
        "calculado": bool(dados.get("calculado")),
        "percentual": percentual,
        "placa_principal": str(dados.get("placa_principal") or ""),
        "veiculo_principal": str(dados.get("veiculo_principal") or ""),
        "apoio": apoio,
        "placas_apoio": apoios,
        "sobrepostos": sobrepostos,
        "cobertura_sobreposta": ", ".join(sobrepostos),
        "executores": executores,
        "passagens": passagens,
        "nao_confirmados": nao_confirmados,
        "auditoria": [item for item in dados.get("auditoria") or [] if isinstance(item, dict)],
        "diagnostico": dados.get("diagnostico") if isinstance(dados.get("diagnostico"), dict) else {},
        "resumo_veiculos": resumo_executores(
            executores,
            apoio,
            percentual if percentual is not None else None,
            passagens,
            nao_confirmados,
        ),
    }


def cobertura_compartilhada(
    ruas: list[Any],
    veiculos: list[dict[str, Any]],
    geom_setor=None,
) -> dict[str, Any]:
    """União dos trechos oficiais executados por todos os veículos elegíveis.

    O caminhão de maior cobertura individual é o principal. Apoio efetivo é quem
    progrediu por um trecho oficial que nenhum outro cobriu. Cobertura só
    repetida, ou exclusiva apenas por aproximação, fica no detalhe. A união
    geográfica não depende da ordem e não descarta trecho já creditado.
    """
    segmentos = _segmentos_oficiais(ruas)
    vazio_exec: list[dict[str, Any]] = []
    if not segmentos:
        return _pacote_cobertura(0.0, [], [], vazio_exec, [], 0.0)

    avaliados: list[dict[str, Any]] = []
    passagens: list[str] = []
    nao_confirmados: list[str] = []
    auditoria: list[dict[str, Any]] = []
    for veiculo in veiculos or []:
        placa = str(veiculo.get("placa") or "").strip()
        if not placa:
            continue
        pontos = list(veiculo.get("pontos") or [])
        if geom_setor is not None:
            pontos = recortar_pontos(pontos, geom_setor, 90)
        pontos = _perto_das_vias(pontos, segmentos)
        if len(_pontos_com_xy(pontos)) < 2:
            auditoria.append(
                {
                    "placa": placa,
                    "classe": "ausente",
                    "motivo": "menos de dois pulsos a 45 m das vias oficiais",
                    "metros": 0.0,
                    "cobertura_geografica_m": 0.0,
                    "evidencia_operacional_m": 0.0,
                    "entra_na_uniao": False,
                }
            )
            continue
        indices = _indices_cobertos(segmentos, pontos)
        metros = _metros_indices(segmentos, indices)
        classe, motivo = _classe_presenca(pontos, segmentos, indices, str(veiculo.get("execucao") or ""))
        auditoria.append(
            {
                "placa": placa,
                "classe": classe,
                "motivo": motivo,
                "metros": round(metros, 1),
                "cobertura_geografica_m": round(metros, 1),
                "evidencia_operacional_m": 0.0,
                "entra_na_uniao": classe == "executor",
            }
        )
        if classe == "passagem":
            passagens.append(placa)
            continue
        if classe != "executor":
            if classe == "nao_confirmado":
                nao_confirmados.append(placa)
            continue
        avaliados.append(
            {
                "placa": placa,
                "veiculo": str(veiculo.get("veiculo") or placa),
                "pontos": pontos,
                "indices": indices,
                "metros": metros,
                "n": len(_pontos_com_xy(pontos)),
            }
        )

    if not avaliados:
        faltando = _fundir(segmentos)
        return _pacote_cobertura(
            0.0, [], faltando, vazio_exec, passagens, 0.0, nao_confirmados, auditoria
        )

    uniao: set[int] = set()
    for item in avaliados:
        uniao |= item["indices"]
    todos = set(range(len(segmentos)))
    feitos_idx = sorted(uniao)
    falta_idx = sorted(todos - uniao)
    feitos = _fundir([segmentos[i] for i in feitos_idx])
    faltando = _fundir([segmentos[i] for i in falta_idx])
    metros_feitos = _metros_wgs(feitos)
    metros_falta = _metros_wgs(faltando)
    total = metros_feitos + metros_falta
    percentual = percentual_das_vias(metros_feitos, metros_falta)

    avaliados.sort(key=lambda item: (-item["metros"], -item["n"], item["placa"]))
    principal_placa = avaliados[0]["placa"]
    executores: list[dict[str, Any]] = []
    for item in avaliados:
        outros: set[int] = set()
        for vizinho in avaliados:
            if vizinho["placa"] != item["placa"]:
                outros |= vizinho["indices"]
        exclusivos = item["indices"] - outros
        compartilhados = item["indices"] & outros
        progrediu = exclusivos if item["placa"] == principal_placa else _indices_com_progressao(item["pontos"], segmentos, exclusivos)
        sem_progressao = exclusivos - progrediu
        metros_exclusivos = _metros_wgs(_fundir([segmentos[i] for i in sorted(progrediu)])) if progrediu else 0.0
        metros_compartilhados = _metros_wgs(_fundir([segmentos[i] for i in sorted(compartilhados)])) if compartilhados else 0.0
        item["metros_exclusivos"] = metros_exclusivos
        item["metros_compartilhados"] = metros_compartilhados
        item["metros_sem_progressao"] = _metros_wgs(_fundir([segmentos[i] for i in sorted(sem_progressao)])) if sem_progressao else 0.0
        metros_proprios = _metros_wgs(_fundir([segmentos[i] for i in sorted(item["indices"])]))
        janela = _janela_participacao(item["pontos"])
        if item["placa"] == principal_placa:
            papel, classificacao = "principal", "Principal"
        elif progrediu:
            papel, classificacao = "apoio", "Apoio efetivo"
        else:
            papel, classificacao = "sobreposta", "Cobertura sobreposta"
        executores.append(
            {
                "placa": item["placa"],
                "veiculo": item["veiculo"],
                "papel": papel,
                "classificacao": classificacao,
                "percentual": _pct_parte(metros_proprios, total),
                "contribuicao_exclusiva": _pct_parte(metros_exclusivos, total),
                "contribuicao_compartilhada": _pct_parte(metros_compartilhados, total),
                "cor": _cor_placa(item["placa"]),
                "trechos": _trechos_gps(item["pontos"]),
                **janela,
            }
        )
    executores.sort(
        key=lambda item: (
            {"principal": 0, "apoio": 1, "sobreposta": 2}.get(str(item["papel"]), 3),
            -float(item["contribuicao_exclusiva"]),
            -float(item["percentual"]),
            str(item["placa"]),
        )
    )
    por_placa = {item["placa"]: item for item in auditoria}
    for item in executores:
        ficha = por_placa.get(item["placa"])
        if not ficha:
            continue
        ficha["classe"] = item["papel"]
        origem = next(row for row in avaliados if row["placa"] == item["placa"])
        ficha["metros_exclusivos"] = round(float(origem.get("metros_exclusivos") or 0), 1)
        ficha["metros_compartilhados"] = round(float(origem.get("metros_compartilhados") or 0), 1)
        ficha["metros_sem_progressao"] = round(float(origem.get("metros_sem_progressao") or 0), 1)
        ficha["cobertura_geografica_m"] = round(float(ficha.get("metros") or 0), 1)
        ficha["evidencia_operacional_m"] = round(
            max(0.0, ficha["cobertura_geografica_m"] - ficha["metros_sem_progressao"]),
            1,
        )
        if item["papel"] == "apoio":
            ficha["motivo"] = "progressão contínua em trechos oficiais que nenhum outro veículo cobriu"
        elif item["papel"] == "sobreposta" and ficha["metros_sem_progressao"] > 0:
            ficha["motivo"] = (
                "coleta comprovada nas vias já cobertas por outro veículo; "
                "o trecho exclusivo é aproximação ou cruzamento, sem progressão contínua"
            )
        elif item["papel"] == "sobreposta":
            ficha["motivo"] = "coleta comprovada apenas em trechos já cobertos por outro veículo"
    somente_geograficos = sum(float(item.get("metros_sem_progressao") or 0) for item in avaliados)
    evidencia_m = max(0.0, metros_feitos - somente_geograficos)
    diagnostico = {
        "cobertura_geografica_m": round(metros_feitos, 1),
        "cobertura_geografica_pct": percentual,
        "metros_somente_geograficos": round(somente_geograficos, 1),
        "evidencia_operacional_m": round(evidencia_m, 1),
        "evidencia_operacional_pct": percentual_das_vias(evidencia_m, metros_falta + somente_geograficos),
    }
    return _pacote_cobertura(
        percentual,
        feitos,
        faltando,
        executores,
        passagens,
        round(metros_feitos / 1000.0, 3),
        nao_confirmados,
        auditoria,
        diagnostico,
    )


def _pacote_cobertura(
    percentual: float,
    feitos: list[Any],
    faltando: list[Any],
    executores: list[dict[str, Any]],
    passagens: list[str],
    km_feito: float,
    nao_confirmados: list[str] | None = None,
    auditoria: list[dict[str, Any]] | None = None,
    diagnostico: dict[str, Any] | None = None,
) -> dict[str, Any]:
    principal = next((item for item in executores if item.get("papel") == "principal"), None)
    placas_apoio = [str(item.get("placa") or "") for item in executores if item.get("papel") == "apoio"]
    sobrepostos = [str(item.get("placa") or "") for item in executores if item.get("papel") == "sobreposta"]
    apoio = ", ".join(placa for placa in placas_apoio if placa) or SEM_APOIO
    cobertura_sobreposta = ", ".join(placa for placa in sobrepostos if placa)
    fora = list(nao_confirmados or [])
    fichas = list(auditoria or [])
    leitura = diagnostico or {
        "cobertura_geografica_m": round(float(km_feito or 0) * 1000.0, 1),
        "cobertura_geografica_pct": percentual,
        "metros_somente_geograficos": 0.0,
        "evidencia_operacional_m": round(float(km_feito or 0) * 1000.0, 1),
        "evidencia_operacional_pct": percentual,
    }
    persistencia = {
        "calculado": True,
        "percentual": percentual,
        "placa_principal": (principal or {}).get("placa") or "",
        "veiculo_principal": (principal or {}).get("veiculo") or "",
        "apoio": apoio,
        "sobrepostos": sobrepostos,
        "cobertura_sobreposta": cobertura_sobreposta,
        "passagens": passagens,
        "nao_confirmados": fora,
        "auditoria": fichas,
        "diagnostico": leitura,
        "executores": [
            {chave: valor for chave, valor in item.items() if chave != "trechos"}
            for item in executores
        ],
    }
    return {
        "calculado": True,
        "percentual": percentual,
        "km_feito": km_feito,
        "trechos_feitos": feitos,
        "trechos_faltando": faltando,
        "executores": executores,
        "passagens": passagens,
        "nao_confirmados": fora,
        "auditoria": fichas,
        "diagnostico": leitura,
        "apoio": apoio,
        "sobrepostos": sobrepostos,
        "cobertura_sobreposta": cobertura_sobreposta,
        "placa_principal": persistencia["placa_principal"],
        "veiculo_principal": persistencia["veiculo_principal"],
        "persistencia": persistencia,
        "resumo_veiculos": resumo_executores(executores, apoio, percentual, passagens, fora),
    }
