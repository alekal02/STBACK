from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from .navegador import abrir_chromium, preparar_navegador
from .export import _xml
from .config import rotulo_periodo
from .gps import tracos_realistas
from .pdf_mapa import PALETA, completar_extremos_rota, hora_curta, html_mapa_print
from .relatorio_diario import coberturas_relatorio
from .pdf_cache import (
    concorrencia,
    digest,
    gravar_cache,
    gravar_progresso,
    ler_cache,
    limpar_jpeg_orfaos,
    pdf_inteiro_valido,
)
from .verificacao import rotulo_papel_relatorio

_TPL = Path(__file__).resolve().parent / "templates" / "relatorio"
_VIEWPORT = {"width": 1600, "height": 1180}
_SETOR_RE = re.compile(r"\b\d{2}[A-Z]{2,4}\b", re.I)


def gerar_pdf_dia(pasta: Path) -> Path:
    pasta = Path(pasta)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _gerar_pdf_dia_sync(pasta)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_gerar_pdf_dia_sync, pasta).result()


def capturar_mapas_dia(pasta: Path) -> dict[str, Any]:
    pasta = Path(pasta)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _capturar_mapas_dia_sync(pasta)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_capturar_mapas_dia_sync, pasta).result()


def _pasta_mapas(pasta: Path) -> Path:
    return Path(pasta) / "mapas_relatorio"


def _carregar_mapas_cache(pasta: Path) -> dict[str, Any] | None:
    pasta = Path(pasta)
    meta = _pasta_mapas(pasta) / "meta.json"
    if not meta.is_file():
        return None
    fontes = [pasta / "relatorio-diario.json", pasta / "rotas.json", pasta / "mapa.html"]
    existentes = [arq for arq in fontes if arq.is_file()]
    if existentes and meta.stat().st_mtime < max(arq.stat().st_mtime for arq in existentes):
        return None
    try:
        info = json.loads(meta.read_text(encoding="utf-8"))
        if int(info.get("versao") or 0) < 9:
            return None
        pasta_m = _pasta_mapas(pasta)
        geral = (pasta_m / "geral.jpg").read_bytes()
        folhas = []
        for row in info.get("folhas") or []:
            img = pasta_m / str(row.get("arquivo") or "")
            if not img.is_file():
                return None
            item = {k: v for k, v in row.items() if k != "arquivo"}
            item["img"] = img.read_bytes()
            folhas.append(item)
    except Exception:
        return None
    grade = _setores_grade(pasta)
    if grade and len(folhas) < len(grade):
        print("Cache sem todas as rotas da grade. Recapturando as folhas...", flush=True)
        return None
    diario = pasta / "relatorio-diario.json"
    dados = json.loads(diario.read_text(encoding="utf-8")) if diario.is_file() else {}
    print(f"Reusando {len(folhas)} mapas já capturados...", flush=True)
    return {
        "dados": dados,
        "dia_br": info.get("dia_br") or "",
        "fonte": info.get("fonte") or "",
        "frota": info.get("frota") or [],
        "geral": geral,
        "folhas": folhas,
        "legenda": info.get("legenda") or "",
    }


