"""Pulso de um minuto segue a via. Vão ambíguo ou longo não vira trecho executado."""

from datetime import datetime, timedelta

from shapely.geometry import LineString, Point

from inlog_agent.cobertura import TO_UTM, TO_WGS, cobertura_compartilhada
from inlog_agent.live_mapa import _linhas_visuais

_X, _Y = TO_UTM.transform(-49.25, -16.82)


def _rua(dx0, dy0, dx1, dy1) -> LineString:
    return LineString([(_X + dx0, _Y + dy0), (_X + dx1, _Y + dy1)])


def _pulso(dx: float, dy: float, momento: datetime) -> dict:
    lon, lat = TO_WGS.transform(_X + dx, _Y + dy)
    return {
        "latitude": lat,
        "longitude": lon,
        "timestamp": momento.strftime("%Y-%m-%dT%H:%M:%S"),
        "tz": "BRT",
    }


def _perto(linhas, dx: float, dy: float) -> float:
    alvo = Point(_X + dx, _Y + dy)
    menor = 1e9
    for linha in linhas:
        for par in linha:
            if len(par) < 2:
                continue
            x, y = TO_UTM.transform(float(par[1]), float(par[0]))
            menor = min(menor, Point(x, y).distance(alvo))
    return menor


def test_pulso_de_um_minuto_contorna_o_quarteirao_pela_rua():
    ruas = [_rua(0, 0, 0, 220), _rua(0, 220, 200, 220)]
    inicio = datetime(2026, 11, 10, 9, 0)
    pontos = [
        _pulso(0, 0, inicio),
        _pulso(200, 220, inicio + timedelta(seconds=90)),
    ]
    resultado = cobertura_compartilhada(ruas, [{"placa": "VCK-0D86", "veiculo": "VCK", "pontos": pontos}])
    assert resultado["percentual"] > 80
    assert _perto(resultado["trechos_feitos"], 0, 220) < 25
    assert _perto(resultado["trechos_feitos"], 100, 110) > 40

    brutos = [(item["timestamp"], item["latitude"], item["longitude"]) for item in pontos]
    linhas = _linhas_visuais(brutos, ruas)
    assert linhas
    assert _perto(linhas, 0, 220) < 25
    assert _perto(linhas, 100, 110) > 40


def test_dois_caminhos_iguais_nao_marcam_trecho():
    ruas = [
        _rua(0, 0, 0, 80),
        _rua(0, 0, 200, 0),
        _rua(200, 0, 200, 80),
        _rua(0, 80, 200, 80),
    ]
    inicio = datetime(2026, 11, 10, 9, 0)
    pontos = [
        _pulso(0, 40, inicio),
        _pulso(200, 40, inicio + timedelta(seconds=70)),
    ]
    resultado = cobertura_compartilhada(ruas, [{"placa": "VCK-0D86", "veiculo": "VCK", "pontos": pontos}])
    assert resultado["percentual"] == 0
    assert resultado["executores"] == []
    brutos = [(item["timestamp"], item["latitude"], item["longitude"]) for item in pontos]
    assert _linhas_visuais(brutos, ruas) == []


def test_vao_de_dez_minutos_nao_liga_os_pulsos():
    ruas = [_rua(0, 0, 400, 0)]
    inicio = datetime(2026, 11, 10, 9, 0)
    pontos = [
        _pulso(0, 0, inicio),
        _pulso(300, 0, inicio + timedelta(minutes=10)),
    ]
    resultado = cobertura_compartilhada(ruas, [{"placa": "VCK-0D86", "veiculo": "VCK", "pontos": pontos}])
    assert resultado["percentual"] == 0
    brutos = [(item["timestamp"], item["latitude"], item["longitude"]) for item in pontos]
    assert _linhas_visuais(brutos, ruas) == []
