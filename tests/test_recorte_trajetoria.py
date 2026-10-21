"""O desenho da rota recorta o GPS no polígono e não altera a cobertura."""

from __future__ import annotations

from inlog_agent.live_mapa import _linhas_recortadas

QUADRO = [[-16.70, -49.25], [-16.70, -49.24], [-16.69, -49.24], [-16.69, -49.25], [-16.70, -49.25]]
OUTRO = [[-16.68, -49.23], [-16.68, -49.22], [-16.67, -49.22], [-16.67, -49.23], [-16.68, -49.23]]


def _pulso(minuto: int, lat: float, lon: float) -> tuple[str, float, float]:
    return (f"2026-10-19T20:{minuto:02d}:00", lat, lon)


def _latitudes(linhas: list[list[list[float]]]) -> list[float]:
    return [par[0] for linha in linhas for par in linha]


def test_garagem_e_aterro_nao_entram_no_desenho():
    pontos = [
        _pulso(0, -16.80, -49.30),
        _pulso(1, -16.801, -49.301),
        _pulso(10, -16.695, -49.245),
        _pulso(11, -16.694, -49.244),
        _pulso(20, -16.60, -49.10),
        _pulso(21, -16.601, -49.101),
    ]
    linhas = _linhas_recortadas(pontos, [QUADRO])
    assert linhas
    for lat, lon in (par for linha in linhas for par in linha):
        assert -16.70 <= lat <= -16.69
        assert -49.25 <= lon <= -49.24


def test_saida_e_retorno_nao_criam_reta_atraves_do_setor():
    pontos = [
        _pulso(0, -16.698, -49.248),
        _pulso(1, -16.698, -49.247),
        _pulso(2, -16.701, -49.245),
        _pulso(3, -16.698, -49.243),
        _pulso(4, -16.698, -49.242),
    ]
    linhas = _linhas_recortadas(pontos, [QUADRO])
    assert len(linhas) == 2
    assert all(par[0] >= -16.70002 for linha in linhas for par in linha)
    for linha in linhas:
        lons = [par[1] for par in linha]
        assert max(lons) - min(lons) < 0.003


def test_avenida_de_contorno_fora_do_poligono_nao_e_execucao():
    pontos = [
        _pulso(0, -16.702, -49.248),
        _pulso(1, -16.702, -49.246),
        _pulso(2, -16.702, -49.244),
        _pulso(3, -16.702, -49.242),
    ]
    assert _linhas_recortadas(pontos, [QUADRO]) == []


def test_dois_pulsos_externos_nao_atravessam_o_setor():
    pontos = [
        _pulso(0, -16.698, -49.2508),
        _pulso(1, -16.7008, -49.248),
    ]
    assert _linhas_recortadas(pontos, [QUADRO]) == []


def test_trajetoria_de_outro_poligono_nao_aparece_na_rota():
    pontos = [
        _pulso(0, -16.675, -49.225),
        _pulso(1, -16.674, -49.224),
        _pulso(2, -16.673, -49.223),
    ]
    assert _linhas_recortadas(pontos, [QUADRO]) == []
    assert _linhas_recortadas(pontos, [OUTRO])


def test_intervalo_longo_continua_separado():
    pontos = [
        _pulso(0, -16.695, -49.248),
        _pulso(1, -16.694, -49.247),
        ("2026-10-19T20:20:00", -16.694, -49.243),
        ("2026-10-19T20:21:00", -16.695, -49.242),
    ]
    linhas = _linhas_recortadas(pontos, [QUADRO])
    assert len(linhas) == 2


def test_dois_caminhoes_recortam_so_o_proprio_trecho():
    principal = [
        _pulso(0, -16.695, -49.248),
        _pulso(1, -16.694, -49.247),
        _pulso(2, -16.80, -49.30),
    ]
    apoio = [
        _pulso(3, -16.693, -49.243),
        _pulso(4, -16.692, -49.242),
        _pulso(5, -16.60, -49.10),
    ]
    a = _linhas_recortadas(principal, [QUADRO])
    b = _linhas_recortadas(apoio, [QUADRO])
    assert a and b
    assert max(_latitudes(a)) < min(_latitudes(b))
    assert all(-16.70 <= par[0] <= -16.69 for linha in a + b for par in linha)
