"""Trajetoria dos basculantes de dificil acesso.

Usa so os pontos ja gravados no turno. Nao consulta a Segtrack e nao
calcula cobertura, percentual nem status. Entrada, saida e permanencia
sao lidas desses pontos contra os poligonos oficiais. Nada disso e gravado
de novo e nenhum evento e inventado entre dois GPS.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from .gps import haversine_m

# Um item por basculante. Placa nova entra so nesta tupla.
BASCULANTES = (
    {"codigo": "CBT018", "placa": "SCN4B49"},
    {"codigo": "CBT012", "placa": "SCN1G89"},
)

SEM_RASTREAMENTO = "Sem rastreamento"
SINAL_DESATUALIZADO = "Sinal desatualizado"
DESLOCAMENTO = "Deslocamento"
DIFICIL = "Dif\u00edcil acesso"
NA_ROTA = "Na rota"

# Mesmo corte da trilha dos compactadores: buraco de 10 min ou salto de 480 m.
GAP_S = 600.0
GAP_M = 480.0
# Célula de ~24 m. Duas passagens na mesma célula marcam recorrência.
CELULA = 0.00022

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


def _norm_area(texto: str) -> str:
    bruto = (texto or "").strip().upper()
    return (
        bruto.replace("Á", "A")
        .replace("À", "A")
        .replace("Ã", "A")
        .replace("Â", "A")
        .replace("É", "E")
        .replace("Ê", "E")
        .replace("Í", "I")
        .replace("Ó", "O")
        .replace("Ô", "O")
        .replace("Ú", "U")
        .replace("Ç", "C")
    )


def carregar_excecoes(arquivo: Path | None = None) -> list[dict[str, Any]]:
    """Exceções por data. Os dias da semana continuam na frequência do GPKG."""
    from .config import ROOT

    caminho = arquivo or (ROOT / "data" / "programacao_areas_dificil.json")
    if not caminho.is_file():
        return []
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(dados, dict):
        linhas = dados.get("excecoes") or []
    elif isinstance(dados, list):
        linhas = dados
    else:
        return []
    return [item for item in linhas if isinstance(item, dict)]


def _excecao_desta(excecao: dict[str, Any], area: dict[str, Any], dia: date, turno: str) -> bool:
    bruto = str(excecao.get("data") or "").strip()
    if bruto != dia.isoformat():
        return False
    pedido = str(excecao.get("turno") or "").strip().lower()
    if pedido and pedido != (turno or "").strip().lower():
        return False
    ident = str(excecao.get("id") or "").strip()
    nome = _norm_area(str(excecao.get("nome") or ""))
    if ident:
        return ident == str(area.get("id") or "")
    if nome:
        return nome == _norm_area(str(area.get("nome") or ""))
    return False


def programada_na_data(
    area: dict[str, Any],
    dia: date,
    turno: str,
    excecoes: list[dict[str, Any]] | None = None,
) -> bool:
    """Dia e turno saem da frequência oficial. Uma exceção da mesma data pode inverter."""
    from .mapa_oficial import _freq_do_dia
    from .unificado import _casa_turno

    freq = str(area.get("frequencia") or "")
    prevista = bool(freq) and _freq_do_dia(freq, dia) and _casa_turno(freq, turno)
    for excecao in excecoes or []:
        if "executar" not in excecao or not _excecao_desta(excecao, area, dia, turno):
            continue
        prevista = bool(excecao.get("executar"))
    return prevista


def anotar_programacao(
    areas: list[dict[str, Any]],
    dia: date,
    turno: str,
    excecoes: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Copia cada área e marca `programada`. Não altera o anel oficial."""
    linhas = carregar_excecoes() if excecoes is None else excecoes
    saida: list[dict[str, Any]] = []
    for area in areas:
        item = dict(area)
        item["programada"] = programada_na_data(item, dia, turno, linhas)
        saida.append(item)
    return saida


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
    """Trajetoria dos pontos já gravados, sem cortar pelo polígono.

    contem(lat, lon) diz se o ponto cai numa rota oficial. Fora dela,
    passagem única vira deslocamento e repetição vira difícil acesso.
    Os polígonos oficiais vão na camada separada de áreas. Aqui poligonos
    fica vazio para não misturar o contorno do bairro com o GPS.
    """
    ordenados = sorted(set(pontos), key=lambda item: (item[0], item[1], item[2]))
    corridas = _quebrar(ordenados)
    fora: list[list[tuple[float, float]]] = []
    dentro: list[list[tuple[float, float]]] = []
    base: list[list[tuple[float, float]]] = []
    soltos: list[tuple[float, float]] = []
    pontos_fora = 0
    pontos_dentro = 0
    for corrida in corridas:
        coords = [(lat, lon) for _ts, lat, lon in corrida]
        for lat, lon in coords:
            if _dentro(lat, lon, contem):
                pontos_dentro += 1
            else:
                pontos_fora += 1
        if len(coords) >= 2:
            base.append(coords)
        else:
            soltos.extend(coords)
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
        "trajeto": _linhas(base),
        "na_rota": _linhas(dentro),
        "deslocamento": _linhas(desloc),
        "dificil": _linhas(dificil),
        "isolados": _pontos(soltos),
        "poligonos": [],
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
    if dist > GAP_M:
        return True
    return _segundos(anterior[0], atual[0]) > GAP_S


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
        linha = _pontos(trecho)
        if len(linha) >= 2:
            saida.append(linha)
    return saida


