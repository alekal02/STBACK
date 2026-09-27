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
    """A Sagtrack manda o pulso em UTC. O painel mostra o horário de Brasília."""
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

