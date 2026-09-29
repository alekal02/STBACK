"""Foto do mapa de ruas para o PDF.

Abre uma página Leaflet, espera o mapa oficial carregar e grava o JPEG.
Se a mesma geometria já foi fotografada, reusa o arquivo.
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import ROOT

_VERSAO = 4
_SALTO_M = 160
_pw: Any = None
_browser: Any = None
_profundidade = 0
_sem_navegador = False
_aviso = False


def _pasta() -> Path:
    bruto = os.getenv("OUTPUT_DIR", "").strip()
    pasta = (Path(bruto) if bruto else ROOT / "output") / "folhas"
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta


def _hex(cor: Any) -> str:
    if isinstance(cor, str) and cor.startswith("#"):
        return cor
    if isinstance(cor, (tuple, list)) and len(cor) >= 3:
        return "#{:02x}{:02x}{:02x}".format(int(cor[0]), int(cor[1]), int(cor[2]))
    return "#1d4e89"


def _fino(pts: list, max_pts: int) -> list:
    limpos = [p for p in pts or [] if isinstance(p, (list, tuple)) and len(p) >= 2]
    if len(limpos) <= max_pts:
        return limpos
    passo = max(1, len(limpos) // max_pts)
    saida = list(limpos[::passo])
    if saida[-1] != limpos[-1]:
        saida.append(limpos[-1])
    return saida


def _linhas(grupos: list, limite: int, max_pts: int) -> list:
    saida = []
    for linha in grupos or []:
        fina = _fino(linha, max_pts)
        if len(fina) >= 2:
            saida.append(fina)
        if len(saida) >= limite:
            break
    return saida


def _extremo(marca: Any) -> dict[str, Any] | None:
    if not isinstance(marca, dict):
        return None
    try:
        return {
            "p": [float(marca["latitude"]), float(marca["longitude"])],
            "hora": str(marca.get("hora") or ""),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _especificar(
    poligonos: list[dict[str, Any]],
    tracos: list,
    pontos: list[dict[str, Any]],
    enquadramento: list | None,
    vias: str,
    cor_traco: tuple[int, int, int],
    camadas: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    setores = []
    for item in poligonos or []:
        setores.append(
            {
                "rotulo": str(item.get("rotulo") or ""),
                "aneis": _linhas(item.get("aneis") or [], 4, 220),
                "feitos": _linhas(item.get("feitos") or [], 220, 80) if vias in {"ambas", "feitas"} else [],
                "faltando": _linhas(item.get("faltando") or [], 220, 80) if vias in {"ambas", "abertas"} else [],
            }
        )
    faixas = []
    origem = camadas or [{"tracos": tracos, "cor": cor_traco}]
    for faixa in origem:
        linhas = _linhas(faixa.get("tracos") or [], 4, 2400)
        if linhas:
            faixas.append(
                {
                    "cor": _hex(faixa.get("cor")),
                    "tracos": linhas,
                    "entrada": _extremo(faixa.get("entrada")),
                    "saida": _extremo(faixa.get("saida")),
                }
            )
    marcas = []
    for ponto in pontos or []:
        try:
            marcas.append([float(ponto["latitude"]), float(ponto["longitude"])])
        except (KeyError, TypeError, ValueError):
            continue
    return {
        "v": _VERSAO,
        "setores": setores,
        "faixas": faixas,
        "marcas": marcas[:400],
        "moldura": _linhas(enquadramento or [], 4, 180),
    }


def _html(spec: dict[str, Any]) -> str:
    dados = json.dumps(spec, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body {{ margin: 0; background: #edf0f3; }}
  #folha {{ width: 1280px; height: 900px; }}
  #mapa {{ width: 1280px; height: 900px; }}
  .pino {{ background: none !important; border: none !important; }}
  .pino .cx {{
    color: #fff; font: 700 13px/1.2 Segoe UI, Arial, sans-serif;
    padding: 3px 7px; border-radius: 5px; border: 2px solid #fff;
    box-shadow: 0 1px 4px rgba(0,0,0,.4); white-space: nowrap;
  }}
</style></head>
<body><div id="folha"><div id="mapa"></div></div>
<script>
const spec = {dados};
const mapa = L.map("mapa", {{ zoomControl: false, attributionControl: false }});
const tiles = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{{z}}/{{y}}/{{x}}", {{ maxZoom: 19 }});
let pendentes = 0;
tiles.on("loading", () => {{ pendentes += 1; }});
tiles.on("load", () => {{
  pendentes = Math.max(0, pendentes - 1);
  if (pendentes === 0) window.__tilesOk = true;
}});
tiles.addTo(mapa);
function limpar(pts) {{
  return (pts || []).filter((p) => p && p.length >= 2 && Math.abs(+p[0]) <= 90 && Math.abs(+p[1]) <= 180);
}}
function partir(pts) {{
  const linhas = [];
  let atual = [];
  limpar(pts).forEach((p) => {{
    if (atual.length) {{
      const a = atual[atual.length - 1];
      const dLat = (p[0] - a[0]) * 111320;
      const dLon = (p[1] - a[1]) * 111320 * Math.cos(a[0] * Math.PI / 180);
      if (Math.hypot(dLat, dLon) > {_SALTO_M}) {{
        if (atual.length >= 2) linhas.push(atual);
        atual = [p];
        return;
      }}
    }}
    atual.push(p);
  }});
  if (atual.length >= 2) linhas.push(atual);
  return linhas;
}}
const camadas = [];
(spec.setores || []).forEach((setor) => {{
  (setor.aneis || []).forEach((anel) => {{
    const pts = limpar(anel);
    if (pts.length < 4) return;
    camadas.push(L.polygon(pts, {{
      color: "#8a6a12", weight: 2, dashArray: "8 6",
      fillColor: "#e6c35c", fillOpacity: 0.12
    }}).addTo(mapa));
  }});
  (setor.faltando || []).forEach((linha) => {{
    partir(linha).forEach((pts) => {{
      L.polyline(pts, {{ color: "#fff", weight: 7, opacity: 0.9 }}).addTo(mapa);
      camadas.push(L.polyline(pts, {{ color: "#b01c12", weight: 4, opacity: 0.95 }}).addTo(mapa));
    }});
  }});
  (setor.feitos || []).forEach((linha) => {{
    partir(linha).forEach((pts) => {{
      L.polyline(pts, {{ color: "#fff", weight: 8, opacity: 0.95 }}).addTo(mapa);
      camadas.push(L.polyline(pts, {{ color: "#0f7a44", weight: 5, opacity: 0.98 }}).addTo(mapa));
    }});
  }});
}});
(spec.faixas || []).forEach((faixa) => {{
  (faixa.tracos || []).forEach((linha) => {{
    const pts = limpar(linha);
    if (pts.length < 2) return;
    L.polyline(pts, {{ color: "#fff", weight: 10, opacity: 0.92 }}).addTo(mapa);
    camadas.push(L.polyline(pts, {{ color: faixa.cor || "#1d4e89", weight: 5, opacity: 0.96 }}).addTo(mapa));
  }});
  function extremo(marca, titulo, cor) {{
    if (!marca || !marca.p || marca.p.length < 2) return;
    camadas.push(L.circleMarker(marca.p, {{
      radius: 8, color: "#fff", weight: 3, fillColor: cor, fillOpacity: 1
    }}).addTo(mapa));
    const texto = marca.hora ? (titulo + " " + marca.hora) : titulo;
    L.marker(marca.p, {{
      icon: L.divIcon({{
        className: "pino",
        html: "<div class='cx' style='background:" + cor + "'>" + texto + "</div>",
        iconSize: [108, 24],
        iconAnchor: [-14, 12]
      }}),
      zIndexOffset: 900
    }}).addTo(mapa);
  }}
  extremo(faixa.entrada, "Entrada", "#1b8a5a");
  extremo(faixa.saida, "Saída", "#c0392b");
}});
(spec.marcas || []).forEach((p) => {{
  if (!p || p.length < 2) return;
  camadas.push(L.circleMarker(p, {{
    radius: 6, color: "#fff", weight: 2, fillColor: "#c2410c", fillOpacity: 1
  }}).addTo(mapa));
}});
(spec.moldura || []).forEach((anel) => {{
  const pts = limpar(anel);
  if (pts.length >= 4) camadas.push(L.polygon(pts, {{ opacity: 0, fillOpacity: 0 }}).addTo(mapa));
}});
function enquadrar() {{
  mapa.invalidateSize(true);
  if (camadas.length) {{
    const caixa = L.featureGroup(camadas).getBounds();
    if (caixa.isValid()) mapa.fitBounds(caixa, {{ padding: [36, 36], maxZoom: 17 }});
    else mapa.setView([-16.82, -49.24], 13);
  }} else mapa.setView([-16.82, -49.24], 13);
  window.__mapaPronto = true;
}}
setTimeout(enquadrar, 200);
</script></body></html>"""