def _pontos(coords: list[tuple[float, float]]) -> list[list[float]]:
    saida: list[list[float]] = []
    for lat, lon in coords:
        ponto = [round(lat, 5), round(lon, 5)]
        if not saida or ponto != saida[-1]:
            saida.append(ponto)
    return saida


def preparar_areas(areas: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Polígonos prontos para o ponto. O anel continua sendo o do mapa."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    from shapely.prepared import prep

    prontos: list[dict[str, Any]] = []
    for area in areas or []:
        polys = []
        for anel in area.get("poligonos") or []:
            coords = []
            for par in anel:
                if not isinstance(par, (list, tuple)) or len(par) < 2:
                    continue
                try:
                    lat = float(par[0])
                    lon = float(par[1])
                except (TypeError, ValueError):
                    continue
                coords.append((lon, lat))
            if len(coords) < 4:
                continue
            poly = Polygon(coords)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty:
                continue
            polys.append(poly)
        if not polys:
            continue
        geom = unary_union(polys) if len(polys) > 1 else polys[0]
        if geom.is_empty:
            continue
        prontos.append(
            {
                "id": str(area.get("id") or area.get("nome") or ""),
                "nome": str(area.get("nome") or ""),
                "prep": prep(geom),
                "tamanho": float(geom.area),
            }
        )
    return prontos


def _area_em(lat: float, lon: float, prontos: list[dict[str, Any]]) -> dict[str, Any] | None:
    from shapely.geometry import Point

    ponto = Point(float(lon), float(lat))
    melhor = None
    tamanho = None
    for area in prontos:
        if not area["prep"].covers(ponto):
            continue
        if tamanho is None or area["tamanho"] < tamanho:
            tamanho = area["tamanho"]
            melhor = area
    return melhor


def eventos_nas_areas(
    pontos: list[tuple[str, float, float]],
    prontos: list[dict[str, Any]],
    *,
    codigo: str,
    placa: str,
    data: str,
    turno: str,
) -> dict[str, Any]:
    """Entrada, saída e permanência só quando um GPS real muda de polígono.

    Um gap encerra a permanência no último ponto visto. Não cria saída,
    porque ninguém observou o veículo do lado de fora.
    """
    ordenados = sorted(set(pontos), key=lambda item: (item[0], item[1], item[2]))
    eventos: list[dict[str, Any]] = []
    permanencias: list[dict[str, Any]] = []
    aberta: dict[str, Any] | None = None
    anterior: tuple[str, float, float] | None = None
    atual: dict[str, Any] | None = None

    def emitir(tipo: str, area: dict[str, Any], ponto: tuple[str, float, float]) -> None:
        ts, lat, lon = ponto
        eventos.append(
            {
                "tipo": tipo,
                "veiculo": codigo,
                "placa": placa,
                "area_id": area["id"],
                "area": area["nome"],
                "data": data,
                "turno": turno,
                "ts": ts,
                "lat": round(lat, 5),
                "lon": round(lon, 5),
            }
        )

    def encerrar(saida: tuple[str, float, float] | None, motivo: str) -> None:
        nonlocal aberta
        if aberta is None:
            return
        fim = aberta["ponto"]
        saida_ts = ""
        if motivo == "saida" and saida is not None:
            saida_ts = saida[0]
            emitir("saida", aberta["area"], saida)
        permanencias.append(
            {
                "veiculo": codigo,
                "placa": placa,
                "area_id": aberta["area"]["id"],
                "area": aberta["area"]["nome"],
                "data": data,
                "turno": turno,
                "entrada": aberta["entrada"],
                "fim": fim[0],
                "saida": saida_ts,
                "aberta": False,
                "segundos": int(round(_segundos(aberta["entrada"], fim[0]))),
            }
        )
        aberta = None

    def abrir(area: dict[str, Any], ponto: tuple[str, float, float]) -> None:
        nonlocal aberta
        emitir("entrada", area, ponto)
        aberta = {"area": area, "entrada": ponto[0], "ponto": ponto}

    for ponto in ordenados:
        if anterior is not None and _gap(anterior, ponto):
            encerrar(None, "gap")
            atual = None
        area = _area_em(ponto[1], ponto[2], prontos)
        novo = area["id"] if area else ""
        velho = atual["id"] if atual else ""
        if novo != velho:
            if atual is not None:
                encerrar(ponto, "saida")
            atual = area
            if area is not None:
                abrir(area, ponto)
        elif aberta is not None:
            aberta["ponto"] = ponto
        anterior = ponto
    area_atual = ""
    area_id = ""
    if aberta is not None:
        fim = aberta["ponto"]
        permanencias.append(
            {
                "veiculo": codigo,
                "placa": placa,
                "area_id": aberta["area"]["id"],
                "area": aberta["area"]["nome"],
                "data": data,
                "turno": turno,
                "entrada": aberta["entrada"],
                "fim": fim[0],
                "saida": "",
                "aberta": True,
                "segundos": int(round(_segundos(aberta["entrada"], fim[0]))),
            }
        )
        area_atual = aberta["area"]["nome"]
        area_id = aberta["area"]["id"]
    return {
        "eventos": eventos,
        "permanencias": permanencias,
        "area": area_atual,
        "area_id": area_id,
    }


def rotulo_operacional(
    pulsos: int,
    ultimo_gps: str,
    situacao: str,
    area: str,
    *,
    vivo: bool,
    momento: datetime,
) -> dict[str, str]:
    """Sem GPS e sinal velho só no turno que ainda está aberto."""
    if pulsos <= 0:
        return {"sinal": "sem_rastreamento", "aviso": SEM_RASTREAMENTO, "rotulo": SEM_RASTREAMENTO}
    from .live_sync import classificar_telemetria

    if vivo and classificar_telemetria(ultimo_gps, momento) != "atualizado":
        rotulo = SINAL_DESATUALIZADO if not area else f"{SINAL_DESATUALIZADO} · {area}"
        return {"sinal": "desatualizado", "aviso": SINAL_DESATUALIZADO, "rotulo": rotulo}
    if area:
        return {
            "sinal": "atualizado" if vivo else "historico",
            "aviso": "",
            "rotulo": f"Permanência em {area}",
        }
    return {
        "sinal": "atualizado" if vivo else "historico",
        "aviso": "",
        "rotulo": situacao,
    }
