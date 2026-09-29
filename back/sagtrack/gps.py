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
        self._wgs: list[list[Ponto]] | None = None

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
        geoms = _soldar_pontas(geoms)
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

    def __getstate__(self) -> dict[str, Any]:
        estado = dict(self.__dict__)
        estado["_arvore"] = None
        estado["_wgs"] = None
        return estado

    def __setstate__(self, estado: dict[str, Any]) -> None:
        from shapely.strtree import STRtree

        self.__dict__.update(estado)
        if not hasattr(self, "_wgs"):
            self._wgs = None
        self._arvore = STRtree(self.geoms) if self.geoms else None

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
        for indice in self._arvore.query(ponto.buffer(raio_m).envelope):
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

    def candidatos(
        self,
        coord: Ponto,
        raio_m: float,
        rumo: tuple[float, float] | None = None,
        limite: int = 4,
    ) -> list[_Snap]:
        """Até `limite` vias dentro do raio. A primeira é a mais compatível com o rumo."""
        from shapely.geometry import Point

        from .mapa import TO_UTM

        if self._arvore is None:
            return []
        x, y = TO_UTM.transform(float(coord[1]), float(coord[0]))
        ponto = Point(x, y)
        achados: list[tuple[float, float, int, float]] = []
        for indice in self._arvore.query(ponto.buffer(raio_m).envelope):
            aresta = int(indice)
            geom = self.geoms[aresta]
            dist = float(geom.distance(ponto))
            if dist > raio_m:
                continue
            along = float(geom.project(ponto))
            nota = dist + _penalidade_rumo(geom, along, rumo)
            achados.append((nota, dist, aresta, along))
            if len(achados) >= limite * 8:
                break
        achados.sort(key=lambda item: item[0])
        return [_Snap(aresta, along, dist) for _nota, dist, aresta, along in achados[:limite]]

    def ambiguo(self, snaps: list[_Snap]) -> bool:
        """Duas vias paralelas quase à mesma distância. O caminho não pode ser confirmado."""
        if len(snaps) < 2:
            return False
        if snaps[1].dist - snaps[0].dist > 8:
            return False
        if snaps[0].aresta == snaps[1].aresta:
            return False
        nos_a = {self.no0[snaps[0].aresta], self.no1[snaps[0].aresta]}
        nos_b = {self.no0[snaps[1].aresta], self.no1[snaps[1].aresta]}
        return not bool(nos_a & nos_b)

    def componentes(self) -> int:
        vistos: set[tuple[int, int]] = set()
        total = 0
        for no in self.adj:
            if no in vistos:
                continue
            total += 1
            fila = [no]
            vistos.add(no)
            while fila:
                atual = fila.pop()
                for outro, _aresta, _frente in self.adj.get(atual, ()):
                    if outro not in vistos:
                        vistos.add(outro)
                        fila.append(outro)
        return total

    def recorte(self, pontos: list[Ponto], folga_m: float = 350.0) -> Malha:
        """Submalha só nas vias perto dos pulsos. O relatório e a coleta usam isto para não varrer a cidade."""
        from shapely.geometry import box
        from shapely.strtree import STRtree

        from .mapa import TO_UTM

        if self._arvore is None or not pontos:
            return self
        xs: list[float] = []
        ys: list[float] = []
        for lat, lon in pontos:
            x, y = TO_UTM.transform(float(lon), float(lat))
            xs.append(x)
            ys.append(y)
        if not xs:
            return self
        zona = box(min(xs) - folga_m, min(ys) - folga_m, max(xs) + folga_m, max(ys) + folga_m)
        indices = sorted({int(i) for i in self._arvore.query(zona)})
        if not indices:
            return self
        if len(indices) >= len(self.geoms) * 0.55:
            return self
        nova = Malha()
        mapa_aresta: dict[int, int] = {}
        for velho in indices:
            novo = len(nova.geoms)
            mapa_aresta[velho] = novo
            nova.geoms.append(self.geoms[velho])
            nova.comp.append(self.comp[velho])
            nova.custo_ida.append(self.custo_ida[velho])
            nova.custo_volta.append(self.custo_volta[velho])
            nova.no0.append(self.no0[velho])
            nova.no1.append(self.no1[velho])
        for velho, novo in mapa_aresta.items():
            no_a, no_b = nova.no0[novo], nova.no1[novo]
            nova.adj.setdefault(no_a, []).append((no_b, novo, True))
            nova.adj.setdefault(no_b, []).append((no_a, novo, False))
        if nova.geoms:
            nova._arvore = STRtree(nova.geoms)
        return nova

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
        if abs(fim_m - inicio_m) < 0.4:
            return []
        inverter = fim_m < inicio_m
        a, b = (fim_m, inicio_m) if inverter else (inicio_m, fim_m)
        a = max(0.0, a)
        b = min(self.comp[aresta], b)
        if getattr(self, "_wgs", None) is None:
            self._wgs = [[] for _ in self.geoms]
        if not self._wgs[aresta]:
            from .mapa import TO_WGS

            convertidos: list[Ponto] = []
            for x, y in self.geoms[aresta].coords:
                lon, lat = TO_WGS.transform(float(x), float(y))
                convertidos.append((lat, lon))
            self._wgs[aresta] = convertidos
        wgs = self._wgs[aresta]
        coords_utm = list(self.geoms[aresta].coords)
        if len(wgs) != len(coords_utm) or len(wgs) < 2:
            from shapely.ops import substring

            from .mapa import TO_WGS

            trecho = substring(self.geoms[aresta], a, b)
            saida = []
            for x, y in trecho.coords:
                lon, lat = TO_WGS.transform(x, y)
                saida.append((lat, lon))
            if inverter:
                saida.reverse()
            return saida
        saida: list[Ponto] = []
        percorrido = 0.0
        for indice in range(len(coords_utm) - 1):
            x0, y0 = coords_utm[indice]
            x1, y1 = coords_utm[indice + 1]
            comp = math.hypot(float(x1) - float(x0), float(y1) - float(y0))
            if comp < 0.5:
                continue
            prox = percorrido + comp
            if prox < a:
                percorrido = prox
                continue
            if not saida:
                t = 0.0 if comp <= 0 else max(0.0, min(1.0, (a - percorrido) / comp))
                lat0, lon0 = wgs[indice]
                lat1, lon1 = wgs[indice + 1]
                saida.append((lat0 + (lat1 - lat0) * t, lon0 + (lon1 - lon0) * t))
            if prox <= b:
                saida.append(wgs[indice + 1])
            else:
                t = 0.0 if comp <= 0 else max(0.0, min(1.0, (b - percorrido) / comp))
                lat0, lon0 = wgs[indice]
                lat1, lon1 = wgs[indice + 1]
                saida.append((lat0 + (lat1 - lat0) * t, lon0 + (lon1 - lon0) * t))
                break
            percorrido = prox
        if inverter:
            saida.reverse()
        return saida


