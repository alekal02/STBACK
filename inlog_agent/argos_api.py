from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import Settings

AUTH_LOGIN = "https://gateway-api.argosmonitoramento.com.br/auth/usuario/login"
TEL = "https://gateway-api.argosmonitoramento.com.br/telemetria/v1"
GE_WEB_LOGIN = (
    "https://web.argosmonitoramento.com.br/ge-web/"
    "?wicket:bookmarkablePage=:com.frw.base.web.pages.LoginPage&token="
)


class ArgosError(RuntimeError):
    pass


def _request(method: str, url: str, token: str | None = None, payload: dict | None = None) -> Any:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://sso.argosmonitoramento.com.br",
        "Referer": "https://sso.argosmonitoramento.com.br/",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detalhe = exc.read().decode("utf-8", errors="replace")[:400]
        raise ArgosError(f"{method} {url} -> {exc.code}: {detalhe}") from exc
    except URLError as exc:
        raise ArgosError(f"{method} {url} falhou: {exc.reason}") from exc
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def login(settings: Settings) -> dict[str, Any]:
    if not settings.argos_user or not settings.argos_password:
        raise ArgosError("Preencha ARGOS_USER e ARGOS_PASSWORD no .env")
    body = _request(
        "POST",
        AUTH_LOGIN,
        payload={"login": settings.argos_user, "senha": settings.argos_password},
    )
    data = (body or {}).get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict) or not data.get("token"):
        raise ArgosError("O Argos não devolveu token. Confira usuário e senha.")
    urls = {item.get("nome"): item.get("url") for item in (data.get("urls") or []) if isinstance(item, dict)}
    return {
        "user_id": data.get("userId"),
        "login": data.get("login") or settings.argos_user,
        "user_name": data.get("userName") or settings.argos_user,
        "token": data["token"],
        "refresh_token": data.get("refreshToken"),
        "urls": urls,
        "ge_web": urls.get("ARGOS WEB") or (GE_WEB_LOGIN + data["token"]),
        "telemetria": urls.get("TELEMETRIA"),
    }


def tel_get(token: str, caminho: str, params: dict[str, Any] | None = None) -> Any:
    query = urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})
    url = f"{TEL}{caminho}"
    if query:
        url = f"{url}?{query}"
    return _request("GET", url, token=token)


def listar_obras(token: str) -> list[dict[str, Any]]:
    dados = tel_get(token, "/filtro/obra", {"organizacao": "null"})
    return list(dados or []) if isinstance(dados, list) else []


def listar_organizacoes(token: str) -> list[dict[str, Any]]:
    dados = tel_get(token, "/filtro/organizacao", {"obra": "null"})
    return list(dados or []) if isinstance(dados, list) else []


def listar_equipamentos(token: str, organizacao: str | None = None, obra: str | None = None) -> list[dict[str, Any]]:
    dados = tel_get(
        token,
        "/filtro/equipamento",
        {"organizacao": organizacao or "null", "obra": obra or "null"},
    )
    return [item for item in (dados or []) if isinstance(item, dict) and not item.get("excluido")]


def listar_alertas(
    token: str,
    dia: date,
    obra: str | None = None,
    organizacao: str | None = None,
    equipamento: str | None = None,
) -> list[dict[str, Any]]:
    inicio = datetime.combine(dia, time.min, tzinfo=timezone(timedelta(hours=-3)))
    fim = datetime.combine(dia, time.max, tzinfo=timezone(timedelta(hours=-3)))
    params = {
        "dataInicio": inicio.isoformat(),
        "dataFim": fim.isoformat(),
        "equipamento": equipamento or "",
        "funcionario": "",
        "obra": obra or "",
        "organizacao": organizacao or "",
        "tipo": "",
    }
    try:
        dados = tel_get(token, "/alerta/detalhado", params)
    except ArgosError:
        dados = []
    return list(dados or []) if isinstance(dados, list) else []


def historico_rastreio(
    token: str,
    equipamento_id: str,
    inicio: datetime,
    fim: datetime,
) -> list[dict[str, Any]]:
    brt = timezone(timedelta(hours=-3))
    if inicio.tzinfo is None:
        inicio = inicio.replace(tzinfo=brt)
    if fim.tzinfo is None:
        fim = fim.replace(tzinfo=brt)
    dados = tel_get(
        token,
        "/rastreio",
        {
            "equipamento": equipamento_id,
            "dataInicio": inicio.isoformat(),
            "dataFim": fim.isoformat(),
        },
    )
    pontos: list[dict[str, Any]] = []
    for item in dados or []:
        if not isinstance(item, dict):
            continue
        try:
            lat = float(item.get("lat"))
            lon = float(item.get("log") if item.get("log") not in (None, "") else item.get("lng") or item.get("lon"))
        except (TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        pontos.append(
            {
                "latitude": lat,
                "longitude": lon,
                "fonte": "argos-api",
                "id": item.get("id"),
            }
        )
    return pontos


def coletar_percursos(
    token: str,
    equipamentos: list[dict[str, Any]],
    inicio: datetime,
    fim: datetime,
    on_progress=None,
) -> dict[str, list[dict[str, Any]]]:
    alvos = [eq for eq in equipamentos if eq.get("id") and eq.get("placa")]
    saida: dict[str, list[dict[str, Any]]] = {}
    if not alvos:
        return saida

    def _uma(eq: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        placa = str(eq.get("placa") or "").strip().upper()
        pontos = historico_rastreio(token, str(eq["id"]), inicio, fim)
        for ponto in pontos:
            ponto["placa"] = placa
            ponto["veiculo"] = placa
        return placa, pontos

    with ThreadPoolExecutor(max_workers=min(6, len(alvos))) as pool:
        futuros = [pool.submit(_uma, eq) for eq in alvos]
        for fut in as_completed(futuros):
            placa, pontos = fut.result()
            saida[placa] = pontos
            if on_progress:
                on_progress(f"  {placa}: {len(pontos)} pontos GPS")
    return saida
