from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import Settings, janela_datetime, limitar_janela_api
from .gps import filtrar_pontos_periodo, haversine_m, utc_para_brt_texto

APARECIDA_LAT = (-16.98, -16.68)
APARECIDA_LON = (-49.42, -49.05)
TIPOS_CAMINHAO = {3, 9, 12, 25, 28}
RAIO_BASE_M = 800.0


def _req(method: str, url: str, headers: dict[str, str], data: bytes | None = None, timeout: int = 90) -> Any:
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as resp:
            raw = resp.read()
    except HTTPError as exc:
        detalhe = exc.read()[:400].decode("utf-8", "replace")
        raise RuntimeError(f"Fulltrack {exc.code} {url.split('ftdata.com.br', 1)[-1]}: {detalhe[:180]}") from exc
    except URLError as exc:
        raise RuntimeError(f"Fulltrack sem conexão: {exc.reason}") from exc
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8", "replace"))


BRT = timezone(timedelta(hours=-3))


def _unix(dt: datetime) -> int:
    local = dt.replace(tzinfo=BRT) if dt.tzinfo is None else dt
    return int(local.timestamp())


def _norm_placa(placa: str) -> str:
    return "".join(ch for ch in (placa or "").upper() if ch.isalnum())


def _fmt_placa(placa: str) -> str:
    chave = _norm_placa(placa)
    if len(chave) == 7:
        return f"{chave[:3]}-{chave[3:]}"
    return (placa or chave).upper()


def _em_aparecida(lat: Any, lon: Any) -> bool:
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
        raise RuntimeError("Fulltrack login sem access_token")
    return str(token)


def _headers(token: str) -> dict[str, str]:
    return {"Accept": "application/json", "Authorization": "Bearer " + token}


def listar_veiculos(settings: Settings, token: str | None = None) -> list[dict[str, Any]]:
    at = token or login(settings)
    corpo = _req("GET", settings.fulltrack_base_url.rstrip("/") + "/vehicles/all", _headers(at))
    data = corpo.get("data") if isinstance(corpo, dict) else corpo
    return [item for item in (data or []) if isinstance(item, dict)]


def _eventos_atuais(settings: Settings, token: str) -> list[dict[str, Any]]:
    corpo = _req("GET", settings.fulltrack_base_url.rstrip("/") + "/events/all", _headers(token))
    data = corpo.get("data") if isinstance(corpo, dict) else corpo
    return [item for item in (data or []) if isinstance(item, dict)]


def historico_veiculo(
    settings: Settings,
    vehicle_id: str,
    inicio: datetime,
    fim: datetime,
    token: str | None = None,
) -> list[dict[str, Any]]:
    at = token or login(settings)
    begin, end = _unix(inicio), _unix(fim)
    if end <= begin:
        raise RuntimeError("Janela Fulltrack inválida: data/hora final menor que a inicial")
    path = f"/events/interval/id/{vehicle_id}/begin/{begin}/end/{end}"
    corpo = _req("GET", settings.fulltrack_base_url.rstrip("/") + path, _headers(at))
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
    ts = str(evento.get("ras_eve_data_gps") or evento.get("ras_eve_data_enviado") or "")
    return {
        "veiculo": veiculo,
        "timestamp": utc_para_brt_texto(ts) if ts else "",
        "latitude": lat,
        "longitude": lon,
        "velocidade": evento.get("ras_eve_velocidade") or "",
        "endereco": "",
        "fonte": "fulltrack-api",
        "tz": "BRT",
    }


def _placa_liberada(placa: str, filtro: tuple[str, ...]) -> bool:
    if not filtro:
        return True
    chave = _norm_placa(placa)
    return any(_norm_placa(item) == chave for item in filtro)


def _placas_da_frota(settings: Settings, placas_inlog: set[str] | None) -> set[str]:
    placas = set(placas_inlog or ())
    for item in settings.argos_veiculos or ():
        chave = _norm_placa(item)
        if chave:
            placas.add(chave)
    return placas


