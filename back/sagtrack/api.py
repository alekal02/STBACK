from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import Settings, janela_datetime
from .frota import placas_da_frota, prefixo_da_placa
from .gps import _datetime_do_timestamp, filtrar_pontos_periodo, ordem_do_pulso, para_horario_brasilia

APARECIDA_LAT = (-16.98, -16.68)
APARECIDA_LON = (-49.42, -49.05)
BRT = timezone(timedelta(hours=-3))


def _req(method: str, url: str, headers: dict[str, str], data: bytes | None = None, timeout: int = 90) -> Any:
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as resp:
            raw = resp.read()
    except HTTPError as exc:
        detalhe = exc.read()[:400].decode("utf-8", "replace")
        raise RuntimeError(f"Segtrack {exc.code}: {detalhe[:180]}") from exc
    except URLError as exc:
        raise RuntimeError(f"Segtrack sem conexão: {exc.reason}") from exc
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8", "replace"))


def _unix(dt: datetime) -> int:
    local = dt.replace(tzinfo=BRT) if dt.tzinfo is None else dt
    return int(local.timestamp())


def norm_placa(placa: str) -> str:
    return "".join(ch for ch in (placa or "").upper() if ch.isalnum())


def fmt_placa(placa: str) -> str:
    chave = norm_placa(placa)
    if len(chave) == 7:
        return f"{chave[:3]}-{chave[3:]}"
    return (placa or chave).upper()


def em_aparecida(lat: Any, lon: Any) -> bool:
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    return APARECIDA_LAT[0] <= la <= APARECIDA_LAT[1] and APARECIDA_LON[0] <= lo <= APARECIDA_LON[1]


def login(settings: Settings) -> str:
    if not settings.fulltrack_username or not settings.fulltrack_password:
        raise RuntimeError("Preencha FULLTRACK_USERNAME e FULLTRACK_PASSWORD no .env")
    corpo = _req(
        "POST",
        settings.fulltrack_base_url.rstrip("/") + "/auth/login",
        {"Content-Type": "application/json", "Accept": "application/json"},
        json.dumps(
            {
                "username": settings.fulltrack_username,
                "password": settings.fulltrack_password,
                "client_id": settings.fulltrack_client_id,
                "api_key": settings.fulltrack_api_key,
                "secret_key": settings.fulltrack_secret_key,
            }
        ).encode(),
        timeout=30,
    )
    token = (corpo or {}).get("access_token")
    if not token:
        raise RuntimeError("Login Segtrack sem access_token")
    return str(token)


def _headers(token: str) -> dict[str, str]:
    return {"Accept": "application/json", "Authorization": "Bearer " + token}


def listar_veiculos(settings: Settings, token: str) -> list[dict[str, Any]]:
    corpo = _req("GET", settings.fulltrack_base_url.rstrip("/") + "/vehicles/all", _headers(token))
    data = corpo.get("data") if isinstance(corpo, dict) else corpo
    return [item for item in (data or []) if isinstance(item, dict)]


def eventos_atuais(settings: Settings, token: str) -> list[dict[str, Any]]:
    corpo = _req("GET", settings.fulltrack_base_url.rstrip("/") + "/events/all", _headers(token))
    data = corpo.get("data") if isinstance(corpo, dict) else corpo
    return [item for item in (data or []) if isinstance(item, dict)]


def historico_veiculo(
    settings: Settings,
    vehicle_id: str,
    inicio: datetime,
    fim: datetime,
    token: str,
) -> list[dict[str, Any]]:
    begin, end = _unix(inicio), _unix(fim)
    if end <= begin:
        raise RuntimeError("Janela inválida: data final menor que a inicial")
    path = f"/events/interval/id/{vehicle_id}/begin/{begin}/end/{end}"
    corpo = _req("GET", settings.fulltrack_base_url.rstrip("/") + path, _headers(token))
    data = corpo.get("data") if isinstance(corpo, dict) else corpo
    return [item for item in (data or []) if isinstance(item, dict)]


