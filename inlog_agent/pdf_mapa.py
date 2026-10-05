from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from typing import Any

_VENDOR = Path(__file__).resolve().parent / "web" / "vendor"

PALETA = [
    "#c0392b", "#2471a3", "#1e8449", "#8e44ad", "#d35400",
    "#117a65", "#1f618d", "#b9770e", "#6c3483", "#196f3d",
    "#0e6655", "#922b21", "#1a5276", "#af601a", "#4a235a",
]


def cor_da_placa(placa: str, indice: int | None = None) -> str:
    if indice is not None:
        return PALETA[indice % len(PALETA)]
    chave = "".join(ch for ch in (placa or "").upper() if ch.isalnum())
    if not chave:
        return PALETA[0]
    return PALETA[sum(ord(ch) for ch in chave) % len(PALETA)]


def _par_valido(pt: Any) -> list[float] | None:
    if not pt or not isinstance(pt, (list, tuple)) or len(pt) < 2:
        return None
    try:
        lat, lon = float(pt[0]), float(pt[1])
    except (TypeError, ValueError):
        return None
    if abs(lat) > 90 or abs(lon) > 180:
        return None
    return [lat, lon]


def _horas_do_horario(horario: str) -> tuple[str, str]:
    texto = str(horario or "").replace("—", "–").replace("-", "–")
    if "–" not in texto:
        return "", ""
    a, b = texto.split("–", 1)
    return a.strip(), b.strip()


def _pontos_gps(rota: dict[str, Any]) -> list[list[float]]:
    pts: list[list[float]] = []
    for trecho in rota.get("trechos") or []:
        for par in trecho or []:
            ok = _par_valido(par)
            if ok:
                pts.append(ok)
    if pts:
        return pts
    for par in rota.get("executada") or []:
        ok = _par_valido(par)
        if ok:
            pts.append(ok)
    return pts


def _papel_cam(cam: dict[str, Any]) -> str:
    return str(cam.get("papel") or "").strip().lower()


