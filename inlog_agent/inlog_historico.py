from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from . import inlog_api as api
from .config import janela_datetime, resolver_data_fim, limitar_janela_api
from .gps import extract_points, filtrar_pontos_periodo
from .ui import HISTORICO_ALIASES, click_by_text

APLICAR_ALIASES = ("aplicar",)


def _coords(payload: Any, veiculo: str, fonte: str) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        return extract_points(payload.get("Lista") or payload, veiculo=veiculo, fonte=fonte)
    return extract_points(payload, veiculo=veiculo, fonte=fonte)


def pontos_do_mapa(page: Page, veiculo: str) -> list[dict[str, Any]]:
    try:
        bruto = page.evaluate(
            """() => {
                const saida = [];
                const containers = Array.from(document.querySelectorAll('.leaflet-container'));
                const mapas = [];
                for (const el of containers) {
                    for (const chave of Object.keys(el)) {
                        const val = el[chave];
                        if (val && typeof val.eachLayer === 'function' && typeof val.getCenter === 'function') {
                            mapas.push(val);
                        }
                    }
                }
                const walk = (arr) => {
                    if (!arr) return;
                    if (Array.isArray(arr) && arr.length && arr[0] && typeof arr[0].lat === 'number') {
                        for (const p of arr) saida.push({ latitude: p.lat, longitude: p.lng });
                        return;
                    }
                    if (Array.isArray(arr)) arr.forEach(walk);
                };
                for (const mapa of mapas) {
                    mapa.eachLayer((layer) => {
                        if (layer && typeof layer.getLatLngs === 'function') walk(layer.getLatLngs());
                    });
                }
                return saida;
            }"""
        )
    except Exception:
        return []
    pontos = []
    for item in bruto or []:
        lat = item.get("latitude")
        lon = item.get("longitude")
        if lat is None or lon is None:
            continue
        pontos.append(
            {
                "veiculo": veiculo,
                "timestamp": "",
                "latitude": float(lat),
                "longitude": float(lon),
                "velocidade": "",
                "endereco": "",
                "fonte": "mapa-historico",
            }
        )
    return pontos


def _preencher_periodo(
    page: Page,
    dia: date,
    hora_inicio: str = "00:00",
    hora_fim: str = "23:59",
    data_fim: date | None = None,
) -> None:
    inicio, fim = limitar_janela_api(*janela_datetime(dia, hora_inicio, hora_fim, data_fim))
    data_br = inicio.strftime("%d/%m/%Y")
    data_fim_br = fim.strftime("%d/%m/%Y")
    h_ini = inicio.strftime("%H:%M")
    h_fim = fim.strftime("%H:%M")
    try:
        page.evaluate(
            """({dataBr, dataFim, hIni, hFim}) => {
                const visiveis = Array.from(document.querySelectorAll('input')).filter((el) => {
                    if (el.type === 'hidden' || el.disabled) return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                });
                const datas = visiveis.filter((el) =>
                    el.type === 'date'
                    || /data|date|inicio|fim/i.test((el.id || '') + (el.name || '') + (el.placeholder || ''))
                    || /\\d{2}\\/\\d{2}\\/\\d{4}/.test(el.value || '')
                );
                const horas = visiveis.filter((el) =>
                    el.type === 'time'
                    || /hora|time/i.test((el.id || '') + (el.name || '') + (el.placeholder || ''))
                    || /^\\d{2}:\\d{2}/.test((el.value || '').trim())
                );
                const setVal = (el, valor) => {
                    el.focus();
                    el.value = valor;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.blur();
                };
                if (datas[0]) setVal(datas[0], dataBr);
                if (datas[1]) setVal(datas[1], dataFim);
                if (horas[0]) setVal(horas[0], hIni);
                if (horas[1]) setVal(horas[1], hFim);
            }""",
            {"dataBr": data_br, "dataFim": data_fim_br, "hIni": h_ini, "hFim": h_fim},
        )
    except Exception:
        pass


def _clicar_aplicar(page: Page) -> bool:
    for seletor in (
        "#Detalhes-BotaoBuscarHistorico",
        "button#Detalhes-BotaoBuscarHistorico",
        "button.btn-info:has-text('Aplicar')",
        "button:has-text('Aplicar')",
    ):
        loc = page.locator(seletor)
        try:
            if loc.count() == 0:
                continue
            alvo = loc.first
            if not alvo.is_visible():
                continue
            alvo.click(timeout=4000)
            return True
        except Exception:
            continue
    return click_by_text(page, APLICAR_ALIASES, timeout=4000)


def _historico_api(
    page: Page,
    codigo: int,
    ident: str,
    dia: date,
    hora_inicio: str = "00:00",
    hora_fim: str = "23:59",
    data_fim: date | None = None,
) -> list[dict[str, Any]]:
    if not codigo:
        return []
    try:
        return _coords(
            api.buscar_historico_sinotico(
                page,
                codigo,
                dia,
                hora_inicio=hora_inicio,
                hora_fim=hora_fim,
                data_fim=data_fim,
            ),
            ident,
            "historico",
        )
    except Exception:
        return []


