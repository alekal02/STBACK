from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import Settings, janela_datetime, limitar_janela_api
from .gps import extract_points, filtrar_pontos_periodo


def _req(method: str, url: str, headers: dict[str, str], data: bytes | None = None, timeout: int = 60) -> Any:
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as resp:
            raw = resp.read()
    except HTTPError as exc:
        detalhe = exc.read()[:400].decode("utf-8", "replace")
        raise RuntimeError(f"Inlog Coleta {exc.code}: {detalhe[:180]}") from exc
    except URLError as exc:
        raise RuntimeError(f"Inlog Coleta sem conexão: {exc.reason}") from exc
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8", "replace"))


def token(settings: Settings) -> str:
    if not settings.inlog_coleta_client_id or not settings.inlog_coleta_client_secret:
        raise RuntimeError("Preencha INLOG_COLETA_CLIENT_ID e INLOG_COLETA_CLIENT_SECRET no .env")
    corpo = _req(
        "POST",
        settings.inlog_coleta_token_url,
        {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        urlencode(
            {
                "grant_type": settings.inlog_coleta_grant_type or "client_credentials",
                "client_id": settings.inlog_coleta_client_id,
                "client_secret": settings.inlog_coleta_client_secret,
                "scope": settings.inlog_coleta_scope,
            }
        ).encode(),
        timeout=30,
    )
    acesso = (corpo or {}).get("access_token")
    if not acesso:
        raise RuntimeError("Inlog Coleta login sem access_token")
    return str(acesso)


def _headers(acesso: str) -> dict[str, str]:
    return {"Accept": "application/json", "Authorization": "Bearer " + acesso}


def _data(corpo: Any) -> list[Any]:
    if isinstance(corpo, dict):
        bruto = corpo.get("data")
        return bruto if isinstance(bruto, list) else []
    return corpo if isinstance(corpo, list) else []


def listar_veiculos(settings: Settings, acesso: str | None = None) -> list[dict[str, Any]]:
    at = acesso or token(settings)
    corpo = _req("GET", settings.inlog_coleta_base_url.rstrip("/") + "/api/Veiculos", _headers(at))
    return [item for item in _data(corpo) if isinstance(item, dict)]


def tempo_real(settings: Settings, acesso: str | None = None) -> list[dict[str, Any]]:
    at = acesso or token(settings)
    corpo = _req("GET", settings.inlog_coleta_base_url.rstrip("/") + "/api/Veiculos/TempoReal", _headers(at))
    return [item for item in _data(corpo) if isinstance(item, dict)]


def historico_placa(
    settings: Settings,
    placa: str,
    inicio: datetime,
    fim: datetime,
    acesso: str | None = None,
) -> list[dict[str, Any]]:
    at = acesso or token(settings)
    qs = urlencode(
        {
            "placa": placa,
            "dataInicio": inicio.strftime("%Y-%m-%dT%H:%M:%S"),
            "dataFim": fim.strftime("%Y-%m-%dT%H:%M:%S"),
        }
    )
    corpo = _req(
        "GET",
        settings.inlog_coleta_base_url.rstrip("/") + "/api/Historico?" + qs,
        _headers(at),
    )
    return [item for item in _data(corpo) if isinstance(item, dict)]


def _limpo(valor: Any) -> str:
    return str(valor or "").replace("\r", "").replace("\n", "").strip()


def _liberado(item: dict[str, Any], filtro: tuple[str, ...]) -> bool:
    if not filtro:
        return True
    blob = " ".join(
        _limpo(item.get(chave)) for chave in ("identificador", "placa", "tipo", "modelo")
    ).upper()
    return any(parte.upper() in blob for parte in filtro)


def coletar_percursos(
    settings: Settings,
    dia: date | None = None,
    on_progress: Any | None = None,
) -> list[dict[str, Any]]:
    dia = dia or settings.data_rota
    inicio, fim = limitar_janela_api(*janela_datetime(dia, settings.hora_inicio, settings.hora_fim, settings.data_fim))
    acesso = token(settings)
    veiculos = [
        item for item in listar_veiculos(settings, acesso) if _liberado(item, settings.filtro_veiculos)
    ]
    if on_progress:
        on_progress(f"Inlog Coleta API: {len(veiculos)} compactadores no Consórcio Aparecida.")
    rotas: list[dict[str, Any]] = []

    def _uma(item: dict[str, Any]) -> dict[str, Any]:
        placa = _limpo(item.get("placa"))
        ident = _limpo(item.get("identificador")) or placa
        eventos = historico_placa(settings, placa, inicio, fim, acesso=acesso) if placa else []
        if not eventos and ident and ident != placa:
            eventos = historico_placa(settings, ident, inicio, fim, acesso=acesso)
        pontos = extract_points(eventos, veiculo=ident, fonte="inlog-coleta-api")
        for ponto in pontos:
            ponto["placa"] = placa
            ponto["veiculo"] = ident
        pontos = filtrar_pontos_periodo(
            pontos, dia, settings.hora_inicio, settings.hora_fim, data_fim=settings.data_fim
        )
        return {
            "veiculo": ident,
            "placa": placa,
            "descricao": _limpo(item.get("modelo")) or ident,
            "setor": "APARECIDA DE GOIANIA - DOMICILIAR - INLOG",
            "rota": "Percurso do dia",
            "macro": "",
            "codigo_rota": None,
            "codigo_veiculo": _limpo(item.get("numeroSerieRastreador")),
            "percentual_concluido": 0,
            "veredicto": "SEM RASTREIO" if not pontos else "NÃO FEITA",
            "confianca": "baixa",
            "fonte": "inlog-coleta-api",
            "km_dia": 0,
            "total_pontos": len(pontos),
            "pontos": pontos,
            "rota_planejada": [],
            "trechos_feitos": [],
            "trechos_faltando": [],
            "trechos_planejados": [],
            "trechos_gps": [],
            "poligonos": [],
        }

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(veiculos)))) as pool:
        futuros = [pool.submit(_uma, item) for item in veiculos]
        for fut in as_completed(futuros):
            rota = fut.result()
            rotas.append(rota)
            if on_progress:
                on_progress(f"  {rota.get('veiculo')}: {rota.get('total_pontos')} pontos GPS")
    rotas.sort(key=lambda item: str(item.get("veiculo") or ""))
    return rotas
