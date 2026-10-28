"""Eventos de condução estimados pelos pulsos, na mesma régua de 10 s.

Frenagem, aceleração e curva comparam janelas fixas, não o pulso cru.
Excesso e motor ocioso continuam no pulso. Alerta da API, quando existe,
substitui o estimado da mesma família naquele caminhão.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

JANELA_S = 10
LACUNA_JANELA_S = 15
LIMITE_VELOCIDADE_KMH = 60
FRENAGEM_QUEDA_KMH = 30
ACELERACAO_SUBIDA_KMH = 25
CURVA_GRAUS = 70
CURVA_VEL_MIN_KMH = 25
OCIOSO_VEL_MAX_KMH = 3
OCIOSO_MIN_S = 5 * 60
LACUNA_EXCESSO_S = 15
LACUNA_OCIOSO_S = 10 * 60

AVISO_ESTIMADO = "valores estimados pelo GPS"
ALERTA_IGNICAO = "Ignição sempre ligada (suspeita de instalação do rastreador)."

_BRT = timezone(timedelta(hours=-3))
_FAMILIA = {
    "frenagem_brusca": "frenagem",
    "aceleracao_brusca": "aceleracao",
    "curva_brusca": "curva",
    "curva_brusca_esquerda": "curva",
    "curva_brusca_direita": "curva",
}
_ROTULO = {
    "excesso_velocidade": "Excesso de velocidade",
    "motor_ocioso": "Motor ocioso",
    "frenagem_brusca": "Frenagem brusca",
    "aceleracao_brusca": "Aceleração brusca",
    "curva_brusca": "Curva brusca",
    "curva_brusca_esquerda": "Curva brusca à esquerda",
    "curva_brusca_direita": "Curva brusca à direita",
}
_CHAVES_TIPO = (
    "tipo",
    "type",
    "evento",
    "nome",
    "descricao",
    "descrição",
    "ras_ale_tipo",
    "ras_ale_descricao",
    "ras_ale_desc",
    "alert_type",
    "alert",
)
_CHAVES_PLACA = ("placa", "ras_vei_placa", "tag", "tag_ativo", "veiculo_placa")
_CHAVES_HORA = (
    "data_hora",
    "data",
    "timestamp",
    "dt",
    "dt_gps",
    "ras_ale_data",
    "ras_ale_data_gps",
    "ras_eve_data_gps",
)
_CHAVES_VEL = ("velocidade", "vel", "ras_eve_velocidade", "vl_velocidade", "ras_ale_velocidade")
_CHAVES_LAT = ("lat", "latitude", "ras_eve_latitude", "ras_ale_latitude")
_CHAVES_LON = ("lon", "lng", "longitude", "ras_eve_longitude", "ras_ale_longitude")


def limite_kmh(valor: Any) -> float:
    """Limite do cadastro quando é positivo. Zero e vazio caem no padrão."""
    numero = _num(valor)
    if numero is None or numero <= 0:
        return float(LIMITE_VELOCIDADE_KMH)
    return numero


def diferenca_rumo(origem: float, destino: float) -> float:
    """Menor giro, de -180 a 180. Positivo é à direita (rumo cresce no sentido horário)."""
    return (destino - origem + 180.0) % 360.0 - 180.0


def analisar(
    pontos: list[dict[str, Any]] | None,
    limite: Any = None,
    alertas: list[dict[str, Any]] | None = None,
    ocioso_api: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Contagem, lista e ociosidade de um caminhão já recortado no turno."""
    lista = _pontos(pontos)
    teto = limite_kmh(limite)
    estimados = _excesso(lista, teto) + _ocioso(lista) + _bruscos(lista)
    prontos = [item for item in (alertas or []) if item.get("origem") == "api"]
    eventos = _mesclar(estimados, prontos)
    eventos.sort(key=lambda item: (item.get("data_hora") or "", item.get("tipo") or ""))
    contagem: dict[str, int] = {chave: 0 for chave in _ROTULO}
    for item in eventos:
        tipo = str(item.get("tipo") or "")
        contagem[tipo] = contagem.get(tipo, 0) + 1
    ocioso = _resumo_ocioso(lista, eventos, ocioso_api)
    aviso = AVISO_ESTIMADO if any(item.get("origem") == "estimado" for item in eventos) else ""
    return {
        "limite_kmh": teto,
        "ignicao_sempre_ligada": _ignicao_sempre_ligada(lista),
        "contagem": contagem,
        "eventos": eventos,
        "ocioso": ocioso,
        "aviso": aviso,
    }