def _pontos_da_rede(spy, a_partir: int, ident: str) -> list[dict[str, Any]]:
    pontos: list[dict[str, Any]] = []
    if spy is None:
        return pontos
    for call in spy.calls[a_partir:]:
        if not call.json_body:
            continue
        url = (call.url or "").lower()
        if "historico" in url or call.has_gps_hint:
            pontos.extend(_coords(call.json_body, ident, "historico-tela"))
    return pontos


def coletar_historico_api(
    page: Page,
    veiculo: dict[str, Any],
    dia: date,
    hora_inicio: str = "00:00",
    hora_fim: str = "23:59",
    data_fim: date | None = None,
    payload: Any = None,
) -> list[dict[str, Any]]:
    codigo = int(veiculo.get("CodigoVeiculo") or veiculo.get("Codigo") or 0)
    ident = (
        str(veiculo.get("Identificador") or veiculo.get("Prefixo") or f"veiculo-{codigo}")
        .strip()
        .upper()
        .replace("\r", "")
        .replace("\n", "")
    )
    juntos: list[dict[str, Any]] = []
    if payload is not None:
        juntos.extend(_coords(payload, ident, "historico"))
    else:
        juntos.extend(_historico_api(page, codigo, ident, dia, hora_inicio, hora_fim, data_fim))
    if not juntos and codigo:
        try:
            juntos.extend(_coords(api.carregar_historico(page, codigo, dia), ident, "historico-editor"))
        except Exception:
            pass
    vistos: set[tuple[Any, ...]] = set()
    saida: list[dict[str, Any]] = []
    for ponto in juntos:
        lat = ponto.get("latitude")
        lon = ponto.get("longitude")
        if lat is None or lon is None:
            continue
        chave = (str(ponto.get("timestamp") or ""), round(float(lat), 6), round(float(lon), 6))
        if chave in vistos:
            continue
        vistos.add(chave)
        saida.append(ponto)
    saida.sort(key=lambda item: str(item.get("timestamp") or ""))
    return filtrar_pontos_periodo(saida, dia, hora_inicio, hora_fim, data_fim=data_fim)


def coletar_historico_caminhao(
    page: Page,
    veiculo: dict[str, Any],
    dia: date,
    spy=None,
    hora_inicio: str = "00:00",
    hora_fim: str = "23:59",
    data_fim: date | None = None,
) -> list[dict[str, Any]]:
    codigo = int(veiculo.get("CodigoVeiculo") or veiculo.get("Codigo") or 0)
    ident = (
        str(veiculo.get("Identificador") or veiculo.get("Prefixo") or f"veiculo-{codigo}")
        .strip()
        .upper()
        .replace("\r", "")
        .replace("\n", "")
    )
    n0 = len(spy.calls) if spy else 0
    if codigo:
        try:
            page.goto(api.url_detalhes_veiculo(codigo), wait_until="domcontentloaded")
            page.wait_for_timeout(800)
        except Exception:
            pass
        click_by_text(page, HISTORICO_ALIASES, timeout=5000)
        _preencher_periodo(page, dia, hora_inicio, hora_fim, data_fim)
        try:
            with page.expect_response(
                lambda resp: "Historico" in resp.url or "historico" in resp.url.lower(),
                timeout=6000,
            ):
                _clicar_aplicar(page)
        except PlaywrightTimeout:
            _clicar_aplicar(page)
        except Exception:
            _clicar_aplicar(page)
        try:
            page.wait_for_function(
                "() => document.querySelectorAll('.leaflet-overlay-pane path').length > 0",
                timeout=4000,
            )
        except Exception:
            page.wait_for_timeout(800)

    juntos = []
    juntos.extend(pontos_do_mapa(page, ident))
    juntos.extend(_pontos_da_rede(spy, n0, ident))
    juntos.extend(_historico_api(page, codigo, ident, dia, hora_inicio, hora_fim, data_fim))
    if not juntos and veiculo.get("Latitude") and veiculo.get("Longitude"):
        juntos.extend(extract_points(veiculo, veiculo=ident, fonte="ultima-posicao"))
    vistos: set[tuple[Any, ...]] = set()
    saida: list[dict[str, Any]] = []
    for ponto in juntos:
        lat = ponto.get("latitude")
        lon = ponto.get("longitude")
        if lat is None or lon is None:
            continue
        chave = (str(ponto.get("timestamp") or ""), round(float(lat), 6), round(float(lon), 6))
        if chave in vistos:
            continue
        vistos.add(chave)
        saida.append(ponto)
    saida.sort(key=lambda item: str(item.get("timestamp") or ""))
    return filtrar_pontos_periodo(saida, dia, hora_inicio, hora_fim, data_fim=data_fim)
