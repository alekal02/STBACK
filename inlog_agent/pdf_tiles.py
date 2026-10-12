"""Fundo cartográfico do PDF, com tiles estáticos.

Usa o mesmo World Street Map da Esri que o mapa do painel já consome.
O crédito é o texto publicado pelo serviço. O cache em disco e em RAM tem
teto. Se faltar tile, o mapa inteiro volta ao desenho anterior.
"""

from __future__ import annotations

import math
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw, ImageFont

from .config import ROOT

# copyrightText de World Street Map/MapServer, lido no serviço da Esri.
ATRIBUICAO = (
    "Sources: Esri, HERE, Garmin, USGS, Intermap, INCREMENT P, NRCan, "
    "Esri Japan, METI, Esri China (Hong Kong), Esri Korea, Esri (Thailand), "
    "NGCC, (c) OpenStreetMap contributors, and the GIS User Community"
)
_URL = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "World_Street_Map/MapServer/tile/{z}/{y}/{x}"
)
_AGENTE = "SAGTrack-PDF/1.0 (relatorio operacional; cache limitado)"
_ZOOM_MIN = 12
_ZOOM_MAX = 17
_TILE = 256
_MAX_TILES = 36
_WORKERS = 4
_TIMEOUT_TILE = 2.5
_ORCAMENTO_MAPA = 4.5
_ORCAMENTO_PDF = 70.0
_REDE_MAX = 420
_RAM_MAX = 32
_DISCO_MAX = 32 * 1024 * 1024
_FAIXA = 46

_TRAVA = threading.Lock()
_RAM: OrderedDict[tuple[int, int, int], bytes] = OrderedDict()
_REDE = 0
_PRAZO = 0.0
_DISCO_BYTES: int | None = None


def reiniciar_orcamento() -> None:
    """Um PDF, um prazo. Não acumula downloads de um relatório para o outro."""
    global _REDE, _PRAZO
    with _TRAVA:
        _REDE = 0
        _PRAZO = time.perf_counter() + _ORCAMENTO_PDF


def dentro_do_prazo() -> bool:
    with _TRAVA:
        return _PRAZO <= 0 or time.perf_counter() < _PRAZO


def ram_tiles() -> int:
    with _TRAVA:
        return len(_RAM)


def _pasta() -> Path:
    bruto = (os.getenv("OUTPUT_DIR") or "").strip()
    base = Path(bruto) if bruto else ROOT / "output"
    pasta = base / "cache-tiles"
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta


