from __future__ import annotations

import math
import re
from datetime import date, datetime, time, timedelta
from typing import Any

from .config import normalizar_hora

Ponto = tuple[float, float]

LAT_KEYS = {
    "lat",
    "latitude",
    "latitud",
    "y",
    "coordy",
    "coord_y",
    "posicaoy",
    "posicaolatitude",
    "rasevelatitude",
    "rasevelat",
    "rasreflatitude",
    "raslat",
}
LON_KEYS = {
    "lng",
    "lon",
    "long",
    "longitude",
    "longitud",
    "x",
    "coordx",
    "coord_x",
    "posicaox",
    "posicaolongitude",
    "rasevelongitude",
    "rasevelng",
    "rasreflongitude",
    "raslng",
    "raslon",
}
TIME_KEYS = {
    "data",
    "hora",
    "datetime",
    "timestamp",
    "datahora",
    "data_hora",
    "dhposicao",
    "dh_posicao",
    "datacoleta",
    "dataevento",
    "dtposicao",
    "dtgps",
    "dtservidor",
    "dtgpsformatado",
    "rasevedataservidor",
    "rasevedatagps",
    "rasevedataenviado",
    "rasdatahora",
}
SPEED_KEYS = {"velocidade", "speed", "vel", "velocity", "kmh", "km_h"}
ADDR_KEYS = {"endereco", "endereço", "address", "logradouro", "rua", "local"}
VEIC_KEYS = {
    "veiculo",
    "veículo",
    "prefixo",
    "placa",
    "frota",
    "identificador",
    "codigo",
    "codigoveiculo",
    "idveiculo",
    "nome",
    "descricao",
    "rasveiplaca",
    "rasveiveiculo",
}