def _pares_latlon(pontos: list[dict[str, Any]]) -> list[tuple[float, float]]:
    pares: list[tuple[float, float]] = []
    for ponto in pontos or []:
        try:
            pares.append((float(ponto["latitude"]), float(ponto["longitude"])))
        except (KeyError, TypeError, ValueError):
            continue
    return pares


def _deslocamento_m(pontos: list[dict[str, Any]]) -> float:
    pares = _pares_latlon(pontos)
    if not pares:
        return 0.0
    amostra = pares[: min(40, len(pares))]
    origem = (
        sum(p[0] for p in amostra) / len(amostra),
        sum(p[1] for p in amostra) / len(amostra),
    )
    return max(haversine_m(origem, par) for par in pares)


def _saiu_da_base(pontos: list[dict[str, Any]]) -> bool:
    return _deslocamento_m(pontos) >= RAIO_BASE_M


def _modelo_compactador(item: dict[str, Any]) -> bool:
    texto = f"{item.get('ras_vei_modelo') or ''} {item.get('ras_vei_veiculo') or ''}".upper()
    compacto = texto.replace(" ", "").replace("/", "").replace("-", "").replace(".", "")
    return "18260" in compacto or "17260" in compacto


def _compactador_rsd_aparecida(
    item: dict[str, Any],
    evento: dict[str, Any] | None,
    placas_inlog: set[str] | None = None,
) -> bool:
    """Compactador de Aparecida: frota Inlog (CCL) ou VW 17.260/18.260. Sem Iveco nem Argos puro."""
    placa = _norm_placa(str(item.get("ras_vei_placa") or ""))
    if not placa:
        return False
    if placas_inlog and placa in placas_inlog:
        return True
    if placa.startswith("RB"):
        return False
    try:
        tipo = int(item.get("ras_vei_tipo") or 0)
    except (TypeError, ValueError):
        tipo = 0
    if tipo not in TIPOS_CAMINHAO:
        return False
    if not _modelo_compactador(item):
        return False
    lat = evento.get("ras_eve_latitude") if evento else None
    lon = evento.get("ras_eve_longitude") if evento else None
    if _em_aparecida(lat, lon):
        return True
    return placa.startswith("SD") or placa.startswith("SCW") or placa.startswith("SCH")


def _escolher_veiculos(
    veiculos: list[dict[str, Any]],
    eventos: list[dict[str, Any]],
    filtro: tuple[str, ...],
    placas_frota: set[str] | None = None,
) -> list[dict[str, Any]]:
    por_placa: dict[str, dict[str, Any]] = {}
    for item in veiculos:
        chave = _norm_placa(str(item.get("ras_vei_placa") or ""))
        if chave and chave not in por_placa:
            por_placa[chave] = item
    if filtro:
        alvos = {_norm_placa(item) for item in filtro if _norm_placa(item)}
    else:
        alvos = set(placas_frota or ())
        por_equip: dict[str, dict[str, Any]] = {}
        for ev in eventos:
            eid = str(ev.get("ras_ras_id") or "")
            if eid:
                por_equip[eid] = ev
        for item in veiculos:
            chave = _norm_placa(str(item.get("ras_vei_placa") or ""))
            if not chave or chave in alvos:
                continue
            ev = por_equip.get(str(item.get("ras_vei_equipamento") or ""))
            if _compactador_rsd_aparecida(item, ev, placas_frota):
                alvos.add(chave)
    escolhidos: list[dict[str, Any]] = []
    vistos: set[str] = set()
    for placa in sorted(alvos):
        item = por_placa.get(placa)
        if not item or placa in vistos:
            continue
        vistos.add(placa)
        escolhidos.append(item)
    return escolhidos


