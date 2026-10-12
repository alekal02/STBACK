from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, Iterator
from urllib.parse import urlparse

from playwright.sync_api import Page, Playwright, sync_playwright

from .navegador import abrir_contexto

from .config import Settings, hora_com_segundos, janela_datetime
from .gps import extract_points, filtrar_pontos_periodo, utc_para_brt_texto

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
API_V4 = "https://api-fulltrack4.fulltrackapp.com/"
APARECIDA_LAT = (-16.98, -16.68)
APARECIDA_LON = (-49.42, -49.05)
TIPOS_CAMINHAO = {3, 9, 12, 25, 28}
COMPACTOR_HINTS = (
    "compactador",
    "coleta",
    "ccl",
    "lixo",
    "domiciliar",
    "caminhao",
    "caminhão",
    "truck",
    "cacamba",
    "caçamba",
)
HISTORICO_API = (
    "historico/posicoes",
    "posicoes/historico",
    "relatorios/historico",
    "veiculos/historico",
    "rastreamento/historico",
    "maps/historico",
    "mapaGeral/historico",
    "historico",
    "posicoes",
)
FROTA_API = (
    "veiculos",
    "veiculos/listar",
    "mapaGeral/getVeiculos",
    "maps/getVeiculos",
)
HISTORICO_SITE = (
    "maps/getHistorico",
    "mapaGeral/getHistorico",
    "veiculos/getHistorico",
    "relatorios/historicoPosicao/gerar",
    "relatorios/getHistorico",
    "maps/buscarHistorico",
    "maps/getPositionsHistory",
)


def _norm_placa(placa: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (placa or "").upper())


def _fmt_placa(placa: str) -> str:
    chave = _norm_placa(placa)
    if len(chave) == 7:
        return f"{chave[:3]}-{chave[3:]}"
    return (placa or chave).upper()


def _em_aparecida(lat: Any, lon: Any, endereco: str = "") -> bool:
    texto = (endereco or "").lower()
    if "aparecida" in texto:
        return True
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    return APARECIDA_LAT[0] <= la <= APARECIDA_LAT[1] and APARECIDA_LON[0] <= lo <= APARECIDA_LON[1]


def _parece_compactador_rsd(item: dict[str, Any], placas_inlog: set[str] | None = None) -> bool:
    placa = "".join(ch for ch in str(item.get("placa") or item.get("texto") or "").upper() if ch.isalnum())
    if placas_inlog and placa in placas_inlog:
        return True
    blob = " ".join(
        str(item.get(chave) or "")
        for chave in ("placa", "veiculo", "descricao", "tipo_nome", "grupo", "texto")
    ).upper()
    compacto = blob.replace(" ", "").replace("/", "").replace("-", "").replace(".", "")
    vw = "18260" in compacto or "17260" in compacto
    if placa.startswith("RB") and not vw:
        return False
    if vw:
        return True
    if placa.startswith("SD") or placa.startswith("SCW"):
        return True
    blob_l = blob.lower()
    if any(hint in blob_l for hint in COMPACTOR_HINTS):
        return True
    return False


def _placa_liberada(placa: str, filtro: tuple[str, ...]) -> bool:
    if not filtro:
        return True
    chave = _norm_placa(placa)
    return any(_norm_placa(item) == chave for item in filtro)


def _item_liberado(item: dict[str, Any], filtro: tuple[str, ...]) -> bool:
    if not filtro:
        return True
    blob = " ".join(
        str(item.get(chave) or "")
        for chave in ("placa", "veiculo", "descricao", "id", "tag")
    )
    return any(_norm_placa(alvo) and _norm_placa(alvo) in _norm_placa(blob) for alvo in filtro)