def _salvar_mapas_cache(pasta: Path, cap: dict[str, Any]) -> None:
    pasta_m = _pasta_mapas(pasta)
    pasta_m.mkdir(parents=True, exist_ok=True)
    (pasta_m / "geral.jpg").write_bytes(cap["geral"])
    folhas_meta = []
    for i, folha in enumerate(cap.get("folhas") or [], start=1):
        nome = f"folha-{i:02d}.jpg"
        (pasta_m / nome).write_bytes(folha["img"])
        folhas_meta.append({k: v for k, v in folha.items() if k != "img"} | {"arquivo": nome})
    frota_leve = []
    for rota in cap.get("frota") or []:
        frota_leve.append(
            {
                "veiculo": rota.get("veiculo"),
                "placa": rota.get("placa"),
                "trechos": [1] if rota.get("trechos") or rota.get("executada") else [],
                "executada": [1] if rota.get("executada") or rota.get("trechos") else [],
            }
        )
    (pasta_m / "meta.json").write_text(
        json.dumps(
            {
                "versao": 9,
                "dia_br": cap.get("dia_br"),
                "fonte": cap.get("fonte"),
                "legenda": cap.get("legenda"),
                "frota": frota_leve,
                "folhas": folhas_meta,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _capturar_mapas_dia_sync(pasta: Path) -> dict[str, Any]:
    cache = _carregar_mapas_cache(pasta)
    if cache:
        return cache
    mapa = pasta / "mapa.html"
    diario = pasta / "relatorio-diario.json"
    if not mapa.is_file():
        raise FileNotFoundError(f"Mapa não encontrado: {mapa}")
    if not diario.is_file():
        raise FileNotFoundError(f"Relatório não encontrado: {diario}")

    dados = json.loads(diario.read_text(encoding="utf-8"))
    dia_iso = str(dados.get("data") or pasta.name)
    hora_inicio = str(dados.get("hora_inicio") or "00:00")
    hora_fim = str(dados.get("hora_fim") or "23:59")
    data_fim_raw = str(dados.get("data_fim") or "").strip() or None
    try:
        dia = date.fromisoformat(dia_iso)
        dia_br = str(dados.get("periodo") or rotulo_periodo(dia, hora_inicio, hora_fim, data_fim_raw))
    except ValueError:
        dia_br = dia_iso

    rotas_mapa = json.loads(_extrair_rotas_js(mapa))
    detalhes = _paginas_detalhe(pasta, rotas_mapa)
    fonte = str(dados.get("fonte") or "")
    if not fonte:
        if pasta.parent.name == "argos":
            fonte = "argos"
        elif pasta.parent.name == "segtrack":
            fonte = "segtrack"
        else:
            fonte = "inlog"

    preparar_navegador()
    with sync_playwright() as pw:
        browser = abrir_chromium(pw, headless=True)
        try:
            cap = _montar_capturas(browser, dados, dia_br, fonte, rotas_mapa, detalhes, pasta)
        finally:
            browser.close()
    _salvar_mapas_cache(pasta, cap)
    return cap


def _arred(valor: Any, casas: int = 5) -> Any:
    if isinstance(valor, float):
        return round(valor, casas)
    if isinstance(valor, list):
        return [_arred(item, casas) for item in valor]
    if isinstance(valor, dict):
        return {chave: _arred(item, casas) for chave, item in valor.items()}
    return valor


def _pacote_mapa(item: dict[str, Any], *, detalhe: bool, zoom: int) -> dict[str, Any]:
    leve = _simplificar_rota(item, 1400 if detalhe else 450, 400 if detalhe else 180, detalhe=detalhe)
    caminhoes = []
    for cam in leve.get("caminhoes") or []:
        caminhoes.append(
            {
                "placa": cam.get("placa") or cam.get("veiculo") or "",
                "trechos": cam.get("trechos") or [],
                "passagens": cam.get("passagens") or [],
                "ponto_inicio": cam.get("ponto_inicio") or [],
                "ponto_fim": cam.get("ponto_fim") or [],
                "hora_inicio": cam.get("hora_inicio") or "",
                "hora_fim": cam.get("hora_fim") or "",
                "papel": cam.get("papel") or "",
            }
        )
    return _arred(
        {
            "detalhe": detalhe,
            "zoom": zoom,
            "tela": [1600, 1180],
            "poligonos": leve.get("poligonos") or [],
            "trechos": leve.get("trechos") or [],
            "vias": leve.get("vias") or [],
            "caminhoes": caminhoes,
        }
    )


def _texto_folha(item: dict[str, Any]) -> dict[str, Any]:
    campos = _folha_campos(item)
    campos.pop("caminhoes", None)
    return {
        "setor": campos.get("setor"),
        "placa": campos.get("placa"),
        "percentual": campos.get("percentual"),
        "km_coleta": campos.get("km_coleta"),
        "km_total": campos.get("km_total"),
        "horario": campos.get("horario"),
        "hora_inicio": campos.get("hora_inicio"),
        "hora_fim": campos.get("hora_fim"),
        "veredicto": campos.get("veredicto"),
        "sem_gps": campos.get("sem_gps"),
    }


def _hash_pdf(dados: dict[str, Any], geral_hash: str, folhas: list[tuple[str, dict[str, Any]]]) -> str:
    return digest(
        {
            "visual": 1,
            "geral": geral_hash,
            "linhas": [
                {
                    "setor": row.get("Setor"),
                    "status": row.get("Status"),
                    "pct": row.get("% Concluída") or row.get("percentual"),
                }
                for row in (dados.get("linhas") or [])
            ],
            "folhas": [{"mapa": h, "texto": texto} for h, texto in folhas],
        }
    )


def _montar_capturas(
    browser,
    dados: dict[str, Any],
    dia_br: str,
    fonte: str,
    rotas_mapa: list[dict[str, Any]],
    detalhes: list[dict[str, Any]],
    pasta: Path | None = None,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    frota = _rotas_frota(rotas_mapa, detalhes)
    cache_dir = (Path(pasta) / "mapas_relatorio") if pasta else None
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
    geral_hash = digest(
        _arred(
            {
                "zoom": 13,
                "tela": [1600, 1180],
                "rotas": [
                    {
                        "trechos": rota.get("trechos") or [],
                        "executada": rota.get("executada") or [],
                        "poligonos": rota.get("poligonos") or [],
                    }
                    for rota in frota
                ],
            }
        )
    )
    geral_nome = f"geral-{geral_hash[:20]}.jpg"
    geral_arq = cache_dir / geral_nome if cache_dir else None
    if geral_arq and geral_arq.is_file():
        print("[MAP] geral CACHE HIT", flush=True)
        geral = geral_arq.read_bytes()
    else:
        t = time.perf_counter()
        print(f"[MAP] geral RENDER ({len(frota)} camadas)", flush=True)
        geral = _capturar_mapa(browser, frota, max_zoom=13, detalhe=False)
        if geral_arq:
            geral_arq.write_bytes(geral)
        print(f"[MAP] geral RENDER {time.perf_counter() - t:.1f}s", flush=True)

    total = max(1, len(detalhes))
    folhas_idx: list[tuple[int, dict[str, Any]]] = []
    pendentes: list[tuple[int, dict[str, Any], Path | None]] = []
    hashes: list[tuple[str, dict[str, Any]]] = []
    manter = {geral_nome}
    for i, item in enumerate(detalhes):
        h = digest(_pacote_mapa(item, detalhe=True, zoom=16))
        hashes.append((h, _texto_folha(item)))
        nome = f"{h[:20]}.jpg"
        manter.add(nome)
        arq = cache_dir / nome if cache_dir else None
        rotulo = str(item.get("setor") or item.get("placa") or i + 1)
        if arq and arq.is_file():
            print(f"[MAP] {rotulo} CACHE HIT", flush=True)
            folhas_idx.append((i, {**_folha_campos(item), "img": arq.read_bytes()}))
        else:
            pendentes.append((i, item, arq))
    if pasta:
        feitos = total - len(pendentes)
        gravar_progresso(pasta, 15 + int(60 * feitos / total), f"Mapas em cache {feitos}/{total}")

    lote = concorrencia()
    for ini in range(0, len(pendentes), lote):
        fatia = pendentes[ini : ini + lote]
        t = time.perf_counter()
        capturadas = _capturar_lote(browser, [item for _i, item, _arq in fatia])
        for (i, item, arq), folha in zip(fatia, capturadas):
            if arq:
                arq.write_bytes(folha["img"])
            rotulo = str(item.get("setor") or item.get("placa") or i + 1)
            print(f"[MAP] {rotulo} RENDER {time.perf_counter() - t:.1f}s", flush=True)
            folhas_idx.append((i, folha))
        if pasta:
            feitos = total - len(pendentes) + ini + len(fatia)
            gravar_progresso(pasta, 15 + int(67 * feitos / total), f"Gerando mapas {min(feitos, total)}/{total}")
    folhas_idx.sort(key=lambda par: par[0])
    if cache_dir:
        limpar_jpeg_orfaos(pasta, manter)
    print(f"[PDF] Mapas: {time.perf_counter() - t0:.1f}s", flush=True)
    return {
        "dados": dados,
        "dia_br": dia_br,
        "fonte": fonte,
        "frota": frota,
        "geral": geral,
        "folhas": [folha for _i, folha in folhas_idx],
        "legenda": _legenda_html(frota),
        "pdf_hash": _hash_pdf(dados, geral_hash, hashes),
        "mapas": {h[:20]: f"{h[:20]}.jpg" for h, _texto in hashes} | {"geral": geral_nome},
    }


def _gerar_pdf_dia_sync(pasta: Path) -> Path:
    pasta = Path(pasta)
    dest = pasta / "relatorio-rotas.pdf"
    t0 = time.perf_counter()
    pronto = pdf_inteiro_valido(pasta)
    if pronto:
        print(f"[PDF] CACHE HIT pdf {time.perf_counter() - t0:.1f}s", flush=True)
        print(f"[PDF] TOTAL: {time.perf_counter() - t0:.1f}s", flush=True)
        gravar_progresso(pasta, 100, "Concluído")
        return pronto
    gravar_progresso(pasta, 5, "Preparando dados")
    mapa = pasta / "mapa.html"
    diario = pasta / "relatorio-diario.json"
    dados = json.loads(diario.read_text(encoding="utf-8"))
    dia_iso = str(dados.get("data") or pasta.name)
    hora_inicio = str(dados.get("hora_inicio") or "00:00")
    hora_fim = str(dados.get("hora_fim") or "23:59")
    data_fim_raw = str(dados.get("data_fim") or "").strip() or None
    try:
        dia = date.fromisoformat(dia_iso)
        dia_br = str(dados.get("periodo") or rotulo_periodo(dia, hora_inicio, hora_fim, data_fim_raw))
    except ValueError:
        dia_br = dia_iso
    gravar_progresso(pasta, 10, "Verificando cache dos mapas")
    rotas_mapa = json.loads(_extrair_rotas_js(mapa))
    detalhes = _paginas_detalhe(pasta, rotas_mapa)
    fonte = str(dados.get("fonte") or "")
    if not fonte:
        fonte = pasta.parent.name if pasta.parent.name in {"argos", "segtrack", "diurno", "noturno"} else "inlog"
    preparar_navegador()
    with sync_playwright() as pw:
        browser = abrir_chromium(pw, headless=True)
        try:
            cap = _montar_capturas(browser, dados, dia_br, fonte, rotas_mapa, detalhes, pasta)
            guardado = ler_cache(pasta)
            if guardado.get("pdf_hash") == cap.get("pdf_hash") and dest.is_file() and dest.stat().st_size >= 20_000:
                print("[PDF] CACHE HIT montagem", flush=True)
            else:
                t_pdf = time.perf_counter()
                gravar_progresso(pasta, 90, "Montando PDF")
                folhas = [{**folha, "img": _data_jpeg(folha["img"])} for folha in cap["folhas"]]
                htmls = _html_pdf(
                    cap["dia_br"],
                    cap["dados"],
                    _data_jpeg(cap["geral"]),
                    folhas,
                    cap["fonte"],
                    cap["legenda"],
                )
                parcial = pasta / "relatorio-rotas.pdf.part"
                parcial.write_bytes(_pdf_paginas(browser, htmls))
                parcial.replace(dest)
                print(f"[PDF] Montagem: {time.perf_counter() - t_pdf:.1f}s", flush=True)
            gravar_cache(pasta, str(cap.get("pdf_hash") or ""), cap.get("mapas") or {})
        finally:
            browser.close()
    gravar_progresso(pasta, 100, "Concluído")
    print(f"[PDF] TOTAL: {time.perf_counter() - t0:.1f}s", flush=True)
    return dest


def _data_jpeg(dados: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(dados).decode("ascii")


def gerar_pdf_inspecao(
    pasta: Path,
    rotas: list[dict[str, Any]],
    *,
    slug: str,
    periodo: str,
    fonte: str = "unificado",
) -> Path:
    """Uma folha: polígono oficial + trajetórias do ciclo (06:00 → 05:59)."""
    pasta = Path(pasta)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _gerar_pdf_inspecao_sync(pasta, rotas, slug=slug, periodo=periodo, fonte=fonte)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(
            _gerar_pdf_inspecao_sync, pasta, rotas, slug, periodo, fonte
        ).result()


def _gerar_pdf_inspecao_sync(
    pasta: Path,
    rotas: list[dict[str, Any]],
    slug: str,
    periodo: str,
    fonte: str = "unificado",
) -> Path:
    pasta = Path(pasta)
    pasta.mkdir(parents=True, exist_ok=True)
    dest = pasta / f"inspecao-{slug}.pdf"
    detalhes = [dict(rota) for rota in (rotas or [])]
    preparar_navegador()
    with sync_playwright() as pw:
        browser = abrir_chromium(pw, headless=True)
        try:
            folhas_raw = _capturar_lote(browser, detalhes) if detalhes else []
            folhas = [{**folha, "img": _data_jpeg(folha["img"])} for folha in folhas_raw]
            htmls = _html_pdf_inspecao(periodo, folhas, fonte)
            dest.write_bytes(_pdf_paginas(browser, htmls))
        finally:
            browser.close()
    return dest


def _html_pdf_inspecao(periodo: str, folhas: list[dict[str, Any]], fonte: str) -> list[str]:
    gerado = datetime.now().strftime("%d/%m/%Y %H:%M")
    nome_fonte = _fonte_nome(fonte)
    header = _tpl("header.html", FONTE=_xml(nome_fonte), DIA=_xml(periodo))
    paginas = []
    for folha in folhas:
        pct = float(folha.get("percentual") or 0)
        sit, cls = _situacao(pct)
        rotulo, sim_nao = _rotulo_conclusao(pct)
        codigo, nome = _codigo_setor(str(folha.get("setor") or ""))
        titulo = f"{codigo}" if codigo else "Rota"
        if nome:
            titulo = f"{titulo}  ·  {nome}"
        img = str(folha.get("img") or "")
        mapa_html = (
            f'<img class="mapa rota" src="{img}" alt="Rota" />'
            if img
            else '<div class="mapa-vazio">Nenhum GPS neste polígono no ciclo.</div>'
        )
        paginas.append(
            _documento(
                _tpl(
                    "rota.html",
                    HEADER=header,
                    TITULO=_xml(titulo),
                    TURNO=_xml(str(folha.get("rota") or "")),
                    DIA=_xml(periodo),
                    MAPA_HTML=mapa_html,
                    VEICULO=_xml(str(folha.get("placa") or folha.get("veiculo") or "—")),
                    CLS=cls,
                    PCT=f"{pct:.0f}",
                    KM_COLETA=_xml(str(folha.get("km_coleta") or "—")),
                    KM_TOTAL=_xml(str(folha.get("km_total") or "—")),
                    ROTULO=rotulo,
                    SIM_NAO=sim_nao,
                    SITUACAO=sit,
                    HORA_INI=_xml(str(folha.get("hora_inicio") or "—")),
                    HORA_FIM=_xml(str(folha.get("hora_fim") or "—")),
                    LEGENDA_PLACAS=_xml(_legenda_placas(folha)),
                    AVISO_ONTEM="",
                    AVISO=_xml(
                        "Ciclo 06:00 → 05:59 do dia seguinte. Faixa preta + laranja = polígono oficial. "
                        "Cada cor = rastreador que entrou no setor. Cobertura GPS, não prova de coleta."
                    ),
                    JUSTIFICATIVA="",
                    GERADO=_xml(gerado),
                    FONTE=_xml(nome_fonte),
                )
            )
        )
    if not paginas:
        paginas.append(
            _documento(
                _tpl(
                    "rota.html",
                    HEADER=header,
                    TITULO="Rota",
                    TURNO="",
                    DIA=_xml(periodo),
                    MAPA_HTML='<div class="mapa-vazio">Sem mapa para esta rota.</div>',
                    VEICULO="—",
                    CLS="no",
                    PCT="0",
                    KM_COLETA="—",
                    KM_TOTAL="—",
                    ROTULO="—",
                    SIM_NAO="NÃO",
                    SITUACAO="NÃO INICIADA",
                    HORA_INI="—",
                    HORA_FIM="—",
                    LEGENDA_PLACAS="",
                    AVISO_ONTEM="",
                    AVISO=_xml("Sem GPS neste polígono no ciclo."),
                    JUSTIFICATIVA="",
                    GERADO=_xml(gerado),
                    FONTE=_xml(nome_fonte),
                )
            )
        )
    return paginas


def _pdf_paginas(browser, htmls: list[str]) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    page = browser.new_page(viewport={"width": 900, "height": 1270})
    try:
        for html in htmls:
            page.set_content(html, wait_until="domcontentloaded")
            blob = page.pdf(
                format="A4",
                landscape=False,
                print_background=True,
                margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
            )
            writer.append(BytesIO(blob))
    finally:
        page.close()
    saida = BytesIO()
    writer.write(saida)
    return saida.getvalue()


def _fmt_km(valor: Any) -> str:
    if valor in (None, "", "---"):
        return "---"
    texto = str(valor)
    if "km" in texto.lower():
        return texto
    try:
        return f"{float(str(valor).replace(',', '.')):.1f} km".replace(".", ",")
    except (TypeError, ValueError):
        return texto


def _pct_num(row: dict[str, Any] | Any) -> float:
    if isinstance(row, dict):
        if row.get("percentual") not in (None, ""):
            try:
                return float(row["percentual"])
            except (TypeError, ValueError):
                pass
        texto = str(row.get("% Concluída") or "")
    else:
        texto = str(row or "")
    texto = texto.replace("%", "").replace(",", ".").strip()
    try:
        return float(texto)
    except (TypeError, ValueError):
        return 0.0


def _rotas_frota(rotas_mapa: list[dict[str, Any]], detalhes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    frota: list[dict[str, Any]] = []
    for rota in rotas_mapa:
        item = dict(rota)
        item["poligonos"] = []
        frota.append(item)
    vistos: set[str] = set()
    for item in detalhes:
        setor = str(item.get("setor") or "")
        if not setor or setor in vistos or setor == "Percurso do dia":
            continue
        vistos.add(setor)
        polys = item.get("poligonos") or []
        if polys:
            frota.append(
                {
                    "veiculo": "",
                    "trechos": [],
                    "executada": [],
                    "poligonos": polys[:1],
                }
            )
    return frota or rotas_mapa


def _pct_item(item: dict[str, Any], cob: dict[str, Any] | None = None) -> float:
    for origem in (cob or {}, item):
        for chave in ("percentual", "percentual_concluido"):
            if origem.get(chave) not in (None, ""):
                try:
                    return float(origem[chave])
                except (TypeError, ValueError):
                    continue
    return 0.0


def _km_item(item: dict[str, Any], cob: dict[str, Any] | None, chave: str) -> Any:
    for origem in (cob or {}, item):
        if origem.get(chave) not in (None, ""):
            return origem.get(chave)
    return None


def _setores_grade(pasta: Path) -> dict[str, dict[str, Any]]:
    diario = pasta / "relatorio-diario.json"
    mapa: dict[str, dict[str, Any]] = {}
    if not diario.is_file():
        return mapa
    try:
        dados = json.loads(diario.read_text(encoding="utf-8"))
    except Exception:
        return mapa
    for row in dados.get("linhas") or []:
        if str(row.get("Na grade") or "sim").strip().lower() in {"não", "nao", "false", "0"}:
            continue
        codigo, _nome = _codigo_setor(str(row.get("Setor") or ""))
        if codigo:
            mapa[codigo] = row
    return mapa


def _poligonos_oficiais() -> dict[str, list]:
    global _POLYS_OFICIAIS
    cache = globals().get("_POLYS_OFICIAIS")
    if cache is not None:
        return cache
    from .mapa_oficial import carregar_setores

    cache = {str(s.get("codigo") or ""): s.get("poligonos") or [] for s in carregar_setores(None)}
    globals()["_POLYS_OFICIAIS"] = cache
    return cache


def _folha_vazia(row: dict[str, Any], codigo: str, polys: list) -> dict[str, Any]:
    veiculo = str(row.get("Veículo") or row.get("veiculo") or "").strip()
    placa = str(row.get("Placa") or row.get("placa") or "").strip()
    if veiculo in {"", "—", "-"}:
        veiculo = "Sem rastreamento"
        placa = placa if placa not in {"", "—", "-"} else "—"
    return {
        "veiculo": veiculo,
        "placa": placa,
        "setor": row.get("Setor") or row.get("setor") or codigo,
        "rota": row.get("Rota") or row.get("rota") or "",
        "percentual": _pct_num(row),
        "veredicto": row.get("Status") or row.get("veredicto") or "Não iniciada",
        "execucao": row.get("Status") or row.get("execucao") or "NÃO FEITA",
        "km_coleta": row.get("Km Coleta") or row.get("km_coleta") or "—",
        "km_total": row.get("Km Total") or row.get("km_total") or "—",
        "trechos": [],
        "executada": [],
        "poligonos": polys[:1],
        "horario": row.get("Horário") or row.get("horario") or "",
        "sem_gps": True,
        "caminhoes": [],
        "aviso": "Nenhum GPS neste polígono. Se a coleta foi feita por caminhão sem rastreador, justifique abaixo.",
    }


def _completar_folhas_grade(pasta: Path, saida: list[dict[str, Any]], grade: dict[str, dict[str, Any]]) -> None:
    vistos = {_codigo_setor(str(row.get("setor") or ""))[0] for row in saida}
    oficiais = _poligonos_oficiais()
    for codigo, row in sorted(grade.items()):
        if codigo in vistos:
            continue
        saida.append(_folha_vazia(row, codigo, oficiais.get(codigo) or []))


def _paginas_detalhe(pasta: Path, rotas_mapa: list[dict[str, Any]]) -> list[dict[str, Any]]:
    arquivo = pasta / "rotas.json"
    saida: list[dict[str, Any]] = []
    rotas: list[dict[str, Any]] = []
    grade = _setores_grade(pasta)
    oficiais = _poligonos_oficiais() if grade else {}
    if arquivo.is_file():
        try:
            bruto = json.loads(arquivo.read_text(encoding="utf-8"))
        except Exception:
            bruto = {}
        rotas = list(bruto.get("rotas") or [])
    extra = pasta.parent.name in {"argos", "segtrack"} and not any(
        item.get("caminhoes") or item.get("codigo_rota") or item.get("na_grade")
        for item in rotas
    )
    for item in rotas:
        codigo, _nome = _codigo_setor(str(item.get("setor") or item.get("codigo_rota") or ""))
        na_grade = item.get("na_grade")
        if na_grade is None:
            na_grade = bool(codigo and codigo in grade)
        execucao = str(item.get("execucao") or item.get("veredicto") or "").upper()
        if "APOIO" in execucao and not na_grade:
            continue
        visiveis = coberturas_relatorio(item, max_n=5)
        if not visiveis:
            visiveis = [
                cob
                for cob in (item.get("coberturas_inlog") or [])
                if str(cob.get("execucao") or cob.get("veredicto") or "").upper() in {"FEITA", "PARCIAL", "APOIO"}
                or _pct_item(item, cob) >= 25
            ]
        if not visiveis and (_pct_item(item) >= 25 or na_grade or item.get("ontem")):
            visiveis = [
                {
                    "setor": item.get("setor"),
                    "rota": item.get("rota"),
                    "percentual": _pct_item(item),
                    "execucao": item.get("execucao") or item.get("veredicto"),
                    "veredicto": item.get("veredicto"),
                    "km_coleta": item.get("km_coleta"),
                    "km_total": item.get("km_total"),
                    "trechos_gps": item.get("trechos") or [],
                    "poligonos": item.get("poligonos") or oficiais.get(codigo) or [],
                    "caminhoes": item.get("caminhoes") or [],
                }
            ]
        if not visiveis:
            continue
        pontos = item.get("pontos_traco") or item.get("pontos_dia") or item.get("pontos") or []
        trechos_dia = [t for t in (item.get("trechos") or []) if t]
        if not trechos_dia:
            trechos_dia = tracos_realistas(pontos, salto_max_m=95, tolerancia_m=6) if pontos else []
        if extra and trechos_dia:
            polys = [anel for cob in visiveis for anel in (cob.get("poligonos") or [])[:1]][:3]
            saida.append(
                {
                    "veiculo": item.get("veiculo"),
                    "placa": item.get("placa"),
                    "setor": "Percurso do dia",
                    "rota": item.get("rota") or "Dia completo",
                    "percentual": _pct_item(item, visiveis[0]),
                    "veredicto": visiveis[0].get("execucao") or visiveis[0].get("veredicto") or "",
                    "km_coleta": _km_item(item, visiveis[0], "km_coleta"),
                    "km_total": _km_item(item, visiveis[0], "km_total"),
                    "trechos": trechos_dia,
                    "executada": [pt for trecho in trechos_dia for pt in trecho],
                    "poligonos": polys,
                }
            )
        for cob in visiveis:
            trechos = [t for t in (cob.get("trechos_gps") or []) if t]
            if extra and trechos_dia:
                from .traco_agente import recortar_trechos_ao_setor

                trechos = recortar_trechos_ao_setor(trechos_dia, (cob.get("poligonos") or [])[:3], buffer_m=40)
            lacunas: list = []
            setor_nome = cob.get("setor") or item.get("setor")
            codigo_cob, _ = _codigo_setor(str(setor_nome or ""))
            polys = [anel for anel in (cob.get("poligonos") or item.get("poligonos") or oficiais.get(codigo_cob) or []) if anel]
            if not trechos and not polys and not na_grade:
                continue
            sem_gps = not trechos
            saida.append(
                {
                    "veiculo": item.get("veiculo") or cob.get("veiculo") or ("Sem rastreamento" if sem_gps else ""),
                    "placa": item.get("placa") or cob.get("placa") or "—",
                    "setor": setor_nome,
                    "rota": cob.get("rota") or item.get("rota") or "",
                    "percentual": _pct_item(item, cob),
                    "veredicto": cob.get("execucao") or cob.get("veredicto") or item.get("veredicto") or "",
                    "km_coleta": _km_item(item, cob, "km_coleta"),
                    "km_total": _km_item(item, cob, "km_total"),
                    "trechos": trechos,
                    "lacunas": lacunas,
                    "executada": [pt for trecho in trechos for pt in trecho],
                    "poligonos": polys,
                    "vias": [],
                    "horario": cob.get("horario") or item.get("horario") or "",
                    "hora_inicio": cob.get("hora_inicio") or item.get("hora_inicio") or "",
                    "hora_fim": cob.get("hora_fim") or item.get("hora_fim") or "",
                    "ponto_inicio": cob.get("ponto_inicio") or item.get("ponto_inicio") or [],
                    "ponto_fim": cob.get("ponto_fim") or item.get("ponto_fim") or [],
                    "sem_gps": sem_gps,
                    "aviso": (
                        "Nenhum GPS neste polígono. Se a coleta foi feita por caminhão sem rastreador, justifique abaixo."
                        if sem_gps
                        else ""
                    ),
                    "caminhoes": cob.get("caminhoes") or item.get("caminhoes") or [],
                    "apoio": cob.get("apoio") or item.get("apoio") or "",
                    "ontem": bool(item.get("ontem")),
                }
            )
    _completar_folhas_grade(pasta, saida, grade)
    if saida:
        def _ordem(row: dict[str, Any]) -> tuple:
            setor = str(row.get("setor") or "")
            dia = 0 if setor == "Percurso do dia" else 1
            return (dia, setor, str(row.get("veiculo") or ""))

        saida.sort(key=_ordem)
        for row in saida:
            completar_extremos_rota(row)
        return saida
    return [
        {
            **rota,
            "percentual": _pct_item(rota),
            "km_coleta": rota.get("km_coleta"),
            "km_total": rota.get("km_total"),
        }
        for rota in rotas_mapa
        if (rota.get("trechos") or rota.get("executada") or rota.get("poligonos"))
        and _pct_item(rota) >= 25
    ]


def _extrair_rotas_js(mapa: Path) -> str:
    texto = mapa.read_text(encoding="utf-8")
    chave = "const rotas = "
    inicio = texto.find(chave)
    if inicio < 0:
        raise RuntimeError("Não achei os dados do mapa.")
    inicio += len(chave)
    fim = texto.find(";\n    const mapa", inicio)
    if fim < 0:
        fim = texto.find("; const mapa", inicio)
    if fim < 0:
        raise RuntimeError("Não achei o fim dos dados do mapa.")
    return texto[inicio:fim]


def _thin(pts: list, max_pts: int) -> list:
    if not pts or len(pts) <= max_pts:
        return pts
    passo = max(1, len(pts) // max_pts)
    saida = list(pts[::passo])
    if saida[-1] != pts[-1]:
        saida.append(pts[-1])
    return saida


def _simplificar_rota(rota: dict[str, Any], max_linha: int = 450, max_poly: int = 250, *, detalhe: bool = True) -> dict[str, Any]:
    trechos = [list(t) for t in (rota.get("trechos") or []) if t and len(t) >= 2]
    item = {
        "veiculo": rota.get("veiculo"),
        "placa": rota.get("placa"),
        "setor": rota.get("setor"),
        "rota": rota.get("rota"),
        "percentual": rota.get("percentual") or 0,
        "veredicto": rota.get("veredicto") or "",
        "km_coleta": rota.get("km_coleta"),
        "km_total": rota.get("km_total"),
        "trechos": trechos,
        "lacunas": [],
        "executada": [list(p) for p in (rota.get("executada") or []) if p],
        "poligonos": [_thin(p, max_poly) for p in (rota.get("poligonos") or []) if p],
        "vias": [],
        "horario": rota.get("horario") or "",
        "hora_inicio": rota.get("hora_inicio") or "",
        "hora_fim": rota.get("hora_fim") or "",
        "ponto_inicio": rota.get("ponto_inicio") or [],
        "ponto_fim": rota.get("ponto_fim") or [],
        "inspecao": bool(rota.get("inspecao")),
        "codigo_rota": rota.get("codigo_rota") or "",
        "caminhoes": [
            {
                **cam,
                "trechos": [list(t) for t in (cam.get("trechos") or []) if t and len(t) >= 2],
                "lacunas": [],
            }
            for cam in (rota.get("caminhoes") or [])
        ],
        "bounds": None,
    }
    if not item["trechos"] and item["executada"]:
        from .traco_agente import montar_traco_continuo

        pontos = [
            {"latitude": p[0], "longitude": p[1]}
            for p in item["executada"]
            if p and len(p) >= 2
        ]
        item["trechos"] = montar_traco_continuo(pontos)["trechos"]
    return completar_extremos_rota(item)


def _esperar_mapa(page, detalhe: bool) -> None:
    try:
        page.wait_for_function("() => window.__capturaPronta === true", timeout=4_000)
    except Exception:
        print("[MAP] timeout de tile; a foto segue com o que já carregou", flush=True)


def _folha_campos(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "veiculo": str(item.get("veiculo") or ""),
        "placa": str(item.get("placa") or ""),
        "setor": str(item.get("setor") or ""),
        "rota": str(item.get("rota") or ""),
        "percentual": float(item.get("percentual") or 0),
        "km_coleta": _fmt_km(item.get("km_coleta")),
        "km_total": _fmt_km(item.get("km_total")),
        "veredicto": str(item.get("veredicto") or ""),
        "execucao": str(item.get("execucao") or item.get("veredicto") or ""),
        "horario": str(item.get("horario") or ""),
        "hora_inicio": str(item.get("hora_inicio") or ""),
        "hora_fim": str(item.get("hora_fim") or ""),
        "ponto_inicio": item.get("ponto_inicio") or [],
        "ponto_fim": item.get("ponto_fim") or [],
        "sem_gps": bool(item.get("sem_gps")),
        "aviso": str(item.get("aviso") or ""),
        "caminhoes": item.get("caminhoes") or [],
        "ontem": bool(item.get("ontem")),
    }


def _capturar_mapa(browser, rotas: list[dict[str, Any]], *, max_zoom: int, detalhe: bool) -> bytes:
    leve = [_simplificar_rota(rota, 1400 if detalhe else 450, 400 if detalhe else 180, detalhe=detalhe) for rota in rotas]
    page = browser.new_page(viewport=_VIEWPORT, device_scale_factor=1)
    try:
        page.set_content(html_mapa_print(leve, max_zoom, detalhe), wait_until="domcontentloaded")
        _esperar_mapa(page, detalhe)
        return page.locator("#folha").screenshot(type="jpeg", quality=72)
    finally:
        page.close()


def _capturar_lote(browser, itens: list[dict[str, Any]]) -> list[dict[str, Any]]:
    paginas = []
    try:
        for item in itens:
            leve = [_simplificar_rota(item, 1400, 400, detalhe=True)]
            page = browser.new_page(viewport=_VIEWPORT, device_scale_factor=1)
            page.set_content(html_mapa_print(leve, 16, True), wait_until="domcontentloaded")
            paginas.append((page, item))
        folhas = []
        for page, item in paginas:
            _esperar_mapa(page, True)
            folhas.append({**_folha_campos(item), "img": page.locator("#folha").screenshot(type="jpeg", quality=72)})
        return folhas
    finally:
        for page, _item in paginas:
            try:
                page.close()
            except Exception:
                pass


def _linhas_capa(linhas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Toda a grade do dia, inclusive zerada, só passagem e apoio (com %)."""
    saida = []
    for row in linhas:
        setor = str(row.get("Setor") or "")
        if not _SETOR_RE.search(setor):
            continue
        if str(row.get("Na grade") or "sim").strip().lower() in {"não", "nao", "false", "0"}:
            continue
        saida.append(row)
    return saida


def _linhas_dia(linhas: list[dict[str, Any]], fonte: str = "") -> list[dict[str, Any]]:
    if (fonte or "").lower() not in {"unificado", "segtrack"}:
        return _linhas_capa(linhas)
    saida = []
    for row in linhas:
        setor = str(row.get("Setor") or "")
        if not _SETOR_RE.search(setor):
            continue
        if str(row.get("Na grade") or "sim").strip().lower() in {"não", "nao", "false", "0"}:
            continue
        saida.append(row)
    return saida


def _situacao_linha(row: dict[str, Any]) -> tuple[str, str]:
    st = str(row.get("Status") or "").upper()
    if "APOIO" in st:
        return "APOIO", "apoio"
    if "PASSAGEM" in st:
        return "SÓ PASSAGEM", "no"
    if "SEM GPS" in st or "SEM RASTRE" in st or "NÃO INICIADA" in st or "NAO INICIADA" in st:
        return "NÃO INICIADA", "no"
    if "PARCIALMENTE" in st:
        return "CONCLUÍDA PARCIALMENTE", "mid"
    if "INÍCIO" in st or "INICIO" in st:
        return "INÍCIO DE COLETA", "mid"
    if st.startswith("CONCLUÍDA") or st == "CONCLUIDA":
        return "CONCLUÍDA", "ok"
    return _situacao(_pct_num(row))


def _situacao(pct: float) -> tuple[str, str]:
    if pct >= 80:
        return "CONCLUÍDA", "ok"
    if pct >= 45:
        return "CONCLUÍDA PARCIALMENTE", "mid"
    if pct >= 25:
        return "INÍCIO DE COLETA", "mid"
    return "NÃO INICIADA", "no"


def _rotulo_conclusao(pct: float) -> tuple[str, str]:
    if pct >= 80:
        return "ROTA CONCLUÍDA", "SIM"
    if pct >= 45:
        return "CONCLUÍDA PARCIALMENTE", "PARCIAL"
    if pct >= 25:
        return "INÍCIO DE COLETA", "PARCIAL"
    return "ROTA NÃO CONCLUÍDA", "NÃO"


def _codigo_setor(setor: str) -> tuple[str, str]:
    bruto = (setor or "").strip()
    if " - " in bruto:
        codigo, nome = bruto.split(" - ", 1)
        return codigo.strip(), nome.strip()
    return bruto, ""


def _fonte_nome(fonte: str) -> str:
    return "SEGTRACK · FULLTRACK"


def _legenda_html(frota: list[dict[str, Any]]) -> str:
    chips = []
    vistos: set[str] = set()
    for indice, rota in enumerate(frota):
        nome = str(rota.get("placa") or rota.get("veiculo") or "").strip()
        if not nome or nome in vistos:
            continue
        if not (rota.get("trechos") or rota.get("executada")):
            continue
        vistos.add(nome)
        cor = PALETA[indice % len(PALETA)]
        chips.append(f'<span class="chip"><i style="background:{cor}"></i>{_xml(nome)}</span>')
    return "".join(chips) or '<span class="chip">Sem GPS no dia</span>'


def _barras_html(linhas: list[dict[str, Any]]) -> str:
    blocos = []
    ordenadas = sorted(linhas, key=lambda row: (-_pct_num(row), str(row.get("Setor") or "")))
    for row in ordenadas:
        pct = _pct_num(row)
        codigo, nome = _codigo_setor(str(row.get("Setor") or ""))
        label = f"{codigo} — {nome}" if nome else codigo
        st = str(row.get("Status") or "").upper()
        if "APOIO" in st:
            cls = "apoio"
        else:
            cls = "ok" if pct >= 80 else "mid" if pct >= 45 else "no"
        blocos.append(
            "<div class='barra'>"
            f"<span class='nome'>{_xml(label)}</span>"
            f"<b class='{cls}'>{pct:.0f}%</b>"
            "</div>"
        )
    return "".join(blocos) or "<p class='nota'>Sem percentual oficial neste dia.</p>"


def _pct_ontem_num(row: dict[str, Any]) -> float:
    bruto = str(row.get("% ontem") or "").replace("%", "").replace(",", ".").strip()
    if bruto:
        try:
            return float(bruto)
        except ValueError:
            pass
    return _pct_num(row)


def _recuperadas_html(linhas: list[dict[str, Any]]) -> str:
    if not linhas:
        return ""
    blocos = []
    ordenadas = sorted(linhas, key=lambda row: (-_pct_num(row), str(row.get("Setor") or "")))
    for row in ordenadas:
        pct = _pct_num(row)
        codigo, nome = _codigo_setor(str(row.get("Setor") or ""))
        label = f"{codigo} — {nome}" if nome else codigo
        extra = " <small><span class='tag'>dia anterior</span>GPS hoje</small>"
        cls = "ok" if pct >= 80 else "mid" if pct >= 45 else "no"
        blocos.append(
            "<div class='barra'>"
            f"<span class='nome'>{_xml(label)}{extra}</span>"
            f"<b class='{cls}'>{pct:.0f}%</b>"
            "</div>"
        )
    return (
        "<div class='bloco-ontem'>"
        "<h2>Ontem não feitas · GPS hoje</h2>"
        "<p class='sub'>Grade de ontem incompleta, só se o rastreador entrou no polígono hoje. O % é o de hoje.</p>"
        "<div class='resumo-grade'>" + "".join(blocos) + "</div>"
        "</div>"
    )


def _legenda_placas(folha: dict[str, Any]) -> str:
    partes = []
    for cam in folha.get("caminhoes") or []:
        papel = rotulo_papel_relatorio(cam)
        placa = str(cam.get("placa") or cam.get("veiculo") or "").strip()
        if not placa:
            continue
        visitas = cam.get("passagens") or []
        if visitas:
            janela = " · ".join(
                "–".join(
                    p for p in (
                        hora_curta(str(vis.get("hora_inicio") or "")),
                        hora_curta(str(vis.get("hora_fim") or "")),
                    ) if p
                )
                for vis in visitas
            )
        else:
            janela = "–".join(
                p for p in (hora_curta(str(cam.get("hora_inicio") or "")), hora_curta(str(cam.get("hora_fim") or ""))) if p
            )
        if not janela:
            janela = hora_curta(str(cam.get("horario") or ""))
        pct = float(cam.get("percentual") or 0)
        extra = f" {papel}"
        if pct > 0:
            extra += f" {pct:.0f}%"
        if cam.get("contribuicao_exclusiva") not in (None, ""):
            extra += f" exclusivo {float(cam.get('contribuicao_exclusiva') or 0):.0f}%"
        label = f"{placa}{extra} {janela}".strip()
        partes.append(label)
    apoio = str(folha.get("apoio") or "").strip()
    if apoio and apoio not in " · ".join(partes):
        partes.append(apoio)
    if partes:
        return " · ".join(partes)
    placa = str(folha.get("placa") or "").strip()
    if not placa:
        return ""
    janela = "–".join(
        p for p in (hora_curta(str(folha.get("hora_inicio") or "")), hora_curta(str(folha.get("hora_fim") or ""))) if p
    )
    return f"{placa} {janela}".strip()


def _tpl(nome: str, **ctx: str) -> str:
    texto = (_TPL / nome).read_text(encoding="utf-8")
    for chave, valor in ctx.items():
        texto = texto.replace("{{" + chave + "}}", valor)
    return texto


def _documento(body: str) -> str:
    return _tpl("base.html", CSS=(_TPL / "relatorio.css").read_text(encoding="utf-8"), BODY=body)


def _html_pdf(
    dia_br: str,
    dados: dict[str, Any],
    mapa_geral: str,
    folhas: list[dict[str, Any]],
    fonte: str = "inlog",
    legenda: str = "",
) -> list[str]:
    linhas_barras = _linhas_capa(dados.get("linhas") or [])
    recuperadas = dados.get("recuperadas") or []
    codigos_ontem = set()
    for row in recuperadas:
        codigo_rec, _ = _codigo_setor(str(row.get("Setor") or ""))
        if codigo_rec:
            codigos_ontem.add(codigo_rec.upper())
    gerado = datetime.now().strftime("%d/%m/%Y %H:%M")
    nome_fonte = _fonte_nome(fonte)
    header = _tpl("header.html", FONTE=_xml(nome_fonte), DIA=_xml(dia_br))
    rodape = {"FONTE": _xml(nome_fonte), "DIA": _xml(dia_br), "GERADO": _xml(gerado)}
    titulo_capa = "Rotas do dia"

    capa = _tpl(
        "capa.html",
        HEADER=header,
        TITULO=_xml(titulo_capa),
        DIA=_xml(dia_br),
        FONTE=_xml(nome_fonte),
        BARRAS=_barras_html(linhas_barras),
        RECUPERADAS=_recuperadas_html(recuperadas),
        GERADO=_xml(gerado),
    )
    paginas = [_documento(capa)]
    paginas.append(
        _documento(
            _tpl(
                "frota.html",
                HEADER=header,
                MAPA=mapa_geral,
                LEGENDA=legenda,
                **rodape,
            )
        )
    )
    rotas = []
    for folha in folhas:
        pct = float(folha.get("percentual") or 0)
        vered = str(folha.get("execucao") or folha.get("veredicto") or "")
        if "APOIO" in vered.upper():
            sit, cls = "APOIO", "apoio"
            rotulo, sim_nao = "APOIO EM VIZINHO", "APOIO"
        else:
            sit, cls = _situacao(pct)
            rotulo, sim_nao = _rotulo_conclusao(pct)
        codigo, nome = _codigo_setor(str(folha.get("setor") or ""))
        if str(folha.get("setor") or "") == "Percurso do dia":
            titulo = "Percurso do dia"
        else:
            titulo = f"{codigo}" if codigo else "Rota"
            if nome:
                titulo = f"{titulo}  ·  {nome}"
        placa = str(folha.get("placa") or "").strip()
        nome_veic = str(folha.get("veiculo") or "").strip()
        sem_gps = bool(folha.get("sem_gps")) or sit == "NÃO INICIADA"
        if sem_gps and (not nome_veic or nome_veic in {"—", "-"}):
            nome_veic = "Sem rastreamento"
            rotulo, sim_nao = "SEM RASTREAMENTO", "NÃO"
            sit, cls = "NÃO INICIADA", "no"
        if placa and (not nome_veic or placa in nome_veic or len(nome_veic) > 22):
            veiculo = _xml(placa)
        elif nome_veic and placa and nome_veic != placa and placa not in {"—", "-"}:
            veiculo = f"{_xml(nome_veic)} · {_xml(placa)}"
        else:
            veiculo = _xml(nome_veic or placa or "Sem rastreamento")
        img = str(folha.get("img") or "")
        if img:
            mapa_html = f'<img class="mapa rota" src="{img}" alt="Rota" />'
        else:
            mapa_html = '<div class="mapa-vazio">Setor planejado sem GPS neste ciclo. Use a justificativa abaixo.</div>'
        rotas.append(
            _tpl(
                "rota.html",
                HEADER=header,
                TITULO=_xml(titulo),
                TURNO=_xml(str(folha.get("rota") or "")),
                DIA=_xml(dia_br),
                MAPA_HTML=mapa_html,
                VEICULO=veiculo,
                CLS=cls,
                PCT=f"{pct:.0f}",
                KM_COLETA=_xml(str(folha.get("km_coleta") or "—")),
                KM_TOTAL=_xml(str(folha.get("km_total") or "—")),
                ROTULO=rotulo,
                SIM_NAO=sim_nao,
                SITUACAO=sit,
                HORA_INI=_xml(str(folha.get("hora_inicio") or "—")),
                HORA_FIM=_xml(str(folha.get("hora_fim") or "—")),
                LEGENDA_PLACAS=_xml(_legenda_placas(folha)),
                AVISO_ONTEM=(
                    "<p class='aviso-ontem'>Rota de ontem (não concluída). Mapa e % de hoje.</p>"
                    if (folha.get("ontem") or codigo.upper() in codigos_ontem)
                    else ""
                ),
                AVISO=_xml(str(folha.get("aviso") or (
                    "Nenhum GPS neste polígono. Se a coleta foi feita por caminhão sem rastreador, justifique abaixo."
                    if sem_gps else ""
                ))),
                JUSTIFICATIVA=_xml("Escreva aqui a justificativa operacional desta rota."),
                GERADO=_xml(gerado),
                FONTE=_xml(nome_fonte),
            )
        )
    return paginas + [_documento(r) for r in rotas]
