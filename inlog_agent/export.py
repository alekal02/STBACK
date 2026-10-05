from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .gps import tracos_realistas
from .pdf_mapa import bounds_nucleo
from .traco_agente import recortar_trechos_ao_setor


def _safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in value)
    return cleaned.strip("_") or "veiculo"


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_csv(path: Path, pontos: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    campos = [
        "veiculo", "placa", "setor", "rota", "percentual_concluido",
        "timestamp", "latitude", "longitude", "velocidade", "endereco", "fonte",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=campos, extrasaction="ignore")
        writer.writeheader()
        for ponto in pontos:
            writer.writerow({campo: ponto.get(campo, "") for campo in campos})
    return path


def write_gpx(path: Path, veiculo: str, pontos: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    linhas = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="inlog-agent" xmlns="http://www.topografix.com/GPX/1/1">',
        f"  <trk><name>{_xml(veiculo)}</name><trkseg>",
    ]
    for ponto in pontos:
        lat = ponto.get("latitude")
        lon = ponto.get("longitude")
        if lat is None or lon is None:
            continue
        extra = ""
        if ponto.get("timestamp"):
            extra += f"<time>{_xml(str(ponto['timestamp']))}</time>"
        if ponto.get("velocidade") not in (None, ""):
            extra += f"<speed>{_xml(str(ponto['velocidade']))}</speed>"
        linhas.append(f'    <trkpt lat="{lat}" lon="{lon}"><ele>0</ele>{extra}</trkpt>')
    linhas.extend(["  </trkseg></trk>", "</gpx>"])
    path.write_text("\n".join(linhas), encoding="utf-8")
    return path


def write_geojson(path: Path, veiculo: str, pontos: list[dict[str, Any]]) -> Path:
    coords = [
        [ponto["longitude"], ponto["latitude"]]
        for ponto in pontos
        if ponto.get("latitude") is not None and ponto.get("longitude") is not None
    ]
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"veiculo": veiculo, "pontos": len(coords)},
                "geometry": {"type": "LineString", "coordinates": coords},
            }
        ],
    }
    return write_json(path, payload)


def _cor_percentual(pct: float) -> str:
    if pct >= 80:
        return "#1b9e4b"
    if pct >= 45:
        return "#d4a017"
    return "#d64545"