class SegtrackSession:
    def __init__(self, settings: Settings, headed: bool | None = None) -> None:
        self.settings = settings
        self.headed = settings.headless is False if headed is None else headed
        self.playwright: Playwright | None = None
        self.context = None
        self.page: Page | None = None
        self.chamadas: list[dict[str, Any]] = []

    def start(self) -> Page:
        self.playwright = sync_playwright().start()
        self.context = abrir_contexto(
            self.playwright,
            str(self.settings.session_dir / "segtrack"),
            headless=not self.headed,
            slow_mo=self.settings.slow_mo_ms or None,
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            viewport={"width": 1440, "height": 900},
            user_agent=CHROME_UA,
            extra_http_headers={"Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8"},
            args=["--disable-dev-shm-usage"],
        )
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(self.settings.timeout_ms)
        self.page.on("response", self._on_response)
        return self.page

    def _on_response(self, response) -> None:
        try:
            request = response.request
            if request.resource_type not in {"xhr", "fetch"}:
                return
            host = urlparse(response.url).netloc.lower()
            if "fulltrackapp.com" not in host and "ftdata.com.br" not in host:
                return
            if response.status >= 400:
                return
            corpo: Any = None
            try:
                texto = response.text()
            except Exception:
                return
            if not texto or len(texto) > 4_000_000:
                return
            try:
                corpo = json.loads(texto)
            except json.JSONDecodeError:
                corpo = texto[:4000]
            path = urlparse(response.url).path
            self.chamadas.append(
                {
                    "method": request.method,
                    "url": response.url,
                    "path": path,
                    "status": response.status,
                    "json": corpo if not isinstance(corpo, str) else None,
                    "texto": corpo if isinstance(corpo, str) else None,
                    "quando": datetime.now().isoformat(timespec="seconds"),
                }
            )
        except Exception:
            return

    def close(self) -> None:
        if self.context:
            self.context.close()
        if self.playwright:
            self.playwright.stop()


@contextmanager
def open_segtrack_session(settings: Settings, headed: bool | None = None) -> Iterator[SegtrackSession]:
    session = SegtrackSession(settings, headed=headed)
    try:
        session.start()
        yield session
    finally:
        session.close()


def _logout_url(settings: Settings) -> str:
    base = settings.segtrack_login_url.rstrip("/")
    slug = urlparse(base).hostname.split(".")[0] if urlparse(base).hostname else ""
    return f"{base}/logout/{slug}" if slug else f"{base}/logout"


def _usuario_na_tela(page: Page) -> str:
    return (
        page.evaluate(
            """() => {
                const el = document.querySelector('.user-name,.usuario,.navbar-right .dropdown-toggle,a[href*=\"logout\"]');
                const texto = ((el && el.innerText) || document.body.innerText || '');
                const m = texto.match(/[A-Z0-9_]{6,}/);
                return (m && m[0]) || '';
            }"""
        )
        or ""
    )