def _mundo(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    n = 2**zoom
    x = (lon + 180.0) / 360.0 * n * _TILE
    lat = max(min(lat, 85.05112878), -85.05112878)
    rad = math.radians(lat)
    y = (1.0 - math.log(math.tan(rad) + 1.0 / math.cos(rad)) / math.pi) / 2.0 * n * _TILE
    return x, y


def _grades(oeste: float, leste: float, sul: float, norte: float, zoom: int) -> tuple[float, float, list[tuple[int, int]]]:
    x0, y0 = _mundo(oeste, norte, zoom)
    x1, y1 = _mundo(leste, sul, zoom)
    tx0 = math.floor(x0 / _TILE)
    tx1 = math.floor((x1 - 1) / _TILE)
    ty0 = math.floor(y0 / _TILE)
    ty1 = math.floor((y1 - 1) / _TILE)
    limite = 2**zoom - 1
    chaves = []
    for ty in range(max(0, ty0), min(limite, ty1) + 1):
        for tx in range(max(0, tx0), min(limite, tx1) + 1):
            chaves.append((tx, ty))
    return x1 - x0, y1 - y0, chaves


def enquadrar(
    pontos: list[tuple[float, float]],
    largura: int,
    altura: int,
) -> dict[str, Any] | None:
    """Escolhe o zoom em que a rota ocupa o quadro, com poucos tiles."""
    if len(pontos) < 2 or largura < 64 or altura < 64:
        return None
    oeste = min(p[0] for p in pontos)
    leste = max(p[0] for p in pontos)
    sul = min(p[1] for p in pontos)
    norte = max(p[1] for p in pontos)
    if leste - oeste < 1e-6:
        leste += 0.001
        oeste -= 0.001
    if norte - sul < 1e-6:
        norte += 0.001
        sul -= 0.001
    folga_x = max((leste - oeste) * 0.035, 0.00035)
    folga_y = max((norte - sul) * 0.035, 0.00035)
    oeste -= folga_x
    leste += folga_x
    sul -= folga_y
    norte += folga_y
    util_w = largura - 16
    util_h = altura - _FAIXA - 16
    for zoom in range(_ZOOM_MAX, _ZOOM_MIN - 1, -1):
        bw, bh, _chaves = _grades(oeste, leste, sul, norte, zoom)
        if bw <= 0 or bh <= 0:
            continue
        escala = min(util_w / bw, util_h / bh, 1.8)
        x0, y0 = _mundo(oeste, norte, zoom)
        x1, y1 = _mundo(leste, sul, zoom)
        origem_x = (x0 + x1) / 2 - largura / (2 * escala)
        origem_y = (y0 + y1) / 2 - util_h / (2 * escala)
        tiles = _tiles_visiveis(zoom, origem_x, origem_y, largura, altura, escala)
        if tiles and len(tiles) <= _MAX_TILES:
            return {
                "zoom": zoom,
                "escala": escala,
                "origem_x": origem_x,
                "origem_y": origem_y,
                "largura": largura,
                "altura": altura,
                "oeste": oeste,
                "leste": leste,
                "sul": sul,
                "norte": norte,
                "tiles": tiles,
            }
    return None


def _tiles_visiveis(
    zoom: int,
    origem_x: float,
    origem_y: float,
    largura: int,
    altura: int,
    escala: float,
) -> list[tuple[int, int, int]]:
    """Tiles que cobrem o quadro inteiro. Sem buraco cinza nas bordas."""
    if escala <= 0:
        return []
    x1 = origem_x + largura / escala
    y1 = origem_y + altura / escala
    limite = 2**zoom - 1
    tx0 = math.floor(origem_x / _TILE)
    tx1 = math.floor((x1 - 1) / _TILE)
    ty0 = math.floor(origem_y / _TILE)
    ty1 = math.floor((y1 - 1) / _TILE)
    if tx0 < 0 or ty0 < 0 or tx1 > limite or ty1 > limite:
        return []
    return [(zoom, tx, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)]


def projetar(vista: dict[str, Any], lon: float, lat: float) -> tuple[float, float]:
    x, y = _mundo(lon, lat, int(vista["zoom"]))
    escala = float(vista["escala"])
    return (x - vista["origem_x"]) * escala, (y - vista["origem_y"]) * escala


def _ram_ler(chave: tuple[int, int, int]) -> bytes | None:
    with _TRAVA:
        dado = _RAM.get(chave)
        if dado is not None:
            _RAM.move_to_end(chave)
        return dado


def _ram_por(chave: tuple[int, int, int], dado: bytes) -> None:
    with _TRAVA:
        _RAM[chave] = dado
        _RAM.move_to_end(chave)
        while len(_RAM) > _RAM_MAX:
            _RAM.popitem(last=False)


def _arquivo(chave: tuple[int, int, int]) -> Path:
    z, x, y = chave
    return _pasta() / f"{z}_{x}_{y}.jpg"


def _medir_disco(pasta: Path) -> int:
    total = 0
    for arq in pasta.glob("*.jpg"):
        try:
            total += arq.stat().st_size
        except OSError:
            continue
    return total


def _podar_disco(pasta: Path) -> None:
    global _DISCO_BYTES
    with _TRAVA:
        if _DISCO_BYTES is None:
            _DISCO_BYTES = _medir_disco(pasta)
        if _DISCO_BYTES <= _DISCO_MAX:
            return
        arquivos = []
        for arq in pasta.glob("*.jpg"):
            try:
                stat = arq.stat()
            except OSError:
                continue
            arquivos.append((stat.st_mtime, stat.st_size, arq))
        arquivos.sort()
        for _mtime, tamanho, arq in arquivos:
            if _DISCO_BYTES <= int(_DISCO_MAX * 0.75):
                break
            try:
                arq.unlink()
                _DISCO_BYTES -= tamanho
            except OSError:
                continue


def _gravar_disco(chave: tuple[int, int, int], dado: bytes) -> None:
    global _DISCO_BYTES
    pasta = _pasta()
    destino = _arquivo(chave)
    try:
        destino.write_bytes(dado)
    except OSError:
        return
    with _TRAVA:
        if _DISCO_BYTES is None:
            _DISCO_BYTES = _medir_disco(pasta)
        else:
            _DISCO_BYTES += len(dado)
    _podar_disco(pasta)


def _ler_disco(chave: tuple[int, int, int]) -> bytes | None:
    arq = _arquivo(chave)
    if not arq.is_file():
        return None
    try:
        dado = arq.read_bytes()
    except OSError:
        return None
    if len(dado) < 200:
        return None
    _ram_por(chave, dado)
    return dado


def _baixar_rede(chave: tuple[int, int, int]) -> bytes | None:
    z, x, y = chave
    url = _URL.format(z=z, x=x, y=y)
    pedido = Request(url, headers={"User-Agent": _AGENTE})
    try:
        with urlopen(pedido, timeout=_TIMEOUT_TILE) as resp:
            dado = resp.read()
            tipo = str(resp.headers.get("Content-Type") or "")
    except Exception:
        return None
    if len(dado) < 200:
        return None
    if tipo and "image" not in tipo.lower() and "octet-stream" not in tipo.lower():
        return None
    try:
        with Image.open(BytesIO(dado)) as img:
            img.verify()
    except Exception:
        return None
    _ram_por(chave, dado)
    _gravar_disco(chave, dado)
    return dado


def _reservar(quantidade: int) -> bool:
    global _REDE
    if quantidade <= 0:
        return True
    with _TRAVA:
        if _PRAZO > 0 and time.perf_counter() >= _PRAZO:
            return False
        if _REDE + quantidade > _REDE_MAX:
            return False
        _REDE += quantidade
        return True


def obter_tiles(chaves: list[tuple[int, int, int]]) -> dict[tuple[int, int, int], bytes] | None:
    """Todos os tiles, ou nenhum. Mapa incompleto não é entregue."""
    if not chaves or not dentro_do_prazo():
        return None
    prontos: dict[tuple[int, int, int], bytes] = {}
    faltam: list[tuple[int, int, int]] = []
    for chave in chaves:
        dado = _ram_ler(chave) or _ler_disco(chave)
        if dado:
            prontos[chave] = dado
        else:
            faltam.append(chave)
    if faltam and not _reservar(len(faltam)):
        return None
    if faltam:
        prazo = time.perf_counter() + _ORCAMENTO_MAPA
        with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
            futuros = {pool.submit(_baixar_rede, chave): chave for chave in faltam}
            try:
                for futuro in as_completed(futuros, timeout=max(0.2, prazo - time.perf_counter())):
                    dado = futuro.result()
                    if not dado:
                        return None
                    prontos[futuros[futuro]] = dado
            except Exception:
                return None
    if len(prontos) != len(chaves):
        return None
    return prontos


def pintar_fundo(vista: dict[str, Any], tiles: dict[tuple[int, int, int], bytes]) -> Image.Image | None:
    largura = int(vista["largura"])
    altura = int(vista["altura"])
    escala = float(vista["escala"])
    lado = max(1, int(math.ceil(_TILE * escala)) + 1)
    img = Image.new("RGB", (largura, altura), (236, 240, 242))
    try:
        for chave in vista["tiles"]:
            blob = tiles.get(chave)
            if not blob:
                return None
            _z, tx, ty = chave
            px = (tx * _TILE - vista["origem_x"]) * escala
            py = (ty * _TILE - vista["origem_y"]) * escala
            with Image.open(BytesIO(blob)) as tile:
                tile = tile.convert("RGB").resize((lado, lado), Image.Resampling.BILINEAR)
                img.paste(tile, (int(round(px)), int(round(py))))
    except Exception:
        return None
    return img


def carimbar(img: Image.Image) -> None:
    """Crédito exigido pelo serviço, sobre uma faixa clara."""
    desenho = ImageDraw.Draw(img)
    try:
        fonte = ImageFont.load_default(size=15)
    except TypeError:
        fonte = ImageFont.load_default()
    y = img.height - _FAIXA + 4
    desenho.rectangle((0, img.height - _FAIXA, img.width, img.height), fill=(255, 255, 255))
    texto = ATRIBUICAO
    meio = texto.find("Esri China")
    if meio > 0:
        linhas = [texto[:meio].rstrip(" ,"), texto[meio:]]
    else:
        linhas = [texto]
    for i, linha in enumerate(linhas[:2]):
        desenho.text((8, y + i * 18), linha, fill=(50, 58, 64), font=fonte)