def write_mapa_html(
    path: Path,
    titulo: str,
    rotas: list[dict[str, Any]],
    centro: tuple[float, float] = (-16.82, -49.24),
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    leve = []
    for rota in rotas:
        pontos_linha = rota.get("pontos_traco") or rota.get("pontos_dia") or rota.get("pontos") or []
        trechos = [t for t in (rota.get("trechos") or []) if t]
        if not trechos:
            trechos = tracos_realistas(pontos_linha, salto_max_m=95, tolerancia_m=7) if pontos_linha else []
        if not trechos:
            trechos = rota.get("trechos_gps") or []
        visiveis = [
            cob
            for cob in (rota.get("coberturas_inlog") or [])
            if str(cob.get("execucao") or cob.get("veredicto") or "").upper()
            in {"PARCIAL", "FEITA", "APOIO"}
        ]
        if rota.get("inspecao"):
            folhas = (rota.get("coberturas_inlog") or [None])[:1] or [None]
        else:
            folhas = visiveis or [None]
        clip_m = 40
        for cob in folhas:
            if cob:
                aneis = [anel for anel in (cob.get("poligonos") or []) if anel and len(anel) >= 4]
                recorte = recortar_trechos_ao_setor(trechos, aneis, buffer_m=clip_m)
                trechos_cob = recorte
                lacunas_cob: list = []
                leve.append(
                    {
                        "veiculo": rota.get("veiculo"),
                        "placa": rota.get("placa"),
                        "setor": cob.get("setor") or rota.get("setor"),
                        "codigo_rota": rota.get("codigo_rota") or "",
                        "rota": cob.get("rota") or rota.get("rota"),
                        "percentual": cob.get("percentual") or rota.get("percentual") or rota.get("percentual_concluido") or 0,
                        "percentual_concluido": cob.get("percentual") or rota.get("percentual_concluido") or 0,
                        "veredicto": cob.get("execucao") or cob.get("veredicto") or rota.get("veredicto") or "",
                        "execucao": cob.get("execucao") or rota.get("execucao") or "",
                        "fonte": rota.get("fonte"),
                        "fonte_nome": rota.get("fonte_nome"),
                        "confianca": cob.get("confianca") or "",
                        "horario": cob.get("horario") or rota.get("horario") or "",
                        "horario_rota": cob.get("horario_rota") or rota.get("horario_rota") or "",
                        "horario_apoio": cob.get("horario_apoio") or rota.get("horario_apoio") or "",
                        "hora_inicio": cob.get("hora_inicio") or rota.get("hora_inicio") or "",
                        "hora_fim": cob.get("hora_fim") or rota.get("hora_fim") or "",
                        "ponto_inicio": cob.get("ponto_inicio") or rota.get("ponto_inicio") or [],
                        "ponto_fim": cob.get("ponto_fim") or rota.get("ponto_fim") or [],
                        "rota_principal": cob.get("rota_principal") or rota.get("rota_principal") or "",
                        "executada": [pt for trecho in trechos_cob for pt in trecho],
                        "trechos": trechos_cob,
                        "lacunas": lacunas_cob,
                        "poligonos": aneis,
                        "vias": cob.get("vias") or rota.get("vias") or [],
                        "caminhoes": cob.get("caminhoes") or rota.get("caminhoes") or [],
                        "ontem": bool(rota.get("ontem")),
                        "inspecao": bool(rota.get("inspecao")),
                        "bounds": bounds_nucleo(aneis) if rota.get("inspecao") else (bounds_nucleo(trechos_cob) or bounds_nucleo(aneis)),
                    }
                )
            else:
                poligonos = []
                origem = rota.get("coberturas_inlog") or []
                for row in origem:
                    for anel in row.get("poligonos") or []:
                        if anel and len(anel) >= 4:
                            poligonos.append(anel)
                if not poligonos:
                    poligonos = [anel for anel in (rota.get("poligonos") or []) if anel and len(anel) >= 4]
                if poligonos:
                    recorte = recortar_trechos_ao_setor(trechos, poligonos, buffer_m=clip_m)
                    trechos = recorte
                    pacote = {"trechos": trechos, "lacunas": []}
                else:
                    pacote = {"trechos": trechos, "lacunas": []}
                leve.append(
                    {
                        "veiculo": rota.get("veiculo"),
                        "placa": rota.get("placa"),
                        "setor": rota.get("setor"),
                        "codigo_rota": rota.get("codigo_rota") or "",
                        "rota": rota.get("rota"),
                        "percentual": rota.get("percentual") or rota.get("percentual_concluido") or 0,
                        "percentual_concluido": rota.get("percentual_concluido") or rota.get("percentual") or 0,
                        "veredicto": rota.get("veredicto") or "",
                        "execucao": rota.get("execucao") or "",
                        "fonte": rota.get("fonte"),
                        "fonte_nome": rota.get("fonte_nome"),
                        "confianca": rota.get("confianca") or "",
                        "horario": rota.get("horario") or "",
                        "horario_rota": rota.get("horario_rota") or "",
                        "horario_apoio": rota.get("horario_apoio") or "",
                        "hora_inicio": rota.get("hora_inicio") or "",
                        "hora_fim": rota.get("hora_fim") or "",
                        "ponto_inicio": rota.get("ponto_inicio") or [],
                        "ponto_fim": rota.get("ponto_fim") or [],
                        "rota_principal": rota.get("rota_principal") or "",
                        "executada": [pt for trecho in trechos for pt in trecho],
                        "trechos": trechos,
                        "lacunas": pacote.get("lacunas") or rota.get("lacunas") or [],
                        "poligonos": poligonos,
                        "vias": rota.get("vias") or [],
                        "caminhoes": rota.get("caminhoes") or [],
                        "ontem": bool(rota.get("ontem")),
                        "inspecao": bool(rota.get("inspecao")),
                        "bounds": bounds_nucleo(poligonos) if rota.get("inspecao") else bounds_nucleo(trechos),
                    }
                )
    dados = json.dumps(leve, ensure_ascii=False)
    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8" />
  <title>{_xml(titulo)}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <style>
    html, body {{ height: 100%; margin: 0; font: 13px/1.4 "Segoe UI", sans-serif; }}
    #mapa {{ height: 100%; }}
    .painel {{
      position: absolute; top: 10px; right: 10px; z-index: 500;
      width: 360px; max-height: calc(100% - 20px); overflow: auto;
      background: #fff; border-radius: 10px; box-shadow: 0 4px 18px rgba(0,0,0,.18);
      padding: 12px;
    }}
    .item {{ padding: 8px 0; border-bottom: 1px solid #eee; cursor: pointer; }}
    .item:hover, .item.ativo {{ background: #f4f8ff; }}
    .barra {{ height: 8px; background: #ececec; border-radius: 99px; overflow: hidden; margin-top: 4px; }}
    .barra span {{ display: block; height: 100%; }}
    .pct {{ font-weight: 700; }}
    small {{ color: #667; }}
    .pino-hora {{ background: #0f1c24; color: #fff; border: 0; border-radius: 3px; padding: 1px 4px; font: 10px/1.1 Segoe UI, sans-serif; }}
    .pino-hora::before {{ display: none; }}
    button.todos {{ margin: 8px 0; padding: 6px 10px; }}
    .toggle {{ display: none; }}
    .linha-rota {{ display: flex; justify-content: space-between; gap: 8px; align-items: baseline; }}
    @media (max-width: 720px) {{
      .toggle {{
        display: block; width: 100%; background: #12232e; color: #fff;
        border: 0; border-radius: 10px; padding: 11px 12px; font-weight: 700;
      }}
      .painel {{
        top: auto; right: 0; bottom: 0; left: 0; width: auto;
        max-height: 42%;
        border-radius: 16px 16px 0 0;
        padding: 8px 10px calc(8px + env(safe-area-inset-bottom));
      }}
      .painel.fechado {{ max-height: 56px; overflow: hidden; }}
      .painel.fechado .titulo-mapa,
      .painel.fechado .legenda-mapa,
      .painel.fechado #ver-todos,
      .painel.fechado #lista {{ display: none; }}
      .titulo-mapa, .legenda-mapa {{ display: none; }}
      .item .detalhe {{ display: none; }}
      .item {{ padding: 11px 2px; }}
    }}
  </style>
</head>
<body>
  <div id="mapa"></div>
  <aside class="painel" id="painel">
    <button class="toggle" type="button" id="toggle-painel">Rotas e %</button>
    <strong class="titulo-mapa">{_xml(titulo)}</strong>
    <p class="legenda-mapa"><small>Faixa preta + laranja = deliminação oficial da rota. Área amarela = polígono. Cada cor de linha = um caminhão. Tracejado = lacuna de sinal. Pino verde = entrada. Pino vermelho = saída.</small></p>
    <button class="todos" type="button" id="ver-todos">Ver todos</button>
    <div id="lista"></div>
  </aside>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const rotas = {dados};
    const mapa = L.map("mapa");
    L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{{z}}/{{y}}/{{x}}", {{
      attribution: "Esri",
      maxZoom: 19
    }}).addTo(mapa);
    const camadas = [];
    const grupo = [];
    const linhasGps = [];
    const lista = document.getElementById("lista");
    const painel = document.getElementById("painel");
    const estreito = window.matchMedia("(max-width: 720px)").matches;
    if (estreito && painel) painel.classList.add("fechado");
    const togglePainel = document.getElementById("toggle-painel");
    if (togglePainel && painel) {{
      togglePainel.onclick = () => {{
        painel.classList.toggle("fechado");
        setTimeout(() => mapa.invalidateSize(), 180);
      }};
    }}
    function cor(pct, rota) {{
      const sit = String((rota && (rota.execucao || rota.veredicto)) || "");
      if (rota && rota.ontem) return "#2c6fbb";
      if (/APOIO/i.test(sit)) return "#6b4c9a";
      if (pct >= 80) return "#1b9e4b";
      if (pct >= 45) return "#d4a017";
      return "#d64545";
    }}
    function pctDe(rota) {{
      const n = Number(rota.percentual);
      if (!Number.isNaN(n) && n > 0) return n;
      const alt = Number(rota.percentual_concluido);
      return Number.isNaN(alt) ? 0 : alt;
    }}
    function rotuloSit(rota, pct) {{
      const v = String(rota.veredicto || rota.execucao || "");
      let sit = "Não concluída";
      if (/APOIO/i.test(v)) sit = "Apoio";
      else if (v && !/^(FEITA|PARCIAL|NÃO FEITA|NAO FEITA)$/i.test(v)) sit = v;
      else if (pct >= 80) sit = "Concluída";
      else if (pct >= 45) sit = "Concluída parcialmente";
      else if (pct >= 25) sit = "Início de coleta";
      else if (v) sit = v;
      if (rota.ontem) sit += " · ontem";
      return sit;
    }}
    function trechosDe(rota) {{
      if (rota.trechos && rota.trechos.length) return rota.trechos;
      return (rota.executada && rota.executada.length) ? [rota.executada] : [];
    }}
    function caminhoesDe(rota) {{
      const lista = (rota.caminhoes || []).filter((c) => (c.trechos && c.trechos.length) || (c.lacunas && c.lacunas.length));
      if (lista.length) return lista;
      return [{{
        placa: rota.placa,
        veiculo: rota.veiculo,
        cor: rota.cor || "#c0392b",
        papel: "coleta",
        trechos: trechosDe(rota),
        lacunas: rota.lacunas || [],
        horario: rota.horario,
        hora_inicio: rota.hora_inicio,
        hora_fim: rota.hora_fim,
        ponto_inicio: rota.ponto_inicio,
        ponto_fim: rota.ponto_fim,
        percentual: pctDe(rota)
      }}];
    }}
    rotas.forEach((rota, idx) => {{
      const pct = pctDe(rota);
      const sit = rotuloSit(rota, pct);
      const c = cor(pct, rota);
      const horas = [rota.horario_apoio || rota.horario, (rota.horario_rota && rota.rota_principal) ? ("rota " + rota.rota_principal + ": " + rota.horario_rota) : ""].filter(Boolean).join(" · ");
      const layer = {{ exe: [], plan: [], feitos: [], falta: [], setor: [], vias: [], marks: [], bounds: rota.bounds || null }};
      (rota.vias || []).forEach((pts) => {{
        if (!pts || pts.length < 2) return;
        const via = L.polyline(pts, {{
          color: "#6b7280",
          weight: 1.6,
          opacity: 0.55,
          lineJoin: "round"
        }}).addTo(mapa);
        layer.vias.push(via);
        grupo.push(via);
      }});
      (rota.poligonos || []).forEach((pts) => {{
        if (!pts || pts.length < 4) return;
        const corBorda = rota.ontem ? "#1a4e86" : "#6b5210";
        const poly = L.polygon(pts, {{
          color: corBorda,
          weight: 0,
          fillColor: rota.ontem ? "#7eb6e8" : "#f4c430",
          fillOpacity: rota.inspecao ? 0.32 : 0.22
        }}).addTo(mapa);
        poly.bindPopup(`<b>${{rota.setor || ""}}</b><br>${{rota.ontem ? "ontem · " : ""}}${{rota.rota || ""}}`);
        layer.setor.push(poly);
        grupo.push(poly);
      }});
      const frota = caminhoesDe(rota);
      frota.forEach((cam) => {{
        const corCam = cam.cor || "#c0392b";
        const papel = cam.papel || "passagem";
        (cam.trechos && cam.trechos.length ? cam.trechos : []).forEach((pts) => {{
          if (!pts.length) return;
          L.polyline(pts, {{ color: "#fff", weight: 10, opacity: 0.85, lineJoin: "round" }}).addTo(mapa);
          const exe = L.polyline(pts, {{
            color: corCam,
            weight: papel === "passagem" ? 4 : 5,
            opacity: papel === "passagem" ? 0.82 : 0.95,
            lineJoin: "round",
            lineCap: "round"
          }}).addTo(mapa);
          exe.bindPopup(`<b>${{cam.placa || cam.veiculo || ""}}</b><br>${{papel}} · ${{Number(cam.percentual || 0).toFixed(0)}}%<br>${{cam.horario || ""}}<br>${{rota.setor || ""}}`);
          exe.on("click", () => focar(idx));
          layer.exe.push(exe);
          grupo.push(exe);
          linhasGps.push(exe);
        }});
      }});
      (rota.poligonos || []).forEach((pts) => {{
        if (!pts || pts.length < 4) return;
        const corBorda = rota.ontem ? "#1a4e86" : "#0f172a";
        const corLimite = rota.ontem ? "#1d4ed8" : "#ea580c";
        const fechado = (pts[0][0] !== pts[pts.length - 1][0] || pts[0][1] !== pts[pts.length - 1][1])
          ? pts.concat([pts[0]]) : pts;
        const halo = L.polyline(fechado, {{
          color: "#0f172a", weight: rota.inspecao ? 16 : 13, opacity: 0.95, lineJoin: "round", lineCap: "round"
        }}).addTo(mapa);
        const meio = L.polyline(fechado, {{
          color: "#fff", weight: rota.inspecao ? 10 : 8, opacity: 1, lineJoin: "round", lineCap: "round"
        }}).addTo(mapa);
        const borda = L.polyline(fechado, {{
          color: corLimite, weight: rota.inspecao ? 5 : 4, opacity: 1, lineJoin: "round", lineCap: "round"
        }}).addTo(mapa);
        layer.setor.push(halo, meio, borda);
        grupo.push(halo, meio, borda);
      }});
      function horaPino(txt) {{
        const p = String(txt || "").trim().split(/\\s+/);
        return (p.length && p[p.length-1].indexOf(":") >= 0) ? p[p.length-1] : String(txt || "").trim();
      }}
      frota.forEach((cam) => {{
        const apoio = /apoio/i.test(String(cam.papel || ""));
        const visitas = (cam.passagens && cam.passagens.length) ? cam.passagens : [{{
          ponto_inicio: cam.ponto_inicio,
          ponto_fim: cam.ponto_fim,
          hora_inicio: cam.hora_inicio,
          hora_fim: cam.hora_fim
        }}];
        const primeira = visitas[0] || {{}};
        const ultima = visitas[visitas.length - 1] || primeira;
        const iniPt = (primeira.ponto_inicio && primeira.ponto_inicio.length >= 2) ? primeira.ponto_inicio : null;
        const fimPt = (ultima.ponto_fim && ultima.ponto_fim.length >= 2) ? ultima.ponto_fim : null;
        if (iniPt) {{
          layer.marks.push(L.circleMarker(iniPt, {{ radius: 6, color: "#fff", fillColor: apoio ? "#6b4c9a" : "#20c05c", fillOpacity: 1, weight: 2 }}).addTo(mapa).bindTooltip(horaPino(primeira.hora_inicio), {{permanent:true, direction:"right", className:"pino-hora"}}));
        }}
        if (fimPt) {{
          layer.marks.push(L.circleMarker(fimPt, {{ radius: 6, color: "#fff", fillColor: apoio ? "#6b4c9a" : "#e74c3c", fillOpacity: 1, weight: 2 }}).addTo(mapa).bindTooltip(horaPino(ultima.hora_fim), {{permanent:true, direction:"left", className:"pino-hora"}}));
        }}
      }});
      camadas.push(layer);
      const chips = frota.map((cam) => {{
        const visitasCam = cam.passagens || [];
        const iniH = horaPino((visitasCam[0] || cam).hora_inicio || cam.hora_inicio);
        const fimH = horaPino((visitasCam[visitasCam.length - 1] || cam).hora_fim || cam.hora_fim);
        const janela = [iniH, fimH].filter(Boolean).join("–");
        return `<span style="display:inline-block;width:10px;height:10px;background:${{cam.cor || '#c0392b'}};border-radius:2px;margin-right:4px"></span>${{cam.placa || cam.veiculo || "—"}} <small>${{cam.papel || "passagem"}} ${{Number(cam.percentual || 0).toFixed(0)}}%${{janela ? " · " + janela : ""}}</small>`;
      }}).join("<br>");
      const box = document.createElement("div");
      box.className = "item";
      box.innerHTML = `<div class="linha-rota"><b>${{rota.setor || rota.veiculo || ""}}</b> <span class="pct" style="color:${{c}}">${{pct.toFixed(0)}}%</span></div>
        <div class="detalhe"><small>${{sit}} · Segtrack<br>${{rota.rota || ""}}${{rota.ontem ? "<br>rota de ontem" : ""}}${{horas ? "<br>" + horas : ""}}</small>
        <div style="margin-top:6px;font-size:12px;line-height:1.45">${{chips}}</div></div>
        <div class="barra"><span style="width:${{Math.min(pct,100)}}%;background:${{c}}"></span></div>`;
      box.onclick = () => focar(idx);
      lista.appendChild(box);
    }});
    function focar(idx) {{
      document.querySelectorAll(".item").forEach((el, i) => el.classList.toggle("ativo", i === idx));
      camadas.forEach((layer, i) => {{
        const on = idx === null || i === idx;
        layer.exe.forEach((l) => l.setStyle({{ opacity: on ? 0.92 : 0.08, weight: on ? 5 : 2 }}));
        layer.plan.forEach((l) => l.setStyle({{ opacity: on ? 0.7 : 0.05 }}));
        (layer.feitos || []).forEach((l) => l.setStyle({{ opacity: on ? 0.95 : 0.08, weight: on ? 6 : 2 }}));
        (layer.falta || []).forEach((l) => l.setStyle({{ opacity: on ? 0.88 : 0.08, weight: on ? 5 : 2 }}));
        (layer.setor || []).forEach((l) => {{
          const fill = l.options && l.options.fillColor;
          if (fill) l.setStyle({{ fillOpacity: on ? 0.28 : 0.08 }});
          else l.setStyle({{ opacity: on ? 1 : 0.4, weight: on ? (l.options.color === "#fff" ? 10 : (l.options.color === "#0f172a" ? 16 : 5)) : 2 }});
        }});
        layer.marks.forEach((m) => {{ if (on) {{ if (!mapa.hasLayer(m)) m.addTo(mapa); }} else {{ mapa.removeLayer(m); }} }});
      }});
      if (idx !== null) {{
        const layer = camadas[idx];
        const pecas = [...(layer.setor || []), ...layer.exe];
        if (layer.bounds && layer.bounds.length === 2 && !layer.setor.length) {{
          mapa.fitBounds(layer.bounds, {{ padding: [40, 40], maxZoom: 16 }});
        }} else if (pecas.length) {{
          mapa.fitBounds(L.featureGroup(pecas).getBounds(), {{ padding: [40, 40], maxZoom: 16 }});
        }}
        if (estreito && painel) painel.classList.add("fechado");
      }}
    }}
    document.getElementById("ver-todos").onclick = () => {{
      document.querySelectorAll(".item").forEach((el) => el.classList.remove("ativo"));
      camadas.forEach((layer) => {{
        layer.exe.forEach((l) => l.setStyle({{ opacity: 0.92, weight: 5 }}));
        layer.plan.forEach((l) => l.setStyle({{ opacity: 0.7 }}));
        (layer.feitos || []).forEach((l) => l.setStyle({{ opacity: 0.95, weight: 6 }}));
        (layer.falta || []).forEach((l) => l.setStyle({{ opacity: 0.88, weight: 5 }}));
        (layer.setor || []).forEach((l) => {{
          const fill = l.options && l.options.fillColor;
          if (fill) l.setStyle({{ fillOpacity: 0.22 }});
          else l.setStyle({{ opacity: 1, weight: l.options.color === "#fff" ? 10 : (l.options.color === "#0f172a" ? 16 : 5) }});
        }});
        layer.marks.forEach((m) => {{ if (!mapa.hasLayer(m)) m.addTo(mapa); }});
      }});
      const polys = camadas.flatMap((layer) => layer.setor || []);
      const alvo = (linhasGps.length && polys.length)
        ? linhasGps.concat(polys)
        : (linhasGps.length ? linhasGps : (polys.length ? polys : grupo));
      if (alvo.length) mapa.fitBounds(L.featureGroup(alvo).getBounds(), {{ padding: [30, 30], maxZoom: 15 }});
    }};
    const polysIni = camadas.flatMap((layer) => layer.setor || []);
    const inspecao = rotas.length === 1 && rotas[0] && rotas[0].inspecao;
    const alvoInicial = (inspecao && polysIni.length)
      ? polysIni
      : ((linhasGps.length && polysIni.length)
        ? linhasGps.concat(polysIni)
        : (linhasGps.length ? linhasGps : (polysIni.length ? polysIni : grupo)));
    if (alvoInicial.length) mapa.fitBounds(L.featureGroup(alvoInicial).getBounds(), {{ padding: [30, 30], maxZoom: 15 }});
    else mapa.setView([{centro[0]}, {centro[1]}], 12);
    function indiceRota(codigo) {{
      const alvo = String(codigo || "").toUpperCase().trim();
      if (!alvo) return -1;
      return rotas.findIndex((r) => {{
        const cod = String(r.codigo_rota || "").toUpperCase();
        const setor = String(r.setor || "").toUpperCase();
        return cod === alvo || setor.indexOf(alvo + " ") === 0 || setor.indexOf(alvo + "-") === 0 || setor.indexOf(alvo) === 0;
      }});
    }}
    function focarCodigo(codigo) {{
      const idx = indiceRota(codigo);
      if (idx >= 0) focar(idx);
    }}
    window.addEventListener("message", (ev) => {{
      const d = ev.data || {{}};
      if (d.tipo === "focar-rota") focarCodigo(d.codigo);
    }});
    const hash = (location.hash || "").replace(/^#/, "");
    if (hash) focarCodigo(hash);
    else if (rotas.length === 1) focar(0);
  </script>
</body>
</html>
"""
    path.write_text(html, encoding="utf-8")
    return path


def reconstruir_mapa(pasta: Path) -> Path:
    dados = json.loads((pasta / "rotas.json").read_text(encoding="utf-8"))
    dia = dados.get("data") or pasta.name
    if pasta.parent.name == "argos":
        titulo = f"Argos Aparecida — {dia}"
    elif pasta.parent.name == "segtrack":
        titulo = f"Segtrack Aparecida — {dia}"
    else:
        titulo = f"Rotas {dia}"
    return write_mapa_html(pasta / "mapa.html", titulo, dados.get("rotas") or [])


def export_rotas(
    output_dir: Path,
    data_rota: str,
    rotas: list[dict[str, Any]],
    *,
    leve: bool = False,
) -> dict[str, str]:
    pasta = output_dir / "rotas" / data_rota
    pasta.mkdir(parents=True, exist_ok=True)
    gerados: dict[str, str] = {}

    todos_pontos: list[dict[str, Any]] = []
    for rota in rotas:
        veiculo = str(rota.get("veiculo") or "veiculo")
        slug = _safe_name(veiculo)
        pontos = rota.get("pontos") or []
        if not leve:
            write_json(pasta / f"{slug}.json", rota)
            write_geojson(pasta / f"{slug}.geojson", veiculo, pontos)
            write_gpx(pasta / f"{slug}.gpx", veiculo, pontos)
        for ponto in pontos:
            todos_pontos.append({"veiculo": veiculo, **ponto})

    frota_resumo = [
        {
            "veiculo": rota.get("veiculo"),
            "placa": rota.get("placa"),
            "setor": rota.get("setor"),
            "rota": rota.get("rota"),
            "percentual_concluido": rota.get("percentual_concluido"),
            "km_coleta": rota.get("km_coleta"),
            "total_pontos": rota.get("total_pontos"),
        }
        for rota in rotas
    ]
    write_csv(pasta / "frota.csv", [
        {
            "veiculo": item.get("veiculo"),
            "placa": item.get("placa"),
            "setor": item.get("setor"),
            "rota": item.get("rota"),
            "percentual_concluido": item.get("percentual_concluido"),
            "timestamp": "",
            "latitude": "",
            "longitude": "",
            "velocidade": "",
            "endereco": "",
            "fonte": "",
        }
        for item in frota_resumo
    ])
    gerados["resumo"] = str(write_json(pasta / "rotas.json", {
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
        "data": data_rota,
        "total_veiculos": len(rotas),
        "rotas": rotas,
    }))
    if not leve:
        gerados["csv"] = str(write_csv(pasta / "rotas.csv", todos_pontos))
        gerados["mapa"] = str(write_mapa_html(
            pasta / "mapa.html",
            f"Rotas {data_rota}",
            rotas,
        ))
    return gerados


def _xml(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