def fazer_login(page: Page, settings: Settings) -> dict[str, Any]:
    if not settings.segtrack_user or not settings.segtrack_password:
        raise SystemExit("Preencha SEGTRACK_USER e SEGTRACK_PASSWORD no .env")
    page.goto(settings.segtrack_login_url, wait_until="domcontentloaded")
    page.wait_for_timeout(800)
    if not page.locator("#login").count():
        atual = _usuario_na_tela(page)
        if _norm_placa(atual) != _norm_placa(settings.segtrack_user):
            page.goto(_logout_url(settings), wait_until="domcontentloaded")
            page.wait_for_timeout(800)
        else:
            if settings.segtrack_home_url:
                page.goto(settings.segtrack_home_url, wait_until="domcontentloaded")
                page.wait_for_timeout(1200)
            return {
                "url": page.url,
                "titulo": page.title(),
                "login": settings.segtrack_user,
                "home": settings.segtrack_home_url,
                "reusou_sessao": True,
            }
    page.wait_for_selector("#login", timeout=25_000)
    page.fill("#login", settings.segtrack_user)
    page.fill("#password", settings.segtrack_password)
    page.click("#btn-login")
    try:
        page.wait_for_function(
            """() => {
                const senha = document.querySelector('#password');
                const btn = document.querySelector('#btn-login');
                if (!senha || !btn) return true;
                const path = (location.pathname || '').toLowerCase();
                return path.indexOf('/emp') === -1 && path !== '/' && path !== '';
            }""",
            timeout=25_000,
        )
    except Exception:
        pass
    page.wait_for_timeout(1500)
    if page.locator("#password").count() and page.locator("#btn-login").count():
        erro = page.evaluate(
            """() => (document.querySelector('.alert,.erro,.error,swal-text,.sweet-alert')
                || {}).innerText || ''"""
        )
        raise RuntimeError(
            "Login SEGTRACK recusado. Confira SEGTRACK_USER e SEGTRACK_PASSWORD no .env"
            + (f" ({erro.strip()})" if erro and str(erro).strip() else "")
        )
    if settings.segtrack_home_url and "dashboard_controller" not in (page.url or "").lower():
        page.goto(settings.segtrack_home_url, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
    return {
        "url": page.url,
        "titulo": page.title(),
        "login": settings.segtrack_user,
        "home": settings.segtrack_home_url,
    }


def _ultimas_posicoes_v3(page: Page, settings: Settings) -> list[dict[str, Any]]:
    capturado: list[Any] = []

    def on_response(resp) -> None:
        if "last-positions" not in resp.url or resp.request.resource_type not in {"xhr", "fetch"}:
            return
        try:
            corpo = resp.json()
        except Exception:
            return
        if isinstance(corpo, list):
            capturado.append(corpo)

    page.on("response", on_response)
    page.goto(settings.segtrack_mapa_url, wait_until="domcontentloaded")
    for _ in range(25):
        page.wait_for_timeout(400)
        if capturado:
            break
    try:
        page.remove_listener("response", on_response)
    except Exception:
        pass
    bruto = capturado[-1] if capturado else []
    saida = []
    for row in bruto if isinstance(bruto, list) else []:
        if not isinstance(row, dict):
            continue
        at = row.get("ativo") if isinstance(row.get("ativo"), dict) else {}
        ll = row.get("lat_lng") or []
        lat = ll[0] if isinstance(ll, (list, tuple)) and len(ll) >= 2 else None
        lon = ll[1] if isinstance(ll, (list, tuple)) and len(ll) >= 2 else None
        placa_raw = str(at.get("plate") or row.get("plate") or "")
        saida.append(
            {
                "id": str(row.get("ativo_id") or at.get("id") or ""),
                "placa": _fmt_placa(placa_raw),
                "texto": f"{placa_raw} - {at.get('description') or at.get('ativo_name') or ''}".strip(" -"),
                "tipo": at.get("type"),
                "latitude": lat,
                "longitude": lon,
                "timestamp": str(row.get("dt_gps") or ""),
                "fonte": "mapa-v3",
            }
        )
    return saida


def _buscar_placa_mapa_v3(page: Page, placa: str) -> None:
    chave = _norm_placa(placa)
    if not chave:
        return
    page.evaluate(
        """(alvo) => {
            const icone = Array.from(document.querySelectorAll('button,a,i,span,[role=button]'))
                .find((el) => /busca|search|pesquis|lupa/i.test(
                    (el.innerText || '') + ' ' + (el.className || '') + ' ' + (el.getAttribute('aria-label') || '')
                ));
            if (icone) icone.click();
            const input = Array.from(document.querySelectorAll('input'))
                .find((el) => /buscar|placa|pesquis|veic/i.test(
                    (el.placeholder || '') + ' ' + (el.getAttribute('aria-label') || '')
                )) || document.querySelector('input[placeholder=\"Buscar...\"]');
            if (!input) return;
            input.value = alvo;
            input.dispatchEvent(new Event('input', {bubbles: true}));
            input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
        }""",
        chave,
    )
    page.wait_for_timeout(1200)


def _veiculos_do_mapa(page: Page) -> list[dict[str, Any]]:
    bruto = page.evaluate(
        """() => {
            const out = [];
            const push = (item) => {
                if (!item) return;
                out.push(item);
            };
            const markers = (window.FTFRONT && FTFRONT.maps && FTFRONT.maps._mapAllMarkers) || [];
            for (const m of markers) {
                const ll = (m.getLatLng && m.getLatLng()) || m._latlng || {};
                const opt = m.options || {};
                const data = m.data || opt.data || {};
                const vei = data.ras_veiculos || opt.ras_veiculos || {};
                push({
                    latitude: ll.lat,
                    longitude: ll.lng,
                    placa: vei.ras_vei_placa || data.placa || opt.placa || data.ras_vei_placa,
                    veiculo: vei.ras_vei_veiculo || data.desc_veiculo || data.veiculo || opt.markerLabel,
                    id: data.idveiculo || vei.ras_vei_id || data.ras_vei_id || opt.idveiculo,
                    tipo: vei.ras_vei_tipo || data.tipo || opt.tipo,
                    descricao: data.desc_veiculo || vei.ras_vei_tag_identificacao || '',
                    fonte: 'mapa-markers',
                });
            }
            return out;
        }"""
    )
    return [item for item in (bruto or []) if item]


def _token_api(page: Page) -> dict[str, Any] | None:
    token = page.evaluate(
        """() => {
            try { return JSON.parse(localStorage.getItem('token-ftk4') || 'null'); }
            catch (e) { return null; }
        }"""
    )
    if token and token.get("access_token"):
        return token
    page.evaluate(
        """() => {
            if (window.REQUESTER_TOKEN && typeof REQUESTER_TOKEN.gerarToken === 'function') {
                REQUESTER_TOKEN.gerarToken(function () {});
            }
        }"""
    )
    page.wait_for_timeout(2200)
    token = page.evaluate(
        """() => {
            try { return JSON.parse(localStorage.getItem('token-ftk4') || 'null'); }
            catch (e) { return null; }
        }"""
    )
    if token and token.get("access_token"):
        return token
    return None


def _datas_dia(
    dia: date,
    hora_inicio: str = "00:00",
    hora_fim: str = "23:59",
    data_fim: date | None = None,
) -> dict[str, str]:
    inicio, fim = janela_datetime(dia, hora_inicio, hora_fim, data_fim)
    inicio_s = hora_com_segundos(inicio.strftime("%H:%M"), fim=False)
    fim_s = hora_com_segundos(fim.strftime("%H:%M"), fim=True)
    br = inicio.strftime("%d/%m/%Y")
    br_fim = fim.strftime("%d/%m/%Y")
    iso = inicio.date().isoformat()
    iso_fim = fim.date().isoformat()
    return {
        "data_inicio": f"{br} {inicio_s}",
        "data_fim": f"{br_fim} {fim_s}",
        "dataInicio": f"{iso} {inicio_s}",
        "dataFim": f"{iso_fim} {fim_s}",
        "inicio": f"{iso} {inicio_s}",
        "fim": f"{iso_fim} {fim_s}",
        "de": f"{br} {inicio_s}",
        "ate": f"{br_fim} {fim_s}",
        "data": iso,
    }


def _chamar_api_v4(page: Page, caminho: str, data: dict[str, Any]) -> Any:
    return page.evaluate(
        """async ({caminho, data}) => {
            if (!window.REQUESTER_TOKEN) return {erro: 'REQUESTER_TOKEN ausente'};
            return await new Promise((resolve) => {
                try {
                    REQUESTER_TOKEN.enviar(caminho, data, {
                        fnSuccess: (result) => resolve({ok: true, data: result}),
                        fnerror: (err) => resolve({ok: false, erro: err}),
                    });
                    setTimeout(() => resolve({ok: false, erro: 'timeout'}), 20000);
                } catch (e) {
                    resolve({ok: false, erro: String(e)});
                }
            });
        }""",
        {"caminho": caminho, "data": data},
    )


def _chamar_site(page: Page, caminho: str, data: dict[str, Any]) -> Any:
    return page.evaluate(
        """async ({caminho, data}) => {
            if (!window.FTFRONT || typeof FTFRONT.send !== 'function' || typeof FTFRONT.generateUrl !== 'function') {
                return {erro: 'FTFRONT ausente'};
            }
            return await new Promise((resolve) => {
                try {
                    FTFRONT.send(FTFRONT.generateUrl(caminho), data, {
                        exceptForm: true,
                        success: (result) => resolve({ok: true, data: result}),
                        error: (err) => resolve({ok: false, erro: err}),
                    });
                    setTimeout(() => resolve({ok: false, erro: 'timeout'}), 20000);
                } catch (e) {
                    resolve({ok: false, erro: String(e)});
                }
            });
        }""",
        {"caminho": caminho, "data": data},
    )


def _abrir_historico_menu(page: Page) -> list[dict[str, str]]:
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('a,button,span,li'))
            .map((el) => ({
                text: (el.innerText || '').trim().slice(0, 80),
                href: el.href || el.getAttribute('onclick') || '',
            }))
            .filter((x) => x.text && /histor|percurso|trajeto|posi|relat|mapa/i.test(x.text + ' ' + x.href))
            .slice(0, 80)"""
    )


def _clicar_texto(page: Page, padrao: str) -> bool:
    return bool(
        page.evaluate(
            """(padrao) => {
                const re = new RegExp(padrao, 'i');
                const el = Array.from(document.querySelectorAll('a,button,span,li,td'))
                    .find((n) => re.test((n.innerText || '').trim()));
                if (!el) return false;
                el.click();
                return true;
            }""",
            padrao,
        )
    )


def _veiculos_de_payloads(payloads: list[Any]) -> list[dict[str, Any]]:
    achados: dict[str, dict[str, Any]] = {}
    for payload in payloads:
        for ponto in extract_points(payload, fonte="segtrack"):
            placa = _fmt_placa(str(ponto.get("veiculo") or ""))
            chave = _norm_placa(placa) or f"{ponto.get('latitude')},{ponto.get('longitude')}"
            item = achados.setdefault(
                chave,
                {
                    "placa": placa,
                    "veiculo": placa,
                    "id": None,
                    "tipo": None,
                    "descricao": "",
                    "pontos": [],
                    "fonte": "api",
                },
            )
            item["pontos"].append(ponto)
        if isinstance(payload, dict):
            _colher_veiculo(payload, achados)
        elif isinstance(payload, list):
            for row in payload:
                if isinstance(row, dict):
                    _colher_veiculo(row, achados)
    return list(achados.values())


def _colher_veiculo(row: dict[str, Any], dest: dict[str, dict[str, Any]]) -> None:
    vei = row.get("ras_veiculos") if isinstance(row.get("ras_veiculos"), dict) else row
    placa = _fmt_placa(str(vei.get("ras_vei_placa") or vei.get("placa") or row.get("placa") or ""))
    if not placa:
        return
    chave = _norm_placa(placa)
    item = dest.setdefault(
        chave,
        {
            "placa": placa,
            "veiculo": placa,
            "id": None,
            "tipo": None,
            "descricao": "",
            "pontos": [],
            "fonte": "cadastro",
        },
    )
    item["id"] = item.get("id") or vei.get("ras_vei_id") or row.get("idveiculo") or row.get("id")
    item["tipo"] = item.get("tipo") or vei.get("ras_vei_tipo") or row.get("tipo")
    item["descricao"] = item.get("descricao") or vei.get("ras_vei_veiculo") or row.get("veiculo") or ""
    if item["descricao"] and item["veiculo"] == item["placa"]:
        item["veiculo"] = str(item["descricao"])
    lat = row.get("latitude") or row.get("lat") or vei.get("ras_ref_latitude")
    lon = row.get("longitude") or row.get("lng") or row.get("lon")
    extra = extract_points(row, veiculo=placa, fonte="segtrack-cadastro")
    if extra:
        item["pontos"].extend(extra)
    elif lat not in (None, "") and lon not in (None, ""):
        item["pontos"].extend(extract_points({"latitude": lat, "longitude": lon, "placa": placa}, fonte="segtrack-cadastro"))


def _pontos_em_aparecida(item: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        ponto
        for ponto in (item.get("pontos") or [])
        if _em_aparecida(ponto.get("latitude"), ponto.get("longitude"), ponto.get("endereco") or "")
    ]


def _filtrar_compactadores(
    veiculos: list[dict[str, Any]],
    filtro: tuple[str, ...],
    placas_inlog: set[str] | None = None,
) -> list[dict[str, Any]]:
    saida = []
    for item in veiculos:
        placa = str(item.get("placa") or "")
        if filtro and not (_item_liberado(item, filtro) or _placa_liberada(placa, filtro)):
            continue
        if not filtro and not _parece_compactador_rsd(item, placas_inlog):
            continue
        no_setor = _pontos_em_aparecida(item)
        if not no_setor:
            continue
        saida.append({**item, "pontos": no_setor})
    return saida


def _historico_url(settings: Settings) -> str:
    return settings.segtrack_login_url.rstrip("/") + "/relatorios/historico_posicoes"


def listar_ativos_historico(page: Page, settings: Settings) -> list[dict[str, str]]:
    page.goto(_historico_url(settings), wait_until="domcontentloaded")
    page.wait_for_selector("#id_ativo", timeout=25_000)
    page.wait_for_timeout(1500)
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('#id_ativo option'))
            .map((o) => ({id: o.value, texto: (o.text || '').trim()}))
            .filter((o) => o.id && o.id !== '0')"""
    ) or []


