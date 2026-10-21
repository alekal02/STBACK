"""O mapa do relatório mostra a trajetória feita e não pinta a área oficial."""

from io import BytesIO

from PIL import Image

from inlog_agent.live_pdf import _linhas_feitas, _mapa


ANEL = [
    (-49.25, -16.70),
    (-49.24, -16.70),
    (-49.24, -16.69),
    (-49.25, -16.69),
    (-49.25, -16.70),
]
FEITA = [(-49.248, -16.698), (-49.242, -16.692)]


def test_linhas_feitas_le_lon_lat():
    colecao = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": [[-49.24, -16.69], [-49.23, -16.68]]},
            }
        ],
    }
    assert _linhas_feitas(colecao) == [[(-49.24, -16.69), (-49.23, -16.68)]]
    assert _linhas_feitas(None) == []


def test_mapa_da_rota_nao_pinta_a_area_oficial(monkeypatch):
    import inlog_agent.live_pdf as pdf

    monkeypatch.setattr(pdf, "obter_tiles", lambda _chaves: None)
    blob, itens = _mapa(
        {"81BN": [ANEL]},
        [FEITA],
        [{"codigo": "81BN", "grupo": "parcial"}],
        largura=480,
        altura_px=360,
        individual=True,
    )
    assert ("area", "Área oficial") not in itens
    assert ("gps", "GPS") not in itens
    assert itens == [("feita", "Trajetória feita")]
    img = Image.open(BytesIO(blob)).convert("RGB")
    pixels = list(img.get_flattened_data())
    laranja = sum(1 for r, g, b in pixels if r > 180 and g < 140 and r > g + 40 and b < 100)
    verde = sum(1 for r, g, b in pixels if g > 90 and g > r + 25 and g > b + 10)
    assert laranja == 0
    assert verde > 20


def test_area_sem_trajetoria_fica_sem_legenda(monkeypatch):
    import inlog_agent.live_pdf as pdf

    monkeypatch.setattr(pdf, "obter_tiles", lambda _chaves: None)
    _blob, itens = _mapa(
        {"86BN": [ANEL]},
        [],
        [{"codigo": "86BN", "grupo": "nao"}],
        largura=320,
        altura_px=240,
        individual=True,
    )
    assert itens == []
