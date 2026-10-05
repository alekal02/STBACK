from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import argos_api as api
from .argos_web import abrir_ge_web, extrair_trajetos, ler_frota_monitoramento
from .browser import open_session
from .config import Settings, janela_datetime, limitar_janela_api, rotulo_dia_periodo, rotulo_periodo
from .cobertura import avaliar_cobertura
from .export import write_json, write_mapa_html
from .gps import extract_points, haversine_m
from .mapa_oficial import aplicar_setores_oficiais, carregar_setores
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


def _placa_liberada(placa: str, filtro: tuple[str, ...]) -> bool:
    if not filtro:
        return True
    chave = _norm_placa(placa)
    return any(_norm_placa(item) == chave for item in filtro)


def _placas_inlog(output_dir: Path) -> dict[str, str]:
    base = output_dir / "rotas"
    if not base.exists():
        return {}
    pastas = [p for p in base.iterdir() if p.is_dir() and p.name != "comparacao"]
    if not pastas:
        return {}
    mais_recente = max(pastas, key=lambda p: p.stat().st_mtime)
    frota = mais_recente / "frota.json"
    if not frota.exists():
        return {}
    try:
        itens = json.loads(frota.read_text(encoding="utf-8"))
    except Exception:
        return {}
    mapa: dict[str, str] = {}
    for item in itens if isinstance(itens, list) else []:
        placa = _norm_placa(str(item.get("Placa") or item.get("placa") or ""))
        ident = str(item.get("Identificador") or item.get("veiculo") or "").strip()
        if placa and ident:
            mapa[placa] = ident.replace("\r", "").replace("\n", "")
    return mapa


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


def _rotas_inlog_do_dia(output_dir: Path, dia: date) -> list[dict[str, Any]]:
    arquivo = output_dir / "rotas" / dia.isoformat() / "rotas.json"
    if not arquivo.is_file():
        return []
    try:
        dados = json.loads(arquivo.read_text(encoding="utf-8"))
    except Exception:
        return []
    vistos: dict[int, dict[str, Any]] = {}
    for item in dados.get("rotas") or []:
        codigo = item.get("codigo_rota")
        planejada = item.get("rota_planejada") or []
        if not codigo or not planejada:
            continue
        chave = int(codigo)
        if chave in vistos:
            continue
        vistos[chave] = {
            "codigo_rota": chave,
            "setor": item.get("setor") or "",
            "rota": item.get("rota") or "",
            "macro": item.get("macro") or "",
            "km_coleta": item.get("km_coleta"),
            "km_total": item.get("km_total"),
            "qtd_pacmans": item.get("qtd_pacmans"),
            "eficiencia_planejada": item.get("eficiencia_planejada"),
            "veiculo_inlog": item.get("veiculo") or "",
            "placa_inlog": item.get("placa") or "",
            "rota_planejada": planejada,
        }
    return list(vistos.values())


def _aplicar_rotas_inlog(rotas: list[dict[str, Any]], inlog: list[dict[str, Any]]) -> None:
    if not inlog:
        return
    for item in rotas:
        coberturas = []
        melhor = None
        melhor_cob: dict[str, Any] | None = None
        melhor_pct = -1.0
        for rota in inlog:
            cob = avaliar_cobertura(rota["rota_planejada"], item.get("pontos") or [])
            pct = float(cob.get("percentual") or 0)
            row = {
                "codigo_rota": rota["codigo_rota"],
                "setor": rota["setor"],
                "rota": rota["rota"],
                "km_coleta": cob.get("km_coleta") if cob.get("km_coleta") else rota.get("km_coleta"),
                "km_total": rota.get("km_total"),
                "qtd_pacmans": rota.get("qtd_pacmans"),
                "eficiencia_planejada": rota.get("eficiencia_planejada"),
                "veiculo_inlog": rota.get("veiculo_inlog") or "",
                "placa_inlog": rota.get("placa_inlog") or "",
                "percentual": pct,
                "veredicto": cob.get("veredicto"),
                "confianca": cob.get("confianca"),
                "trechos_feitos": cob.get("trechos_feitos") or [],
                "trechos_faltando": cob.get("trechos_faltando") or [],
                "trechos_planejados": cob.get("trechos_planejados") or [],
                "trechos_gps": cob.get("trechos_gps") or [],
            }
            coberturas.append(row)
            if pct > melhor_pct:
                melhor_pct = pct
                melhor = rota
                melhor_cob = row
        coberturas.sort(key=lambda row: float(row.get("percentual") or 0), reverse=True)
        item["coberturas_inlog"] = coberturas
        item["percentual_concluido"] = max(melhor_pct, 0.0) if item.get("pontos") else 0.0
        if melhor and melhor_cob:
            item["setor"] = melhor["setor"]
            item["rota"] = melhor["rota"]
            item["codigo_rota"] = melhor["codigo_rota"]
            item["inlog"] = melhor.get("veiculo_inlog") or ""
            item["km_coleta"] = melhor_cob.get("km_coleta")
            item["km_total"] = melhor.get("km_total")
            item["qtd_pacmans"] = melhor.get("qtd_pacmans")
            item["eficiencia_planejada"] = melhor.get("eficiencia_planejada")
            item["veredicto"] = melhor_cob.get("veredicto")
            item["confianca"] = melhor_cob.get("confianca")
            item["trechos_feitos"] = melhor_cob.get("trechos_feitos") or []
            item["trechos_faltando"] = melhor_cob.get("trechos_faltando") or []
            item["trechos_planejados"] = melhor_cob.get("trechos_planejados") or []
            item["trechos_gps"] = melhor_cob.get("trechos_gps") or []
            item["rota_planejada"] = melhor["rota_planejada"]


