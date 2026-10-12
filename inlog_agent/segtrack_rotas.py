from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .config import Settings, rotulo_dia_periodo, rotulo_periodo
from .export import write_json
from .gps import haversine_m
from .relatorio_diario import (
    _classe,
    _esc,
    aplicar_identidade_inlog,
    css_relatorio_web,
    html_secao_mapas_caminhao,
    montar_linhas_setores,
)
def _norm_placa(placa: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (placa or "").upper())


def _fmt_placa(placa: str) -> str:
    chave = _norm_placa(placa)
    if len(chave) == 7:
        return f"{chave[:3]}-{chave[3:]}"
    return (placa or chave).upper()


def _km_do_percurso(pontos: list[dict[str, Any]]) -> float:
    total = 0.0
    anterior = None
    for ponto in pontos:
        lat = ponto.get("latitude")
        lon = ponto.get("longitude")
        if lat is None or lon is None:
            continue
        atual = (float(lat), float(lon))
        if anterior is not None:
            dist = haversine_m(anterior, atual)
            if dist < 2500:
                total += dist
        anterior = atual
    return total / 1000.0


def _fmt_km(km: float) -> str:
    return f"{km:.3f}".replace(".", ",") + " km"


def _fmt_km_valor(valor: Any) -> str:
    if valor in (None, "", "---"):
        return "---"
    texto = str(valor)
    if "km" in texto.lower():
        return texto
    try:
        return _fmt_km(float(texto.replace(",", ".")))
    except (TypeError, ValueError):
        return texto


