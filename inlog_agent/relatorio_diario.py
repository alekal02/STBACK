from __future__ import annotations

import csv
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .export import write_json
from .gps import tracos_realistas
from .config import rotulo_dia_periodo, rotulo_periodo
from .pdf_mapa import bounds_nucleo

from .verificacao import coberturas_executadas

COBERTURA_MAX = 5


def _norm_placa(placa: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (placa or "").upper())


def coberturas_relatorio(
    item: dict[str, Any],
    *,
    min_pct: float = 0.0,
    min_km: float = 0.0,
    max_n: int = COBERTURA_MAX,
) -> list[dict[str, Any]]:
    """Só rotas com coleta confirmada (feita ou parcial). Passagem pelo polígono fica de fora."""
    return coberturas_executadas(item, max_n=max_n)


def mapa_frota_inlog(output_dir: Path, dia: date | None = None) -> dict[str, dict[str, str]]:
    base = Path(output_dir) / "rotas"
    candidatos: list[Path] = []
    if dia is not None:
        arquivo = base / dia.isoformat() / "frota.json"
        if arquivo.is_file():
            candidatos.append(arquivo)
    if base.is_dir():
        pastas = [p for p in base.iterdir() if p.is_dir() and p.name != "comparacao"]
        if pastas:
            recente = max(pastas, key=lambda p: p.stat().st_mtime) / "frota.json"
            if recente.is_file() and recente not in candidatos:
                candidatos.append(recente)
    mapa: dict[str, dict[str, str]] = {}
    for arquivo in candidatos:
        try:
            itens = json.loads(arquivo.read_text(encoding="utf-8"))
        except Exception:
            continue
        for item in itens if isinstance(itens, list) else []:
            placa = _norm_placa(str(item.get("Placa") or item.get("placa") or ""))
            ident = str(item.get("Identificador") or item.get("veiculo") or "").strip().replace("\r", "").replace("\n", "")
            if not placa or not ident or placa in mapa:
                continue
            mapa[placa] = {
                "veiculo": ident,
                "descricao": str(item.get("Descricao") or item.get("descricao") or "").strip(),
                "placa": str(item.get("Placa") or item.get("placa") or ""),
            }
    return mapa


def aplicar_identidade_inlog(rotas: list[dict[str, Any]], output_dir: Path, dia: date | None = None) -> None:
    cruzamento = mapa_frota_inlog(output_dir, dia)
    for item in rotas:
        placa = _norm_placa(str(item.get("placa") or item.get("veiculo") or ""))
        info = cruzamento.get(placa)
        if not info:
            continue
        item["inlog"] = info["veiculo"]
        item["descricao_inlog"] = info["descricao"]
        veiculo = str(item.get("veiculo") or "")
        if not veiculo.startswith("CCL"):
            item["veiculo"] = info["veiculo"]


def intervalo_gps(item: dict[str, Any]) -> tuple[str, str]:
    pontos = item.get("pontos_dia") or item.get("pontos") or []
    stamps = [str(p.get("timestamp") or "").strip() for p in pontos if str(p.get("timestamp") or "").strip()]
    if stamps:
        return stamps[0], stamps[-1]
    return "", str(item.get("ultima_posicao") or item.get("ultimo_rastreio") or "")


def _fmt_km_valor(valor: Any) -> str:
    if valor in (None, "", "---"):
        return "---"
    texto = str(valor)
    if "km" in texto.lower():
        return texto
    try:
        return f"{float(str(valor).replace(',', '.')):.3f}".replace(".", ",") + " km"
    except (TypeError, ValueError):
        return texto


