"""O mapa do relatório fica travado no modelo de 19/10/2026: área oficial e GPS."""

from inlog_agent.live_pdf import _legenda_de, _mapa_rota


def test_legenda_do_mapa_e_area_oficial_e_gps():
    itens = _legenda_de(
        [{"codigo": "01AD", "grupo": "parcial"}],
        [[[-16.70, -49.25], [-16.69, -49.24]]],
        tiles=True,
        tem_area=True,
    )
    assert itens == [("area", "Área oficial"), ("gps", "GPS")]


def test_pagina_da_rota_usa_o_gps_e_nao_a_malha():
    rota = {"codigo": "01AD", "grupo": "parcial", "placa": "SDG-1J76"}
    anel = [(-49.25, -16.70), (-49.24, -16.70), (-49.24, -16.69), (-49.25, -16.69)]
    gps = [[-16.695, -49.245], [-16.694, -49.244]]
    detalhe = {
        "trechos_feitos": {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[-49.2, -16.6], [-49.1, -16.5]]}}]},
        "trechos_faltando": {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[-49.3, -16.8], [-49.2, -16.7]]}}]},
    }
    import inlog_agent.live_pdf as pdf

    capturado = {}

    def falso(poligonos, trilhas, rotas, **kwargs):
        capturado["trilhas"] = trilhas
        capturado["kwargs"] = kwargs
        return b"", [("area", "Área oficial"), ("gps", "GPS")]

    original = pdf._mapa
    pdf._mapa = falso
    try:
        _blob, itens = _mapa_rota(rota, [anel], [gps], detalhe)
    finally:
        pdf._mapa = original
    assert capturado["trilhas"] == [gps]
    assert "feitos" not in capturado["kwargs"]
    assert "faltando" not in capturado["kwargs"]
    assert itens == [("area", "Área oficial"), ("gps", "GPS")]