def _norm(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if abs(number) > 180:
            number = number / 1_000_000
    else:
        texto = str(value).strip().replace(",", ".")
        if not re.fullmatch(r"-?\d+(\.\d+)?", texto):
            return None
        number = float(texto)
        if abs(number) > 180:
            number = number / 1_000_000
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _is_lat(value: float) -> bool:
    return -90.0 <= value <= 90.0 and abs(value) > 0.5


def _is_lon(value: float) -> bool:
    return -180.0 <= value <= 180.0 and abs(value) > 0.5


def _pair_from_list(item: Any) -> tuple[float, float] | None:
    if not isinstance(item, (list, tuple)) or len(item) < 2:
        return None
    a = _as_float(item[0])
    b = _as_float(item[1])
    if a is None or b is None:
        return None
    if _is_lat(a) and _is_lon(b):
        return a, b
    if _is_lat(b) and _is_lon(a):
        return b, a
    return None


def _first_key(obj: dict[str, Any], candidates: set[str]) -> Any:
    for key, value in obj.items():
        if _norm(str(key)) in candidates:
            return value
    return None


def extract_points(payload: Any, veiculo: str = "", fonte: str = "") -> list[dict[str, Any]]:
    pontos: list[dict[str, Any]] = []
    _walk(payload, pontos, veiculo=veiculo, fonte=fonte)
    return _dedupe(pontos)


def _walk(node: Any, acc: list[dict[str, Any]], veiculo: str, fonte: str) -> None:
    if isinstance(node, dict):
        herdado = str(_first_key(node, VEIC_KEYS) or veiculo or "").strip()
        point = _point_from_dict(node, herdado, fonte)
        if point:
            acc.append(point)
        for value in node.values():
            _walk(value, acc, herdado, fonte)
        return

    if isinstance(node, list):
        pair = _pair_from_list(node)
        if pair and all(not isinstance(item, (dict, list)) for item in node[:2]):
            acc.append(
                {
                    "veiculo": veiculo,
                    "timestamp": "",
                    "latitude": pair[0],
                    "longitude": pair[1],
                    "velocidade": "",
                    "endereco": "",
                    "fonte": fonte,
                }
            )
            return
        for item in node:
            _walk(item, acc, veiculo, fonte)


def _point_from_dict(obj: dict[str, Any], veiculo: str, fonte: str) -> dict[str, Any] | None:
    lat = _as_float(_first_key(obj, LAT_KEYS))
    lon = _as_float(_first_key(obj, LON_KEYS))
    if lat is None or lon is None:
        coords = obj.get("coordinates") or obj.get("coordenadas")
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            pair = _pair_from_list(coords)
            if pair:
                lat, lon = pair
    if lat is None or lon is None or not _is_lat(lat) or not _is_lon(lon):
        return None

    veiculo_local = _first_key(obj, VEIC_KEYS)
    return {
        "veiculo": str(veiculo_local or veiculo or "").strip(),
        "timestamp": str(_first_key(obj, TIME_KEYS) or "").strip(),
        "latitude": lat,
        "longitude": lon,
        "velocidade": _first_key(obj, SPEED_KEYS) or "",
        "endereco": str(_first_key(obj, ADDR_KEYS) or "").strip(),
        "fonte": fonte,
    }


def _dedupe(pontos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    unique: list[dict[str, Any]] = []
    for ponto in pontos:
        key = (
            ponto.get("veiculo"),
            ponto.get("timestamp"),
            round(float(ponto["latitude"]), 6),
            round(float(ponto["longitude"]), 6),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(ponto)
    return unique


def haversine_m(a: Ponto, b: Ponto) -> float:
    r = 6371000
    lat1, lon1, lat2, lon2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(min(1.0, h)))


def _xy(ponto: Ponto, origem: Ponto) -> Ponto:
    lat0 = math.radians(origem[0])
    return ((ponto[1] - origem[1]) * 111_320 * math.cos(lat0), (ponto[0] - origem[0]) * 110_540)


def _dist_ponto_seg(p: Ponto, a: Ponto, b: Ponto) -> float:
    if a == b:
        return haversine_m(p, a)
    ax, ay = 0.0, 0.0
    bx, by = _xy(b, a)
    px, py = _xy(p, a)
    ab2 = bx * bx + by * by
    t = 0.0 if ab2 == 0 else max(0.0, min(1.0, (px * bx + py * by) / ab2))
    qx, qy = ax + t * bx, ay + t * by
    return math.hypot(px - qx, py - qy)


def _rdp(pontos: list[Ponto], tolerancia_m: float) -> list[Ponto]:
    if len(pontos) < 3:
        return pontos
    inicio, fim = pontos[0], pontos[-1]
    idx = 0
    dist_max = -1.0
    for i in range(1, len(pontos) - 1):
        dist = _dist_ponto_seg(pontos[i], inicio, fim)
        if dist > dist_max:
            idx = i
            dist_max = dist
    if dist_max > tolerancia_m:
        esquerda = _rdp(pontos[: idx + 1], tolerancia_m)
        direita = _rdp(pontos[idx:], tolerancia_m)
        return esquerda[:-1] + direita
    return [inicio, fim]


def _proximo_na_via(ponto: Ponto, via: list[list[float]]) -> tuple[float, float, Ponto] | None:
    """Distância em metros, posição ao longo da via e o ponto projetado."""
    if len(via) < 2:
        return None
    melhor: tuple[float, float, Ponto] | None = None
    percorrido = 0.0
    for indice in range(len(via) - 1):
        a = (float(via[indice][0]), float(via[indice][1]))
        b = (float(via[indice + 1][0]), float(via[indice + 1][1]))
        comp = haversine_m(a, b)
        if comp < 0.5:
            continue
        ax, ay = _xy(a, a)
        bx, by = _xy(b, a)
        px, py = _xy(ponto, a)
        abx, aby = bx - ax, by - ay
        denom = abx * abx + aby * aby
        t = 0.0 if denom <= 0 else max(0.0, min(1.0, ((px - ax) * abx + (py - ay) * aby) / denom))
        proj = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        dist = haversine_m(ponto, proj)
        along = percorrido + comp * t
        if melhor is None or dist < melhor[0]:
            melhor = (dist, along, proj)
        percorrido += comp
    return melhor


def _fatia_via(via: list[list[float]], inicio_m: float, fim_m: float) -> list[Ponto]:
    if fim_m < inicio_m:
        inicio_m, fim_m = fim_m, inicio_m
        inverter = True
    else:
        inverter = False
    saida: list[Ponto] = []
    percorrido = 0.0
    for indice in range(len(via) - 1):
        a = (float(via[indice][0]), float(via[indice][1]))
        b = (float(via[indice + 1][0]), float(via[indice + 1][1]))
        comp = haversine_m(a, b)
        if comp < 0.5:
            continue
        prox = percorrido + comp
        if prox < inicio_m:
            percorrido = prox
            continue
        if not saida:
            t = 0.0 if comp <= 0 else max(0.0, min(1.0, (inicio_m - percorrido) / comp))
            saida.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
        if prox <= fim_m:
            saida.append(b)
        else:
            t = 0.0 if comp <= 0 else max(0.0, min(1.0, (fim_m - percorrido) / comp))
            saida.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            break
        percorrido = prox
    if inverter:
        saida.reverse()
    return saida


def _no_via(ponto: Ponto) -> tuple[int, int]:
    return (int(round(float(ponto[0]) * 1_000_000)), int(round(float(ponto[1]) * 1_000_000)))


def _alongar(destino: list[Ponto], extra: list[Ponto]) -> None:
    if not extra:
        return
    if destino and haversine_m(destino[-1], extra[0]) < 4:
        destino.extend(extra[1:])
    else:
        destino.extend(extra)


_REDE_CACHE: dict[tuple, list[list[list[float]]]] = {}


def _rede_viaria(ruas: list[list[list[float]]]) -> list[list[list[float]]]:
    """Une as ruas oficiais nos cruzamentos para o trajeto não quebrar no meio da quadra."""
    from shapely.geometry import LineString
    from shapely.ops import linemerge, snap, unary_union

    if not ruas:
        return []
    chave = (
        len(ruas),
        round(float(ruas[0][0][0]), 5),
        round(float(ruas[0][0][1]), 5),
        round(float(ruas[-1][-1][0]), 5),
        round(float(ruas[-1][-1][1]), 5),
    )
    pronta = _REDE_CACHE.get(chave)
    if pronta is not None:
        return pronta
    geoms = []
    for via in ruas:
        coords = [(float(p[1]), float(p[0])) for p in via if len(p) >= 2]
        if len(coords) < 2 or coords[0] == coords[-1]:
            continue
        try:
            linha = LineString(coords)
        except (TypeError, ValueError):
            continue
        if not linha.is_empty and linha.length > 0:
            geoms.append(linha)
    if not geoms:
        return []
    base = unary_union(geoms)
    grudado = unary_union([snap(geom, base, 0.0002) for geom in geoms])
    if grudado.geom_type == "MultiLineString":
        try:
            fundido = linemerge(grudado)
        except ValueError:
            fundido = grudado
    else:
        fundido = grudado
    partes = [fundido] if fundido.geom_type == "LineString" else list(getattr(fundido, "geoms", []) or [])
    saida: list[list[list[float]]] = []
    for geom in partes:
        linhas = [geom] if geom.geom_type == "LineString" else list(getattr(geom, "geoms", []) or [])
        for linha in linhas:
            if linha.geom_type != "LineString" or len(linha.coords) < 2:
                continue
            saida.append([[lat, lon] for lon, lat in linha.coords])
    _REDE_CACHE[chave] = saida
    return saida


def _ligar_na_rede(
    ruas: list[list[list[float]]],
    comps: list[float],
    adj: dict[tuple[int, int], list[tuple[tuple[int, int], int, bool]]],
    a_idx: int,
    a_along: float,
    b_idx: int,
    b_along: float,
    limite: float,
) -> list[Ponto] | None:
    """Caminho pela rua oficial entre dois pulsos, se a distância cabe no tempo."""
    import heapq

    if limite < 8:
        return None
    if a_idx == b_idx:
        if abs(b_along - a_along) > limite:
            return None
        fatia = _fatia_via(ruas[a_idx], a_along, b_along)
        return fatia if len(fatia) >= 2 else None

    via_a = ruas[a_idx]
    via_b = ruas[b_idx]
    comp_a = comps[a_idx]
    comp_b = comps[b_idx]
    no_a0, no_a1 = _no_via((via_a[0][0], via_a[0][1])), _no_via((via_a[-1][0], via_a[-1][1]))
    no_b0, no_b1 = _no_via((via_b[0][0], via_b[0][1])), _no_via((via_b[-1][0], via_b[-1][1]))
    inicio: dict[tuple[int, int], list[Ponto]] = {}
    pai: dict[tuple[int, int], tuple[tuple[int, int], int, bool]] = {}
    melhor: dict[tuple[int, int], float] = {}
    heap: list[tuple[float, tuple[int, int]]] = []

    def sair(custo: float, no: tuple[int, int], geom: list[Ponto]) -> None:
        if custo > limite or custo >= melhor.get(no, limite + 1):
            return
        melhor[no] = custo
        inicio[no] = geom
        heapq.heappush(heap, (custo, no))

    sair(a_along, no_a0, _fatia_via(via_a, a_along, 0))
    sair(max(0.0, comp_a - a_along), no_a1, _fatia_via(via_a, a_along, comp_a))

    def geometria_ate(no: tuple[int, int]) -> list[Ponto] | None:
        passos: list[tuple[int, bool]] = []
        atual = no
        vistos: set[tuple[int, int]] = set()
        while atual not in inicio and atual in pai and atual not in vistos:
            vistos.add(atual)
            prev, edge, forward = pai[atual]
            passos.append((edge, forward))
            atual = prev
        if atual not in inicio:
            return None
        geom = list(inicio[atual])
        for edge, forward in reversed(passos):
            via = ruas[edge] if forward else list(reversed(ruas[edge]))
            _alongar(geom, [(float(p[0]), float(p[1])) for p in via])
        return geom

    melhor_geom: list[Ponto] | None = None
    melhor_total = limite + 1
    assentados: set[tuple[int, int]] = set()
    while heap:
        custo, no = heapq.heappop(heap)
        if no in assentados or custo > melhor.get(no, custo):
            continue
        assentados.add(no)
        if custo >= melhor_total:
            continue
        geom = geometria_ate(no)
        if geom is None:
            continue
        chegadas = []
        if no == no_b0:
            chegadas.append((b_along, _fatia_via(via_b, 0, b_along)))
        if no == no_b1:
            chegadas.append((max(0.0, comp_b - b_along), _fatia_via(via_b, comp_b, b_along)))
        for extra, fatia in chegadas:
            total = custo + extra
            if total <= limite and total < melhor_total and len(fatia) >= 1:
                copia = list(geom)
                _alongar(copia, fatia)
                if len(copia) >= 2:
                    melhor_total = total
                    melhor_geom = copia
        for outro, edge, forward in adj.get(no, []):
            if edge in {a_idx, b_idx}:
                continue
            custo2 = custo + comps[edge]
            if custo2 > limite or custo2 >= melhor_total or custo2 >= melhor.get(outro, limite + 1):
                continue
            melhor[outro] = custo2
            pai[outro] = (no, edge, forward)
            inicio.pop(outro, None)
            heapq.heappush(heap, (custo2, outro))
    return melhor_geom


def trajetoria_uniforme(
    pontos: list[dict[str, Any]] | None,
    vias: list[list[list[float]]] | None = None,
    *,
    dia: date | None = None,
    salto_max_m: float = 120.0,
    cola_m: float = 12.0,
    rua_m: float = 40.0,
    teto_m: float = 1200.0,
) -> list[list[list[float]]]:
    """Liga os pulsos pela hora e, quando o pulso está na via oficial, segue o meio da rua."""
    marcas: list[tuple[datetime, Ponto]] = []
    referencia = dia or date.today()
    for ponto in pontos or []:
        try:
            coord = (float(ponto["latitude"]), float(ponto["longitude"]))
        except (KeyError, TypeError, ValueError):
            continue
        momento = _datetime_do_timestamp(str(ponto.get("timestamp") or ""), referencia)
        marcas.append((momento or datetime.min, coord))
    marcas.sort(key=lambda item: item[0])
    seguidos: list[tuple[datetime, Ponto]] = []
    for momento, coord in marcas:
        if seguidos and haversine_m(seguidos[-1][1], coord) < cola_m:
            continue
        seguidos.append((momento, coord))
    if len(seguidos) < 2:
        return []

    ruas = [via for via in (vias or []) if via and len(via) >= 2]
    if not ruas:
        return _quebrar_salto([coord for _momento, coord in seguidos], salto_max_m)

    ruas = _rede_viaria(ruas) or ruas
    caixas = []
    comps: list[float] = []
    adj: dict[tuple[int, int], list[tuple[tuple[int, int], int, bool]]] = {}
    for via in ruas:
        lats = [float(p[0]) for p in via]
        lons = [float(p[1]) for p in via]
        caixas.append((min(lats) - 0.0005, max(lats) + 0.0005, min(lons) - 0.0005, max(lons) + 0.0005))
        comp = 0.0
        for i in range(len(via) - 1):
            comp += haversine_m((float(via[i][0]), float(via[i][1])), (float(via[i + 1][0]), float(via[i + 1][1])))
        comps.append(comp)
        if comp < 1:
            continue
        no0 = _no_via((float(via[0][0]), float(via[0][1])))
        no1 = _no_via((float(via[-1][0]), float(via[-1][1])))
        indice = len(comps) - 1
        adj.setdefault(no0, []).append((no1, indice, True))
        adj.setdefault(no1, []).append((no0, indice, False))

    grade: dict[tuple[int, int], list[int]] = {}
    for indice, via in enumerate(ruas):
        vistos = set()
        for ponto in via:
            celula = (int(float(ponto[0]) * 900), int(float(ponto[1]) * 900))
            if celula in vistos:
                continue
            vistos.add(celula)
            grade.setdefault(celula, []).append(indice)

    def projetar_rede(coord: Ponto, preferida: int | None) -> tuple[int, float, Ponto] | None:
        celula = (int(coord[0] * 900), int(coord[1] * 900))
        perto: list[int] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                perto.extend(grade.get((celula[0] + dx, celula[1] + dy), ()))
        if preferida is not None:
            perto.append(preferida)
        melhor_local: tuple[float, int, float, Ponto] | None = None
        for indice in dict.fromkeys(perto):
            via = ruas[indice]
            sul, norte, oeste, leste = caixas[indice]
            if not (sul <= coord[0] <= norte and oeste <= coord[1] <= leste):
                continue
            achado = _proximo_na_via(coord, via)
            if achado is None or achado[0] > rua_m:
                continue
            dist, along, proj = achado
            if melhor_local is None or dist < melhor_local[0]:
                melhor_local = (dist, indice, along, proj)
        if melhor_local is None:
            return None
        if preferida is not None and preferida != melhor_local[1]:
            achado = _proximo_na_via(coord, ruas[preferida])
            if achado is not None and achado[0] <= rua_m and achado[0] <= melhor_local[0] + 12:
                return preferida, achado[1], achado[2]
        return melhor_local[1], melhor_local[2], melhor_local[3]

    trechos: list[list[Ponto]] = []
    atual: list[Ponto] = []
    anterior: tuple[int, float, Ponto, datetime] | None = None

    def quebrar() -> None:
        nonlocal atual
        if len(atual) >= 2:
            trechos.append(atual)
        atual = []

    for momento, coord in seguidos:
        proj = projetar_rede(coord, anterior[0] if anterior else None)
        if proj is None:
            quebrar()
            anterior = None
            continue
        indice, along, ponto = proj
        if anterior is None:
            anterior = (indice, along, ponto, momento)
            continue
        reta = haversine_m(anterior[2], ponto)
        dt = abs((momento - anterior[3]).total_seconds())
        pela_forma = max(reta * 2.4, reta + 140.0)
        pela_hora = (42 / 3.6) * dt + 50 if dt > 0 else 80.0
        limite = min(teto_m, pela_forma, pela_hora)
        fatia = _ligar_na_rede(ruas, comps, adj, anterior[0], anterior[1], indice, along, limite)
        if fatia is None and reta <= 40:
            fatia = [anterior[2], ponto]
        if fatia and len(fatia) >= 2:
            if atual:
                _alongar(atual, fatia)
            else:
                atual = list(fatia)
        else:
            quebrar()
        anterior = (indice, along, ponto, momento)
    quebrar()
    if not trechos:
        return _quebrar_salto([coord for _momento, coord in seguidos], salto_max_m)
    saida = []
    for trecho in trechos:
        fino = _rdp(trecho, 6)
        if len(fino) >= 2:
            saida.append([[lat, lon] for lat, lon in fino])
    return saida


def _quebrar_salto(pontos: list[Ponto], salto_max_m: float) -> list[list[list[float]]]:
    trechos: list[list[Ponto]] = []
    atual = [pontos[0]]
    for coord in pontos[1:]:
        if haversine_m(atual[-1], coord) > salto_max_m:
            if len(atual) >= 2:
                trechos.append(atual)
            atual = [coord]
            continue
        atual.append(coord)
    if len(atual) >= 2:
        trechos.append(atual)
    return [[[lat, lon] for lat, lon in _rdp(trecho, 8)] for trecho in trechos if len(trecho) >= 2]


def ordem_do_pulso(ponto: dict[str, Any]) -> tuple[int, datetime | str]:
    """Ordena pelo instante do pulso. O texto DD/MM/AAAA inverte na virada do mês."""
    texto = str(ponto.get("timestamp") or "")
    marca = _datetime_do_timestamp(texto, date(2000, 1, 1))
    if marca is None:
        return (1, texto)
    return (0, marca)


class _Snap:
    def __init__(self, aresta: int, along: float, dist: float) -> None:
        self.aresta = aresta
        self.along = along
        self.dist = dist


class Malha:
    """Rede viária noded. O custo prefere o sentido oficial; o limite usa metros reais."""

    def __init__(self) -> None:
        self.geoms: list[Any] = []
        self.comp: list[float] = []
        self.custo_ida: list[float] = []
        self.custo_volta: list[float] = []
        self.no0: list[tuple[int, int]] = []
        self.no1: list[tuple[int, int]] = []
        self.adj: dict[tuple[int, int], list[tuple[tuple[int, int], int, bool]]] = {}
        self._arvore: Any = None

    @classmethod
    def de_wgs(
        cls,
        linhas: list[list[list[float]]],
        sentidos: list[int] | None = None,
    ) -> Malha:
        from .mapa import TO_UTM
        from shapely.geometry import LineString

        pares: list[tuple[Any, int]] = []
        for indice, linha in enumerate(linhas or []):
            coords = []
            for ponto in linha:
                if len(ponto) < 2:
                    continue
                x, y = TO_UTM.transform(float(ponto[1]), float(ponto[0]))
                coords.append((x, y))
            if len(coords) < 2:
                continue
            geom = LineString(coords)
            if geom.length < 1:
                continue
            sentido = 0 if not sentidos else int(sentidos[indice])
            pares.append((geom, sentido))
        return cls._montar(pares)

    @classmethod
    def oficial(cls) -> Malha:
        from .mapa import linhas_oficiais

        return cls._montar(linhas_oficiais())

    @classmethod
    def _montar(cls, originais: list[tuple[Any, int]]) -> Malha:
        from shapely.ops import unary_union
        from shapely.strtree import STRtree

        malha = cls()
        geoms = [geom for geom, _sentido in originais if geom is not None and not geom.is_empty]
        sentidos = [sentido for geom, sentido in originais if geom is not None and not geom.is_empty]
        if not geoms:
            return malha
        unido = unary_union(geoms)
        partes = _partes_de_linha(unido)
        arvore = STRtree(geoms)
        for parte in partes:
            if parte.geom_type != "LineString" or len(parte.coords) < 2 or parte.length < 0.8:
                continue
            meio = parte.interpolate(0.5, normalized=True)
            indice = int(arvore.nearest(meio))
            origem = geoms[indice]
            sentido = sentidos[indice] if origem.distance(meio) <= 3 else 0
            if sentido and _trecho_invertido(parte, origem, meio):
                sentido = -sentido
            no_a = _no_utm(parte.coords[0][0], parte.coords[0][1])
            no_b = _no_utm(parte.coords[-1][0], parte.coords[-1][1])
            if no_a == no_b:
                continue
            comp = float(parte.length)
            malha.geoms.append(parte)
            malha.comp.append(comp)
            malha.custo_ida.append(comp if sentido != -1 else comp * 1.85)
            malha.custo_volta.append(comp if sentido != 1 else comp * 1.85)
            malha.no0.append(no_a)
            malha.no1.append(no_b)
            ordem = len(malha.geoms) - 1
            malha.adj.setdefault(no_a, []).append((no_b, ordem, True))
            malha.adj.setdefault(no_b, []).append((no_a, ordem, False))
        if malha.geoms:
            malha._arvore = STRtree(malha.geoms)
        return malha

    def projetar(
        self,
        coord: Ponto,
        raio_m: float,
        preferida: int | None = None,
        rumo: tuple[float, float] | None = None,
    ) -> _Snap | None:
        from shapely.geometry import Point

        from .mapa import TO_UTM

        if self._arvore is None:
            return None
        x, y = TO_UTM.transform(float(coord[1]), float(coord[0]))
        ponto = Point(x, y)
        melhor: tuple[float, int, float, float] | None = None
        for indice in self._arvore.query(ponto.buffer(raio_m)):
            aresta = int(indice)
            geom = self.geoms[aresta]
            dist = float(geom.distance(ponto))
            if dist > raio_m:
                continue
            along = float(geom.project(ponto))
            penalidade = _penalidade_rumo(geom, along, rumo)
            nota = dist + penalidade
            if preferida is not None and preferida == aresta and penalidade == 0:
                nota -= 12.0
            if melhor is None or nota < melhor[0]:
                melhor = (nota, aresta, along, dist)
        if melhor is None:
            return None
        return _Snap(melhor[1], melhor[2], melhor[3])

    def caminho(self, origem: _Snap, destino: _Snap, limite_m: float) -> tuple[list[Ponto], float] | None:
        import heapq

        if limite_m < 8 or not self.geoms:
            return None
        if origem.aresta == destino.aresta:
            metros = abs(destino.along - origem.along)
            if metros < 0.8 or metros > limite_m:
                return None
            geom = self._fatia_wgs(origem.aresta, origem.along, destino.along)
            return (geom, metros) if len(geom) >= 2 else None

        no_a0, no_a1 = self.no0[origem.aresta], self.no1[origem.aresta]
        inicio: dict[tuple[int, int], list[Ponto]] = {}
        pai: dict[tuple[int, int], tuple[tuple[int, int], int, bool]] = {}
        melhor: dict[tuple[int, int], float] = {}
        heap: list[tuple[float, float, tuple[int, int]]] = []

        def sair(custo: float, metros: float, no: tuple[int, int], geom: list[Ponto]) -> None:
            if metros > limite_m or custo >= melhor.get(no, 1e18):
                return
            melhor[no] = custo
            inicio[no] = geom
            heapq.heappush(heap, (custo, metros, no))

        for qual, no in ((0, no_a0), (1, no_a1)):
            custo, metros, geom = self._ate_no(origem, qual)
            sair(custo, metros, no, geom)

        melhor_geom: list[Ponto] | None = None
        melhor_custo = 1e18
        melhor_metros = 0.0
        vistos: set[tuple[int, int]] = set()
        while heap:
            custo, metros, no = heapq.heappop(heap)
            if no in vistos or custo > melhor.get(no, custo) + 1e-6:
                continue
            vistos.add(no)
            if melhor_geom is not None and custo >= melhor_custo:
                continue
            geom = _geometria_dos_nos(self, no, pai, inicio)
            if geom is None:
                continue
            for extra, fatia in self._chegadas(no, destino):
                total_m = metros + extra
                total_c = custo + extra
                if total_m <= limite_m and total_c < melhor_custo and len(fatia) >= 1:
                    copia = list(geom)
                    _alongar(copia, fatia)
                    if len(copia) >= 2:
                        melhor_custo = total_c
                        melhor_geom = copia
                        melhor_metros = total_m
            for outro, aresta, forward in self.adj.get(no, ()):
                if aresta in {origem.aresta, destino.aresta}:
                    continue
                add_c = self.custo_ida[aresta] if forward else self.custo_volta[aresta]
                add_m = self.comp[aresta]
                custo2 = custo + add_c
                metros2 = metros + add_m
                if metros2 > limite_m or custo2 >= melhor_custo or custo2 >= melhor.get(outro, 1e18):
                    continue
                melhor[outro] = custo2
                pai[outro] = (no, aresta, forward)
                inicio.pop(outro, None)
                heapq.heappush(heap, (custo2, metros2, outro))
        if melhor_geom is None:
            return None
        return melhor_geom, melhor_metros

    def _ate_no(self, snap: _Snap, qual: int) -> tuple[float, float, list[Ponto]]:
        comp = self.comp[snap.aresta]
        if qual == 0:
            metros = snap.along
            fator = (self.custo_volta[snap.aresta] / comp) if comp else 1.0
            geom = self._fatia_wgs(snap.aresta, snap.along, 0.0)
        else:
            metros = max(0.0, comp - snap.along)
            fator = (self.custo_ida[snap.aresta] / comp) if comp else 1.0
            geom = self._fatia_wgs(snap.aresta, snap.along, comp)
        return metros * fator, metros, geom

    def _chegadas(self, no: tuple[int, int], destino: _Snap) -> list[tuple[float, list[Ponto]]]:
        comp = self.comp[destino.aresta]
        saida = []
        if no == self.no0[destino.aresta]:
            saida.append((destino.along, self._fatia_wgs(destino.aresta, 0.0, destino.along)))
        if no == self.no1[destino.aresta]:
            saida.append((max(0.0, comp - destino.along), self._fatia_wgs(destino.aresta, comp, destino.along)))
        return saida

    def _fatia_wgs(self, aresta: int, inicio_m: float, fim_m: float) -> list[Ponto]:
        from shapely.ops import substring

        from .mapa import TO_WGS

        if abs(fim_m - inicio_m) < 0.4:
            return []
        inverter = fim_m < inicio_m
        a, b = (fim_m, inicio_m) if inverter else (inicio_m, fim_m)
        trecho = substring(self.geoms[aresta], max(0.0, a), min(self.comp[aresta], b))
        coords = [(float(x), float(y)) for x, y in trecho.coords]
        if inverter:
            coords.reverse()
        saida: list[Ponto] = []
        for x, y in coords:
            lon, lat = TO_WGS.transform(x, y)
            saida.append((lat, lon))
        return saida


def _partes_de_linha(geom: Any) -> list[Any]:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom]
    if geom.geom_type == "MultiLineString":
        return list(geom.geoms)
    if geom.geom_type == "GeometryCollection":
        saida: list[Any] = []
        for parte in geom.geoms:
            saida.extend(_partes_de_linha(parte))
        return saida
    return []


def _no_utm(x: float, y: float) -> tuple[int, int]:
    return (int(round(float(x) / 2.0) * 2), int(round(float(y) / 2.0) * 2))


def _trecho_invertido(parte: Any, origem: Any, meio: Any) -> bool:
    along = float(origem.project(meio))
    a = origem.interpolate(max(0.0, along - 4))
    b = origem.interpolate(min(origem.length, along + 4))
    palong = float(parte.project(meio))
    pa = parte.interpolate(max(0.0, palong - 4))
    pb = parte.interpolate(min(parte.length, palong + 4))
    return (b.x - a.x) * (pb.x - pa.x) + (b.y - a.y) * (pb.y - pa.y) < 0


def _penalidade_rumo(geom: Any, along: float, rumo: tuple[float, float] | None) -> float:
    if not rumo:
        return 0.0
    rx, ry = rumo
    norma = math.hypot(rx, ry)
    if norma < 8:
        return 0.0
    a = geom.interpolate(max(0.0, along - 8))
    b = geom.interpolate(min(geom.length, along + 8))
    vx, vy = b.x - a.x, b.y - a.y
    norma_via = math.hypot(vx, vy)
    if norma_via < 1:
        return 0.0
    cosseno = abs((vx * rx + vy * ry) / (norma_via * norma))
    if cosseno >= 0.55:
        return 0.0
    return 22.0


def _geometria_dos_nos(
    malha: Malha,
    no: tuple[int, int],
    pai: dict[tuple[int, int], tuple[tuple[int, int], int, bool]],
    inicio: dict[tuple[int, int], list[Ponto]],
) -> list[Ponto] | None:
    passos: list[tuple[int, bool]] = []
    atual = no
    vistos: set[tuple[int, int]] = set()
    while atual not in inicio and atual in pai and atual not in vistos:
        vistos.add(atual)
        prev, aresta, forward = pai[atual]
        passos.append((aresta, forward))
        atual = prev
    if atual not in inicio:
        return None
    geom = list(inicio[atual])
    for aresta, forward in reversed(passos):
        if forward:
            fatia = malha._fatia_wgs(aresta, 0.0, malha.comp[aresta])
        else:
            fatia = malha._fatia_wgs(aresta, malha.comp[aresta], 0.0)
        _alongar(geom, fatia)
    return geom


_MALHA: Malha | None = None
_RAIO_CASAMENTO_M = 60.0
_CURTO_M = 42.0
_TETO_CASAMENTO_M = 6500.0
_SILENCIO_S = 8 * 60


def malha_oficial() -> Malha:
    global _MALHA
    if _MALHA is None:
        _MALHA = Malha.oficial()
    return _MALHA


def _rumo_utm(origem: Ponto, destino: Ponto) -> tuple[float, float] | None:
    from .mapa import TO_UTM

    ax, ay = TO_UTM.transform(float(origem[1]), float(origem[0]))
    bx, by = TO_UTM.transform(float(destino[1]), float(destino[0]))
    vetor = (bx - ax, by - ay)
    if math.hypot(vetor[0], vetor[1]) < 8:
        return None
    return vetor


def _limite_do_salto(dist: float, dt: float) -> float:
    pela_forma = max(dist * 3.8, dist + 340.0)
    pela_hora = (52 / 3.6) * dt + 200.0 if dt > 0 else pela_forma
    return min(_TETO_CASAMENTO_M, max(pela_forma, pela_hora))


def _pulso_impossivel(dist: float, dt: float) -> bool:
    if dist < 180:
        return False
    if dt <= 0:
        return True
    return dist > (85 / 3.6) * dt + 90


def _caminho_plausivel(dist: float, comp: float, dt: float) -> bool:
    if comp > _TETO_CASAMENTO_M or dt >= _SILENCIO_S:
        return False
    if dt > 0:
        teto_tempo = (52 / 3.6) * dt + 200.0
        if comp > teto_tempo and comp > dist + 30:
            return False
    return comp <= max(dist * 3.8, dist + 340.0)


def _trecho_confirmado(dist: float, comp: float, da: float, db: float, dt: float) -> bool:
    if da > 40 or db > 40 or comp > 220:
        return False
    if comp > max(dist * 1.7, dist + 70):
        return False
    if dt > 0 and comp > (40 / 3.6) * dt + 160:
        return False
    return True


def _guardar_trecho(atual: list[Ponto], destino: list[list[list[float]]]) -> None:
    if len(atual) < 2:
        return
    fino = _rdp(atual, 6)
    if len(fino) >= 2:
        destino.append([[lat, lon] for lat, lon in fino])


def casar_tracos(
    pontos: list[dict[str, Any]],
    dia: date | None = None,
    vias: list[list[list[float]]] | None = None,
    malha: Malha | None = None,
) -> dict[str, Any]:
    """Casa cada pulso com a via oficial e liga o próximo pela hora.

    A linha desenhada não entra na porcentagem da rota. Rua feita continua
    sendo só a que tem pulso a até 45 m. Sem via compatível, o salto fica aberto.
    """
    ordenados = sorted(pontos or [], key=ordem_do_pulso)
    referencia = dia or date.today()
    marcas: list[tuple[datetime, Ponto]] = []
    for ponto in ordenados:
        lat = ponto.get("latitude")
        lon = ponto.get("longitude")
        if lat is None or lon is None:
            continue
        momento = _datetime_do_timestamp(str(ponto.get("timestamp") or ""), referencia)
        marcas.append((momento or datetime.min, (float(lat), float(lon))))
    seguidos: list[tuple[datetime, Ponto]] = []
    for momento, coord in marcas:
        if seguidos and haversine_m(seguidos[-1][1], coord) < 8:
            continue
        seguidos.append((momento, coord))
    original = _quebrar_salto([coord for _momento, coord in seguidos], _CURTO_M) if len(seguidos) >= 2 else []
    vazio = {
        "confirmado": [],
        "estimado": [],
        "original": original,
        "resumo": {"confirmado": 0, "estimado": 0, "indeterminado": 0},
    }
    if len(seguidos) < 2:
        return vazio
    if malha is None:
        malha = Malha.de_wgs(vias) if vias is not None else malha_oficial()
    if not malha.geoms:
        return vazio

    confirmado: list[list[list[float]]] = []
    estimado: list[list[list[float]]] = []
    contagem = {"confirmado": 0, "estimado": 0, "indeterminado": 0}
    classe = ""
    atual: list[Ponto] = []
    anterior = seguidos[0]
    snap_anterior = malha.projetar(anterior[1], _RAIO_CASAMENTO_M)

    def fechar() -> None:
        nonlocal classe, atual
        if classe == "confirmado":
            _guardar_trecho(atual, confirmado)
        elif classe == "estimado":
            _guardar_trecho(atual, estimado)
        classe = ""
        atual = []

    def usar(nova: str, geom: list[Ponto]) -> None:
        nonlocal classe, atual
        if classe != nova:
            fechar()
            classe = nova
        if atual:
            _alongar(atual, geom)
        else:
            atual = list(geom)
        contagem[nova] += 1

    for momento, coord in seguidos[1:]:
        dist = haversine_m(anterior[1], coord)
        dt = abs((momento - anterior[0]).total_seconds()) if momento and anterior[0] else 0.0
        rumo = _rumo_utm(anterior[1], coord)
        snap = malha.projetar(
            coord,
            _RAIO_CASAMENTO_M,
            preferida=snap_anterior.aresta if snap_anterior else None,
            rumo=rumo,
        )
        if _pulso_impossivel(dist, dt):
            fechar()
            contagem["indeterminado"] += 1
        elif snap_anterior is None or snap is None:
            if dist <= _CURTO_M:
                usar("confirmado", [anterior[1], coord])
            else:
                fechar()
                contagem["indeterminado"] += 1
        else:
            achado = malha.caminho(snap_anterior, snap, _limite_do_salto(dist, dt))
            if achado is None:
                if dist <= _CURTO_M:
                    usar("confirmado", [anterior[1], coord])
                else:
                    fechar()
                    contagem["indeterminado"] += 1
            else:
                geom, comp = achado
                if _trecho_confirmado(dist, comp, snap_anterior.dist, snap.dist, dt):
                    usar("confirmado", geom)
                elif _caminho_plausivel(dist, comp, dt):
                    usar("estimado", geom)
                else:
                    fechar()
                    contagem["indeterminado"] += 1
        anterior = (momento, coord)
        snap_anterior = snap
    fechar()
    return {
        "confirmado": confirmado,
        "estimado": estimado,
        "original": original,
        "resumo": contagem,
    }


def tracos_entre_pulsos(
    pontos: list[dict[str, Any]],
    salto_max_m: float = 160,
    dia: date | None = None,
    vias: list[list[list[float]]] | None = None,
) -> list[list[list[float]]]:
    """Trajeto casado com a via. O argumento antigo de salto não volta a cortar a quadra."""
    del salto_max_m
    casado = casar_tracos(pontos, dia=dia, vias=vias)
    return list(casado["confirmado"]) + list(casado["estimado"])


def tracos_na_ordem(pontos: list[dict[str, Any]], salto_max_m: float = 160) -> list[list[list[float]]]:
    """Liga os pulsos na ordem do horário. Um salto acima de 160 m abre outro trecho."""
    ordenados = sorted(pontos or [], key=ordem_do_pulso)
    pares: list[Ponto] = []
    for ponto in ordenados:
        lat = ponto.get("latitude")
        lon = ponto.get("longitude")
        if lat is None or lon is None:
            continue
        pares.append((float(lat), float(lon)))
    if len(pares) < 2:
        return []
    return _quebrar_salto(pares, salto_max_m)


def tracos_realistas(
    pontos: list[dict[str, Any]],
    salto_max_m: float = 380,
    tolerancia_m: float = 11,
    jitter_m: float = 4,
) -> list[list[list[float]]]:
    pares: list[Ponto] = []
    for ponto in pontos or []:
        lat = ponto.get("latitude")
        lon = ponto.get("longitude")
        if lat is None or lon is None:
            continue
        pares.append((float(lat), float(lon)))
    if not pares:
        return []

    brutos: list[list[Ponto]] = []
    atual = [pares[0]]
    for pt in pares[1:]:
        dist = haversine_m(atual[-1], pt)
        if dist > salto_max_m:
            if len(atual) >= 2:
                brutos.append(atual)
            atual = [pt]
            continue
        if dist >= jitter_m:
            atual.append(pt)
    if len(atual) >= 2:
        brutos.append(atual)

    trechos = []
    for trecho in brutos:
        limpo = _rdp(trecho, tolerancia_m)
        if len(limpo) >= 2:
            trechos.append([[lat, lon] for lat, lon in limpo])
    return trechos


def group_by_vehicle(pontos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    agrupado: dict[str, list[dict[str, Any]]] = {}
    for ponto in pontos:
        nome = ponto.get("veiculo") or "sem-identificacao"
        agrupado.setdefault(nome, []).append(ponto)
    rotas = []
    for veiculo, items in agrupado.items():
        rotas.append(
            {
                "veiculo": veiculo,
                "total_pontos": len(items),
                "inicio": items[0].get("timestamp"),
                "fim": items[-1].get("timestamp"),
                "pontos": items,
            }
        )
    return rotas


def para_horario_brasilia(texto: str) -> str:
    """A Segtrack manda o pulso em UTC. O painel mostra o horário de Brasília."""
    bruto = (texto or "").strip()
    if not bruto:
        return bruto
    dt = _datetime_do_timestamp(bruto, date.today())
    if dt is None:
        return bruto
    local = dt - timedelta(hours=3)
    com_segundo = bool(re.search(r"\d{1,2}:\d{2}:\d{2}", bruto))
    if re.search(r"\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4}", bruto):
        if "/" in bruto:
            return local.strftime("%d/%m/%Y %H:%M:%S" if com_segundo else "%d/%m/%Y %H:%M")
        return local.strftime("%Y-%m-%d %H:%M:%S" if com_segundo else "%Y-%m-%d %H:%M")
    return local.strftime("%H:%M:%S" if com_segundo else "%H:%M")


def _datetime_do_timestamp(ts: str, dia: date) -> datetime | None:
    texto = (ts or "").strip()
    if not texto:
        return None
    bruto = texto.replace("T", " ")
    for fmt, tamanho in (
        ("%Y-%m-%d %H:%M:%S", 19),
        ("%d/%m/%Y %H:%M:%S", 19),
        ("%Y-%m-%d %H:%M", 16),
        ("%d/%m/%Y %H:%M", 16),
    ):
        fatia = bruto[:tamanho]
        try:
            return datetime.strptime(fatia, fmt)
        except ValueError:
            continue
    achado = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", texto)
    if not achado:
        return None
    hora, minuto, segundo = int(achado.group(1)), int(achado.group(2)), int(achado.group(3) or 0)
    if hora > 23 or minuto > 59:
        return None
    return datetime.combine(dia, time(hora, minuto, min(segundo, 59)))


def filtrar_pontos_periodo(
    pontos: list[dict[str, Any]] | None,
    dia: date,
    hora_inicio: str = "00:00",
    hora_fim: str = "23:59",
    data_fim: date | None = None,
    dia_referencia: date | None = None,
) -> list[dict[str, Any]]:
    """Mantém GPS no intervalo real (ex.: 21/09 06:00 → 22/09 05:59). Sem timestamp permanece."""
    from .config import janela_datetime

    inicio_txt = normalizar_hora(hora_inicio, "00:00")
    fim_txt = normalizar_hora(hora_fim, "23:59")
    inicio, fim = janela_datetime(dia, inicio_txt, fim_txt, data_fim)
    if inicio.date() == fim.date() and inicio_txt == "00:00" and fim_txt == "23:59":
        return list(pontos or [])
    ref = dia_referencia or dia
    saida: list[dict[str, Any]] = []
    for ponto in pontos or []:
        dt = _datetime_do_timestamp(str(ponto.get("timestamp") or ""), ref)
        if dt is None:
            saida.append(ponto)
            continue
        if inicio <= dt <= fim:
            saida.append(ponto)
    return saida


def _fmt_pulso(dt: datetime, outro: datetime | None = None) -> str:
    if outro is None or dt.date() == outro.date():
        return dt.strftime("%d/%m %H:%M")
    return dt.strftime("%d/%m %H:%M")


def extremos_por_hora(
    pontos: list[dict[str, Any]] | None,
    dia: date,
) -> dict[str, Any]:
    """Primeiro e último pulso do rastreador, ordenados pela hora (não pela linha do mapa)."""
    pares: list[tuple[datetime, float, float]] = []
    for ponto in pontos or []:
        dt = _datetime_do_timestamp(str(ponto.get("timestamp") or ""), dia)
        if dt is None:
            continue
        try:
            lat = float(ponto["latitude"])
            lon = float(ponto["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        pares.append((dt, lat, lon))
    if not pares:
        return {}
    pares.sort(key=lambda item: item[0])
    ini, fim = pares[0], pares[-1]
    if ini[0].date() == fim[0].date():
        texto = f"{ini[0].strftime('%d/%m %H:%M')}–{fim[0].strftime('%H:%M')}"
    else:
        texto = f"{ini[0].strftime('%d/%m %H:%M')}–{fim[0].strftime('%d/%m %H:%M')}"
    return {
        "inicio": [ini[1], ini[2]],
        "fim": [fim[1], fim[2]],
        "hora_inicio": ini[0].strftime("%d/%m %H:%M"),
        "hora_fim": fim[0].strftime("%d/%m %H:%M") if ini[0].date() != fim[0].date() else fim[0].strftime("%H:%M"),
        "horario": texto,
        "inicio_dt": ini[0],
        "fim_dt": fim[0],
    }


def periodo_gps(
    pontos: list[dict[str, Any]] | None,
    dia: date,
) -> tuple[datetime | None, datetime | None, str]:
    ext = extremos_por_hora(pontos, dia)
    if not ext:
        return None, None, ""
    return ext["inicio_dt"], ext["fim_dt"], str(ext["horario"])