def _casar_ativo(ativos: list[dict[str, str]], alvo: str) -> dict[str, str] | None:
    chave = _norm_placa(alvo)
    if not chave:
        return None
    for item in ativos:
        if chave in _norm_placa(item.get("texto") or "") or chave == _norm_placa(item.get("id") or ""):
            return item
    return None


def _pontos_do_historico(payload: Any, veiculo: str) -> list[dict[str, Any]]:
    pontos = []
    rows = []
    if isinstance(payload, dict):
        rows = payload.get("rows") or payload.get("data") or payload.get("result") or []
        if not isinstance(rows, list):
            rows = []
    elif isinstance(payload, list):
        rows = payload
    for row in rows:
        if not isinstance(row, dict):
            continue
        loc = row.get("lst_localizacao") or row.get("localizacao") or []
        lat = lon = None
        if isinstance(loc, (list, tuple)) and len(loc) >= 2:
            try:
                lat, lon = float(loc[0]), float(loc[1])
            except (TypeError, ValueError):
                lat = lon = None
        extra = extract_points(row, veiculo=veiculo, fonte="segtrack-historico")
        ts = utc_para_brt_texto(str(row.get("dt_servidor") or row.get("dt_gps") or row.get("data") or ""))
        if extra:
            for ponto in extra:
                bruto = str(ponto.get("timestamp") or "").strip()
                if bruto:
                    ponto["timestamp"] = utc_para_brt_texto(bruto)
                elif ts:
                    ponto["timestamp"] = ts
                ponto["tz"] = "BRT"
                if veiculo:
                    ponto["veiculo"] = veiculo
            pontos.extend(extra)
            continue
        if lat is None or lon is None:
            continue
        pontos.append(
            {
                "veiculo": veiculo,
                "timestamp": ts,
                "latitude": lat,
                "longitude": lon,
                "velocidade": row.get("vl_velocidade") or "",
                "endereco": str(row.get("desc_endereco") or ""),
                "fonte": "segtrack-historico",
                "tz": "BRT",
            }
        )
    return pontos