def _exes() -> list[str]:
    local = os.environ.get("LOCALAPPDATA", "")
    return [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        str(Path(local) / "Microsoft" / "Edge" / "Application" / "msedge.exe") if local else "",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        str(Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe") if local else "",
    ]


def _lancar(pw: Any) -> Any:
    ultimo: Exception | None = None
    for canal in ("msedge", "chrome"):
        try:
            return pw.chromium.launch(headless=True, channel=canal)
        except Exception as exc:
            ultimo = exc
    for caminho in _exes():
        if not caminho or not Path(caminho).is_file():
            continue
        try:
            return pw.chromium.launch(headless=True, executable_path=caminho)
        except Exception as exc:
            ultimo = exc
    detalhe = str(ultimo).splitlines()[0] if ultimo else ""
    raise RuntimeError("Não foi possível abrir o Edge nem o Chrome." + (f" {detalhe}" if detalhe else ""))


def _fechar() -> None:
    global _pw, _browser
    if _browser is not None:
        try:
            _browser.close()
        except Exception:
            pass
    _browser = None
    if _pw is not None:
        try:
            _pw.stop()
        except Exception:
            pass
    _pw = None


@contextmanager
def sessao() -> Iterator[None]:
    """Mantém o navegador aberto enquanto vários mapas do mesmo PDF são fotografados."""
    global _pw, _browser, _profundidade, _sem_navegador
    _profundidade += 1
    abriu = False
    if not _sem_navegador and _browser is None:
        try:
            from playwright.sync_api import sync_playwright

            _pw = sync_playwright().start()
            _browser = _lancar(_pw)
            abriu = True
        except Exception as exc:
            _sem_navegador = True
            _fechar()
            _avisar(exc)
    try:
        yield
    finally:
        _profundidade -= 1
        if _profundidade <= 0 and abriu:
            _fechar()


def _avisar(exc: Exception) -> None:
    global _aviso
    if _aviso:
        return
    _aviso = True
    print(f"Mapa Leaflet indisponível. O PDF segue com o desenho local. {exc}", flush=True)


def _tirar(spec: dict[str, Any]) -> bytes:
    chave = hashlib.sha256(
        json.dumps(spec, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    arquivo = _pasta() / f"{chave}.jpg"
    if arquivo.is_file() and arquivo.stat().st_size > 8000:
        return arquivo.read_bytes()
    page = _browser.new_page(viewport={"width": 1280, "height": 900}, device_scale_factor=2)
    try:
        page.set_content(_html(spec), wait_until="domcontentloaded", timeout=20_000)
        try:
            page.wait_for_function("() => window.__mapaPronto === true", timeout=12_000)
        except Exception:
            pass
        try:
            page.wait_for_function("() => window.__tilesOk === true", timeout=8_000)
        except Exception:
            pass
        page.wait_for_timeout(400)
        foto = page.locator("#folha").screenshot(type="jpeg", quality=84)
    finally:
        page.close()
    if len(foto) > 8000:
        arquivo.write_bytes(foto)
    return foto


def fotografar(
    poligonos: list[dict[str, Any]],
    tracos: list,
    pontos: list[dict[str, Any]],
    enquadramento: list | None = None,
    *,
    vias: str = "ambas",
    cor_traco: tuple[int, int, int] = (29, 78, 137),
    camadas: list[dict[str, Any]] | None = None,
) -> bytes | None:
    """Devolve o JPEG do mapa. None deixa o PDF desenhar as ruas no próprio processo."""
    if _sem_navegador:
        return None
    spec = _especificar(poligonos, tracos, pontos, enquadramento, vias, cor_traco, camadas)
    try:
        if _browser is None:
            with sessao():
                if _browser is None:
                    return None
                return _tirar(spec)
        return _tirar(spec)
    except Exception as exc:
        _avisar(exc)
        return None
