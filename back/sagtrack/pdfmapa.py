"""PDFs da frota, da rota e da placa com o mapa oficial e o trajeto do turno."""

from __future__ import annotations

import io
import json
import math
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from .config import ROOT, rotulo_periodo

_FONTE = False
PALETA = (
    (31, 122, 77),
    (194, 65, 12),
    (29, 78, 137),
    (138, 106, 18),
    (91, 33, 182),
    (14, 116, 144),
)


def _fonte() -> str:
    global _FONTE
    if _FONTE:
        return "Arial"
    regular = Path(r"C:\Windows\Fonts\arial.ttf")
    negrito = Path(r"C:\Windows\Fonts\arialbd.ttf")
    if regular.is_file():
        pdfmetrics.registerFont(TTFont("Arial", str(regular)))
        if negrito.is_file():
            pdfmetrics.registerFont(TTFont("Arial-Bold", str(negrito)))
        _FONTE = True
        return "Arial"
    return "Helvetica"


def _ler(pasta: Path, nome: str) -> Any:
    arquivo = pasta / nome
    if not arquivo.is_file():
        return None
    return json.loads(arquivo.read_text(encoding="utf-8"))


def _turno(dia: date) -> str:
    return rotulo_periodo(dia, "06:00", "05:59")


def _conclusao(pct: float, teve: bool) -> str:
    if not teve:
        return "Sem coleta"
    if pct >= 90:
        return "Feita"
    if pct >= 45:
        return "Parcial"
    return "Não feita"


def _cor_situacao(situacao: str) -> tuple[int, int, int]:
    if situacao == "Feita":
        return (31, 122, 77)
    if situacao == "Parcial":
        return (165, 107, 0)
    if situacao == "Não feita":
        return (180, 35, 24)
    return (148, 163, 184)


def _ruas(frota: list[dict[str, Any]], codigo: str) -> tuple[list, list]:
    """Usa a cobertura que mais marcou via feita. O restante fica só nessa mesma cobertura."""
    melhor: tuple[int, float, list, list] | None = None
    for item in frota:
        for cob in item.get("coberturas_inlog") or []:
            if str(cob.get("codigo_rota") or "") != codigo:
                continue
            feitos = [t for t in (cob.get("trechos_feitos") or []) if t and len(t) >= 2]
            faltando = [t for t in (cob.get("trechos_faltando") or []) if t and len(t) >= 2]
            if not feitos and not faltando:
                continue
            nota = (len(feitos), float(cob.get("percentual") or 0), feitos, faltando)
            if melhor is None or nota[0] > melhor[0] or (nota[0] == melhor[0] and nota[1] > melhor[1]):
                melhor = nota
    if melhor is None:
        return [], []
    return melhor[2], melhor[3]


_MALHA: dict[str, list[dict[str, Any]]] = {}


def _malha(dia: date) -> list[dict[str, Any]]:
    chave = dia.isoformat()
    if chave not in _MALHA:
        from .mapa import carregar_setores

        _MALHA[chave] = carregar_setores(dia)
    return _MALHA[chave]


def _linhas_wgs(geom) -> list[list[list[float]]]:
    from .cobertura import _iter_linhas, _wgs

    saida = []
    for linha in _iter_linhas(geom):
        if linha.is_empty or linha.length < 8:
            continue
        coords = _wgs([(float(p[0]), float(p[1])) for p in linha.coords])
        if len(coords) >= 2:
            saida.append(coords)
    return saida


def _vias_no_poligono(dia: date, codigo: str, tracos: list[list[list[float]]]) -> tuple[list, list]:
    """Recorta a via oficial no polígono e separa o que o trajeto cobriu do que ficou em aberto."""
    from shapely.geometry import LineString
    from shapely.ops import unary_union

    from .cobertura import TO_UTM

    setor = next((item for item in _malha(dia) if str(item.get("codigo") or "") == codigo), None)
    if setor is None or setor.get("geom") is None:
        return [], []
    geom = setor["geom"]
    ruas = list(setor.get("ruas") or [])
    if not ruas:
        return [], []
    linhas = []
    for traco in tracos or []:
        xy = []
        for ponto in traco:
            if len(ponto) < 2:
                continue
            x, y = TO_UTM.transform(float(ponto[1]), float(ponto[0]))
            xy.append((x, y))
        if len(xy) >= 2:
            linhas.append(LineString(xy))
    zona = None
    if linhas:
        try:
            zona = unary_union(linhas).simplify(12).buffer(45)
        except Exception:
            zona = None
    feitos: list = []
    faltando: list = []
    for rua in ruas:
        try:
            dentro = rua.intersection(geom)
        except Exception:
            continue
        if dentro.is_empty:
            continue
        if zona is None or zona.is_empty:
            faltando.extend(_linhas_wgs(dentro))
            continue
        try:
            feito = dentro.intersection(zona)
            falta = dentro.difference(zona)
        except Exception:
            faltando.extend(_linhas_wgs(dentro))
            continue
        feitos.extend(_linhas_wgs(feito))
        faltando.extend(_linhas_wgs(falta))
    return feitos, faltando


def _aneis(item: dict[str, Any]) -> list[list]:
    aneis = [anel for anel in (item.get("poligonos") or []) if anel and len(anel) >= 4]
    if aneis:
        return aneis
    poligono = item.get("poligono") or []
    return [poligono] if len(poligono) >= 4 else []