def _pares_setores(
    veiculos: list[dict[str, Any]],
    *,
    min_pct: float = 15.0,
    min_km: float = 6.0,
    max_n: int = 3,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    escolhidas: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for item in veiculos:
        cobs = coberturas_relatorio(item, max_n=max_n)
        if not cobs:
            continue
        for cob in cobs:
            escolhidas.append((item, cob))
    return escolhidas


def montar_linhas_setores(
    dia: date,
    veiculos: list[dict[str, Any]],
    *,
    min_pct: float = 15.0,
    min_km: float = 6.0,
    max_n: int = 3,
) -> list[dict[str, Any]]:
    linhas = []
    for item, cob in _pares_setores(veiculos, min_pct=min_pct, min_km=min_km, max_n=max_n):
        pontos = item.get("pontos_dia") or item.get("pontos") or []
        inicio, fim = intervalo_gps(item)
        ident = str(item.get("veiculo") or item.get("placa") or "")
        placa = str(item.get("placa") or "")
        pct_num = float(cob.get("percentual") or 0)
        veredicto = cob.get("execucao") or cob.get("veredicto") or item.get("veredicto") or ("Com GPS" if pontos else "Sem rastreio")
        confianca = cob.get("confianca") or item.get("confianca") or ""
        status = veredicto
        if confianca:
            status = f"{status} · confiança {confianca}"
        if cob.get("detectado_fora_da_grade"):
            status = f"{status} · setor detectado pelo GPS (fora da grade do dia)"
        if cob.get("motivo_agente") and "detectado" not in status:
            status = f"{status} · {cob.get('motivo_agente')}"
        if inicio or fim:
            status = f"{status} · {inicio} → {fim}"
        km_desloc = "---"
        try:
            dia_km = float(item.get("km_dia") or 0)
            coleta = float(cob.get("km_coleta") or 0)
            if dia_km > coleta > 0:
                km_desloc = _fmt_km_valor(dia_km - coleta)
        except (TypeError, ValueError):
            pass
        linhas.append(
            {
                "Data": dia.strftime("%d/%m/%Y"),
                "Macro Setor": item.get("macro") or "",
                "Descrição": ident,
                "Setor": cob.get("setor") or item.get("setor") or "",
                "Rota": cob.get("rota") or item.get("rota") or "",
                "Veículo": ident,
                "Placa": placa,
                "Mobile": "",
                "Nº de Pacmans": cob.get("qtd_pacmans") if cob.get("qtd_pacmans") not in (None, "") else "---",
                "Km Total": _fmt_km_valor(cob.get("km_total") if cob.get("km_total") not in (None, "") else item.get("km_dia")),
                "Km Coleta": _fmt_km_valor(cob.get("km_coleta") if cob.get("km_coleta") not in (None, "") else item.get("km_dia")),
                "Km Deslocamento": km_desloc,
                "Km Coleta Manual": "---",
                "Efic. no setor": "---",
                "Efic. da Operação": _pct(item.get("eficiencia_planejada")) if item.get("eficiencia_planejada") not in (None, "") else "---",
                "% Concluída": _pct(pct_num) if pontos else "---",
                "Etapa": "",
                "Status": status,
                "percentual_numero": pct_num if pontos else None,
            }
        )
    linhas.sort(key=lambda row: (str(row.get("Setor") or ""), str(row.get("Rota") or ""), str(row.get("Veículo") or "")))
    return linhas


def montar_linhas_diarias(
    dia: date,
    grid: list[dict[str, Any]],
    veiculos: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    linhas = []
    for item in grid:
        setor = str(item.get("Setor") or "")
        rota = str(item.get("Rota") or item.get("Descricao") or "")
        ligados = [
            v
            for v in veiculos
            if (v.get("setor") or "") == setor and (not rota or (v.get("rota") or "") == rota)
        ]
        if not ligados:
            ligados = [v for v in veiculos if (v.get("setor") or "") == setor]
        veiculo = max(ligados, key=lambda v: float(v.get("percentual_concluido") or 0), default={})
        pct = veiculo.get("percentual_concluido")
        if pct is None:
            pct = ""
        linhas.append(
            {
                "Data": dia.strftime("%d/%m/%Y"),
                "Macro Setor": item.get("MacroSetor") or "",
                "Setor": setor,
                "Descrição": item.get("Descricao") or "",
                "Rota": item.get("Rota") or "",
                "Mobile": "Sim" if item.get("RotaMobile") else "Não",
                "Nº de Pacmans": item.get("QtdPacmans") if item.get("QtdPacmans") is not None else "",
                "Km Total": item.get("KmTotal") or "",
                "Km Coleta": item.get("KmColeta") or "",
                "Km Deslocamento": item.get("KmDeslocamento") or "",
                "Km Coleta Manual": item.get("KmManual") or "---",
                "Efic. no setor": _pct(item.get("EficienciaSetor")),
                "Efic. da Operação": _pct(item.get("EficienciaOperacao")),
                "% Concluída": _pct(pct) if pct != "" else "---",
                "Veículo": veiculo.get("veiculo") or "",
                "Placa": veiculo.get("placa") or "",
                "Etapa": item.get("DescricaoEtapa") or "",
                "Status": (
                    f"{veiculo.get('veredicto')} · confiança {veiculo.get('confianca')}"
                    if veiculo.get("veredicto")
                    else (item.get("StatusDescricao") or "")
                ),
                "percentual_numero": float(pct) if pct != "" else None,
                "codigo_rota": item.get("CodigoRota"),
                "codigo_setor": item.get("CodigoSetor"),
            }
        )
    linhas.sort(key=lambda row: (str(row["Setor"]), str(row["Rota"])))
    return linhas


def exportar_relatorio_diario(
    pasta: Path,
    dia: date,
    linhas: list[dict[str, Any]],
    rotas: list[dict[str, Any]] | None = None,
    hora_inicio: str = "00:00",
    hora_fim: str = "23:59",
    data_fim: date | None = None,
) -> dict[str, str]:
    pasta.mkdir(parents=True, exist_ok=True)
    limpas = [{k: v for k, v in row.items() if k not in {"percentual_numero", "codigo_rota", "codigo_setor"}} for row in linhas]
    json_path = write_json(pasta / "relatorio-diario.json", {
        "data": dia.isoformat(),
        "data_fim": (data_fim or dia).isoformat(),
        "hora_inicio": hora_inicio,
        "hora_fim": hora_fim,
        "periodo": rotulo_periodo(dia, hora_inicio, hora_fim, data_fim),
        "fonte": "inlog",
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
        "total_rotas": len(linhas),
        "linhas": limpas,
    })
    csv_path = pasta / "relatorio-diario.csv"
    campos = [
        "Data", "Macro Setor", "Setor", "Descrição", "Rota", "Mobile",
        "Nº de Pacmans", "Km Total", "Km Coleta", "Km Deslocamento",
        "Km Coleta Manual", "Efic. no setor", "Efic. da Operação",
        "% Concluída", "Veículo", "Placa", "Etapa", "Status",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=campos, delimiter=";", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(limpas)
    html_path = pasta / "relatorio-diario.html"
    html_path.write_text(_html_inlog(dia, linhas, rotas, hora_inicio, hora_fim, data_fim), encoding="utf-8")
    return {
        "diario_json": str(json_path),
        "diario_csv": str(csv_path),
        "diario_html": str(html_path),
    }


def css_relatorio_web() -> str:
    return """
    :root { --navy:#12232e; --teal:#128086; --bg:#eef2f4; --line:#dce4e8; --muted:#5d717c; }
    body { font: 13px/1.45 "Segoe UI", Arial, sans-serif; margin: 0; background: var(--bg); color: var(--navy); }
    header { background: #0e1b23; color: #fff; padding: 18px 22px 16px; }
    header .marca { display:inline-block; font-size:10px; letter-spacing:1.5px; text-transform:uppercase; color:#8ee0d6; font-weight:700; margin:0 0 6px; }
    header h1 { margin: 0; font-size: 22px; letter-spacing: -.4px; }
    header p { margin: 6px 0 0; color: #c3d8e2; font-size: 13px; }
    .wrap { padding: 18px 22px 40px; }
    table { width: 100%; border-collapse: collapse; background: #fff; border: 1px solid var(--line); }
    th, td { border-bottom: 1px solid #eaf0f3; padding: 8px 10px; text-align: left; vertical-align: top; }
    th { background: #12232e; color: #fff; font-size: 11px; text-transform: uppercase; letter-spacing: .4px; }
    tr:nth-child(even) td { background: #f7fafb; }
    .ok { color: #1b8a5a; font-weight: 700; }
    .medio { color: #d17a25; font-weight: 700; }
    .critico { color: #b34145; font-weight: 700; }
    .resumo { margin: 0 0 14px; color: var(--muted); }
    .resumo b { color: var(--navy); }
    """


def _pct(valor: Any) -> str:
    if valor in (None, "", "---"):
        return "---"
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return str(valor)
    if numero <= 1 and numero > 0:
        numero *= 100
    return f"{numero:.0f}%"


def _classe(valor: Any) -> str:
    try:
        numero = float(str(valor).replace("%", "").replace(",", "."))
    except (TypeError, ValueError):
        return ""
    if numero >= 80:
        return "ok"
    if numero >= 50:
        return "medio"
    return "critico"


def _thin_pts(pts: list, max_pts: int) -> list:
    if not pts or len(pts) <= max_pts:
        return pts
    passo = max(1, len(pts) // max_pts)
    saida = list(pts[::passo])
    if saida[-1] != pts[-1]:
        saida.append(pts[-1])
    return saida


def _trechos_veiculo(item: dict[str, Any]) -> list:
    if item.get("trechos"):
        return [t for t in item["trechos"] if t]
    pontos = item.get("pontos_traco") or item.get("pontos_dia") or item.get("pontos") or []
    trechos = tracos_realistas(pontos, salto_max_m=95, tolerancia_m=7) if pontos else []
    if trechos:
        return trechos
    trechos = [t for t in (item.get("trechos_gps") or []) if t]
    if trechos:
        return trechos
    for cob in item.get("coberturas_inlog") or []:
        trechos.extend(t for t in (cob.get("trechos_gps") or []) if t)
    return trechos


def _folha_caminhao(
    item: dict[str, Any],
    *,
    trechos: list,
    poligonos: list | None = None,
    setor: Any = None,
    rota: Any = None,
    percentual: Any = None,
    veredicto: Any = None,
    km_coleta: Any = None,
    km_total: Any = None,
) -> dict[str, Any]:
    return {
        "veiculo": item.get("veiculo"),
        "placa": item.get("placa"),
        "setor": item.get("setor") if setor is None else setor,
        "rota": item.get("rota") if rota is None else rota,
        "percentual": item.get("percentual_concluido") or 0 if percentual is None else percentual,
        "veredicto": item.get("veredicto") or "" if veredicto is None else veredicto,
        "km_coleta": item.get("km_coleta") if km_coleta is None else km_coleta,
        "km_total": item.get("km_total") if km_total is None else km_total,
        "trechos": trechos,
        "executada": [pt for trecho in trechos for pt in trecho],
        "poligonos": (poligonos or [])[:3],
        "bounds": bounds_nucleo(trechos),
    }


def mapas_por_caminhao(rotas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Uma folha por caminhão com o GPS do dia, para zoom de rua no relatório."""
    saida: list[dict[str, Any]] = []
    for item in rotas or []:
        trechos_dia = _trechos_veiculo(item)
        if not trechos_dia:
            continue
        visiveis = coberturas_relatorio(item, max_n=3)
        poligonos = [anel for cob in visiveis for anel in (cob.get("poligonos") or [])[:1]][:3]
        if not poligonos:
            poligonos = list(item.get("poligonos") or [])[:3]
        saida.append(_folha_caminhao(item, trechos=trechos_dia, poligonos=poligonos))
    saida.sort(key=lambda row: str(row.get("veiculo") or ""))
    return saida


def html_secao_mapas_caminhao(rotas: list[dict[str, Any]] | None) -> str:
    mapas = mapas_por_caminhao(rotas or [])
    if not mapas:
        return ""
    blocos = []
    leve = []
    for i, item in enumerate(mapas):
        pct = float(item.get("percentual") or 0)
        titulo = str(item.get("veiculo") or "Caminhão")
        sub = " · ".join(
            parte
            for parte in [
                str(item.get("placa") or "").strip(),
                str(item.get("setor") or "").strip(),
                f"{pct:.0f}% concluída" if pct else "",
            ]
            if parte
        )
        blocos.append(
            "<article class='mapa-caminhao'>"
            f"<h2>{_esc(titulo)}</h2>"
            f"<p>{_esc(sub)}</p>"
            f"<div class='mapa-folha' id='mapa-caminhao-{i}'></div>"
            "</article>"
        )
        leve.append(
            {
                "trechos": [list(t) for t in (item.get("trechos") or []) if t and len(t) >= 2],
                "poligonos": [_thin_pts(p, 120) for p in (item.get("poligonos") or [])[:3] if p],
                "bounds": item.get("bounds"),
            }
        )
    dados = json.dumps(leve, ensure_ascii=False)
    return f"""
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<style>
  .rotas-por-caminhao {{ margin-top: 36px; }}
  .rotas-por-caminhao > h2 {{ font-size: 20px; margin: 0 0 6px; }}
  .rotas-por-caminhao > .intro {{ margin: 0 0 18px; color: #4b5a68; }}
  .mapa-caminhao {{
    margin: 0 0 28px; background: #fff; border: 1px solid #d5dde6;
    border-radius: 10px; padding: 12px 12px 10px;
  }}
  .mapa-caminhao h2 {{ margin: 0 0 4px; font-size: 16px; }}
  .mapa-caminhao p {{ margin: 0 0 10px; color: #4b5a68; }}
  .mapa-folha {{ height: 520px; border-radius: 8px; border: 1px solid #e2e8ee; }}
</style>
<section class="rotas-por-caminhao">
  <h2>Rota de cada caminhão</h2>
  <p class="intro">Cada mapa mostra só o trajeto daquele caminhão, com zoom nas ruas por onde passou.</p>
  {''.join(blocos)}
</section>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
  const mapasCaminhao = {dados};
  mapasCaminhao.forEach((rota, i) => {{
    const el = document.getElementById("mapa-caminhao-" + i);
    if (!el) return;
    const mapa = L.map(el, {{ zoomControl: true }});
    L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{{z}}/{{y}}/{{x}}", {{
      attribution: "Esri",
      maxZoom: 19
    }}).addTo(mapa);
    const gps = [];
    const polys = [];
    (rota.poligonos || []).slice(0, 3).forEach((pts) => {{
      if (pts && pts.length) {{
        polys.push(L.polygon(pts, {{
          color: "#0f172a", weight: 0, fillColor: "#f4c430", fillOpacity: 0.28
        }}).addTo(mapa));
        const fechado = (pts[0][0] !== pts[pts.length - 1][0] || pts[0][1] !== pts[pts.length - 1][1])
          ? pts.concat([pts[0]]) : pts;
        L.polyline(fechado, {{ color: "#0f172a", weight: 14, opacity: 0.95, lineJoin: "round" }}).addTo(mapa);
        L.polyline(fechado, {{ color: "#fff", weight: 9, opacity: 1, lineJoin: "round" }}).addTo(mapa);
        L.polyline(fechado, {{ color: "#ea580c", weight: 5, opacity: 1, lineJoin: "round" }}).addTo(mapa);
      }}
    }});
    (rota.trechos || []).forEach((pts) => {{
      if (!pts || !pts.length) return;
      L.polyline(pts, {{ color: "#fff", weight: 11, opacity: 0.9, lineJoin: "round" }}).addTo(mapa);
      gps.push(L.polyline(pts, {{
        color: "#d32f2f", weight: 6, opacity: 0.96, lineJoin: "round", lineCap: "round"
      }}).addTo(mapa));
    }});
    mapa.invalidateSize();
    const pecas = gps.concat(polys);
    if (pecas.length) {{
      mapa.fitBounds(L.featureGroup(pecas).getBounds(), {{ padding: [28, 28], maxZoom: 16 }});
    }} else if (rota.bounds && rota.bounds.length === 2) {{
      mapa.fitBounds(rota.bounds, {{ padding: [28, 28], maxZoom: 17 }});
    }} else {{
      mapa.setView([-16.82, -49.24], 13);
    }}
  }});
</script>
"""


def _html_inlog(
    dia: date,
    linhas: list[dict[str, Any]],
    rotas: list[dict[str, Any]] | None = None,
    hora_inicio: str = "00:00",
    hora_fim: str = "23:59",
    data_fim: date | None = None,
) -> str:
    titulo = f"Relatório diário — {rotulo_dia_periodo(dia, hora_inicio, hora_fim, data_fim)}"
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
    concluidas = [r for r in linhas if r.get("percentual_numero") is not None]
    media = (
        round(sum(r["percentual_numero"] for r in concluidas) / len(concluidas), 1)
        if concluidas else 0
    )
    mapas_html = html_secao_mapas_caminhao(rotas)
    return f"""<!DOCTYPE html>
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
    <p>Setor oficial do mapa de coleta (GPKG). % = km do GPS dentro do setor / km oficial do setor. Linha no mapa = trajeto do compactador no histórico do sinótico.</p>
  </header>
  <div class="wrap">
    <p class="resumo"><b>{len(linhas)}</b> setores oficiais com GPS Segtrack · média concluída <b>{str(media).replace('.', ',')}%</b></p>
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
        {''.join(corpo)}
      </tbody>
    </table>
    {mapas_html}
  </div>
</body>
</html>
"""


def _esc(valor: Any) -> str:
    return (
        str(valor if valor is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
