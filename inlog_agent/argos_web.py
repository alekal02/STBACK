from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any

from playwright.sync_api import Page

from .config import hora_com_segundos, janela_datetime
from .gps import extract_points, filtrar_pontos_periodo

TRAJETO_URL = (
    "https://web.argosmonitoramento.com.br/ge-web/"
    "?wicket:bookmarkablePage=:com.frw.base.web.pages.panel.ViewMapRotaDistritoPage"
    "&funcionalidade=168"
)


def abrir_ge_web(page: Page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    if "login" in page.url.lower() and "token=" not in page.url.lower():
        raise RuntimeError("O Argos Web voltou para a tela de login.")


def _clicar_texto(page: Page, texto: str) -> bool:
    return bool(
        page.evaluate(
            """(nome) => {
                const links = Array.from(document.querySelectorAll('a'));
                const a = links.find((el) => (el.innerText || '').trim() === nome);
                if (!a) return false;
                const dd = a.closest('dd') || Array.from(document.querySelectorAll('dd')).find(
                    (el) => (el.innerText || '').includes(nome)
                );
                if (dd && typeof window.ativaDropDown === 'function') {
                    try { window.ativaDropDown(dd); } catch (e) {}
                }
                const src = a.getAttribute('onclick') || '';
                const m = src.match(/wicketAjaxGet\\('([^']+)'/);
                if (m && typeof window.wicketAjaxGet === 'function') {
                    window.wicketAjaxGet(m[1], function () {}, function () {});
                    return true;
                }
                a.click();
                return true;
            }""",
            texto,
        )
    )


def ler_frota_monitoramento(page: Page) -> list[dict[str, Any]]:
    veiculos: list[dict[str, Any]] = []
    vistos: set[str] = set()
    for _ in range(6):
        linhas = page.evaluate(
            """() => Array.from(document.querySelectorAll('table.t1 tr')).slice(1).map((tr) => {
                const tds = Array.from(tr.querySelectorAll('td')).map((td) => (td.innerText || '').trim());
                return {
                    placa: tds[1] || '',
                    descricao: tds[2] || '',
                    condutor: tds[3] || '',
                    hodometro: tds[4] || '',
                    horimetro: tds[5] || '',
                    velocidade: tds[6] || '',
                    temperatura: tds[7] || '',
                    ultimo_rastreio: tds[9] || '',
                    situacao: tds[10] || '',
                    duracao: tds[11] || '',
                };
            }).filter((r) => r.placa)"""
        )
        novos = 0
        for item in linhas or []:
            placa = str(item.get("placa") or "").strip().upper()
            if placa and placa not in vistos:
                vistos.add(placa)
                veiculos.append(item)
                novos += 1
        if novos == 0:
            break
        try:
            page.locator("a").filter(has_text=re.compile(r"^>$")).first.click(force=True, timeout=1500)
            page.wait_for_timeout(900)
        except Exception:
            break
    return veiculos


def carregar_mapa(page: Page) -> list[dict[str, Any]]:
    pontos: list[dict[str, Any]] = []

    def on_response(resp):
        tipo = resp.headers.get("content-type", "")
        if "json" not in tipo and "xml" not in tipo and "text" not in tipo:
            return
        if "ge-web" not in resp.url and "gateway-api" not in resp.url:
            return
        try:
            corpo = resp.text()
        except Exception:
            return
        if not corpo or len(corpo) > 2_000_000:
            return
        try:
            payload = json.loads(corpo)
        except json.JSONDecodeError:
            payload = corpo
        extraidos = extract_points(payload, fonte="argos-mapa")
        if extraidos:
            pontos.extend(extraidos)

    page.on("response", on_response)
    page.locator("button", has_text="Carregar Mapa").click(timeout=8000)
    page.wait_for_timeout(4000)
    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except Exception:
        pass
    js_pontos = page.evaluate(
        """() => {
            const out = [];
            const bruto = (document.getElementById('listPontos') || {}).value || '';
            if (bruto) {
                try {
                    const parsed = JSON.parse(bruto);
                    out.push({bruto: parsed, fonte: 'listPontos'});
                } catch (e) {
                    out.push({texto: bruto.slice(0, 2000), fonte: 'listPontos'});
                }
            }
            const html = document.documentElement.innerHTML;
            const re = /(-1[6-9]\\.[0-9]+)\\s*[,;]\\s*(-4[7-9]\\.[0-9]+)/g;
            let m;
            while ((m = re.exec(html)) && out.length < 400) {
                out.push({latitude: Number(m[1]), longitude: Number(m[2]), fonte: 'argos-html'});
            }
            return out;
        }"""
    )
    for item in js_pontos or []:
        if item.get("latitude") is not None:
            pontos.append(item)
        elif item.get("bruto") is not None:
            pontos.extend(extract_points(item["bruto"], fonte="argos-listapontos"))
        elif item.get("texto"):
            pontos.extend(extract_points(item["texto"], fonte="argos-listapontos"))
    icones = page.locator(".icone.mapa.cursor")
    total = min(icones.count(), 12)
    for i in range(total):
        try:
            extra = page.evaluate(
                """(idx) => {
                    const icon = document.querySelectorAll('.icone.mapa.cursor')[idx];
                    if (!icon) return null;
                    icon.click();
                    const tds = Array.from((icon.closest('tr') || document.createElement('tr')).querySelectorAll('td'))
                        .map((td) => (td.innerText || '').trim());
                    return {
                        placa: tds[1] || '',
                        pontos: (document.getElementById('listPontos') || {}).value || ''
                    };
                }""",
                i,
            )
            page.wait_for_timeout(500)
            if not extra:
                continue
            bruto = extra.get("pontos") or ""
            placa = extra.get("placa") or ""
            parsed: Any = bruto
            if bruto:
                try:
                    parsed = json.loads(bruto)
                except Exception:
                    parsed = bruto
            for ponto in extract_points(parsed, veiculo=placa, fonte="argos-veiculo"):
                ponto["placa"] = placa
                pontos.append(ponto)
        except Exception:
            continue
    return pontos


def abrir_percurso(page: Page) -> dict[str, Any]:
    page.goto(TRAJETO_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    if "ViewMapRotaDistritoPage" not in page.url and not page.locator('input[name="buscarInformacoes"]').count():
        _clicar_texto(page, "Trajeto Realizada MAPA")
        page.wait_for_timeout(2500)
    return {
        "abriu": bool(page.locator('input[name="buscarInformacoes"]').count()),
        "url": page.url,
        "labels": page.evaluate(
            """() => Array.from(document.querySelectorAll('label,h1,h2,h3,.titulo'))
                .map((el) => (el.innerText || '').trim()).filter(Boolean).slice(0, 30)"""
        ),
    }


_MARKER = re.compile(
    r"new google\.maps\.LatLng\((-?\d+\.\d+),\s*(-?\d+\.\d+)\);"
    r"var marker = new google\.maps\.Marker\(\{.*?title:'Data:\s*([^']*)'",
    re.S,
)


def pontos_de_ajax_trajeto(xml: str, placa: str) -> list[dict[str, Any]]:
    pontos: list[dict[str, Any]] = []
    anterior = None
    for lat, lon, titulo in _MARKER.findall(xml):
        chave = (round(float(lat), 6), round(float(lon), 6), titulo.strip())
        if chave == anterior:
            continue
        anterior = chave
        ponto = {
            "latitude": float(lat),
            "longitude": float(lon),
            "placa": placa,
            "veiculo": placa,
            "fonte": "argos-trajeto",
            "timestamp": titulo.split("|")[0].strip(),
        }
        if "Velocidade:" in titulo:
            bruto = titulo.split("Velocidade:", 1)[1].split()[0].replace(",", ".")
            try:
                ponto["velocidade"] = float(bruto)
            except ValueError:
                pass
        pontos.append(ponto)
    return pontos


def _ler_listapontos(page: Page, placa: str) -> list[dict[str, Any]]:
    bruto = ""
    try:
        bruto = page.input_value("#listPontos", timeout=1000)
    except Exception:
        bruto = page.evaluate("() => (document.getElementById('listPontos') || {}).value || ''") or ""
    if not bruto:
        return []
    try:
        payload = json.loads(bruto)
    except json.JSONDecodeError:
        payload = bruto
    pontos = extract_points(payload, veiculo=placa, fonte="argos-trajeto")
    for ponto in pontos:
        ponto["placa"] = placa
    return pontos


def _preencher_filtro_trajeto(
    page: Page,
    placa: str,
    dia: date,
    hora_inicio: str = "00:00",
    hora_fim: str = "23:59",
    data_fim: date | None = None,
) -> None:
    inicio, fim = janela_datetime(dia, hora_inicio, hora_fim, data_fim)
    page.fill('input[name="dateTimePanel:dataInicio"]', inicio.strftime("%d/%m/%Y"))
    page.fill('input[name="dateTimePanel:horaInicio"]', hora_com_segundos(inicio.strftime("%H:%M"), fim=False))
    page.fill('input[name="dateTimePanel:dataFim"]', fim.strftime("%d/%m/%Y"))
    page.fill('input[name="dateTimePanel:horaFim"]', hora_com_segundos(fim.strftime("%H:%M"), fim=True))
    campo = page.locator('input[name="lkpEquipamento:equipamento"]')
    campo.click()
    campo.fill("")
    campo.fill(placa)
    page.wait_for_timeout(900)
    sugestao = page.locator("li, .lookup, .autocomplete, td").filter(has_text=re.compile(placa, re.I))
    if sugestao.count():
        try:
            sugestao.first.click(timeout=1500)
        except Exception:
            campo.press("Enter")
    else:
        campo.press("Enter")
    page.wait_for_timeout(400)


def _chaves_gps(pontos: list[dict[str, Any]]) -> set[tuple[float, float]]:
    chaves: set[tuple[float, float]] = set()
    for ponto in pontos:
        try:
            chaves.add((round(float(ponto["latitude"]), 5), round(float(ponto["longitude"]), 5)))
        except (KeyError, TypeError, ValueError):
            continue
    return chaves


def _trajeto_reaproveitado(pontos: list[dict[str, Any]], anteriores: list[list[dict[str, Any]]]) -> bool:
    atual = _chaves_gps(pontos)
    if len(atual) < 20:
        return False
    for outro in anteriores:
        base = _chaves_gps(outro)
        if not base:
            continue
        comum = len(atual & base)
        if comum / max(len(atual), 1) >= 0.85 or comum / max(len(base), 1) >= 0.85:
            return True
    return False


def _fingerprint_pontos(pontos: list[dict[str, Any]]) -> str:
    if not pontos:
        return ""
    a = pontos[0]
    b = pontos[-1]
    return "|".join(
        [
            str(len(pontos)),
            str(a.get("timestamp") or ""),
            str(a.get("latitude") or ""),
            str(a.get("longitude") or ""),
            str(b.get("timestamp") or ""),
            str(b.get("latitude") or ""),
            str(b.get("longitude") or ""),
        ]
    )


def extrair_trajetos(
    page: Page,
    placas: list[str],
    dia: date,
    hora_inicio: str = "00:00",
    hora_fim: str = "23:59",
    data_fim: date | None = None,
) -> dict[str, list[dict[str, Any]]]:
    resultado: dict[str, list[dict[str, Any]]] = {}
    abrir_percurso(page)
    page.wait_for_selector('input[name="buscarInformacoes"]', timeout=15000)
    coletados: list[dict[str, Any]] = []
    vistos: set[str] = set()

    def on_response(resp):
        if "ge-web" not in resp.url and "gateway-api" not in resp.url:
            return
        tipo = resp.headers.get("content-type", "")
        if not any(x in tipo for x in ("json", "xml", "text", "javascript")):
            return
        try:
            corpo = resp.text()
        except Exception:
            return
        if not corpo or len(corpo) > 3_000_000:
            return
        try:
            payload = json.loads(corpo)
        except json.JSONDecodeError:
            payload = corpo
        extraidos = extract_points(payload, fonte="argos-ajax")
        if extraidos:
            coletados.extend(extraidos)

    ajax_xml: list[str] = []

    def on_ajax(resp):
        if "buscarInformacoes" not in resp.url and "mostrarTodosRastreios" not in resp.url:
            return
        try:
            ajax_xml.append(resp.text())
        except Exception:
            pass

    page.on("response", on_response)
    page.on("response", on_ajax)
    anteriores: list[list[dict[str, Any]]] = []
    for placa in placas:
        chave = re.sub(r"[^A-Z0-9]", "", placa.upper())
        pontos: list[dict[str, Any]] = []
        for tentativa in range(2):
            coletados.clear()
            ajax_xml.clear()
            try:
                page.fill("#listPontos", "")
            except Exception:
                pass
            _preencher_filtro_trajeto(page, placa, dia, hora_inicio, hora_fim, data_fim)
            page.locator('input[name="buscarInformacoes"]').click()
            page.wait_for_timeout(2200 + tentativa * 1500)
            if page.locator('input[name="mostrarTodosRastreios"]').count():
                try:
                    page.locator('input[name="mostrarTodosRastreios"]').click(timeout=2500)
                    page.wait_for_timeout(1800)
                except Exception:
                    pass
            pontos = _ler_listapontos(page, placa)
            fp = _fingerprint_pontos(pontos)
            if fp and fp in vistos:
                pontos = []
            if not pontos:
                for xml in ajax_xml:
                    extraidos = pontos_de_ajax_trajeto(xml, placa)
                    if len(extraidos) > len(pontos) and _fingerprint_pontos(extraidos) not in vistos:
                        pontos = extraidos
            if not pontos:
                candidatos = [dict(p, placa=placa, veiculo=placa) for p in coletados]
                if _fingerprint_pontos(candidatos) not in vistos:
                    pontos = candidatos
            if pontos and not _trajeto_reaproveitado(pontos, anteriores):
                break
            if tentativa < 1:
                pontos = []
                continue
            if pontos and _trajeto_reaproveitado(pontos, anteriores):
                print(f"  aviso: trajeto de {placa} repetido, ignorado", flush=True)
                pontos = []
        fp = _fingerprint_pontos(pontos)
        if fp and fp in vistos:
            pontos = []
        if fp:
            vistos.add(fp)
        if pontos:
            pontos = filtrar_pontos_periodo(pontos, dia, hora_inicio, hora_fim, data_fim=data_fim)
            anteriores.append(pontos)
        resultado[chave] = pontos
    return resultado