def gerar_historico_ativo(
    page: Page,
    settings: Settings,
    ativo_id: str,
    dia: date,
    hora_inicio: str = "00:00",
    hora_fim: str = "23:59",
    data_fim: date | None = None,
) -> list[dict[str, Any]]:
    if "/historico_posicoes" not in page.url:
        page.goto(_historico_url(settings), wait_until="domcontentloaded")
        page.wait_for_selector("#id_ativo", timeout=25_000)
        page.wait_for_timeout(800)
    page.select_option("#id_ativo", str(ativo_id))
    janela = _datas_dia(dia, hora_inicio, hora_fim, data_fim)
    for sel, valor in (
        ("#data_inicial", janela["data_inicio"]),
        ("#data_final", janela["data_fim"]),
        ("#data", f"{janela['data_inicio']} - {janela['data_fim']}"),
    ):
        try:
            if page.locator(sel).count():
                page.locator(sel).fill(valor)
        except Exception:
            pass
    capturado: list[Any] = []

    def on_response(resp) -> None:
        if "HistoricoPosicao/gerar" not in resp.url:
            return
        try:
            capturado.append(resp.json())
        except Exception:
            try:
                capturado.append(resp.text())
            except Exception:
                return

    page.on("response", on_response)
    page.keyboard.press("Escape")
    page.evaluate(
        """() => {
            document.querySelectorAll('.daterangepicker').forEach((el) => {
                el.style.display = 'none';
                el.classList.remove('show-calendar');
            });
        }"""
    )
    page.locator("#gerar_relatorio").click(force=True)
    for _ in range(40):
        page.wait_for_timeout(500)
        if capturado:
            break
    try:
        page.remove_listener("response", on_response)
    except Exception:
        pass
    pontos: list[dict[str, Any]] = []
    for payload in capturado:
        pontos.extend(_pontos_do_historico(payload, ativo_id))
    return filtrar_pontos_periodo(pontos, dia, hora_inicio, hora_fim, data_fim=data_fim)


