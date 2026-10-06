"""Trajetoria dos basculantes de dificil acesso.

Usa so os pontos ja gravados no turno. Nao consulta a Segtrack e nao
calcula cobertura, percentual nem status.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Callable

from .gps import haversine_m

# Um item por basculante. Placa nova entra so nesta tupla.
BASCULANTES = (
    {"codigo": "CBT018", "placa": "SCN4B49"},
    {"codigo": "CBT012", "placa": "SCN1G89"},
)

SEM_RASTREAMENTO = "Sem rastreamento"
DESLOCAMENTO = "Deslocamento"
DIFICIL = "Dif\u00edcil acesso"
NA_ROTA = "Na rota"

# Mesmo corte de buraco de sinal j usado na trilha do mapa.
GAP_S = 600.0
# Amostra rara e longa: a reta entre os dois pontos inventaria a rua.
GAP_CURTO_S = 120.0
MAX_CORDA_M = 280.0
# Salto de coordenada, independente do intervalo.
SALTO_M = 1500.0
# Clula de ~24 m. Duas passagens na mesma clula marcam recorrncia.
CELULA = 0.00022
MINIMO_M = 8.0

Contem = Callable[[float, float], bool]


def norm_placa(texto: str) -> str:
    return "".join(ch for ch in str(texto or "").upper() if ch.isalnum())


def codigo_da_frota(placa: str, veiculo: str = "") -> str | None:
    chave = norm_placa(placa)
    nome = norm_placa(veiculo)
    for item in BASCULANTES:
        if chave == item["placa"] or item["codigo"] in nome:
            return item["codigo"]
    return None


def eh_basculante(placa: str, veiculo: str = "") -> bool:
    return codigo_da_frota(placa, veiculo) is not None


def presentes_no_cadastro(veiculos: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """Basculantes que ja vieram na lista da Segtrack. Ausencia nao gera consulta nem erro."""
    por: dict[str, dict[str, Any]] = {}
    for item in veiculos or []:
        bruto = str(item.get("ras_vei_placa") or item.get("placa") or "")
        chave = norm_placa(bruto)
        if chave and chave not in por:
            por[chave] = item
    saida: list[dict[str, str]] = []
    for basculante in BASCULANTES:
        item = por.get(basculante["placa"])
        if not item:
            continue
        chave = norm_placa(str(item.get("ras_vei_placa") or item.get("placa") or basculante["placa"]))
        placa = f"{chave[:3]}-{chave[3:]}" if len(chave) == 7 else chave
        saida.append(
            {
                "placa": placa,
                "veiculo": basculante["codigo"],
                "vehicle_id": str(item.get("ras_vei_id") or item.get("vehicle_id") or ""),
            }
        )
    return saida


def montar_trajetoria(
    pontos: list[tuple[str, float, float]],
    contem: Contem | None = None,
) -> dict[str, Any]:
    """Trajetoria completa, em ordem de tempo, sem cortar pelo poligono.

    contem(lat, lon) diz se o ponto cai numa rota oficial. Fora dela,
    passagem unica vira deslocamento e repeticao vira dificil acesso.
    """
    ordenados = sorted(set(pontos), key=lambda item: (item[0], item[1], item[2]))
    corridas = _quebrar(ordenados)
    fora: list[list[tuple[float, float]]] = []
    dentro: list[list[tuple[float, float]]] = []
    pontos_fora = 0
    pontos_dentro = 0
    for corrida in corridas:
        coords = [(lat, lon) for _ts, lat, lon in corrida]
        for lat, lon in coords:
            if _dentro(lat, lon, contem):
                pontos_dentro += 1
            else:
                pontos_fora += 1
        parte_dentro, parte_fora = _partir(coords, contem)
        dentro.extend(parte_dentro)
        fora.extend(parte_fora)
    visitas = _visitas(fora)
    dificil: list[list[tuple[float, float]]] = []
    desloc: list[list[tuple[float, float]]] = []
    for trecho in fora:
        parte_dificil, parte_desloc = _partir_flag(trecho, lambda p: visitas.get(_celula(p[0], p[1]), 0) >= 2)
        dificil.extend(parte_dificil)
        desloc.extend(parte_desloc)
    ultimo = ordenados[-1] if ordenados else None
    if ultimo is None:
        situacao = SEM_RASTREAMENTO
        ultimo_gps = ""
        lat = lon = None
    else:
        ultimo_gps = ultimo[0]
        lat, lon = ultimo[1], ultimo[2]
        if _dentro(lat, lon, contem):
            situacao = NA_ROTA
        elif visitas.get(_celula(lat, lon), 0) >= 2:
            situacao = DIFICIL
        else:
            situacao = DESLOCAMENTO
    return {
        "pulsos": len(ordenados),
        "pontos_fora": pontos_fora,
        "pontos_na_rota": pontos_dentro,
        "na_rota": _linhas(dentro),
        "deslocamento": _linhas(desloc),
        "dificil": _linhas(dificil),
        "situacao": situacao,
        "ultimo_gps": ultimo_gps,
        "lat": None if lat is None else round(lat, 5),
        "lon": None if lon is None else round(lon, 5),
    }


def _dentro(lat: float, lon: float, contem: Contem | None) -> bool:
    if contem is None:
        return False
    return bool(contem(lat, lon))


def _quebrar(pontos: list[tuple[str, float, float]]) -> list[list[tuple[str, float, float]]]:
    grupos: list[list[tuple[str, float, float]]] = []
    atual: list[tuple[str, float, float]] = []
    for ponto in pontos:
        if atual and _gap(atual[-1], ponto):
            grupos.append(atual)
            atual = []
        atual.append(ponto)
    if atual:
        grupos.append(atual)
    return grupos


def _gap(anterior: tuple[str, float, float], atual: tuple[str, float, float]) -> bool:
    dist = haversine_m((anterior[1], anterior[2]), (atual[1], atual[2]))
    if dist >= SALTO_M:
        return True
    dt = _segundos(anterior[0], atual[0])
    if dt > GAP_S:
        return True
    return dt >= GAP_CURTO_S and dist >= MAX_CORDA_M


def _ler_hora(texto: str) -> datetime | None:
    bruto = (texto or "").strip().replace("Z", "")
    if not bruto:
        return None
    try:
        return datetime.fromisoformat(bruto[:19])
    except ValueError:
        return None


def _segundos(anterior: str, atual: str) -> float:
    def ler(texto: str) -> datetime | None:
        return _ler_hora(texto)

    a = ler(anterior)
    b = ler(atual)
    if a is None or b is None:
        return 0.0
    return abs((b - a).total_seconds())


def _partir(
    coords: list[tuple[float, float]],
    contem: Contem | None,
) -> tuple[list[list[tuple[float, float]]], list[list[tuple[float, float]]]]:
    return _partir_flag(coords, lambda p: _dentro(p[0], p[1], contem))


def _partir_flag(coords: list[tuple[float, float]], flag) -> tuple[list, list]:
    verdade: list[list[tuple[float, float]]] = []
    falso: list[list[tuple[float, float]]] = []
    if not coords:
        return verdade, falso
    atual = bool(flag(coords[0]))
    balde = [coords[0]]
    for ponto in coords[1:]:
        lugar = bool(flag(ponto))
        if lugar != atual:
            balde.append(ponto)
            (verdade if atual else falso).append(balde)
            balde = [ponto]
            atual = lugar
        else:
            balde.append(ponto)
    (verdade if atual else falso).append(balde)
    return verdade, falso


def _celula(lat: float, lon: float) -> tuple[int, int]:
    return (math.floor(lat / CELULA), math.floor(lon / CELULA))


def _visitas(trechos: list[list[tuple[float, float]]]) -> dict[tuple[int, int], int]:
    visitas: dict[tuple[int, int], int] = {}
    for trecho in trechos:
        anterior = None
        for lat, lon in trecho:
            celula = _celula(lat, lon)
            if celula == anterior:
                continue
            visitas[celula] = visitas.get(celula, 0) + 1
            anterior = celula
    return visitas


def _linhas(trechos: list[list[tuple[float, float]]]) -> list[list[list[float]]]:
    saida = []
    for trecho in trechos:
        fino = _afinar(trecho)
        linha = [[round(lat, 5), round(lon, 5)] for lat, lon in fino]
        compacta = [linha[0]] if linha else []
        for ponto in linha[1:]:
            if ponto != compacta[-1]:
                compacta.append(ponto)
        if len(compacta) >= 2:
            saida.append(compacta)
    return saida


def _afinar(pontos: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if len(pontos) <= 2:
        return list(pontos)
    saida = [pontos[0]]
    for ponto in pontos[1:-1]:
        if haversine_m(saida[-1], ponto) >= MINIMO_M:
            saida.append(ponto)
    if haversine_m(saida[-1], pontos[-1]) < MINIMO_M and len(saida) > 1:
        saida[-1] = pontos[-1]
    else:
        saida.append(pontos[-1])
    return saida
