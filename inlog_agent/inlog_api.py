from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from typing import Any
from urllib.parse import urlencode

from playwright.sync_api import Page

from .config import janela_datetime, limitar_janela_api

API_KEY = "0ff6134bec254f9ba1f96d15082d20f2"
BASE = "https://consorcioaparecida.inlog.com.br/Rastreamento/Apresentacao"
EDITOR = f"{BASE}/EditorRotas"
RAST = f"{BASE}/RastreamentoGeral"


def _headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json; charset=utf-8",
        "x-api-key": API_KEY,
        "Accept": "application/json, text/plain, */*",
    }


def api_get(page: Page, url: str) -> Any:
    resultado = page.evaluate(
        """async ({url, headers}) => {
            const r = await fetch(url, { method: 'GET', headers, credentials: 'include' });
            const text = await r.text();
            let body = text;
            try { body = JSON.parse(text); } catch (e) {}
            return { ok: r.ok, status: r.status, body };
        }""",
        {"url": url, "headers": _headers()},
    )
    if not resultado.get("ok"):
        raise RuntimeError(f"GET {url} -> {resultado.get('status')}")
    return resultado.get("body")


def api_post(page: Page, url: str, payload: dict | None = None) -> Any:
    resultado = page.evaluate(
        """async ({url, headers, payload}) => {
            const r = await fetch(url, {
                method: 'POST',
                headers,
                credentials: 'include',
                body: JSON.stringify(payload || {})
            });
            const text = await r.text();
            let body = text;
            try { body = JSON.parse(text); } catch (e) {}
            return { ok: r.ok, status: r.status, body };
        }""",
        {"url": url, "headers": _headers(), "payload": payload or {}},
    )
    if not resultado.get("ok"):
        raise RuntimeError(f"POST {url} -> {resultado.get('status')}")
    return resultado.get("body")


def listar_rotas_grid(page: Page, take: int = 800) -> list[dict[str, Any]]:
    data = api_post(
        page,
        f"{EDITOR}/EditorRotas/LimparFiltro",
        {"take": take, "typeOrder": "MacroSetor"},
    )
    grid = (data or {}).get("Grid") or {}
    return list(grid.get("Data") or [])


def carregar_todos_veiculos(page: Page) -> list[dict[str, Any]]:
    data = api_get(page, f"{EDITOR}/Etapa1/CarregarTodosVeiculos")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("Data", "Veiculos", "Lista", "itens", "Itens"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    return []


def carregar_tipos_veiculos(page: Page) -> Any:
    return api_get(page, f"{EDITOR}/Etapa3/CarregarTiposVeiculos")


def carregar_rota(page: Page, codigo_rota: int) -> Any:
    return api_get(
        page,
        f"{EDITOR}/EditorRotas/CarregarRota?codigoRota={codigo_rota}&rotaTemp=false",
    )


def carregar_detalhes_rota(page: Page, codigo_rota: int) -> Any:
    return api_get(page, f"{EDITOR}/Etapa3/CarregarDetalhesRota?codigoRota={codigo_rota}")


def carregar_pacmans(page: Page, codigo_rota: int) -> Any:
    return api_get(
        page,
        f"{EDITOR}/EditorRotas/CarregarPacmans?codigoRota={codigo_rota}&temp=false",
    )


def carregar_veiculos_setor(page: Page, codigo_setor: int, dia: date) -> Any:
    qs = urlencode({"codigoSetor": codigo_setor, "data": dia.isoformat()})
    return api_get(page, f"{EDITOR}/Etapa1/CarregarVeiculosSetor?{qs}")


def carregar_historico(page: Page, codigo_veiculo: int, dia: date) -> Any:
    inicio = f"{dia.isoformat()}T00:00"
    fim = f"{dia.isoformat()}T23:59"
    qs = urlencode(
        {
            "codigoVeiculo": codigo_veiculo,
            "dtInicio": inicio,
            "dtFim": fim,
        }
    )
    return api_get(page, f"{EDITOR}/Etapa1/CarregarHistorico?{qs}")


def url_detalhes_veiculo(codigo_veiculo: int) -> str:
    return (
        f"{RAST}/VeiculoGenericoDetalhes"
        f"?Visao=Veiculos&Acao=Detalhes&Data={codigo_veiculo}"
    )


def buscar_historico_sinotico(
    page: Page,
    codigo_veiculo: int,
    dia: date,
    hora_inicio: str = "00:00",
    hora_fim: str = "23:59",
    data_fim: date | None = None,
) -> Any:
    inicio_dt, fim_dt = limitar_janela_api(*janela_datetime(dia, hora_inicio, hora_fim, data_fim))
    inicio = inicio_dt.strftime("%Y-%m-%dT%H:%M")
    fim = fim_dt.strftime("%Y-%m-%dT%H:%M")
    qs = urlencode(
        {
            "codigoVeiculo": codigo_veiculo,
            "dataInicio": inicio,
            "dataFim": fim,
        }
    )
    return api_get(page, f"{RAST}/VeiculoGenericoDetalhes/BuscarHistorico?{qs}")


def buscar_historicos_lote(
    page: Page,
    veiculos: list[dict[str, Any]],
    dia: date,
    hora_inicio: str = "00:00",
    hora_fim: str = "23:59",
    data_fim: date | None = None,
) -> dict[int, Any]:
    inicio_dt, fim_dt = limitar_janela_api(*janela_datetime(dia, hora_inicio, hora_fim, data_fim))
    inicio = inicio_dt.strftime("%Y-%m-%dT%H:%M")
    fim = fim_dt.strftime("%Y-%m-%dT%H:%M")
    itens = []
    for item in veiculos:
        codigo = int(item.get("CodigoVeiculo") or item.get("Codigo") or 0)
        if codigo:
            itens.append({"codigo": codigo})
    if not itens:
        return {}
    bruto = page.evaluate(
        """async ({itens, inicio, fim, headers, urlBase}) => {
            const saida = {};
            await Promise.all(itens.map(async (it) => {
                const qs = new URLSearchParams({
                    codigoVeiculo: String(it.codigo),
                    dataInicio: inicio,
                    dataFim: fim,
                });
                try {
                    const r = await fetch(urlBase + '/VeiculoGenericoDetalhes/BuscarHistorico?' + qs, {
                        method: 'GET',
                        headers,
                        credentials: 'include',
                    });
                    const text = await r.text();
                    let body = text;
                    try { body = JSON.parse(text); } catch (e) {}
                    saida[String(it.codigo)] = { ok: r.ok, body };
                } catch (e) {
                    saida[String(it.codigo)] = { ok: false, body: null, erro: String(e) };
                }
            }));
            return saida;
        }""",
        {
            "itens": itens,
            "inicio": inicio,
            "fim": fim,
            "headers": _headers(),
            "urlBase": RAST,
        },
    )
    saida: dict[int, Any] = {}
    for chave, valor in (bruto or {}).items():
        try:
            codigo = int(chave)
        except (TypeError, ValueError):
            continue
        if valor and valor.get("ok"):
            saida[codigo] = valor.get("body")
    return saida


def index_sinotico(page: Page) -> dict[str, Any]:
    data = api_get(page, f"{RAST}/VeiculosGenerico/IndexVeiculo?connectionID=")
    return data if isinstance(data, dict) else {}


def index_veiculos(page: Page) -> Any:
    return api_get(page, f"{RAST}/VeiculosGenerico/IndexVeiculo?connectionID=")