def _caminhoes_operacao(rota: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Só o caminhão da coleta e os de apoio — ignora quem só passou."""
    cams = [c for c in (rota.get("caminhoes") or []) if isinstance(c, dict)]
    coleta = next((c for c in cams if _papel_cam(c) == "coleta"), None)
    apoios = [c for c in cams if _papel_cam(c) == "apoio"]
    vered = str(rota.get("execucao") or rota.get("veredicto") or "").upper()
    if coleta is None and "APOIO" in vered and apoios:
        coleta = None
    elif coleta is None and vered in {"FEITA", "PARCIAL", "CONCLUÍDA", "CONCLUIDA"}:
        if rota.get("ponto_inicio") or rota.get("hora_inicio"):
            coleta = {
                "placa": rota.get("placa"),
                "papel": "coleta",
                "ponto_inicio": rota.get("ponto_inicio"),
                "ponto_fim": rota.get("ponto_fim"),
                "hora_inicio": rota.get("hora_inicio"),
                "hora_fim": rota.get("hora_fim"),
                "trechos": rota.get("trechos") or [],
            }
    return coleta, apoios


def _extremos_do_caminhao(cam: dict[str, Any] | None) -> dict[str, Any]:
    if not cam:
        return {}
    pts: list[list[float]] = []
    for trecho in cam.get("trechos") or []:
        for par in trecho or []:
            ok = _par_valido(par)
            if ok:
                pts.append(ok)
    ini = _par_valido(cam.get("ponto_inicio"))
    fim = _par_valido(cam.get("ponto_fim"))
    if not ini and pts:
        ini = pts[0]
    if not fim and pts:
        fim = pts[-1]
    hora_ini = str(cam.get("hora_inicio") or "").strip()
    hora_fim = str(cam.get("hora_fim") or "").strip()
    if not hora_ini or not hora_fim:
        a, b = _horas_do_horario(str(cam.get("horario") or ""))
        hora_ini = hora_ini or a
        hora_fim = hora_fim or b
    return {
        "ponto_inicio": ini or [],
        "ponto_fim": fim or [],
        "hora_inicio": hora_ini,
        "hora_fim": hora_fim,
    }


def completar_extremos_rota(rota: dict[str, Any]) -> dict[str, Any]:
    """Entrada/saída só do caminhão da coleta (e do apoio, no próprio caminhão)."""
    coleta, apoios = _caminhoes_operacao(rota)
    fonte = coleta or (apoios[0] if apoios else None)
    ext = _extremos_do_caminhao(fonte)
    if ext:
        rota["ponto_inicio"] = ext["ponto_inicio"]
        rota["ponto_fim"] = ext["ponto_fim"]
        rota["hora_inicio"] = ext["hora_inicio"]
        rota["hora_fim"] = ext["hora_fim"]
    else:
        rota["ponto_inicio"] = _par_valido(rota.get("ponto_inicio")) or []
        rota["ponto_fim"] = _par_valido(rota.get("ponto_fim")) or []
    for cam in apoios:
        extra = _extremos_do_caminhao(cam)
        if extra.get("ponto_inicio"):
            cam["ponto_inicio"] = extra["ponto_inicio"]
        if extra.get("ponto_fim"):
            cam["ponto_fim"] = extra["ponto_fim"]
        if extra.get("hora_inicio"):
            cam["hora_inicio"] = extra["hora_inicio"]
        if extra.get("hora_fim"):
            cam["hora_fim"] = extra["hora_fim"]
    return rota


def hora_curta(texto: str) -> str:
    partes = str(texto or "").strip().split()
    if partes and ":" in partes[-1]:
        return partes[-1]
    return str(texto or "").strip()


def bounds_nucleo(trechos: list, raio_km: float = 1.35) -> list[list[float]] | None:
    pts: list[tuple[float, float]] = []
    for trecho in trechos or []:
        for par in trecho or []:
            if not par or len(par) < 2:
                continue
            try:
                pts.append((float(par[0]), float(par[1])))
            except (TypeError, ValueError):
                continue
    if len(pts) < 2:
        return None
    lat_c = statistics.median(p[0] for p in pts)
    lon_c = statistics.median(p[1] for p in pts)
    coslat = math.cos(math.radians(lat_c)) or 1.0

    def km(par: tuple[float, float]) -> float:
        return math.hypot((par[0] - lat_c) * 111.32, (par[1] - lon_c) * 111.32 * coslat)

    nucleo = [par for par in pts if km(par) <= raio_km]
    if len(nucleo) < max(6, int(len(pts) * 0.3)):
        lats = sorted(p[0] for p in pts)
        lons = sorted(p[1] for p in pts)
        a = int(len(lats) * 0.06)
        b = max(a + 1, int(len(lats) * 0.94))
        return [[lats[a], lons[a]], [lats[b], lons[b]]]
    return [
        [min(p[0] for p in nucleo), min(p[1] for p in nucleo)],
        [max(p[0] for p in nucleo), max(p[1] for p in nucleo)],
    ]


def leaflet_assets() -> tuple[str, str]:
    css = (_VENDOR / "leaflet.css").read_text(encoding="utf-8")
    js = (_VENDOR / "leaflet.js").read_text(encoding="utf-8")
    return css.replace("</style>", "<\\/style>"), js.replace("</script>", "<\\/script>")


def html_mapa_print(
    rotas: list[dict[str, Any]],
    max_zoom: int,
    detalhe: bool,
) -> str:
    css, js = leaflet_assets()
    rotas_js = json.dumps(rotas, ensure_ascii=False)
    paleta_js = json.dumps(PALETA)
    detalhe_js = "true" if detalhe else "false"
    nosso_css = """
    html, body { margin: 0; height: 100%; background: #edf0f3; }
    #folha { position: relative; width: 1600px; height: 1180px; background: #fff; }
    #mapa { width: 1600px; height: 1180px; }
    #legenda-placas {
      position: absolute; left: 12px; top: 12px; z-index: 500;
      background: rgba(15,28,36,.82); color: #fff; padding: 6px 8px;
      border-radius: 6px; font: 650 11px/1.3 Segoe UI, Arial, sans-serif;
      max-width: 280px;
    }
    #legenda-placas span { display:flex; align-items:center; gap:6px; margin:2px 0; }
    #legenda-placas i { width:12px; height:4px; border-radius:2px; display:inline-block; }
    .pino-ext { background: none !important; border: none !important; }
    .pino-ext .cx {
      color: #fff; font: 700 10px/1 Segoe UI, Arial, sans-serif;
      padding: 2px 5px; border-radius: 3px; border: 1px solid #fff;
      box-shadow: 0 1px 3px rgba(0,0,0,.35); white-space: nowrap;
    }
    """
    nosso_js = f"""
    const rotas = {rotas_js};
    const paleta = {paleta_js};
    const detalhe = {detalhe_js};
    const mapa = L.map("mapa", {{
      zoomControl: false,
      attributionControl: false
    }});
    const tiles = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{{z}}/{{y}}/{{x}}", {{
      maxZoom: 19
    }});
    tiles.on("load", () => {{ window.__tilesOk = true; }});
    tiles.addTo(mapa);
    const polys = [];
    const linhasGps = [];
    function limpar(pts) {{
      if (!pts || !pts.length || !Array.isArray(pts[0])) return [];
      return pts.filter((p) => p && p.length >= 2 && Math.abs(Number(p[0])) <= 90 && Math.abs(Number(p[1])) <= 180);
    }}
    function caminhoesDe(rota) {{
      const lista = (rota.caminhoes || []).filter((c) => (c.trechos && c.trechos.length) || (c.lacunas && c.lacunas.length));
      if (lista.length) return lista;
      const gps = (rota.trechos && rota.trechos.length)
        ? rota.trechos
        : ((rota.executada && rota.executada.length) ? [rota.executada] : []);
      return [{{
        placa: rota.placa,
        veiculo: rota.veiculo,
        cor: detalhe ? "#c0392b" : paleta[0],
        papel: "coleta",
        trechos: gps,
        lacunas: rota.lacunas || [],
        hora_inicio: rota.hora_inicio,
        hora_fim: rota.hora_fim,
        ponto_inicio: rota.ponto_inicio,
        ponto_fim: rota.ponto_fim
      }}];
    }}
    rotas.forEach((rota, idx) => {{
      (rota.vias || []).forEach((pts) => {{
        const via = limpar(pts);
        if (via.length < 2) return;
        L.polyline(via, {{
          color: "#7b8790", weight: detalhe ? 1.5 : 1, opacity: 0.45, lineJoin: "round"
        }}).addTo(mapa);
      }});
      (rota.poligonos || []).forEach((pts) => {{
        const anel = limpar(pts);
        if (anel.length < 4) return;
        const poly = L.polygon(anel, {{
          color: "#0f172a",
          weight: 0,
          fillColor: "#f4c430",
          fillOpacity: detalhe ? 0.32 : 0.18
        }}).addTo(mapa);
        polys.push(poly);
      }});
      const frota = caminhoesDe(rota);
      frota.forEach((cam, iCam) => {{
        const cor = cam.cor || (detalhe ? paleta[iCam % paleta.length] : paleta[idx % paleta.length]);
        (cam.trechos || []).forEach((pts) => {{
          const linhaPts = limpar(pts);
          if (linhaPts.length < 2) return;
          if (detalhe) {{
            L.polyline(linhaPts, {{ color: "#fff", weight: 10, opacity: 0.9, lineJoin: "round" }}).addTo(mapa);
          }}
          linhasGps.push(L.polyline(linhaPts, {{
            color: cor, weight: detalhe ? 5 : 5, opacity: 0.95,
            lineJoin: "round", lineCap: "round"
          }}).addTo(mapa));
        }});
      }});
      (rota.poligonos || []).forEach((pts) => {{
        const anel = limpar(pts);
        if (anel.length < 4) return;
        const fechado = (anel[0][0] !== anel[anel.length - 1][0] || anel[0][1] !== anel[anel.length - 1][1])
          ? anel.concat([anel[0]]) : anel;
        const peso = detalhe ? 6 : 3;
        L.polyline(fechado, {{
          color: "#0f172a", weight: detalhe ? 16 : 9, opacity: 0.95, lineJoin: "round", lineCap: "round"
        }}).addTo(mapa);
        L.polyline(fechado, {{
          color: "#fff", weight: detalhe ? 10 : 6, opacity: 1, lineJoin: "round", lineCap: "round"
        }}).addTo(mapa);
        L.polyline(fechado, {{
          color: "#ea580c", weight: peso, opacity: 1, lineJoin: "round", lineCap: "round"
        }}).addTo(mapa);
      }});
      if (detalhe) {{
        function horaCurta(txt) {{
          const p = String(txt || "").trim().split(/\\s+/);
          return (p.length && p[p.length-1].indexOf(":") >= 0) ? p[p.length-1] : String(txt || "").trim();
        }}
        function pino(pt, hora, cor, lado) {{
          if (!pt) return;
          L.circleMarker(pt, {{
            radius: 6, color: "#fff", weight: 2, fillColor: cor, fillOpacity: 1
          }}).addTo(mapa);
          if (!hora) return;
          L.marker(pt, {{
            icon: L.divIcon({{
              className: "pino-ext",
              html: "<div class='cx' style='background:" + cor + "'>" + hora + "</div>",
              iconSize: [48, 16],
              iconAnchor: lado === "ini" ? [-8, 8] : [56, 8]
            }}),
            zIndexOffset: 900
          }}).addTo(mapa);
        }}
        frota.forEach((cam) => {{
          const apoio = /apoio/i.test(String(cam.papel || ""));
          const visitas = (cam.passagens && cam.passagens.length) ? cam.passagens : [{{
            ponto_inicio: cam.ponto_inicio,
            ponto_fim: cam.ponto_fim,
            hora_inicio: cam.hora_inicio,
            hora_fim: cam.hora_fim
          }}];
          visitas.forEach((vis) => {{
            const ini = limpar([vis.ponto_inicio || []])[0];
            const fim = limpar([vis.ponto_fim || []])[0];
            pino(ini, horaCurta(vis.hora_inicio), apoio ? "#6b4c9a" : "#1b8a5a", "ini");
            pino(fim, horaCurta(vis.hora_fim), apoio ? "#6b4c9a" : "#c0392b", "fim");
          }});
        }});
        const box = document.getElementById("legenda-placas");
        if (box) {{
          box.innerHTML = frota.map((cam) => {{
            const papel = String(cam.papel || "passagem").toLowerCase();
            const ex = String(cam.execucao || "").toUpperCase();
            const rotulo = (papel === "apoio" || ex === "APOIO") ? "apoio"
              : (ex === "PARCIAL" ? "coleta parcial"
              : ((ex === "FEITA" || papel === "coleta") ? "coleta" : "passagem"));
            const pct = Number(cam.percentual || 0);
            const janelas = (cam.passagens || []).map((v) => [horaCurta(v.hora_inicio), horaCurta(v.hora_fim)].filter(Boolean).join("–")).filter(Boolean);
            const janela = janelas.length ? janelas.join(" · ") : [horaCurta(cam.hora_inicio), horaCurta(cam.hora_fim)].filter(Boolean).join("–");
            return "<span><i style='background:" + (cam.cor || "#c0392b") + "'></i>" +
              (cam.placa || cam.veiculo || "—") + " " + rotulo +
              (pct > 0 ? " " + pct.toFixed(0) + "%" : "") +
              (janela ? " " + janela : "") + "</span>";
          }}).join("");
        }}
      }}
    }});
    function enquadrar() {{
      mapa.invalidateSize(true);
      const soInspecao = rotas.length === 1 && rotas[0] && rotas[0].inspecao;
      const alvo = soInspecao && polys.length
        ? polys
        : (detalhe
        ? ((polys.length === 1) ? polys : (linhasGps.length ? linhasGps.concat(polys) : polys))
        : (linhasGps.length ? linhasGps : polys));
      if (alvo.length) {{
        const bounds = L.featureGroup(alvo).getBounds();
        if (bounds.isValid()) {{
          mapa.fitBounds(bounds, {{
            padding: detalhe ? [56, 72] : [28, 28],
            maxZoom: detalhe ? 16 : {max_zoom}
          }});
        }} else {{
          mapa.setView([-16.82, -49.24], 13);
        }}
      }} else {{
        mapa.setView([-16.82, -49.24], 13);
      }}
      window.__mapaPronto = true;
    }}
    setTimeout(enquadrar, 250);
    """
    return (
        "<!DOCTYPE html><html lang='pt-BR'><head><meta charset='utf-8'/>"
        f"<style>{css}\n{nosso_css}</style></head><body>"
        "<div id='folha'><div id='mapa'></div>"
        + (
            "<div id='legenda-placas'></div>"
            if detalhe
            else ""
        )
        + "</div>"
        f"<script>{js}</script><script>{nosso_js}</script></body></html>"
    )
