from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from shapely.geometry import LineString, shape

from .config import ROOT
from .mapa_oficial import TO_UTM

CACHE = ROOT / "data" / "ruas-aparecida.geojson"
OVERPASS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
BBOX = (-16.92, -49.38, -16.70, -49.17)
HIGHWAYS = (
    "residential",
    "living_street",
    "unclassified",
    "tertiary",
    "secondary",
    "primary",
    "service",
)


def _query() -> str:
    sul, oeste, norte, leste = BBOX
    tipos = "|".join(HIGHWAYS)
    return (
        f"[out:json][timeout:90];"
        f'way["highway"~"^({tipos})$"]["area"!="yes"]({sul},{oeste},{norte},{leste});'
        f"out geom;"
    )


def _baixar() -> dict[str, Any]:
    body = urllib.parse.urlencode({"data": _query()}).encode("utf-8")
    ultimo = None
    for url in OVERPASS:
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                "User-Agent": "inlog-agent/1.0 (relatorios de coleta Aparecida)",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            ultimo = exc
            continue
    raise ultimo or RuntimeError("Overpass indisponível")


def _geojson_de_overpass(bruto: dict[str, Any]) -> dict[str, Any]:
    features = []
    for el in bruto.get("elements") or []:
        if el.get("type") != "way":
            continue
        tags = el.get("tags") or {}
        if (tags.get("service") or "") in {"parking_aisle", "driveway", "parking"}:
            continue
        coords = []
        for pt in el.get("geometry") or []:
            lon, lat = pt.get("lon"), pt.get("lat")
            if lon is None or lat is None:
                continue
            coords.append([lon, lat])
        if len(coords) < 2:
            continue
        features.append(
            {
                "type": "Feature",
                "properties": {"highway": tags.get("highway"), "name": tags.get("name") or ""},
                "geometry": {"type": "LineString", "coordinates": coords},
            }
        )
    return {"type": "FeatureCollection", "features": features}


def carregar_malha() -> list[LineString]:
    from .mapa_oficial import carregar_vias

    oficiais = carregar_vias()
    if oficiais:
        return oficiais
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    if CACHE.is_file():
        bruto = json.loads(CACHE.read_text(encoding="utf-8"))
    else:
        print("Baixando malha viária de Aparecida (OpenStreetMap)...", flush=True)
        try:
            bruto = _geojson_de_overpass(_baixar())
            CACHE.write_text(json.dumps(bruto, ensure_ascii=False), encoding="utf-8")
            print(f"Malha salva em {CACHE} ({len(bruto.get('features') or [])} ruas).", flush=True)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            print(f"Não consegui baixar as ruas: {exc}", flush=True)
            return []
    linhas = []
    for feat in bruto.get("features") or []:
        hw = (feat.get("properties") or {}).get("highway")
        if hw == "service":
            continue
        try:
            geom = shape(feat.get("geometry"))
        except Exception:
            continue
        if geom.is_empty:
            continue
        partes = list(geom.geoms) if geom.geom_type == "MultiLineString" else [geom]
        for parte in partes:
            xy = [TO_UTM.transform(lon, lat) for lon, lat in parte.coords]
            if len(xy) >= 2:
                linhas.append(LineString(xy))
    return linhas


def ruas_do_setor(geom, malha: list[LineString] | None = None, buffer_m: float = 25) -> list[LineString]:
    if geom is None:
        return []
    malha = malha if malha is not None else carregar_malha()
    if not malha:
        return []
    zona = geom.buffer(buffer_m)
    recorte = []
    for linha in malha:
        if not linha.intersects(zona):
            continue
        inter = linha.intersection(zona)
        if inter.is_empty:
            continue
        partes = list(inter.geoms) if inter.geom_type == "MultiLineString" else [inter]
        for parte in partes:
            if parte.geom_type == "LineString" and parte.length >= 12:
                recorte.append(parte)
    return recorte


def anexar_ruas(setores: list[dict[str, Any]]) -> None:
    malha = carregar_malha()
    for setor in setores:
        setor["ruas"] = ruas_do_setor(setor.get("geom"), malha)