def _soldar_pontas(geoms: list[Any], tol: float = 4.0) -> list[Any]:
    """Une pontas que ficaram a até 4 m. O cruzamento que já se toca o unary_union parte."""
    from shapely.geometry import LineString

    pontas: list[tuple[float, float, int, int]] = []
    for indice, geom in enumerate(geoms):
        coords = list(geom.coords)
        if len(coords) < 2:
            continue
        pontas.append((float(coords[0][0]), float(coords[0][1]), indice, 0))
        pontas.append((float(coords[-1][0]), float(coords[-1][1]), indice, -1))
    if not pontas:
        return geoms
    pai = list(range(len(pontas)))

    def achar(no: int) -> int:
        while pai[no] != no:
            pai[no] = pai[pai[no]]
            no = pai[no]
        return no

    grade: dict[tuple[int, int], list[int]] = {}
    for indice, (x, y, _geom, _qual) in enumerate(pontas):
        grade.setdefault((int(x // tol), int(y // tol)), []).append(indice)
    limite = tol * tol
    for indice, (x, y, _geom, _qual) in enumerate(pontas):
        cx, cy = int(x // tol), int(y // tol)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for outro in grade.get((cx + dx, cy + dy), ()):
                    if outro <= indice:
                        continue
                    ox, oy = pontas[outro][0], pontas[outro][1]
                    if (x - ox) ** 2 + (y - oy) ** 2 <= limite:
                        pai[achar(outro)] = achar(indice)
    grupos: dict[int, list[int]] = {}
    for indice in range(len(pontas)):
        grupos.setdefault(achar(indice), []).append(indice)
    mover: dict[tuple[int, int], tuple[float, float]] = {}
    for membros in grupos.values():
        if len(membros) < 2:
            continue
        mx = sum(pontas[i][0] for i in membros) / len(membros)
        my = sum(pontas[i][1] for i in membros) / len(membros)
        for i in membros:
            mover[(pontas[i][2], pontas[i][3])] = (mx, my)
    saida = []
    for indice, geom in enumerate(geoms):
        coords = [tuple(p) for p in geom.coords]
        if (indice, 0) in mover:
            coords[0] = mover[(indice, 0)]
        if (indice, -1) in mover:
            coords[-1] = mover[(indice, -1)]
        if len(coords) >= 2 and coords[0] != coords[-1]:
            saida.append(LineString(coords))
        else:
            saida.append(geom)
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
    """Rede pronta. Guarda em disco para a frota não remontar 26 mil vias a cada pedido."""
    global _MALHA
    if _MALHA is not None:
        return _MALHA
    import pickle

    from .config import ROOT
    from .mapa import caminho_gpkg

    gpkg = caminho_gpkg()
    cache = ROOT / "data" / "malha-casamento.v4.pkl"
    if gpkg and cache.is_file() and cache.stat().st_mtime >= gpkg.stat().st_mtime:
        try:
            _MALHA = pickle.loads(cache.read_bytes())
            if getattr(_MALHA, "geoms", None):
                return _MALHA
        except Exception:
            _MALHA = None
    _MALHA = Malha.oficial()
    if gpkg and _MALHA.geoms:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(pickle.dumps(_MALHA, protocol=pickle.HIGHEST_PROTOCOL))
        except Exception:
            pass
    return _MALHA


def _afinar_para_casamento(
    pontos: list[dict[str, Any]],
    dia: date | None = None,
    *,
    min_m: float = 18.0,
    min_s: float = 10.0,
) -> list[dict[str, Any]]:
    """Afina pulsos colados sem rarear demais. Densidade boa fecha o trajeto na via."""
    ordenados = sorted(pontos or [], key=ordem_do_pulso)
    if len(ordenados) <= 2:
        return list(ordenados)
    ref = dia or date.today()
    saida: list[dict[str, Any]] = [ordenados[0]]
    for ponto in ordenados[1:-1]:
        ant = saida[-1]
        try:
            a = (float(ant["latitude"]), float(ant["longitude"]))
            b = (float(ponto["latitude"]), float(ponto["longitude"]))
        except (KeyError, TypeError, ValueError):
            continue
        dist = haversine_m(a, b)
        if dist < min_m:
            continue
        ta = _datetime_do_timestamp(str(ant.get("timestamp") or ""), ref)
        tb = _datetime_do_timestamp(str(ponto.get("timestamp") or ""), ref)
        if ta and tb and abs((tb - ta).total_seconds()) < min_s and dist < min_m * 1.8:
            continue
        saida.append(ponto)
    ultimo = ordenados[-1]
    if saida[-1] is not ultimo:
        saida.append(ultimo)
    return saida


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


def _caminho_estimado(dist: float, comp: float, dt: float) -> bool:
    """Caminho na via com folga. Serve para desenhar o traço, não para confirmar a rua."""
    if comp > _TETO_CASAMENTO_M or dt >= _SILENCIO_S:
        return False
    if dt > 0 and comp > (70 / 3.6) * dt + 280.0 and comp > dist + 80:
        return False
    return comp <= max(dist * 5.2, dist + 480.0)


def _trecho_confirmado(dist: float, comp: float, da: float, db: float, dt: float) -> bool:
    """Confirmado só quando o caminho na via tem o tamanho do deslocamento medido."""
    if da > 22 or db > 22:
        return False
    if abs(comp - dist) > max(45.0, 0.35 * max(dist, 1.0)):
        return False
    if dt > 0 and comp > (50 / 3.6) * dt + 80:
        return False
    return True


def _guardar_trecho(atual: list[Ponto], destino: list[list[list[float]]]) -> None:
    if len(atual) < 2:
        return
    fino = _rdp(atual, 6)
    if len(fino) >= 2:
        destino.append([[lat, lon] for lat, lon in fino])


def _sigma_do_pulso(ponto: dict[str, Any]) -> float:
    bruto = ponto.get("precisao")
    if bruto in (None, ""):
        bruto = ponto.get("hdop")
    try:
        valor = float(str(bruto).replace(",", "."))
    except (TypeError, ValueError):
        return 18.0
    if valor <= 0:
        return 18.0
    metros = valor * 6.0 if valor < 15 else valor
    return max(8.0, min(40.0, metros))


def _velocidade_do_pulso(ponto: dict[str, Any]) -> float | None:
    try:
        valor = float(str(ponto.get("velocidade") or "").replace(",", "."))
    except (TypeError, ValueError):
        return None
    if valor < 0 or valor > 180:
        return None
    return valor


def _log_emissao(dist: float, sigma: float) -> float:
    return -0.5 * (dist / sigma) ** 2


def _log_transicao(metros: float, dist: float) -> float:
    return -abs(metros - dist) / 35.0


def _log_rumo_caminho(geom: list[Ponto], rumo: tuple[float, float] | None) -> float:
    if not rumo or len(geom) < 2:
        return 0.0
    norma = math.hypot(rumo[0], rumo[1])
    if norma < 8:
        return 0.0
    a, b = geom[0], geom[min(3, len(geom) - 1)]
    leste = (b[1] - a[1]) * 111_320 * math.cos(math.radians(a[0]))
    norte = (b[0] - a[0]) * 110_540
    norma_via = math.hypot(leste, norte)
    if norma_via < 6:
        return 0.0
    cosseno = (leste * rumo[0] + norte * rumo[1]) / (norma_via * norma)
    if cosseno < -0.15:
        return -2.8
    return 0.0


def _classe_do_salto(
    dist: float,
    comp: float,
    da: float,
    db: float,
    dt: float,
    ambiguo: bool,
    vel_a: float | None,
    vel_b: float | None,
) -> str:
    if not _caminho_plausivel(dist, comp, dt):
        # Ainda há eixo na malha, mas fora do critério firme → estimado, não buraco.
        if _caminho_estimado(dist, comp, dt):
            return "estimado"
        return "indeterminado"
    if dt > 1:
        vel_via = comp / dt * 3.6
        if vel_via > 72:
            return "indeterminado" if not _caminho_estimado(dist, comp, dt) else "estimado"
        if vel_a is not None and vel_b is not None:
            media = (vel_a + vel_b) / 2
            if abs(media - vel_via) > 38 and vel_via > 22:
                ambiguo = True
    if ambiguo or not _trecho_confirmado(dist, comp, da, db, dt):
        return "estimado"
    return "confirmado"


def _confianca_casamento(resumo: dict[str, int]) -> float:
    pesos = (
        int(resumo.get("confirmado") or 0)
        + int(resumo.get("estimado") or 0)
        + int(resumo.get("indeterminado") or 0)
    )
    if pesos <= 0:
        return 0.0
    return round(int(resumo.get("confirmado") or 0) / pesos, 2)


_ZONA_LOCAIS: Any = None


def _zona_locais() -> Any:
    """Pátio e aterro em UTM. O trecho dentro deles é deslocamento, não coleta."""
    global _ZONA_LOCAIS
    if _ZONA_LOCAIS is not None:
        return _ZONA_LOCAIS
    import json

    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    from .config import ROOT
    from .mapa import TO_UTM

    arquivo = ROOT / "data" / "locais.json"
    if not arquivo.is_file():
        _ZONA_LOCAIS = False
        return _ZONA_LOCAIS
    try:
        dado = json.loads(arquivo.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _ZONA_LOCAIS = False
        return _ZONA_LOCAIS
    polys = []
    for local in (dado or {}).get("locais") or []:
        for anel in local.get("aneis") or []:
            coords = []
            for ponto in anel:
                if len(ponto) < 2:
                    continue
                x, y = TO_UTM.transform(float(ponto[1]), float(ponto[0]))
                coords.append((x, y))
            if len(coords) >= 4:
                polys.append(Polygon(coords))
    _ZONA_LOCAIS = unary_union(polys) if polys else False
    return _ZONA_LOCAIS


def _linhas_wgs_de_utm(geom: Any) -> list[list[list[float]]]:
    from .mapa import TO_WGS

    saida = []
    partes = [geom] if geom.geom_type == "LineString" else list(getattr(geom, "geoms", []) or [])
    for parte in partes:
        if parte.geom_type != "LineString" or len(parte.coords) < 2 or parte.length < 8:
            continue
        linha = []
        for x, y in parte.coords:
            lon, lat = TO_WGS.transform(float(x), float(y))
            linha.append([lat, lon])
        if len(linha) >= 2:
            saida.append(linha)
    return saida


def _separar_locais(
    tracos: list[list[list[float]]],
) -> tuple[list[list[list[float]]], list[list[list[float]]]]:
    from shapely.geometry import LineString

    from .mapa import TO_UTM

    zona = _zona_locais()
    if not zona or not tracos:
        return tracos, []
    normais: list[list[list[float]]] = []
    desloc: list[list[list[float]]] = []
    for trecho in tracos:
        xy = []
        for ponto in trecho:
            if len(ponto) < 2:
                continue
            x, y = TO_UTM.transform(float(ponto[1]), float(ponto[0]))
            xy.append((x, y))
        if len(xy) < 2:
            continue
        linha = LineString(xy)
        try:
            fora = linha.difference(zona)
            dentro = linha.intersection(zona)
        except Exception:
            normais.append(trecho)
            continue
        normais.extend(_linhas_wgs_de_utm(fora))
        desloc.extend(_linhas_wgs_de_utm(dentro))
    return normais, desloc


def _viterbi(
    grupo: list[tuple[datetime, Ponto, float | None, float]],
    malha: Malha,
) -> tuple[list[tuple[str, list[Ponto]]], int]:
    """HMM: emissão pela distância à via, transição pelo caminho que cabe no tempo."""
    n = len(grupo)
    camadas = []
    for indice, (_momento, coord, _vel, _sigma) in enumerate(grupo):
        rumo = _rumo_utm(grupo[indice - 1][1], coord) if indice else None
        camadas.append(malha.candidatos(coord, _RAIO_CASAMENTO_M, rumo=rumo, limite=3))
    neg = -1e12
    dp: list[list[float]] = []
    anterior: list[list[int]] = []
    salto: list[list[tuple[list[Ponto], float] | None]] = []
    for i, cands in enumerate(camadas):
        dp.append([neg] * len(cands))
        anterior.append([-1] * len(cands))
        salto.append([None] * len(cands))
        if not cands:
            continue
        if i == 0:
            for j, cand in enumerate(cands):
                dp[i][j] = _log_emissao(cand.dist, grupo[i][3])
            continue
        dist = haversine_m(grupo[i - 1][1], grupo[i][1])
        dt = abs((grupo[i][0] - grupo[i - 1][0]).total_seconds()) if grupo[i][0] and grupo[i - 1][0] else 0.0
        limite = _limite_do_salto(dist, dt)
        limite_folga = min(_TETO_CASAMENTO_M, max(limite * 1.45, dist + 480.0))
        rumo = _rumo_utm(grupo[i - 1][1], grupo[i][1])
        for j, cand in enumerate(cands):
            emissao = _log_emissao(cand.dist, grupo[i][3])
            melhor = neg
            origem = -1
            escolhido: tuple[list[Ponto], float] | None = None
            for k, prev in enumerate(camadas[i - 1]):
                if dp[i - 1][k] <= neg / 2:
                    continue
                achado = malha.caminho(prev, cand, limite)
                folga = False
                if achado is None and limite_folga > limite:
                    achado = malha.caminho(prev, cand, limite_folga)
                    folga = achado is not None
                if achado is None:
                    continue
                geom, metros = achado
                if not _caminho_plausivel(dist, metros, dt):
                    if not _caminho_estimado(dist, metros, dt):
                        continue
                    folga = True
                nota = (
                    dp[i - 1][k]
                    + _log_transicao(metros, dist)
                    + emissao
                    + _log_rumo_caminho(geom, rumo)
                    - (3.5 if folga else 0.0)
                )
                if nota > melhor:
                    melhor = nota
                    origem = k
                    escolhido = (geom, metros)
            dp[i][j] = melhor
            anterior[i][j] = origem
            salto[i][j] = escolhido
    fim = n - 1
    falhas_cauda = 0
    while fim > 0 and (not dp[fim] or max(dp[fim]) <= neg / 2):
        fim -= 1
        falhas_cauda += 1
    if fim == 0 and (not dp[0] or max(dp[0]) <= neg / 2):
        return [], max(0, n - 1)
    estado = max(range(len(dp[fim])), key=lambda j: dp[fim][j])
    ordem: list[int] = [-1] * n
    ordem[fim] = estado
    for i in range(fim, 0, -1):
        if ordem[i] < 0:
            break
        ordem[i - 1] = anterior[i][ordem[i]]
    trechos: list[tuple[str, list[Ponto]]] = []
    falhas = falhas_cauda
    for i in range(1, fim + 1):
        j = ordem[i]
        k = ordem[i - 1]
        ligacao = salto[i][j] if j >= 0 else None
        if k < 0 or j < 0 or ligacao is None:
            falhas += 1
            continue
        geom, metros = ligacao
        dist = haversine_m(grupo[i - 1][1], grupo[i][1])
        dt = abs((grupo[i][0] - grupo[i - 1][0]).total_seconds()) if grupo[i][0] and grupo[i - 1][0] else 0.0
        classe = _classe_do_salto(
            dist,
            metros,
            camadas[i - 1][k].dist,
            camadas[i][j].dist,
            dt,
            malha.ambiguo(camadas[i - 1]) or malha.ambiguo(camadas[i]),
            grupo[i - 1][2],
            grupo[i][2],
        )
        if classe == "indeterminado":
            falhas += 1
            continue
        trechos.append((classe, geom))
    return trechos, falhas


def _runs_com_via(
    grupo: list[tuple[datetime, Ponto, float | None, float]],
    malha: Malha,
) -> tuple[list[list[tuple[datetime, Ponto, float | None, float]]], int]:
    runs: list[list[tuple[datetime, Ponto, float | None, float]]] = []
    atual: list[tuple[datetime, Ponto, float | None, float]] = []
    sem_via = 0
    for item in grupo:
        if malha.candidatos(item[1], _RAIO_CASAMENTO_M, limite=1):
            atual.append(item)
            continue
        sem_via += 1
        if len(atual) >= 2:
            runs.append(atual)
        atual = []
    if len(atual) >= 2:
        runs.append(atual)
    return runs, sem_via


def _casar_grupo(
    grupo: list[tuple[datetime, Ponto, float | None, float]],
    malha: Malha,
    confirmado: list[list[list[float]]],
    estimado: list[list[list[float]]],
) -> dict[str, int]:
    contagem = {"confirmado": 0, "estimado": 0, "indeterminado": 0, "sem_via": 0}
    runs, sem_via = _runs_com_via(grupo, malha)
    contagem["sem_via"] = sem_via
    if sem_via and len(grupo) >= 2:
        contagem["indeterminado"] += sem_via
    classe = ""
    atual: list[Ponto] = []

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

    for run in runs:
        trechos, falhas = _viterbi(run, malha)
        contagem["indeterminado"] += falhas
        for nova, geom in trechos:
            usar(nova, geom)
        fechar()
    return contagem


def _emendar_listas(base: list, extra: list) -> list:
    if not base or not extra:
        return list(base) + list(extra)
    fim = base[-1][-1] if base[-1] else None
    inicio = extra[0][0] if extra[0] else None
    if (
        fim
        and inicio
        and len(fim) >= 2
        and len(inicio) >= 2
        and haversine_m((float(fim[0]), float(fim[1])), (float(inicio[0]), float(inicio[1]))) < 12
    ):
        unidos = list(base)
        unidos[-1] = list(base[-1]) + list(extra[0][1:])
        unidos.extend(extra[1:])
        return unidos
    return list(base) + list(extra)


def _nucleo_casamento(
    pontos: list[dict[str, Any]],
    dia: date | None,
    vias: list[list[list[float]]] | None,
    malha: Malha | None,
) -> dict[str, Any]:
    ordenados = sorted(pontos or [], key=ordem_do_pulso)
    referencia = dia or date.today()
    bruto: list[tuple[datetime, Ponto, float | None, float]] = []
    invalidos = 0
    for ponto in ordenados:
        try:
            lat = float(ponto["latitude"])
            lon = float(ponto["longitude"])
        except (KeyError, TypeError, ValueError):
            invalidos += 1
            continue
        if abs(lat) < 0.5 or abs(lon) < 0.5 or abs(lat) > 90 or abs(lon) > 180:
            invalidos += 1
            continue
        momento = _datetime_do_timestamp(str(ponto.get("timestamp") or ""), referencia)
        bruto.append((momento or datetime.min, (lat, lon), _velocidade_do_pulso(ponto), _sigma_do_pulso(ponto)))
    seguidos: list[tuple[datetime, Ponto, float | None, float]] = []
    duplicados = 0
    for item in bruto:
        if seguidos and haversine_m(seguidos[-1][1], item[1]) < 8:
            duplicados += 1
            continue
        seguidos.append(item)
    saltos = 0
    limpos: list[tuple[datetime, Ponto, float | None, float]] = []
    for indice, item in enumerate(seguidos):
        if 0 < indice < len(seguidos) - 1:
            antes, depois = seguidos[indice - 1], seguidos[indice + 1]
            ida = haversine_m(antes[1], item[1])
            volta = haversine_m(item[1], depois[1])
            dt = abs((depois[0] - antes[0]).total_seconds()) if depois[0] and antes[0] else 0.0
            rapido = dt <= 0 or ida + volta > (45 / 3.6) * dt + 80
            if ida > 250 and volta > 250 and haversine_m(antes[1], depois[1]) < 60 and rapido:
                saltos += 1
                continue
        limpos.append(item)
    seguidos = limpos
    original = _quebrar_salto([coord for _momento, coord, _vel, _sigma in seguidos], 180) if len(seguidos) >= 2 else []
    vazio = {
        "confirmado": [],
        "estimado": [],
        "deslocamento": [],
        "original": original,
        "disponivel": False,
        "resumo": {
            "confirmado": 0,
            "estimado": 0,
            "indeterminado": 0,
            "sem_via": 0,
            "gaps": 0,
            "invalidos": invalidos,
            "duplicados": duplicados,
            "saltos": saltos,
            "confianca": 0.0,
        },
    }
    if len(seguidos) < 2:
        return vazio
    if malha is None:
        malha = Malha.de_wgs(vias) if vias is not None else malha_oficial()
    if not malha.geoms:
        return vazio
    if vias is None and len(seguidos) >= 2 and len(malha.geoms) > 2500:
        malha = malha.recorte([coord for _m, coord, _v, _s in seguidos], folga_m=420.0)

    confirmado: list[list[list[float]]] = []
    estimado: list[list[list[float]]] = []
    contagem = {
        "confirmado": 0,
        "estimado": 0,
        "indeterminado": 0,
        "sem_via": 0,
        "gaps": 0,
        "invalidos": invalidos,
        "duplicados": duplicados,
        "saltos": saltos,
    }
    grupo: list[tuple[datetime, Ponto, float | None, float]] = [seguidos[0]]
    for item in seguidos[1:]:
        dist = haversine_m(grupo[-1][1], item[1])
        dt = abs((item[0] - grupo[-1][0]).total_seconds()) if item[0] and grupo[-1][0] else 0.0
        if dt >= _SILENCIO_S or _pulso_impossivel(dist, dt):
            if len(grupo) >= 2:
                parcial = _casar_grupo(grupo, malha, confirmado, estimado)
                for chave in ("confirmado", "estimado", "indeterminado", "sem_via"):
                    contagem[chave] += parcial[chave]
            contagem["gaps"] += 1
            contagem["indeterminado"] += 1
            grupo = [item]
            continue
        grupo.append(item)
    if len(grupo) >= 2:
        parcial = _casar_grupo(grupo, malha, confirmado, estimado)
        for chave in ("confirmado", "estimado", "indeterminado", "sem_via"):
            contagem[chave] += parcial[chave]
    confirmado, desloc_c = _separar_locais(confirmado)
    estimado, desloc_e = _separar_locais(estimado)
    contagem["confianca"] = _confianca_casamento(contagem)
    return {
        "confirmado": confirmado,
        "estimado": estimado,
        "deslocamento": desloc_c + desloc_e,
        "original": original,
        "disponivel": True,
        "resumo": contagem,
    }


def _chaves_gps(pontos: list[dict[str, Any]]) -> list[str]:
    chaves = []
    for ponto in sorted(pontos or [], key=ordem_do_pulso):
        try:
            lat = round(float(ponto["latitude"]), 5)
            lon = round(float(ponto["longitude"]), 5)
        except (KeyError, TypeError, ValueError):
            continue
        chaves.append(f"{ponto.get('timestamp')}|{lat}|{lon}")
    return chaves


def casar_tracos(
    pontos: list[dict[str, Any]],
    dia: date | None = None,
    vias: list[list[list[float]]] | None = None,
    malha: Malha | None = None,
    *,
    placa: str = "",
    pasta_cache: Any = None,
) -> dict[str, Any]:
    """Casa a sequência de pulsos com a rede viária.

    A emissão é a distância do pulso à via. A transição é o caminho na rede
    cujo comprimento cabe na distância e no tempo observados. Sem candidato
    dentro do raio, ou sem caminho compatível, o trecho fica indeterminado
    e nenhuma reta é desenhada no lugar. O GPS original não é alterado.
    """
    import json
    from pathlib import Path

    versao = "v2"
    cache = Path(pasta_cache) if pasta_cache else None
    arquivo = None
    guardado: dict[str, Any] | None = None
    chaves = _chaves_gps(pontos)
    if cache and placa and vias is None and malha is None:
        cache.mkdir(parents=True, exist_ok=True)
        arquivo = cache / f"{versao}-{placa}.json"
        if arquivo.is_file():
            try:
                guardado = json.loads(arquivo.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                guardado = None
        if guardado and guardado.get("chaves") == chaves and isinstance(guardado.get("resultado"), dict):
            return guardado["resultado"]
        prefixo = list((guardado or {}).get("chaves") or [])
        if (
            guardado
            and prefixo
            and len(chaves) > len(prefixo)
            and chaves[: len(prefixo)] == prefixo
            and isinstance(guardado.get("resultado"), dict)
        ):
            cauda = sorted(pontos or [], key=ordem_do_pulso)[len(prefixo) - 1 :]
            novo = _nucleo_casamento(cauda, dia, vias, malha)
            anterior = guardado["resultado"]
            resultado = {
                "confirmado": _emendar_listas(anterior.get("confirmado") or [], novo.get("confirmado") or []),
                "estimado": _emendar_listas(anterior.get("estimado") or [], novo.get("estimado") or []),
                "deslocamento": _emendar_listas(anterior.get("deslocamento") or [], novo.get("deslocamento") or []),
                "original": _emendar_listas(anterior.get("original") or [], novo.get("original") or []),
                "disponivel": bool(anterior.get("disponivel") or novo.get("disponivel")),
                "resumo": {},
            }
            chaves_resumo = (
                "confirmado",
                "estimado",
                "indeterminado",
                "sem_via",
                "gaps",
                "invalidos",
                "duplicados",
                "saltos",
                "componentes",
            )
            for chave in chaves_resumo:
                resultado["resumo"][chave] = int((anterior.get("resumo") or {}).get(chave) or 0) + int(
                    (novo.get("resumo") or {}).get(chave) or 0
                )
            if "componentes" in (novo.get("resumo") or {}):
                resultado["resumo"]["componentes"] = novo["resumo"]["componentes"]
            resultado["resumo"]["confianca"] = _confianca_casamento(resultado["resumo"])
            arquivo.write_text(
                json.dumps({"chaves": chaves, "resultado": resultado}, ensure_ascii=False),
                encoding="utf-8",
            )
            return resultado
    resultado = _nucleo_casamento(pontos, dia, vias, malha)
    if arquivo is not None and resultado.get("disponivel"):
        arquivo.write_text(
            json.dumps({"chaves": chaves, "resultado": resultado}, ensure_ascii=False),
            encoding="utf-8",
        )
    return resultado


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