def _dedupe_pontos(pontos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    vistos: set[tuple[Any, ...]] = set()
    saida = []
    for ponto in pontos:
        try:
            chave = (
                ponto.get("timestamp"),
                round(float(ponto["latitude"]), 6),
                round(float(ponto["longitude"]), 6),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if chave in vistos:
            continue
        vistos.add(chave)
        saida.append(ponto)
    saida.sort(key=lambda p: str(p.get("timestamp") or ""))
    return saida


def _enriquecer_rotas(rotas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for item in rotas:
        placa = _fmt_placa(str(item.get("placa") or item.get("veiculo") or ""))
        pontos = _dedupe_pontos(item.get("pontos") or [])
        item["placa"] = placa
        item["veiculo"] = item.get("descricao") or item.get("veiculo") or placa
        item["pontos"] = pontos
        item["km_dia"] = round(_km_do_percurso(pontos), 3)
        item["total_pontos"] = len(pontos)
        item["percentual_concluido"] = 0
        item["setor"] = "APARECIDA DE GOIANIA - DOMICILIAR - SEGTRACK"
        item["rota"] = "Percurso do dia"
        item["fonte"] = "segtrack"
    rotas.sort(key=lambda item: str(item.get("placa") or ""))
    return rotas


def _linhas_diario(dia: date, veiculos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return montar_linhas_setores(dia, veiculos)


def _html_relatorio(
    pasta: Path,
    dia: date,
    veiculos: list[dict[str, Any]],
    hora_inicio: str = "00:00",
    hora_fim: str = "23:59",
    data_fim: date | None = None,
) -> Path:
    titulo = f"Relatório diário SEGTRACK — {rotulo_dia_periodo(dia, hora_inicio, hora_fim, data_fim)}"
    linhas = _linhas_diario(dia, veiculos)
    corpo = []
    for row in linhas:
        pct = row.get("% Concluída") or "---"
        corpo.append(
            "<tr>"
            f"<td>{_esc(row.get('Descrição'))}</td>"
            f"<td>{_esc(row.get('Setor'))}</td>"
            f"<td>{_esc(row.get('Rota'))}</td>"
            f"<td>{_esc(row.get('Veículo'))}</td>"
            f"<td>{_esc(row.get('Placa'))}</td>"
            f"<td>{_esc(row.get('Nº de Pacmans'))}</td>"
            f"<td>{_esc(row.get('Km Total'))}</td>"
            f"<td>{_esc(row.get('Km Coleta'))}</td>"
            f"<td>{_esc(row.get('Km Deslocamento'))}</td>"
            f"<td>{_esc(row.get('Efic. no setor'))}</td>"
            f"<td>{_esc(row.get('Efic. da Operação'))}</td>"
            f"<td class='{_classe(pct)}'>{_esc(pct)}</td>"
            f"<td>{_esc(row.get('Status'))}</td>"
            "</tr>"
        )
    pcts = [float(row["percentual_numero"]) for row in linhas if row.get("percentual_numero") is not None]
    media = round(sum(pcts) / len(pcts), 1) if pcts else 0
    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8" />
  <title>{_esc(titulo)}</title>
  <style>{css_relatorio_web()}</style>
</head>
<body>
  <header>
    <div class="marca">SEGTRACK</div>
    <h1>{_esc(titulo)}</h1>
    <p>Setor oficial do mapa de coleta (GPKG). % = km do GPS dentro do setor / km oficial do setor. Linha no mapa = trajeto do caminhão.</p>
  </header>
  <div class="wrap">
    <p class="resumo"><b>{len(linhas)}</b> compactadores SEGTRACK · média concluída <b>{str(media).replace('.', ',')}%</b></p>
    <table>
      <thead>
        <tr>
          <th>Descrição</th>
          <th>Setor</th>
          <th>Rota</th>
          <th>Veículo</th>
          <th>Placa</th>
          <th>Nº de Pacmans</th>
          <th>Km Total</th>
          <th>Km Coleta</th>
          <th>Km Deslocamento</th>
          <th>Efic. no setor</th>
          <th>Efic. da Operação</th>
          <th>% Concluída</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>
        {''.join(corpo) or '<tr><td colspan="13">Nenhum compactador SEGTRACK encontrado em Aparecida.</td></tr>'}
      </tbody>
    </table>
    {html_secao_mapas_caminhao(veiculos)}
  </div>
</body>
</html>
"""
    (pasta / "relatorio-segtrack.html").write_text(html, encoding="utf-8")
    diario = pasta / "relatorio-diario.html"
    diario.write_text(html, encoding="utf-8")
    return diario


def gerar_saidas_segtrack(
    pasta: Path,
    dia: date,
    rotas: list[dict[str, Any]],
    hora_inicio: str = "00:00",
    hora_fim: str = "23:59",
    data_fim: date | None = None,
    so_gps: bool = False,
) -> dict[str, Path]:
    rotas = _enriquecer_rotas(rotas)
    aplicar_identidade_inlog(rotas, pasta.parent.parent, dia)
    write_json(
        pasta / "frota.json",
        [{k: v for k, v in item.items() if k != "rota_planejada"} for item in rotas],
    )
    if so_gps:
        write_json(
            pasta / "rotas.json",
            {
                "data": dia.isoformat(),
                "rotas": [{k: v for k, v in item.items() if k != "rota_planejada"} for item in rotas],
            },
        )
        return {}
    from .unificado import caminhoes_de_rotas, gerar_relatorio_setores

    caminhoes = caminhoes_de_rotas(rotas)
    gerar_relatorio_setores(
        pasta,
        dia,
        caminhoes,
        {"segtrack": "SEGTRACK"},
        hora_inicio,
        hora_fim,
        data_fim,
        fonte="segtrack",
        titulo_mapa=f"SEGTRACK Aparecida — {rotulo_periodo(dia, hora_inicio, hora_fim, data_fim)}",
        output_dir=pasta.parent.parent,
        incorporar=False,
    )
    saidas = {
        "relatorio": pasta / "relatorio-diario.html",
        "mapa": pasta / "mapa.html",
    }
    try:
        from .export_gpkg import escrever_gpkg

        gpkg_rotas = json.loads((pasta / "rotas.json").read_text(encoding="utf-8")).get("rotas") or []
        gpkg = escrever_gpkg(pasta / "segtrack-rotas.gpkg", dia, gpkg_rotas, fonte="segtrack")
    except Exception:
        gpkg = None
    if gpkg is not None:
        saidas["gpkg"] = gpkg
    return saidas


def executar_segtrack(
    settings: Settings,
    headed: bool | None = None,
    on_progress: Callable[[str], None] | None = None,
    gerar_pdf: bool = True,
    so_gps: bool = False,
) -> dict[str, Any]:
    dia = settings.data_rota
    data_iso = dia.isoformat()

    def progress(msg: str) -> None:
        print(msg, flush=True)
        if on_progress:
            on_progress(msg)

    usa_site = bool(settings.segtrack_user and settings.segtrack_password)
    usa_api = bool(settings.fulltrack_username and settings.fulltrack_password)
    if not usa_site and not usa_api:
        raise SystemExit("Preencha FULLTRACK_* ou SEGTRACK_USER/SEGTRACK_PASSWORD no .env")

    pasta = settings.output_dir / "segtrack" / data_iso
    pasta.mkdir(parents=True, exist_ok=True)

    rotas: list[dict[str, Any]] = []
    sessao: dict[str, Any] = {}
    if usa_api:
        try:
            from .fulltrack_api import coletar_percursos

            progress("Buscando GPS na API Fulltrack (sem abrir o site)...")
            rotas = coletar_percursos(settings, dia, on_progress=progress)
            sessao = {"fonte": "fulltrack-api", "url": settings.fulltrack_base_url}
            if not any((item.get("total_pontos") or 0) for item in rotas):
                progress("API Fulltrack sem pontos no período. Tentando o site SEGTRACK...")
                rotas = []
        except Exception as exc:
            progress(f"API Fulltrack falhou ({exc}). Tentando o site SEGTRACK...")
            rotas = []
    if not rotas and usa_site:
        from .segtrack_web import extrair_frota_e_percursos, fazer_login, open_segtrack_session

        with open_segtrack_session(settings, headed=headed) as session:
            page = session.page
            assert page is not None
            progress("Entrando na SEGTRACK / Fulltrack...")
            sessao = fazer_login(page, settings)
            progress(f"Login ok. Tela: {sessao.get('titulo') or sessao.get('url')}")
            rotas, menus, meta = extrair_frota_e_percursos(page, settings, dia, on_progress=progress)
            write_json(
                pasta / "chamadas.json",
                [
                    {
                        "method": c.get("method"),
                        "url": c.get("url"),
                        "path": c.get("path"),
                        "status": c.get("status"),
                        "quando": c.get("quando"),
                        "tem_json": c.get("json") is not None,
                    }
                    for c in session.chamadas
                ],
            )
            write_json(pasta / "menus.json", menus)
            write_json(pasta / "tentativas.json", meta)
    elif not rotas:
        raise SystemExit("Sem GPS da Fulltrack e sem login do site SEGTRACK.")

    saidas = gerar_saidas_segtrack(
        pasta, dia, rotas, settings.hora_inicio, settings.hora_fim, settings.data_fim, so_gps=so_gps
    )
    write_json(pasta / "frota.json", rotas)
    write_json(
        pasta / "sessao.json",
        {
            "login": settings.fulltrack_username or settings.segtrack_user,
            "url": sessao.get("url"),
            "fonte": sessao.get("fonte") or "segtrack-site",
        },
    )
    if gerar_pdf and not so_gps:
        try:
            from .pdf import gerar_pdf_dia

            progress("Gerando PDF SEGTRACK com mapa e a rota de cada circuito...")
            saidas["pdf"] = gerar_pdf_dia(pasta)
        except Exception as exc:
            progress(f"PDF SEGTRACK não gerado: {exc}")
    html = saidas.get("relatorio")
    mapa = saidas.get("mapa")
    payload = {}
    existente = pasta / "relatorio.json"
    if existente.is_file():
        try:
            payload = json.loads(existente.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
    relatorio = {
        **payload,
        "quando": datetime.now().isoformat(timespec="seconds"),
        "data": data_iso,
        "fonte": "segtrack",
        "login": settings.segtrack_user,
        "obra": "Aparecida — SEGTRACK",
        "total_veiculos": len(rotas),
        "total_pontos": sum(item.get("total_pontos") or 0 for item in rotas),
        "arquivos": {
            "relatorio": str(html),
            "mapa": str(mapa),
        },
    }
    write_json(pasta / "relatorio.json", relatorio)
    progress(
        f"Pronto. {relatorio['total_veiculos']} compactadores SEGTRACK, "
        f"{relatorio['total_pontos']} pontos GPS cruzados com a grade do mapa oficial."
    )
    return relatorio