def normalizar_alerta(item: dict[str, Any]) -> dict[str, Any] | None:
    """Traduz um alerta da Fulltrack. Tipo desconhecido ou sem placa não entra."""
    if not isinstance(item, dict):
        return None
    texto = " ".join(str(item.get(chave) or "") for chave in _CHAVES_TIPO)
    tipo = _tipo_texto(texto)
    if not tipo:
        return None
    placa = ""
    for chave in _CHAVES_PLACA:
        placa = str(item.get(chave) or "").strip()
        if placa:
            break
    if not placa:
        return None
    momento = None
    for chave in _CHAVES_HORA:
        momento = _momento(item.get(chave))
        if momento is not None:
            break
    if momento is None:
        return None
    lat = _primeiro_num(item, _CHAVES_LAT)
    lon = _primeiro_num(item, _CHAVES_LON)
    vel = _primeiro_num(item, _CHAVES_VEL)
    return _evento(
        tipo,
        momento,
        vel,
        lat,
        lon,
        "api",
        placa=placa,
    )


def _mesclar(estimados: list[dict[str, Any]], prontos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    familias = {_FAMILIA.get(str(item.get("tipo") or "")) for item in prontos}
    familias.discard(None)
    saida = [
        item
        for item in estimados
        if _FAMILIA.get(str(item.get("tipo") or "")) not in familias
    ]
    saida.extend(prontos)
    return saida


def _ignicao_sempre_ligada(pontos: list[dict[str, Any]]) -> bool:
    if not pontos:
        return False
    return all(item.get("ignicao") is True for item in pontos)


def _excesso(pontos: list[dict[str, Any]], teto: float) -> list[dict[str, Any]]:
    eventos: list[dict[str, Any]] = []
    grupo: list[dict[str, Any]] = []

    def fechar() -> None:
        if not grupo:
            return
        pico = max(grupo, key=lambda item: float(item["vel"]))
        eventos.append(
            _evento(
                "excesso_velocidade",
                grupo[0]["ts"],
                pico["vel"],
                pico["lat"],
                pico["lon"],
                "estimado",
                fim=grupo[-1]["ts"],
            )
        )
        grupo.clear()

    anterior: dict[str, Any] | None = None
    for ponto in pontos:
        vel = ponto.get("vel")
        acima = vel is not None and float(vel) > teto
        if acima and anterior is not None and grupo:
            if (ponto["ts"] - anterior["ts"]).total_seconds() > LACUNA_EXCESSO_S:
                fechar()
        if acima:
            grupo.append(ponto)
        else:
            fechar()
        anterior = ponto
    fechar()
    return eventos


def _ocioso(pontos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    eventos: list[dict[str, Any]] = []
    grupo: list[dict[str, Any]] = []

    def fechar() -> None:
        if len(grupo) < 2:
            grupo.clear()
            return
        duracao = (grupo[-1]["ts"] - grupo[0]["ts"]).total_seconds()
        if duracao > OCIOSO_MIN_S:
            eventos.append(
                _evento(
                    "motor_ocioso",
                    grupo[0]["ts"],
                    max(float(item["vel"]) for item in grupo),
                    grupo[0]["lat"],
                    grupo[0]["lon"],
                    "estimado",
                    fim=grupo[-1]["ts"],
                )
            )
        grupo.clear()

    anterior: dict[str, Any] | None = None
    for ponto in pontos:
        vel = ponto.get("vel")
        parado = (
            vel is not None
            and float(vel) <= OCIOSO_VEL_MAX_KMH
            and ponto.get("ignicao") is True
        )
        if parado and anterior is not None and grupo:
            if (ponto["ts"] - anterior["ts"]).total_seconds() > LACUNA_OCIOSO_S:
                fechar()
        if parado:
            grupo.append(ponto)
        else:
            fechar()
        anterior = ponto
    fechar()
    return eventos


def _bruscos(pontos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    janelas = _reamostrar(pontos)
    eventos: list[dict[str, Any]] = []
    for a, b in zip(janelas, janelas[1:]):
        if (b["primeiro"] - a["ultimo"]).total_seconds() > LACUNA_JANELA_S:
            continue
        dv = b["vel"] - a["vel"]
        if dv <= -FRENAGEM_QUEDA_KMH:
            eventos.append(
                _evento("frenagem_brusca", b["primeiro"], a["vel"], b["lat"], b["lon"], "estimado", variacao=dv)
            )
        if dv >= ACELERACAO_SUBIDA_KMH:
            eventos.append(
                _evento("aceleracao_brusca", b["primeiro"], b["vel"], b["lat"], b["lon"], "estimado", variacao=dv)
            )
        if (
            a["rumo"] is None
            or b["rumo"] is None
            or a["vel"] < CURVA_VEL_MIN_KMH
            or b["vel"] < CURVA_VEL_MIN_KMH
        ):
            continue
        giro = diferenca_rumo(a["rumo"], b["rumo"])
        if abs(giro) < CURVA_GRAUS:
            continue
        tipo = "curva_brusca_direita" if giro > 0 else "curva_brusca_esquerda"
        eventos.append(
            _evento(tipo, b["primeiro"], b["vel"], b["lat"], b["lon"], "estimado", variacao=giro)
        )
    return eventos


def _reamostrar(pontos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    baldes: dict[int, list[dict[str, Any]]] = {}
    for ponto in pontos:
        if ponto.get("vel") is None:
            continue
        chave = int(_epoch(ponto["ts"]) // JANELA_S)
        baldes.setdefault(chave, []).append(ponto)
    janelas: list[dict[str, Any]] = []
    for chave in sorted(baldes):
        grupo = baldes[chave]
        rumos = [float(item["rumo"]) for item in grupo if item.get("rumo") is not None]
        janelas.append(
            {
                "vel": sum(float(item["vel"]) for item in grupo) / len(grupo),
                "rumo": _rumo_medio(rumos),
                "lat": sum(item["lat"] for item in grupo) / len(grupo),
                "lon": sum(item["lon"] for item in grupo) / len(grupo),
                "primeiro": grupo[0]["ts"],
                "ultimo": grupo[-1]["ts"],
            }
        )
    return janelas


def _rumo_medio(graus: list[float]) -> float | None:
    if not graus:
        return None
    seno = sum(math.sin(math.radians(item)) for item in graus)
    cosseno = sum(math.cos(math.radians(item)) for item in graus)
    if abs(seno) < 1e-9 and abs(cosseno) < 1e-9:
        return graus[-1] % 360.0
    return math.degrees(math.atan2(seno, cosseno)) % 360.0


def _resumo_ocioso(
    pontos: list[dict[str, Any]],
    eventos: list[dict[str, Any]],
    ocioso_api: dict[str, Any] | None,
) -> dict[str, Any]:
    episodios = [item for item in eventos if item.get("tipo") == "motor_ocioso"]
    minutos_ep = round(sum(float(item.get("duracao_min") or 0) for item in episodios), 1)
    ocioso_min = _num(None if not ocioso_api else ocioso_api.get("ocioso_min"))
    ligado_min = _num(None if not ocioso_api else ocioso_api.get("ligado_min"))
    if ocioso_min is not None and ligado_min is not None and ligado_min > 0:
        return {
            "tempo_min": round(ocioso_min, 1),
            "tempo_ligado_min": round(ligado_min, 1),
            "percentual": round(ocioso_min / ligado_min * 100.0, 1),
            "origem": "api",
            "episodios": len(episodios),
            "episodios_min": minutos_ep,
        }
    ligado = _tempo_ligado_min(pontos)
    percentual = round(minutos_ep / ligado * 100.0, 1) if ligado > 0 else None
    return {
        "tempo_min": minutos_ep,
        "tempo_ligado_min": round(ligado, 1) if ligado else None,
        "percentual": percentual,
        "origem": "estimado",
        "episodios": len(episodios),
        "episodios_min": minutos_ep,
    }


def _tempo_ligado_min(pontos: list[dict[str, Any]]) -> float:
    total = 0.0
    for a, b in zip(pontos, pontos[1:]):
        if a.get("ignicao") is not True or b.get("ignicao") is not True:
            continue
        dt = (b["ts"] - a["ts"]).total_seconds()
        if 0 < dt <= LACUNA_OCIOSO_S:
            total += dt
    return total / 60.0


def _evento(
    tipo: str,
    quando: datetime,
    velocidade: Any,
    lat: Any,
    lon: Any,
    origem: str,
    *,
    fim: datetime | None = None,
    variacao: float | None = None,
    placa: str = "",
) -> dict[str, Any]:
    inicio = _iso(quando)
    termino = _iso(fim or quando)
    duracao = 0.0
    if fim is not None:
        duracao = max(0.0, (fim - quando).total_seconds()) / 60.0
    la = None if lat is None else round(float(lat), 6)
    lo = None if lon is None else round(float(lon), 6)
    vel = None if velocidade is None else round(float(velocidade), 1)
    corpo = {
        "tipo": tipo,
        "rotulo": _ROTULO.get(tipo, tipo),
        "data_hora": inicio,
        "fim": termino,
        "duracao_min": round(duracao, 1),
        "velocidade": vel,
        "lat": la,
        "lon": lo,
        "endereco": "",
        "link": _link(la, lo),
        "origem": origem,
    }
    if variacao is not None:
        corpo["variacao"] = round(float(variacao), 1)
    if placa:
        corpo["placa"] = placa
    return corpo


def _link(lat: Any, lon: Any) -> str:
    if lat is None or lon is None:
        return ""
    return f"https://www.google.com/maps?q={lat},{lon}"


def _pontos(brutos: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    saida: list[dict[str, Any]] = []
    for bruto in brutos or []:
        if not isinstance(bruto, dict):
            continue
        momento = _momento(bruto.get("ts") if "ts" in bruto else bruto.get("timestamp"))
        if momento is None:
            continue
        lat = _num(bruto.get("lat") if "lat" in bruto else bruto.get("latitude"))
        lon = _num(bruto.get("lon") if "lon" in bruto else bruto.get("longitude"))
        if lat is None or lon is None:
            continue
        rumo = _num(bruto.get("direcao") if "direcao" in bruto else bruto.get("rumo"))
        if rumo is None:
            rumo = _num(bruto.get("ras_eve_direcao"))
        saida.append(
            {
                "ts": momento,
                "lat": lat,
                "lon": lon,
                "vel": _num(bruto.get("vel") if "vel" in bruto else bruto.get("velocidade")),
                "ignicao": _ign(bruto.get("ignicao")),
                "rumo": None if rumo is None else rumo % 360.0,
            }
        )
    saida.sort(key=lambda item: item["ts"])
    return saida


def _tipo_texto(texto: str) -> str | None:
    cru = (texto or "").lower()
    if any(chave in cru for chave in ("frenag", "freada", "frenada", "brak")):
        return "frenagem_brusca"
    if any(chave in cru for chave in ("acelera", "acceler")):
        return "aceleracao_brusca"
    if "curva" in cru or "curve" in cru:
        if "esquer" in cru or "left" in cru:
            return "curva_brusca_esquerda"
        if "direit" in cru or "right" in cru:
            return "curva_brusca_direita"
        return "curva_brusca"
    return None


def _primeiro_num(item: dict[str, Any], chaves: tuple[str, ...]) -> float | None:
    for chave in chaves:
        if chave in item:
            numero = _num(item.get(chave))
            if numero is not None:
                return numero
    return None


def _ign(valor: Any) -> bool | None:
    if valor is None or valor == "":
        return None
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        if valor == 1:
            return True
        if valor == 0:
            return False
        return None
    texto = str(valor).strip().lower()
    if texto in {"1", "true", "on", "ligada", "sim"}:
        return True
    if texto in {"0", "false", "off", "desligada", "nao", "não"}:
        return False
    return None


def _num(valor: Any) -> float | None:
    if valor is None or valor == "":
        return None
    try:
        return float(str(valor).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _momento(valor: Any) -> datetime | None:
    if isinstance(valor, datetime):
        if valor.tzinfo is not None:
            return valor.astimezone(_BRT).replace(tzinfo=None, microsecond=0)
        return valor.replace(microsecond=0)
    texto = str(valor or "").strip()
    if not texto:
        return None
    if texto.endswith("Z") or texto.endswith("z"):
        texto = texto[:-1]
    texto = texto.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(texto[:19] if " " in fmt and len(texto) >= 19 else texto, fmt)
        except ValueError:
            continue
    return None


def _iso(momento: datetime) -> str:
    return momento.strftime("%Y-%m-%dT%H:%M:%S")


def _epoch(momento: datetime) -> float:
    return momento.replace(tzinfo=_BRT).timestamp()