def _tile_xy(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    n = 2**zoom
    x = (lon + 180.0) / 360.0 * n
    seno = math.sin(math.radians(max(min(lat, 85.0), -85.0)))
    y = (1.0 - math.log((1.0 + seno) / (1.0 - seno)) / (2.0 * math.pi)) / 2.0 * n
    return x, y


def _baixar_tile(zoom: int, x: int, y: int) -> Image.Image | None:
    bruto = os.getenv("OUTPUT_DIR", "").strip()
    pasta = (Path(bruto) if bruto else ROOT / "output") / "tiles" / str(zoom)
    pasta.mkdir(parents=True, exist_ok=True)
    arquivo = pasta / f"{x}_{y}.jpg"
    if not arquivo.is_file():
        return None
    try:
        return Image.open(arquivo).convert("RGB")
    except Exception:
        return None


def _limites(grupos: list[list[list[float]]]) -> tuple[float, float, float, float] | None:
    lats: list[float] = []
    lons: list[float] = []
    for grupo in grupos:
        for ponto in grupo:
            if len(ponto) < 2:
                continue
            lats.append(float(ponto[0]))
            lons.append(float(ponto[1]))
    if not lats:
        return None
    return min(lats), max(lats), min(lons), max(lons)


def _fonte_imagem(tamanho: int):
    from PIL import ImageFont

    caminho = Path(r"C:\Windows\Fonts\arialbd.ttf")
    if caminho.is_file():
        return ImageFont.truetype(str(caminho), tamanho)
    return ImageFont.load_default()


def renderizar_mapa(
    poligonos: list[dict[str, Any]],
    tracos: list[list[list[float]]],
    pontos: list[dict[str, Any]],
    enquadramento: list[list[list[float]]] | None = None,
    *,
    vias: str = "ambas",
    cor_traco: tuple[int, int, int] = (29, 78, 137),
) -> bytes:
    moldura = enquadramento or [anel for item in poligonos for anel in item.get("aneis") or []] or tracos
    caixa = _limites(moldura)
    if caixa is None:
        caixa = (-16.98, -16.68, -49.42, -49.05)
    sul, norte, oeste, leste = caixa
    folga_lat = max((norte - sul) * 0.08, 0.002)
    folga_lon = max((leste - oeste) * 0.08, 0.002)
    sul -= folga_lat
    norte += folga_lat
    oeste -= folga_lon
    leste += folga_lon
    cos = max(0.2, math.cos(math.radians((sul + norte) / 2)))
    span_lat = max(norte - sul, 0.002)
    span_lon = max(leste - oeste, 0.002)
    aspecto = (span_lon * cos) / span_lat
    if aspecto < 1.55:
        extra = ((1.55 * span_lat / cos) - span_lon) / 2
        oeste -= extra
        leste += extra
    elif aspecto > 1.7:
        extra = ((span_lon * cos / 1.55) - span_lat) / 2
        sul -= extra
        norte += extra
    zoom = 15
    for candidato in range(17, 14, -1):
        x0, y1 = _tile_xy(sul, oeste, candidato)
        x1, y0 = _tile_xy(norte, leste, candidato)
        if math.floor(x1) - math.floor(x0) <= 8 and math.floor(y1) - math.floor(y0) <= 6:
            zoom = candidato
            break
    x0, y1 = _tile_xy(sul, oeste, zoom)
    x1, y0 = _tile_xy(norte, leste, zoom)
    tx0, tx1 = math.floor(min(x0, x1)), math.floor(max(x0, x1))
    ty0, ty1 = math.floor(min(y0, y1)), math.floor(max(y0, y1))

    def limitar(inicio: int, fim: int, limite: int) -> tuple[int, int]:
        if fim - inicio + 1 <= limite:
            return inicio, fim
        meio = (inicio + fim) // 2
        comeco = max(0, meio - limite // 2)
        return comeco, comeco + limite - 1

    tx0, tx1 = limitar(tx0, tx1, 8)
    ty0, ty1 = limitar(ty0, ty1, 6)
    cols, rows = tx1 - tx0 + 1, ty1 - ty0 + 1
    base = Image.new("RGB", (cols * 256, rows * 256), (244, 241, 234))
    coords = [
        (ix, iy, tile_x, tile_y)
        for ix, tile_x in enumerate(range(tx0, tx1 + 1))
        for iy, tile_y in enumerate(range(ty0, ty1 + 1))
    ]

    def baixar(item: tuple[int, int, int, int]) -> tuple[int, int, Image.Image | None]:
        ix, iy, tile_x, tile_y = item
        return ix, iy, _baixar_tile(zoom, tile_x, tile_y)

    with ThreadPoolExecutor(max_workers=8) as pool:
        for ix, iy, tile in pool.map(baixar, coords):
            if tile is not None:
                base.paste(tile, (ix * 256, iy * 256))
    imagem = base.convert("RGBA")
    escala = max(1.0, imagem.width / 1700)

    def px(lat: float, lon: float) -> tuple[int, int]:
        xf, yf = _tile_xy(lat, lon, zoom)
        return int((xf - tx0) * 256), int((yf - ty0) * 256)

    aneis_px: list[list[tuple[int, int]]] = []
    for item in poligonos:
        for anel in item.get("aneis") or []:
            coords = [px(float(p[0]), float(p[1])) for p in anel if len(p) >= 2]
            if len(coords) >= 4:
                aneis_px.append(coords)
    if aneis_px:
        fora = Image.new("L", imagem.size, 70)
        desenho_fora = ImageDraw.Draw(fora)
        for coords in aneis_px:
            desenho_fora.polygon(coords, fill=0)
        velo = Image.new("RGBA", imagem.size, (246, 242, 234, 255))
        velo.putalpha(fora)
        imagem.alpha_composite(velo)
    desenho = ImageDraw.Draw(imagem, "RGBA")
    via_px = max(2, int(2.2 * escala))
    for item in poligonos:
        grupos = []
        if vias in {"ambas", "abertas"}:
            grupos.append((item.get("faltando") or [], (176, 28, 18)))
        if vias in {"ambas", "feitas"}:
            grupos.append((item.get("feitos") or [], (15, 122, 68)))
        for grupo, cor_via in grupos:
            for trilha in grupo:
                coords = [px(float(p[0]), float(p[1])) for p in trilha if len(p) >= 2]
                if len(coords) < 2:
                    continue
                desenho.line(coords, fill=(255, 255, 255, 220), width=via_px + 2, joint="curve")
                desenho.line(coords, fill=(*cor_via, 255), width=via_px, joint="curve")
        rotulo = str(item.get("rotulo") or "")
        aneis = item.get("aneis") or []
        if rotulo and aneis:
            lats = [float(p[0]) for anel in aneis for p in anel if len(p) >= 2]
            lons = [float(p[1]) for anel in aneis for p in anel if len(p) >= 2]
            if lats and lons:
                canto = px(max(lats), min(lons))
                fonte = _fonte_imagem(max(22, int(18 * escala)))
                caixa = desenho.textbbox(canto, rotulo, font=fonte)
                desenho.rectangle(
                    (caixa[0] - 4, caixa[1] - 2, caixa[2] + 4, caixa[3] + 2),
                    fill=(255, 253, 248, 230),
                )
                desenho.text(canto, rotulo, font=fonte, fill=(28, 36, 48, 255))
    traco_px = max(2, int(1.8 * escala))
    for traco in tracos:
        coords = [px(float(p[0]), float(p[1])) for p in traco if len(p) >= 2]
        if len(coords) < 2:
            continue
        desenho.line(coords, fill=(255, 255, 255, 230), width=traco_px + 2, joint="curve")
        desenho.line(coords, fill=(*cor_traco, 255), width=traco_px, joint="curve")
    for coords in aneis_px:
        desenho.line(coords + [coords[0]], fill=(90, 68, 12, 255), width=max(2, int(1.6 * escala)))
    raio = max(4, int(3.2 * escala))
    for ponto in pontos:
        try:
            centro = px(float(ponto["latitude"]), float(ponto["longitude"]))
        except (KeyError, TypeError, ValueError):
            continue
        cor = ponto.get("cor") or (194, 65, 12)
        desenho.ellipse(
            (centro[0] - raio, centro[1] - raio, centro[0] + raio, centro[1] + raio),
            fill=(*cor, 255),
            outline=(255, 255, 255, 255),
            width=2,
        )
    canto_no = px(norte, oeste)
    canto_sl = px(sul, leste)
    esquerda, topo = max(0, min(canto_no[0], canto_sl[0])), max(0, min(canto_no[1], canto_sl[1]))
    direita, baixo = min(imagem.width, max(canto_no[0], canto_sl[0])), min(imagem.height, max(canto_no[1], canto_sl[1]))
    if direita - esquerda > 80 and baixo - topo > 80:
        imagem = imagem.crop((esquerda, topo, direita, baixo))
    imagem = imagem.convert("RGB")
    if imagem.width > 1400:
        altura = int(imagem.height * (1400 / imagem.width))
        imagem = imagem.resize((1400, max(1, altura)), Image.Resampling.LANCZOS)
    saida = io.BytesIO()
    imagem.save(saida, format="JPEG", quality=72, optimize=True)
    return saida.getvalue()


def _pdf(
    titulo: str,
    linhas: list[str],
    tabela: list[list[str]],
    png: bytes,
    folhas: list[tuple[str, bytes, str]] | None = None,
) -> bytes:
    fonte = _fonte()
    negrito = "Arial-Bold" if fonte == "Arial" else "Helvetica-Bold"
    buffer = io.BytesIO()
    pagina = landscape(A4)
    pdf = canvas.Canvas(buffer, pagesize=pagina)
    largura, altura = pagina

    def cabecalho() -> float:
        pdf.setFillColorRGB(0.11, 0.14, 0.19)
        pdf.rect(0, altura - 36, largura, 36, fill=1, stroke=0)
        pdf.setFillColorRGB(1, 1, 1)
        pdf.setFont(negrito, 14)
        pdf.drawString(16, altura - 23, titulo[:120])
        return altura - 44

    if folhas is None and png:
        folhas = [
            (
                titulo,
                png,
                "Verde: via feita. Vermelho: via em aberto. Azul: trajeto. Ponto: parada dentro do polígono.",
            )
        ]
    if folhas:
        for titulo_folha, blob, legenda in folhas:
            pdf.setFillColorRGB(1, 1, 1)
            pdf.rect(0, 0, largura, altura, fill=1, stroke=0)
            pdf.setFillColorRGB(0.11, 0.14, 0.19)
            pdf.rect(0, altura - 36, largura, 36, fill=1, stroke=0)
            pdf.setFillColorRGB(1, 1, 1)
            pdf.setFont(negrito, 14)
            pdf.drawString(16, altura - 23, titulo_folha[:120])
            imagem = ImageReader(io.BytesIO(blob))
            pdf.drawImage(
                imagem,
                8,
                22,
                width=largura - 16,
                height=altura - 64,
                preserveAspectRatio=True,
                anchor="c",
                mask="auto",
            )
            pdf.setFillColorRGB(0.11, 0.14, 0.19)
            pdf.setFont(fonte, 8)
            pdf.drawString(16, 8, legenda[:170])
            pdf.showPage()
    else:
        y = cabecalho()
        pdf.setFillColorRGB(0.11, 0.14, 0.19)
        pdf.setFont(fonte, 11)
        for linha in linhas:
            pdf.drawString(28, y, linha[:170])
            y -= 16

    if tabela:
        y = cabecalho()
        pdf.setFillColorRGB(0.11, 0.14, 0.19)
        pdf.setFont(fonte, 9)
        for linha in linhas[:7]:
            pdf.drawString(16, y, linha[:165])
            y -= 12
        y -= 6
        colunas = [78, 210, 90, 70, largura - 56 - 448]
        pdf.setFont(negrito, 9)
        x = 28
        for texto, coluna in zip(tabela[0], colunas):
            pdf.drawString(x, y, texto)
            x += coluna
        y -= 16
        pdf.setFont(fonte, 8)
        for linha in tabela[1:]:
            if y < 36:
                pdf.showPage()
                y = cabecalho()
                pdf.setFillColorRGB(0.11, 0.14, 0.19)
                pdf.setFont(fonte, 8)
            x = 28
            for texto, coluna in zip(linha, colunas):
                pdf.drawString(x, y, str(texto)[: int(coluna / 4.4)])
                x += coluna
            y -= 13
    pdf.save()
    return buffer.getvalue()


def _sem_dia(dia: date) -> bytes:
    return _pdf(
        "Segtrack · sem dados do turno",
        [f"Não há pulsos salvos para {_turno(dia)}.", "Atualize a frota no painel e peça o relatório de novo."],
        [],
        b"",
    )


def _preparar_vias(
    dia: date,
    codigo: str,
    tracos: list[list[list[float]]],
    frota: list[dict[str, Any]],
) -> tuple[list, list]:
    feitos, faltando = _ruas(frota, codigo)
    if feitos or faltando:
        return feitos, faltando
    if not tracos:
        return [], []
    return _vias_no_poligono(dia, codigo, tracos)


def _mapa_da_rota(
    titulo: str,
    codigo: str,
    aneis: list,
    tracos: list,
    pontos: list,
    dia: date,
    frota: list[dict[str, Any]],
) -> tuple[str, bytes, str]:
    feitos, _faltando = _preparar_vias(dia, codigo, tracos, frota)
    base = {"aneis": aneis, "rotulo": codigo, "feitos": feitos, "faltando": []}
    if feitos or tracos or pontos:
        legenda = "Somente o executado. Verde: via feita. Azul: trajeto do caminhão. Ponto: parada."
    else:
        legenda = "Programada para o dia. Nenhuma via foi executada neste turno."
    return (
        titulo,
        renderizar_mapa([base], tracos, pontos, aneis, vias="feitas"),
        legenda,
    )


def _folhas_da_rota(
    titulo: str,
    codigo: str,
    aneis: list,
    tracos: list,
    pontos: list,
    dia: date,
    frota: list[dict[str, Any]],
) -> list[tuple[str, bytes, str]]:
    feitos, faltando = _preparar_vias(dia, codigo, tracos, frota)
    base = {"aneis": aneis, "rotulo": codigo, "feitos": feitos, "faltando": faltando}
    if faltando:
        legenda_aberta = "Em aberto. A linha vermelha é a via oficial que ficou sem coleta."
    else:
        legenda_aberta = "Em aberto. Nenhuma via oficial ficou sem coleta nesta rota."
    return [
        (
            f"{titulo} · realizado",
            renderizar_mapa([base], tracos, pontos, aneis, vias="feitas"),
            "Realizado. A linha verde é a via feita. Azul: por onde o caminhão passou. Ponto: parada.",
        ),
        (
            f"{titulo} · em aberto",
            renderizar_mapa([base], [], [], aneis, vias="abertas"),
            legenda_aberta,
        ),
    ]


def _falta_pct(pct: float) -> int:
    return max(0, round(100 - float(pct or 0)))


def _rotulo_andamento(row: dict[str, Any]) -> str:
    pct = float(row.get("percentual") or 0)
    falta = _falta_pct(pct)
    if int(row.get("paradas") or 0) <= 0:
        return "Não executada"
    if row.get("situacao") == "Feita":
        return "Feita"
    if row.get("situacao") == "Parcial":
        return f"Parcial · falta {falta}%"
    if pct > 0:
        return f"Não feita · falta {falta}%"
    return "Não feita"


def _barra_execucao(pdf: canvas.Canvas, x: float, y: float, largura: float, altura: float, pct: float) -> None:
    pct = max(0.0, min(100.0, float(pct or 0)))
    pdf.setFillColorRGB(0.78, 0.22, 0.16)
    pdf.rect(x, y, largura, altura, fill=1, stroke=0)
    feito = largura * (pct / 100.0)
    if feito > 0.4:
        pdf.setFillColorRGB(*_COR_SITUACAO["Feita"])
        pdf.rect(x, y, feito, altura, fill=1, stroke=0)


_COR_SITUACAO = {
    "Feita": (0.12, 0.45, 0.28),
    "Parcial": (0.62, 0.40, 0.02),
    "Não feita": (0.68, 0.16, 0.10),
}


def _quebrar(texto: str, fonte: str, tamanho: float, largura: float) -> list[str]:
    palavras = (texto or "").split()
    if not palavras:
        return []
    linhas: list[str] = []
    atual = ""
    for palavra in palavras:
        candidato = f"{atual} {palavra}".strip()
        if pdfmetrics.stringWidth(candidato, fonte, tamanho) <= largura:
            atual = candidato
        else:
            if atual:
                linhas.append(atual)
            atual = palavra
    if atual:
        linhas.append(atual)
    return linhas


def _pdf_frota_fiscal(
    dia: date,
    periodo: str,
    balanco: list[dict[str, Any]],
    mapas: list[tuple[str, bytes, str, tuple[float, float, float]]],
) -> bytes:
    from .relatorios import _faixa_agenda, _fmt_pct

    fonte = _fonte()
    negrito = "Arial-Bold" if fonte == "Arial" else "Helvetica-Bold"
    buffer = io.BytesIO()
    pagina = landscape(A4)
    pdf = canvas.Canvas(buffer, pagesize=pagina)
    largura, altura = pagina

    def cabecalho(titulo: str, cor: tuple[float, float, float] = (0.11, 0.14, 0.19)) -> float:
        pdf.setFillColorRGB(*cor)
        pdf.rect(0, altura - 36, largura, 36, fill=1, stroke=0)
        pdf.setFillColorRGB(1, 1, 1)
        pdf.setFont(negrito, 13)
        pdf.drawString(16, altura - 23, titulo[:110])
        return altura - 52

    def nova(titulo: str, cor: tuple[float, float, float] = (0.11, 0.14, 0.19)) -> float:
        pdf.showPage()
        return cabecalho(titulo, cor)

    feitas = [r for r in balanco if r["situacao"] == "Feita"]
    parciais = [r for r in balanco if r["situacao"] == "Parcial"]
    nao = [r for r in balanco if r["situacao"] == "Não feita"]
    y = cabecalho(f"Segtrack · decisão da frota · {dia.strftime('%d/%m/%Y')}")
    pdf.setFillColorRGB(0.11, 0.14, 0.19)
    pdf.setFont(fonte, 10)
    abertura = (
        f"Período {periodo}. A barra verde é o executado e a vermelha é o que faltou. "
        "Feita só a partir de 90%. De 45% a 89% é parcial."
    )
    for linha in _quebrar(abertura, fonte, 10, largura - 32):
        pdf.drawString(16, y, linha)
        y -= 13
    y -= 10
    cartoes = [
        (str(len(balanco)), "programadas", (0.16, 0.20, 0.27)),
        (str(len(feitas)), "feitas", _COR_SITUACAO["Feita"]),
        (str(len(parciais)), "parciais", _COR_SITUACAO["Parcial"]),
        (str(len(nao)), "não feitas", _COR_SITUACAO["Não feita"]),
    ]
    card_w = 150
    for indice, (numero, rotulo, cor) in enumerate(cartoes):
        x = 16 + indice * (card_w + 12)
        pdf.setFillColorRGB(*cor)
        pdf.roundRect(x, y - 54, card_w, 54, 6, fill=1, stroke=0)
        pdf.setFillColorRGB(1, 1, 1)
        pdf.setFont(negrito, 20)
        pdf.drawString(x + 12, y - 30, numero)
        pdf.setFont(fonte, 9)
        pdf.drawString(x + 12, y - 46, rotulo)
    y -= 74
    pdf.setFillColorRGB(0.11, 0.14, 0.19)
    pdf.setFont(negrito, 11)
    pdf.drawString(16, y, "O que foi feito")
    y -= 16
    pdf.setFont(fonte, 9)
    prontos = feitas + parciais
    if not prontos:
        pdf.drawString(16, y, "Nenhuma rota programada foi feita neste dia.")
        y -= 14
    for row in prontos:
        if y < 48:
            y = nova(f"Segtrack · decisão da frota · {dia.strftime('%d/%m/%Y')}")
            pdf.setFillColorRGB(0.11, 0.14, 0.19)
            pdf.setFont(fonte, 9)
        quem = ", ".join(row.get("caminhoes") or []) or "—"
        texto = (
            f"{_rotulo_andamento(row)} · {row['codigo']} {row['nome']} · {_faixa_agenda(row)} · "
            f"{_fmt_pct(row['percentual'])} · {row['paradas']} paradas · {quem}"
        )
        for linha in _quebrar(texto, fonte, 9, largura - 32):
            pdf.setFillColorRGB(*_COR_SITUACAO[row["situacao"]])
            pdf.drawString(16, y, linha)
            y -= 12
        y -= 2
    y -= 8
    if y < 80:
        y = nova(f"Segtrack · decisão da frota · {dia.strftime('%d/%m/%Y')}")
    pdf.setFillColorRGB(0.11, 0.14, 0.19)
    pdf.setFont(negrito, 11)
    pdf.drawString(16, y, "O que não foi feito")
    y -= 16
    for faixa in ("Diurno", "Noturno", "Noturno diário", "Sem turno"):
        grupo = [r for r in nao if _faixa_agenda(r) == faixa]
        if not grupo:
            continue
        fraco = [r for r in grupo if r["detalhe"] == "parou na rota, mas cobriu pouco"]
        vazio = [r for r in grupo if r["detalhe"] == "nenhum caminhão coletou"]
        blocos = []
        if fraco:
            blocos.append(
                "Parou e não concluiu: "
                + "; ".join(f"{r['codigo']} {_fmt_pct(r['percentual'])} ({r['paradas']} paradas)" for r in fraco)
            )
        if vazio:
            blocos.append("Sem coleta: " + ", ".join(r["codigo"] for r in vazio) + ".")
        pdf.setFont(negrito, 9)
        if y < 48:
            y = nova(f"Segtrack · o que não foi feito · {dia.strftime('%d/%m/%Y')}")
        pdf.setFillColorRGB(*_COR_SITUACAO["Não feita"])
        pdf.drawString(16, y, f"{faixa} · {len(grupo)} não feitas")
        y -= 13
        pdf.setFont(fonte, 8)
        pdf.setFillColorRGB(0.11, 0.14, 0.19)
        for bloco in blocos:
            for linha in _quebrar(bloco, fonte, 8, largura - 32):
                if y < 36:
                    y = nova(f"Segtrack · o que não foi feito · {dia.strftime('%d/%m/%Y')}")
                    pdf.setFillColorRGB(0.11, 0.14, 0.19)
                    pdf.setFont(fonte, 8)
                pdf.drawString(16, y, linha)
                y -= 11
        y -= 6

    colunas = [64, 150, 128, 124, 52, largura - 32 - 518]
    cab = ["Rota", "Setor", "Situação", "Executado", "Paradas", "Caminhão"]

    def cabeca_faixa(titulo_pagina: str) -> float:
        yy = nova(titulo_pagina)
        pdf.setFillColorRGB(0.35, 0.40, 0.46)
        pdf.setFont(fonte, 8)
        pdf.drawString(
            16,
            yy,
            "Verde: executado. Vermelho: o que faltou. Feita a partir de 90%. De 45% a 89% é parcial.",
        )
        yy -= 14
        pdf.setFillColorRGB(0.11, 0.14, 0.19)
        pdf.setFont(negrito, 8)
        xx = 16
        for texto, coluna in zip(cab, colunas):
            pdf.drawString(xx, yy, texto)
            xx += coluna
        return yy - 16

    for faixa in ("Diurno", "Noturno", "Noturno diário", "Sem turno"):
        grupo = [r for r in balanco if _faixa_agenda(r) == faixa]
        if not grupo:
            continue
        y = cabeca_faixa(f"Segtrack · {faixa} · {dia.strftime('%d/%m/%Y')}")
        for row in grupo:
            if y < 36:
                y = cabeca_faixa(f"Segtrack · {faixa} · {dia.strftime('%d/%m/%Y')}")
            cor = _COR_SITUACAO[row["situacao"]]
            rotulo = _rotulo_andamento(row)
            if row["situacao"] == "Não feita":
                pdf.setFillColorRGB(0.98, 0.94, 0.93)
                pdf.rect(12, y - 4, largura - 24, 16, fill=1, stroke=0)
            valores = [
                row["codigo"],
                str(row["nome"] or ""),
                rotulo,
                None,
                str(row["paradas"] or 0) if row["paradas"] else "—",
                ", ".join(row.get("caminhoes") or []) or "—",
            ]
            x = 16
            for indice, (texto, coluna) in enumerate(zip(valores, colunas)):
                if indice == 3:
                    _barra_execucao(pdf, x, y - 1, 74, 8, float(row["percentual"] or 0))
                    pdf.setFillColorRGB(0.11, 0.14, 0.19)
                    pdf.setFont(fonte, 8)
                    pdf.drawString(x + 78, y, _fmt_pct(row["percentual"]))
                else:
                    pdf.setFillColorRGB(*cor if indice == 2 else (0.11, 0.14, 0.19))
                    pdf.setFont(negrito if indice == 2 else fonte, 8)
                    pdf.drawString(x, y, str(texto)[: int(coluna / 4.2)])
                x += coluna
            y -= 18

    for titulo, blob, legenda, cor in mapas:
        pdf.showPage()
        pdf.setFillColorRGB(*cor)
        pdf.rect(0, altura - 36, largura, 36, fill=1, stroke=0)
        pdf.setFillColorRGB(1, 1, 1)
        pdf.setFont(negrito, 12)
        pdf.drawString(16, altura - 23, titulo[:120])
        imagem = ImageReader(io.BytesIO(blob))
        pdf.drawImage(
            imagem,
            8,
            22,
            width=largura - 16,
            height=altura - 64,
            preserveAspectRatio=True,
            anchor="c",
            mask="auto",
        )
        pdf.setFillColorRGB(0.11, 0.14, 0.19)
        pdf.setFont(fonte, 8)
        pdf.drawString(16, 8, legenda[:170])
    pdf.save()
    return buffer.getvalue()


def pdf_frota(output_dir: Path, dia: date) -> tuple[bytes, str]:
    from .relatorios import _faixa_agenda, _fmt_pct, balanco_frota

    pasta = output_dir / dia.isoformat()
    resumo = _ler(pasta, "resumo.json")
    setores = _ler(pasta, "setores.json") or []
    frota = _ler(pasta, "frota.json") or []
    if not resumo:
        return _sem_dia(dia), f"frota-{dia.isoformat()}.pdf"
    balanco = balanco_frota(setores, frota)
    gps_por_placa = {
        str(caminhao.get("placa") or ""): caminhao.get("gps") or []
        for caminhao in (resumo.get("caminhoes") or [])
    }
    mapas: list[tuple[str, bytes, str, tuple[float, float, float]]] = []
    for row in balanco:
        placas = set(row.get("placas") or [])
        tracos = [traco for placa in placas for traco in (gps_por_placa.get(placa) or [])]
        estado = _rotulo_andamento(row)
        titulo = (
            f"{row['codigo']} {row['nome']} · {estado} · {_fmt_pct(row['percentual'])} · {_faixa_agenda(row)}"
        )
        cor = _COR_SITUACAO[row["situacao"]]
        folha_titulo, png, legenda = _mapa_da_rota(
            titulo,
            row["codigo"],
            _aneis(row),
            tracos,
            row.get("pontos") or [],
            dia,
            frota,
        )
        mapas.append((folha_titulo, png, legenda, cor))
    periodo = resumo.get("periodo") or _turno(dia)
    return _pdf_frota_fiscal(dia, periodo, balanco, mapas), f"frota-{dia.isoformat()}.pdf"


def _cor_da_conclusao(situacao: str) -> tuple[float, float, float]:
    blob = (situacao or "").upper()
    if "PARCIAL" in blob:
        return _COR_SITUACAO["Parcial"]
    if "FEITA" in blob and "NÃO" not in blob and "NAO" not in blob:
        return _COR_SITUACAO["Feita"]
    return _COR_SITUACAO["Não feita"]


def _pdf_leitura(
    titulo: str,
    linhas: list[str],
    tabela: list[list[str]],
    mapas: list[tuple[str, bytes, str, tuple[float, float, float]]],
    cor: tuple[float, float, float],
) -> bytes:
    """Decisão primeiro, mapas depois, com a mesma cor da conclusão."""
    fonte = _fonte()
    negrito = "Arial-Bold" if fonte == "Arial" else "Helvetica-Bold"
    buffer = io.BytesIO()
    pagina = landscape(A4)
    pdf = canvas.Canvas(buffer, pagesize=pagina)
    largura, altura = pagina

    def cabecalho(texto: str, fundo: tuple[float, float, float]) -> float:
        pdf.setFillColorRGB(*fundo)
        pdf.rect(0, altura - 36, largura, 36, fill=1, stroke=0)
        pdf.setFillColorRGB(1, 1, 1)
        pdf.setFont(negrito, 13)
        pdf.drawString(16, altura - 23, texto[:120])
        return altura - 52

    y = cabecalho(titulo, cor)
    pdf.setFillColorRGB(0.11, 0.14, 0.19)
    pdf.setFont(fonte, 10)
    for linha in linhas:
        for trecho in _quebrar(linha, fonte, 10, largura - 32) or [""]:
            if y < 36:
                pdf.showPage()
                y = cabecalho(titulo, cor)
                pdf.setFillColorRGB(0.11, 0.14, 0.19)
                pdf.setFont(fonte, 10)
            pdf.drawString(16, y, trecho)
            y -= 14
    if tabela:
        y -= 8
        if y < 70:
            pdf.showPage()
            y = cabecalho(titulo, cor)
        n = len(tabela[0])
        colunas = [largura - 32] if n <= 1 else None
        if n == 5:
            colunas = [240, 110, 80, 90, largura - 32 - 520]
        elif n == 6:
            colunas = [250, 90, 80, 70, 80, largura - 32 - 570]
        else:
            fatia = (largura - 32) / max(n, 1)
            colunas = [fatia] * n
        pdf.setFont(negrito, 9)
        pdf.setFillColorRGB(0.11, 0.14, 0.19)
        x = 16
        for texto, coluna in zip(tabela[0], colunas):
            pdf.drawString(x, y, texto)
            x += coluna
        y -= 16
        pdf.setFont(fonte, 9)
        for row in tabela[1:]:
            if y < 32:
                pdf.showPage()
                y = cabecalho(titulo, cor)
                pdf.setFillColorRGB(0.11, 0.14, 0.19)
                pdf.setFont(fonte, 9)
            situacao = next((str(cel) for cel in row if str(cel) in {"Feita", "Parcial", "Não feita", "Atuou", "Apoio", "Passagem"}), "")
            if situacao in {"Não feita", "Passagem"}:
                pdf.setFillColorRGB(0.98, 0.94, 0.93)
                pdf.rect(12, y - 3, largura - 24, 14, fill=1, stroke=0)
            x = 16
            for texto, coluna in zip(row, colunas):
                valor = str(texto)
                cor_cel = _cor_da_conclusao(valor) if valor in {"Feita", "Parcial", "Não feita"} else (0.11, 0.14, 0.19)
                pdf.setFillColorRGB(*cor_cel)
                pdf.setFont(negrito if valor in {"Feita", "Parcial", "Não feita"} else fonte, 9)
                pdf.drawString(x, y, valor[: int(coluna / 4.6)])
                x += coluna
            y -= 14
    for titulo_mapa, blob, legenda, cor_mapa in mapas:
        pdf.showPage()
        pdf.setFillColorRGB(*cor_mapa)
        pdf.rect(0, altura - 36, largura, 36, fill=1, stroke=0)
        pdf.setFillColorRGB(1, 1, 1)
        pdf.setFont(negrito, 12)
        pdf.drawString(16, altura - 23, titulo_mapa[:120])
        imagem = ImageReader(io.BytesIO(blob))
        pdf.drawImage(
            imagem,
            8,
            22,
            width=largura - 16,
            height=altura - 64,
            preserveAspectRatio=True,
            anchor="c",
            mask="auto",
        )
        pdf.setFillColorRGB(0.11, 0.14, 0.19)
        pdf.setFont(fonte, 8)
        pdf.drawString(16, 8, legenda[:170])
    pdf.save()
    return buffer.getvalue()


def _papel_legivel(papel: str) -> str:
    return {"ATUANDO": "Atuou", "APOIO": "Apoio", "PERMANENCIA": "Permanência", "PASSAGEM": "Passagem"}.get(papel, papel or "Passagem")


def pdf_rota(output_dir: Path, dia: date, consulta: str) -> tuple[bytes, str]:
    from .relatorios import _fmt_pct, _nome_conclusao, montar

    rel = montar(output_dir, dia, "rota", consulta)
    if not rel.get("setores"):
        return _pdf("Segtrack · rota", rel.get("texto") or ["Rota não encontrada neste turno."], [], b""), "rota.pdf"
    setor = rel["setores"][0]
    codigo = str(setor.get("codigo") or consulta)
    conclusao = _nome_conclusao(str(rel.get("conclusao") or ""))
    cor = _cor_da_conclusao(conclusao)
    pontos = []
    tracos = []
    for caminhao in rel.get("caminhoes") or []:
        if caminhao.get("papel") in {"ATUANDO", "APOIO"}:
            tracos.extend(caminhao.get("gps") or [])
            pontos.extend(caminhao.get("paradas_pontos") or [])
    tabela = [["Caminhão", "Papel", "Paradas", "Minutos", "%"]]
    for caminhao in rel.get("caminhoes") or []:
        tabela.append(
            [
                " · ".join(
                    parte
                    for parte in (str(caminhao.get("prefixo") or ""), str(caminhao.get("placa") or ""))
                    if parte
                ),
                _papel_legivel(str(caminhao.get("papel") or "")),
                str(caminhao.get("paradas") or 0),
                str(caminhao.get("minutos_parado") or 0),
                _fmt_pct(caminhao.get("percentual")) if caminhao.get("papel") in {"ATUANDO", "APOIO"} else "—",
            ]
        )
    if len(tabela) == 1:
        tabela.append(["—", "Passagem", "0", "0", "0%"])
    aneis = _aneis(setor)
    titulo = f"{codigo} {setor.get('nome') or ''} · {conclusao} · {_fmt_pct(rel.get('percentual'))}"
    nome = f"rota-{codigo}-{dia.isoformat()}.pdf"
    mapas = [
        (folha_titulo, png, legenda, cor)
        for folha_titulo, png, legenda in _folhas_da_rota(
            titulo,
            codigo,
            aneis,
            tracos,
            pontos,
            dia,
            _ler(output_dir / dia.isoformat(), "frota.json") or [],
        )
    ]
    return _pdf_leitura(f"Segtrack · rota · {titulo}", list(rel.get("texto") or []), tabela, mapas, cor), nome


def pdf_placa(output_dir: Path, dia: date, consulta: str) -> tuple[bytes, str]:
    from .relatorios import _conclusao, _fmt_pct, _nome_conclusao, montar

    rel = montar(output_dir, dia, "placa", consulta)
    caminhoes = rel.get("caminhoes") or []
    if not caminhoes:
        return _pdf("Segtrack · placa", list(rel.get("texto") or []), [], b""), "placa.pdf"
    caminhao = caminhoes[0]
    frota = _ler(output_dir / dia.isoformat(), "frota.json") or []
    tracos = caminhao.get("gps") or []
    titulo = f"Segtrack · {rel.get('titulo') or caminhao.get('placa') or consulta}"
    tabela = [["Rota", "Situação", "Papel", "Paradas", "Minutos", "%"]]
    mapas: list[tuple[str, bytes, str, tuple[float, float, float]]] = []
    for linha in caminhao.get("tocados") or []:
        if linha.get("papel") not in {"ATUANDO", "APOIO"}:
            continue
        pct = float(linha.get("percentual") or 0)
        conclusao = _nome_conclusao(_conclusao(pct, True))
        cor = _cor_da_conclusao(conclusao)
        tabela.append(
            [
                str(linha.get("setor") or linha.get("codigo") or ""),
                conclusao,
                _papel_legivel(str(linha.get("papel") or "")),
                str(linha.get("paradas") or 0),
                str(linha.get("minutos_parado") or 0),
                _fmt_pct(pct),
            ]
        )
        codigo = str(linha.get("codigo") or "")
        nome_setor = f"{codigo} {conclusao} · {_fmt_pct(pct)}"
        for folha_titulo, png, legenda in _folhas_da_rota(
            nome_setor,
            codigo,
            _aneis(linha),
            tracos,
            linha.get("paradas_pontos") or [],
            dia,
            frota,
        ):
            mapas.append((folha_titulo, png, legenda, cor))
    if len(tabela) == 1:
        tabela.append(["—", "Não feita", "—", "0", "0", "0%"])
    placa = str(caminhao.get("placa") or consulta)
    cor_capa = _cor_da_conclusao(tabela[1][1] if len(tabela) > 1 else "Não feita")
    return _pdf_leitura(titulo, list(rel.get("texto") or []), tabela, mapas, cor_capa), f"placa-{placa}-{dia.isoformat()}.pdf"