def coletar_percursos(
    settings: Settings,
    dia: date | None = None,
    on_progress: Any | None = None,
) -> list[dict[str, Any]]:
    dia = dia or settings.data_rota
    inicio, fim = limitar_janela_api(*janela_datetime(dia, settings.hora_inicio, settings.hora_fim, settings.data_fim))
    token = login(settings)
    veiculos = listar_veiculos(settings, token)
    eventos = []
    try:
        eventos = _eventos_atuais(settings, token)
    except Exception:
        eventos = []
    placas_inlog: set[str] = set()
    try:
        from .relatorio_diario import mapa_frota_inlog

        placas_inlog = set(mapa_frota_inlog(settings.output_dir, dia).keys())
    except Exception:
        placas_inlog = set()
    placas_frota = _placas_da_frota(settings, placas_inlog)
    alvos = _escolher_veiculos(veiculos, eventos, settings.segtrack_veiculos, placas_frota)
    if on_progress:
        on_progress(f"Fulltrack API: {len(alvos)} placa(s) da frota para o histórico.")
        if not settings.segtrack_veiculos:
            achadas = {_norm_placa(str(item.get("ras_vei_placa") or "")) for item in alvos}
            faltando = [placa for placa in sorted(placas_frota) if placa not in achadas]
            if faltando:
                on_progress("Placas da frota sem cadastro Segtrack: " + ", ".join(faltando))

    def um(item: dict[str, Any]) -> dict[str, Any]:
        placa = _fmt_placa(str(item.get("ras_vei_placa") or ""))
        vid = str(item.get("ras_vei_id") or "")
        eventos_hist: list[dict[str, Any]] = []
        if vid:
            try:
                eventos_hist = historico_veiculo(settings, vid, inicio, fim, token=token)
            except RuntimeError as exc:
                if "No data" not in str(exc) and "400" not in str(exc):
                    raise
                eventos_hist = []
        pontos_brutos = [_ponto(ev, placa) for ev in eventos_hist]
        pontos = filtrar_pontos_periodo(
            [p for p in pontos_brutos if p],
            dia,
            settings.hora_inicio,
            settings.hora_fim,
            data_fim=settings.data_fim,
        )
        return {
            "veiculo": item.get("ras_vei_veiculo") or placa,
            "placa": placa,
            "descricao": item.get("ras_vei_veiculo") or placa,
            "codigo_veiculo": vid,
            "equipamento": item.get("ras_vei_equipamento"),
            "setor": "APARECIDA DE GOIANIA - DOMICILIAR - SEGTRACK",
            "rota": "Percurso do dia",
            "fonte": "segtrack",
            "total_pontos": len(pontos),
            "pontos": pontos,
            "rota_planejada": [],
            "trechos_feitos": [],
            "trechos_faltando": [],
            "trechos_planejados": [],
            "trechos_gps": [],
            "poligonos": [],
        }

    rotas: list[dict[str, Any]] = []
    if not alvos:
        return rotas
    with ThreadPoolExecutor(max_workers=min(8, len(alvos))) as pool:
        futuros = {pool.submit(um, item): item for item in alvos}
        for fut in as_completed(futuros):
            try:
                rota = fut.result()
            except Exception as exc:
                if on_progress:
                    on_progress(f"  histórico falhou: {exc}")
                continue
            rotas.append(rota)
            if on_progress:
                on_progress(f"  {rota.get('placa')}: {rota.get('total_pontos')} pontos GPS")
    rotas.sort(key=lambda item: str(item.get("placa") or ""))
    saida: list[dict[str, Any]] = []
    for rota in rotas:
        pontos = rota.get("pontos") or []
        dist = _deslocamento_m(pontos)
        rota["deslocamento_m"] = round(dist)
        rota["saiu_da_base"] = dist >= RAIO_BASE_M
        placa = rota.get("placa") or ""
        if not rota["saiu_da_base"]:
            if on_progress:
                on_progress(
                    f"  {placa}: {rota.get('total_pontos') or 0} pts, "
                    f"não saiu da base ({dist:.0f} m) — ignorado"
                )
            continue
        saida.append(rota)
    return saida