def extrair_frota_e_percursos(
    page: Page,
    settings: Settings,
    dia: date,
    on_progress=None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    def progress(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    placas_inlog: set[str] = set()
    try:
        from .relatorio_diario import mapa_frota_inlog

        placas_inlog = set(mapa_frota_inlog(settings.output_dir, dia).keys())
    except Exception:
        placas_inlog = set()

    if settings.segtrack_home_url:
        progress("Abrindo o painel da SEGTRACK...")
        page.goto(settings.segtrack_home_url, wait_until="domcontentloaded")
        page.wait_for_timeout(1200)
    progress("Abrindo o Mapa Geral 3.0...")
    posicoes = _ultimas_posicoes_v3(page, settings)
    if page.locator("#btn-login").count() and page.locator("#password").count():
        progress("Sessão caiu. Entrando de novo...")
        fazer_login(page, settings)
        posicoes = _ultimas_posicoes_v3(page, settings)
    progress(f"Mapa v3: {len(posicoes)} rastreados.")
    for alvo in settings.segtrack_veiculos:
        _buscar_placa_mapa_v3(page, alvo)

    progress("Abrindo o histórico de posições da SEGTRACK...")
    ativos = listar_ativos_historico(page, settings)
    progress(f"Frota na SEGTRACK: {len(ativos)} veículos.")

    escolhidos: list[dict[str, str]] = []
    if settings.segtrack_veiculos:
        faltando = []
        for alvo in settings.segtrack_veiculos:
            match = _casar_ativo(ativos, alvo) or _casar_ativo(
                [{"id": p.get("id") or "", "texto": p.get("texto") or p.get("placa") or ""} for p in posicoes],
                alvo,
            )
            if match:
                escolhidos.append(match)
            else:
                faltando.append(_fmt_placa(alvo))
        if faltando:
            lista = ", ".join((item.get("texto") or "") for item in ativos[:30])
            raise RuntimeError(
                f"A placa {', '.join(faltando)} não está nesta conta SEGTRACK "
                f"({settings.segtrack_user}). Pesquise sem hífen, ex.: SDG1J76. "
                f"Veículos cadastrados: {lista}"
            )
    else:
        no_mapa = [
            {"id": p.get("id") or "", "texto": p.get("texto") or p.get("placa") or ""}
            for p in posicoes
            if p.get("id")
            and _parece_compactador_rsd(p, placas_inlog)
            and _em_aparecida(p.get("latitude"), p.get("longitude"))
        ]
        escolhidos = no_mapa or [
            item
            for item in ativos
            if _parece_compactador_rsd(
                {"placa": item.get("texto"), "veiculo": item.get("texto"), "tipo": 3},
                placas_inlog,
            )
        ]

    candidatos = []
    for ativo in escolhidos:
        texto = ativo.get("texto") or ""
        placa = _fmt_placa(texto.split(" - ")[0] if " - " in texto else texto)
        progress(f"Gerando histórico de {placa}...")
        pontos = gerar_historico_ativo(
            page,
            settings,
            str(ativo["id"]),
            dia,
            hora_inicio=settings.hora_inicio,
            hora_fim=settings.hora_fim,
            data_fim=settings.data_fim,
        )
        for ponto in pontos:
            ponto["veiculo"] = placa
        progress(f"  {placa}: {len(pontos)} pontos GPS")
        candidatos.append(
            {
                "placa": placa,
                "veiculo": texto,
                "id": ativo.get("id"),
                "tipo": 3,
                "descricao": texto,
                "pontos": pontos,
                "fonte": "segtrack",
            }
        )

    filtrados = _filtrar_compactadores(candidatos, settings.segtrack_veiculos, placas_inlog)
    if settings.segtrack_veiculos and candidatos and not filtrados:
        placas = ", ".join(_fmt_placa(item.get("placa") or "") for item in candidatos)
        raise RuntimeError(
            f"A placa {placas} existe nesta SEGTRACK, mas o GPS não está em Aparecida."
        )
    return filtrados, ativos, {"historico": True}