def _ponto(evento: dict[str, Any], veiculo: str) -> dict[str, Any] | None:
    try:
        lat = float(evento.get("ras_eve_latitude"))
        lon = float(evento.get("ras_eve_longitude"))
    except (TypeError, ValueError):
        return None
    if abs(lat) < 0.5 or abs(lon) < 0.5:
        return None
    return {
        "veiculo": veiculo,
        "timestamp": para_horario_brasilia(
            str(evento.get("ras_eve_data_gps") or evento.get("ras_eve_data_enviado") or "")
        ),
        "latitude": lat,
        "longitude": lon,
        "velocidade": evento.get("ras_eve_velocidade") or "",
        "fonte": "sagtrack",
    }


def _placa_liberada(placa: str, filtro: tuple[str, ...]) -> bool:
    if not filtro:
        return True
    chave = norm_placa(placa)
    return any(norm_placa(item) == chave for item in filtro)


def _hora_evento(evento: dict[str, Any] | None) -> str:
    if not evento:
        return ""
    return str(evento.get("ras_eve_data_gps") or evento.get("ras_eve_data_enviado") or "")


def escolher_frota(
    veiculos: list[dict[str, Any]],
    eventos: list[dict[str, Any]],
    filtro: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Só placas da coleta que já têm cadastro na Segtrack. Placa repetida fica com o pulso mais novo."""
    por_equip: dict[str, dict[str, Any]] = {}
    for ev in eventos:
        eid = str(ev.get("ras_ras_id") or "")
        if eid:
            por_equip[eid] = ev

    permitidas = {norm_placa(item) for item in filtro} if filtro else placas_da_frota()
    grupos: dict[str, dict[str, Any]] = {}
    for item in veiculos:
        placa_bruta = str(item.get("ras_vei_placa") or "")
        chave = norm_placa(placa_bruta)
        if not chave or chave not in permitidas or not _placa_liberada(placa_bruta, filtro):
            continue
        ev = por_equip.get(str(item.get("ras_vei_equipamento") or ""))
        candidato = {**item, "_evento": ev or {}}
        atual = grupos.get(chave)
        if atual is None or _hora_evento(candidato["_evento"]) >= _hora_evento(atual.get("_evento")):
            grupos[chave] = candidato

    escolhidos = list(grupos.values())
    escolhidos.sort(key=lambda row: norm_placa(str(row.get("ras_vei_placa") or "")))
    return escolhidos


def _ate_o_corte(ponto: dict[str, Any], ate: datetime, dia: date) -> bool:
    marca = _datetime_do_timestamp(str(ponto.get("timestamp") or ""), dia)
    if marca is None:
        return True
    return marca <= ate


def _juntar_pontos(salvos: list[dict[str, Any]], novos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    vistos: set[tuple[str, float, float]] = set()
    saida: list[dict[str, Any]] = []
    for ponto in list(salvos) + list(novos):
        try:
            chave = (
                str(ponto.get("timestamp") or ""),
                round(float(ponto["latitude"]), 5),
                round(float(ponto["longitude"]), 5),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if chave in vistos:
            continue
        vistos.add(chave)
        saida.append(ponto)
    saida.sort(key=lambda item: str(item.get("timestamp") or ""))
    return saida


def _ultimo_pulso(pontos: list[dict[str, Any]], dia: date) -> datetime | None:
    marcas = [_datetime_do_timestamp(str(ponto.get("timestamp") or ""), dia) for ponto in pontos]
    validas = [marca for marca in marcas if marca is not None]
    return max(validas) if validas else None


def coletar_percursos(
    settings: Settings,
    dia: date | None = None,
    on_progress: Any | None = None,
    ate: datetime | None = None,
    anteriores: dict[str, list] | None = None,
) -> list[dict[str, Any]]:
    dia = dia or settings.data_rota
    inicio, fim = janela_datetime(dia, settings.hora_inicio, settings.hora_fim, settings.data_fim)
    if ate is not None:
        corte = ate.replace(microsecond=0)
        if inicio <= corte < fim:
            fim = corte
    guardados = anteriores or {}
    token = login(settings)
    veiculos = listar_veiculos(settings, token)
    try:
        eventos = eventos_atuais(settings, token)
    except Exception:
        eventos = []
    alvos = escolher_frota(veiculos, eventos, settings.veiculos)
    for item in alvos:
        item["_prefixo"] = prefixo_da_placa(str(item.get("ras_vei_placa") or ""))
    if on_progress:
        on_progress(f"Segtrack em Aparecida: {len(alvos)} compactador(es) com rastreador.")

    def um(item: dict[str, Any]) -> dict[str, Any]:
        placa = fmt_placa(str(item.get("ras_vei_placa") or ""))
        vid = str(item.get("ras_vei_id") or "")
        salvos = [ponto for ponto in (guardados.get(placa) or []) if isinstance(ponto, dict)]
        ultimo = _ultimo_pulso(salvos, dia)
        evento = item.get("_evento") or {}
        atual = _datetime_do_timestamp(
            para_horario_brasilia(str(evento.get("ras_eve_data_gps") or evento.get("ras_eve_data_enviado") or "")),
            dia,
        )
        erro = ""
        if salvos and atual is not None and ultimo is not None and atual <= ultimo:
            pontos = list(salvos)
            origem = "salvo"
        else:
            inicio_busca = inicio
            if ultimo is not None:
                inicio_busca = max(inicio, ultimo - timedelta(minutes=2))
            try:
                eventos_hist = historico_veiculo(settings, vid, inicio_busca, fim, token) if vid else []
            except Exception as exc:
                eventos_hist = []
                erro = str(exc).split("\n", 1)[0][:120]
            if erro and salvos:
                pontos = list(salvos)
                origem = "falha"
            else:
                pontos_brutos = [p for p in (_ponto(ev, placa) for ev in eventos_hist) if p]
                novos = filtrar_pontos_periodo(
                    pontos_brutos,
                    dia,
                    settings.hora_inicio,
                    settings.hora_fim,
                    data_fim=settings.data_fim,
                )
                novos = [p for p in novos if "latitude" in p]
                pontos = _juntar_pontos(salvos, novos) if salvos else novos
                origem = "falha" if erro else ("novo" if not salvos else "parcial")
        if ate is not None:
            pontos = [p for p in pontos if _ate_o_corte(p, ate, dia)]
        pontos.sort(key=ordem_do_pulso)
        na_cidade = sum(1 for p in pontos if em_aparecida(p.get("latitude"), p.get("longitude")))
        return {
            "veiculo": item.get("ras_vei_veiculo") or placa,
            "prefixo": item.get("_prefixo") or prefixo_da_placa(placa),
            "placa": placa,
            "codigo_veiculo": vid,
            "setor": "",
            "rota": "",
            "fonte": "sagtrack",
            "erro": erro,
            "origem_pulso": origem,
            "total_pontos": len(pontos),
            "pontos_na_cidade": na_cidade,
            "pontos": pontos,
            "percentual_concluido": 0,
        }

    rotas: list[dict[str, Any]] = []
    if not alvos:
        return rotas
    with ThreadPoolExecutor(max_workers=min(6, len(alvos))) as pool:
        futuros = {pool.submit(um, item): item for item in alvos}
        feitos = 0
        for fut in as_completed(futuros):
            rota = fut.result()
            rotas.append(rota)
            feitos += 1
            if on_progress:
                nota = {
                    "salvo": "sem pulso novo",
                    "parcial": "só o trecho novo",
                    "novo": "turno inteiro",
                    "falha": "histórico indisponível",
                }.get(str(rota.get("origem_pulso") or ""), "")
                falha = f" · {rota.get('erro')}" if rota.get("erro") and not int(rota.get("total_pontos") or 0) else ""
                on_progress(
                    f"Pulsos {feitos}/{len(alvos)} · {rota.get('placa')}: {rota.get('total_pontos')} pontos"
                    + (f" · {nota}" if nota else "")
                    + falha
                )
    rotas = [item for item in rotas if int(item.get("pontos_na_cidade") or 0) > 0 or item.get("erro")]
    rotas.sort(key=lambda item: str(item.get("prefixo") or item.get("placa") or ""))
    if on_progress:
        na_cidade = sum(1 for item in rotas if int(item.get("pontos_na_cidade") or 0) > 0)
        on_progress(f"Com pulso em Aparecida: {na_cidade}.")
    return rotas