def _html_relatorio(
    pasta: Path,
    dia: date,
    sessao: dict[str, Any],
    veiculos: list[dict[str, Any]],
    hora_inicio: str = "00:00",
    hora_fim: str = "23:59",
    data_fim: date | None = None,
) -> Path:
    titulo = f"Relatório diário Argos — {rotulo_dia_periodo(dia, hora_inicio, hora_fim, data_fim)}"
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
    <div class="marca">Sala técnica</div>
    <h1>{_esc(titulo)}</h1>
    <p>Setor oficial do mapa de coleta (GPKG). % = km do GPS dentro do setor / km oficial do setor. Linha no mapa = trajeto do caminhão.</p>
  </header>
  <div class="wrap">
    <p class="resumo"><b>{len(linhas)}</b> setores oficiais com GPS Argos · média concluída <b>{str(media).replace('.', ',')}%</b></p>
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
        {''.join(corpo) or '<tr><td colspan="13">Nenhum caminhão Argos encontrado.</td></tr>'}
      </tbody>
    </table>
    {html_secao_mapas_caminhao(veiculos)}
  </div>
</body>
</html>
"""
    (pasta / "relatorio-argos.html").write_text(html, encoding="utf-8")
    diario = pasta / "relatorio-diario.html"
    diario.write_text(html, encoding="utf-8")
    return diario


def _linhas_diario(dia: date, veiculos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return montar_linhas_setores(dia, veiculos)


_ORDEM_PLACAS = ("RBK7H47", "RBM2G21", "RBL0C01")


def _enriquecer_rotas(rotas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for item in rotas:
        placa = _fmt_placa(str(item.get("placa") or item.get("veiculo") or ""))
        pontos = item.get("pontos") or []
        item["placa"] = placa
        item["veiculo"] = placa
        item["km_dia"] = round(_km_do_percurso(pontos), 3)
        item["total_pontos"] = len(pontos)
        item["percentual_concluido"] = 0
        item["setor"] = "APARECIDA DE GOIANIA - DOMICILIAR - ARGOS"
        item["rota"] = "Percurso do dia"
    ordem = {placa: idx for idx, placa in enumerate(_ORDEM_PLACAS)}
    rotas.sort(key=lambda item: ordem.get(_norm_placa(str(item.get("placa") or "")), 99))
    return rotas


def gerar_saidas_argos(
    pasta: Path,
    dia: date,
    rotas: list[dict[str, Any]],
    sessao: dict[str, Any] | None = None,
    hora_inicio: str = "00:00",
    hora_fim: str = "23:59",
    data_fim: date | None = None,
    so_gps: bool = False,
) -> dict[str, Path]:
    rotas = _enriquecer_rotas(rotas)
    aplicar_identidade_inlog(rotas, pasta.parent.parent, dia)
    from .setores_agente import preparar_setores
    from .traco_agente import aplicar_traco

    if so_gps:
        write_json(
            pasta / "rotas.json",
            {
                "data": dia.isoformat(),
                "rotas": [{k: v for k, v in item.items() if k != "rota_planejada"} for item in rotas],
            },
        )
        return {}
    oficiais = preparar_setores(dia, rotas)
    if oficiais:
        aplicar_setores_oficiais(rotas, oficiais, dia)
    else:
        _aplicar_rotas_inlog(rotas, _rotas_inlog_do_dia(pasta.parent.parent, dia))
    aplicar_traco(rotas)
    html = _html_relatorio(pasta, dia, sessao or {}, rotas, hora_inicio, hora_fim, data_fim)
    linhas = _linhas_diario(dia, rotas)
    write_json(
        pasta / "relatorio-diario.json",
        {
            "data": dia.isoformat(),
            "data_fim": (data_fim or dia).isoformat(),
            "hora_inicio": hora_inicio,
            "hora_fim": hora_fim,
            "periodo": rotulo_periodo(dia, hora_inicio, hora_fim, data_fim),
            "fonte": "argos",
            "gerado_em": datetime.now().isoformat(timespec="seconds"),
            "total_rotas": len(linhas),
            "linhas": [
                {k: v for k, v in row.items() if k != "percentual_numero"}
                for row in linhas
            ],
        },
    )
    mapa = write_mapa_html(
        pasta / "mapa.html",
        f"Argos Aparecida — {dia.isoformat()}",
        rotas,
    )
    try:
        from .export_gpkg import escrever_gpkg

        gpkg = escrever_gpkg(pasta / "argos-rotas.gpkg", dia, rotas)
    except Exception:
        gpkg = None
    saidas = {"relatorio": html, "mapa": mapa}
    if gpkg is not None:
        saidas["gpkg"] = gpkg
    write_json(
        pasta / "rotas.json",
        {
            "data": dia.isoformat(),
            "rotas": [{k: v for k, v in item.items() if k != "rota_planejada"} for item in rotas],
        },
    )
    return saidas


def executar_argos(
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

    if not settings.argos_user or not settings.argos_password:
        raise SystemExit("Preencha ARGOS_USER e ARGOS_PASSWORD no .env")

    progress("Argos: login na API (sem abrir o site)...")
    sessao = api.login(settings)
    token = sessao["token"]
    equipamentos = [
        item
        for item in api.listar_equipamentos(token)
        if _placa_liberada(str(item.get("placa") or ""), settings.argos_veiculos)
    ]
    if not equipamentos and settings.argos_veiculos:
        progress("Nenhuma placa do filtro ARGOS_VEICULOS apareceu no cadastro.")
        equipamentos = [{"placa": placa, "id": None} for placa in settings.argos_veiculos]
    progress(f"Login ok: {sessao.get('login')} · {len(equipamentos)} compactadores.")
    obras: list[dict[str, Any]] = []
    orgs: list[dict[str, Any]] = []
    org: dict[str, Any] = {}
    alertas: list[dict[str, Any]] = []
    if not so_gps:
        obras = api.listar_obras(token)
        orgs = api.listar_organizacoes(token)
        org = (orgs[0] if orgs else {}) or {}
        obra = (obras[0] if obras else {}) or {}
        alertas = api.listar_alertas(
            token,
            dia,
            obra=str(obra.get("id") or ""),
            organizacao=str(org.get("id") or ""),
        )

    cruzamento = _placas_inlog(settings.output_dir)
    por_placa: dict[str, dict[str, Any]] = {}
    for eq in equipamentos:
        placa = str(eq.get("placa") or "").strip().upper()
        if not placa:
            continue
        por_placa[_norm_placa(placa)] = {
            "veiculo": placa,
            "placa": placa,
            "codigo": eq.get("id"),
            "descricao": "",
            "fonte": "argos",
            "inlog": cruzamento.get(_norm_placa(placa), ""),
            "pontos": [],
            "alertas": [],
        }

    for alerta in alertas:
        placa = _norm_placa(str(alerta.get("placa") or ""))
        if placa and not _placa_liberada(placa, settings.argos_veiculos):
            continue
        item = por_placa.setdefault(
            placa or f"evt-{alerta.get('id')}",
            {
                "veiculo": alerta.get("placa") or "evento",
                "placa": alerta.get("placa") or "",
                "fonte": "argos",
                "inlog": cruzamento.get(placa, ""),
                "pontos": [],
                "alertas": [],
            },
        )
        item.setdefault("alertas", []).append(alerta)
        item["pontos"].extend(extract_points(alerta, veiculo=item["placa"], fonte="argos-alerta"))

    inicio_dt, fim_dt = limitar_janela_api(
        *janela_datetime(dia, settings.hora_inicio, settings.hora_fim, settings.data_fim)
    )
    progress("Argos: buscando GPS na API de rastreio...")
    try:
        trajetos_api = api.coletar_percursos(
            token,
            [eq for eq in equipamentos if eq.get("id")],
            inicio_dt,
            fim_dt,
            on_progress=progress,
        )
    except Exception as exc:
        progress(f"API de rastreio falhou ({exc}).")
        trajetos_api = {}
    for placa, pontos in trajetos_api.items():
        chave = _norm_placa(placa)
        alvo = por_placa.setdefault(
            chave,
            {
                "veiculo": placa,
                "placa": placa,
                "fonte": "argos",
                "inlog": cruzamento.get(chave, ""),
                "pontos": [],
                "alertas": [],
            },
        )
        if pontos:
            alvo["pontos"] = pontos
    pontos_api = sum(len(item.get("pontos") or []) for item in por_placa.values())

    frota: list[dict[str, Any]] = []
    if pontos_api >= 40:
        progress(f"Argos API ok: {pontos_api} pontos. Sem abrir o site.")
    else:
        try:
            with open_session(settings, headed=headed, perfil="argos") as session:
                page = session.page
                assert page is not None
                progress("API com pouco GPS. Abrindo Argos Web...")
                abrir_ge_web(page, sessao["ge_web"])
                if not so_gps:
                    session.screenshot("argos-home")
                frota = [
                    linha
                    for linha in ler_frota_monitoramento(page)
                    if _placa_liberada(str(linha.get("placa") or ""), settings.argos_veiculos)
                ]
                progress(f"Monitoramento: {len(frota)} compactadores na grade.")
                for linha in frota:
                    chave = _norm_placa(linha.get("placa") or "")
                    alvo = por_placa.setdefault(
                        chave,
                        {
                            "veiculo": linha.get("placa"),
                            "placa": linha.get("placa"),
                            "fonte": "argos",
                            "inlog": cruzamento.get(chave, ""),
                            "pontos": [],
                            "alertas": [],
                        },
                    )
                    alvo.update({k: v for k, v in linha.items() if v})
                placas = [str(item.get("placa") or chave) for chave, item in por_placa.items()]
                progress(f"Consultando trajeto do dia em {len(placas)} placas...")
                trajetos = extrair_trajetos(
                    page, placas, dia, settings.hora_inicio, settings.hora_fim, settings.data_fim
                )
                if not so_gps:
                    session.screenshot("argos-percurso")
                for chave, pontos in trajetos.items():
                    norma = _norm_placa(chave) or chave
                    alvo = por_placa.setdefault(
                        norma,
                        {
                            "veiculo": chave,
                            "placa": chave,
                            "fonte": "argos",
                            "inlog": cruzamento.get(norma, ""),
                            "pontos": [],
                            "alertas": [],
                        },
                    )
                    if pontos:
                        alvo["pontos"] = pontos
                    progress(f"  {alvo.get('placa')}: {len(alvo.get('pontos') or [])} pontos GPS")
        except Exception as exc:
            progress(f"Argos Web não abriu ({exc}). Usando só o que a API trouxe.")

    rotas = [
        item
        for chave, item in por_placa.items()
        if chave and _placa_liberada(chave, settings.argos_veiculos)
    ]
    pasta = settings.output_dir / "argos" / data_iso
    pasta.mkdir(parents=True, exist_ok=True)
    write_json(pasta / "sessao.json", {k: v for k, v in sessao.items() if "token" not in k.lower()})
    write_json(pasta / "obras.json", obras)
    write_json(pasta / "equipamentos.json", equipamentos)
    write_json(pasta / "alertas.json", alertas)
    write_json(pasta / "frota.json", frota or list(por_placa.values()))
    saidas = gerar_saidas_argos(
        pasta,
        dia,
        rotas,
        {"login": sessao.get("login"), "obra": "Aparecida — Argos"},
        hora_inicio=settings.hora_inicio,
        hora_fim=settings.hora_fim,
        data_fim=settings.data_fim,
        so_gps=so_gps,
    )
    write_json(
        pasta / "rotas.json",
        {
            "data": data_iso,
            "rotas": [{k: v for k, v in item.items() if k != "rota_planejada"} for item in rotas],
        },
    )
    if gerar_pdf and not so_gps:
        try:
            from .pdf import gerar_pdf_dia

            progress("Gerando PDF Argos com mapa e a rota de cada caminhão...")
            saidas["pdf"] = gerar_pdf_dia(pasta)
        except Exception as exc:
            progress(f"PDF Argos não gerado: {exc}")
    html = saidas.get("relatorio")
    mapa = saidas.get("mapa")
    cruzados = sum(1 for item in rotas if item.get("codigo_rota"))
    relatorio = {
        "quando": datetime.now().isoformat(timespec="seconds"),
        "data": data_iso,
        "fonte": "argos",
        "login": sessao.get("login"),
        "obra": "Aparecida — Argos",
        "organizacao": org.get("descricao"),
        "total_veiculos": len(rotas),
        "total_pontos": sum(item.get("total_pontos") or 0 for item in rotas),
        "total_alertas": len(alertas),
        "cruzados_inlog": cruzados,
        "arquivos": {
            "relatorio": str(html),
            "mapa": str(mapa),
            "equipamentos": str(pasta / "equipamentos.json"),
        },
    }
    write_json(pasta / "relatorio.json", relatorio)
    progress(
        f"Pronto. {relatorio['total_veiculos']} veículos Argos, "
        f"{relatorio['total_pontos']} pontos, {cruzados} casados com o Inlog."
    )
    return relatorio
