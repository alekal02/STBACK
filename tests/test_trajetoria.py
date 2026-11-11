"""Motor único: corte de turno, limpeza e casamento na via."""

from datetime import date, datetime, timedelta

from shapely.geometry import LineString, Point

from inlog_agent.cobertura import TO_UTM, TO_WGS
from inlog_agent.trajetoria import (
    casar_pulsos,
    cortar_turno,
    limpar_pulsos,
    percentual_da_linha,
)


_X, _Y = TO_UTM.transform(-49.25, -16.82)


def _rua(dx0, dy0, dx1, dy1) -> LineString:
    return LineString([(_X + dx0, _Y + dy0), (_X + dx1, _Y + dy1)])


def _pulso(linha: LineString, dist: float, quando: datetime) -> tuple[str, float, float]:
    ponto = linha.interpolate(dist)
    lon, lat = TO_WGS.transform(ponto.x, ponto.y)
    return quando.strftime("%Y-%m-%dT%H:%M:%S"), float(lat), float(lon)


def test_corte_de_turno_deixa_so_o_relogio_do_diurno():
    dia = date(2026, 11, 11)
    pontos = [
        ("2026-11-11T05:30:00", -16.82, -49.25),
        ("2026-11-11T08:00:00", -16.82, -49.25),
        ("2026-11-11T20:00:00", -16.82, -49.25),
    ]
    diurno = cortar_turno(pontos, dia, "diurno")
    noturno = cortar_turno(pontos, dia, "noturno")
    assert [item[0] for item in diurno] == ["2026-11-11T08:00:00"]
    assert [item[0] for item in noturno] == ["2026-11-11T20:00:00"]


def test_limpeza_remove_teleporte():
    base = datetime(2026, 11, 11, 8, 0, 0)
    pontos = [
        ("2026-11-11T08:00:00", -16.820, -49.250),
        ("2026-11-11T08:00:10", -16.820, -49.251),
        ("2026-11-11T08:00:20", -15.000, -48.000),
        ("2026-11-11T08:00:30", -16.820, -49.252),
    ]
    limpos = limpar_pulsos(pontos)
    assert all(item[1] < -16 for item in limpos)
    assert len(limpos) == 3
    assert base.year == 2026


def test_casamento_segue_a_esquina_e_nao_a_corda():
    leste = _rua(0, 0, 400, 0)
    norte = _rua(400, 0, 400, 300)
    inicio = datetime(2026, 11, 11, 8, 0, 0)
    pontos = []
    for i, dist in enumerate(range(0, 401, 40)):
        pontos.append(_pulso(leste, dist, inicio + timedelta(seconds=12 * i)))
    for i, dist in enumerate(range(40, 301, 40), start=len(pontos)):
        pontos.append(_pulso(norte, dist, inicio + timedelta(seconds=12 * i)))
    trajeto = casar_pulsos(pontos, [leste, norte], date(2026, 11, 11), "diurno")
    assert trajeto.linhas
    assert trajeto.geom is not None
    malha = leste.union(norte)
    partes = [trajeto.geom] if trajeto.geom.geom_type == "LineString" else list(trajeto.geom.geoms)
    for parte in partes:
        for x, y in parte.coords:
            assert malha.distance(Point(x, y)) < 15
    assert trajeto.geom.length > 600


def test_percentual_da_linha_casada_nao_casa_de_novo():
    rua = _rua(0, 0, 400, 0)
    inicio = datetime(2026, 11, 11, 8, 0, 0)
    pontos = [_pulso(rua, dist, inicio + timedelta(seconds=12 * i)) for i, dist in enumerate(range(0, 401, 40))]
    trajeto = casar_pulsos(pontos, [rua], date(2026, 11, 11), "diurno")
    valor = percentual_da_linha(trajeto.geom, [rua], rua.buffer(40))
    assert valor is not None and valor >= 95
    fora = _rua(0, 300, 400, 300)
    fora_traj = casar_pulsos(
        [_pulso(fora, dist, inicio + timedelta(seconds=12 * i)) for i, dist in enumerate(range(0, 401, 40))],
        [rua, fora],
        date(2026, 11, 11),
        "diurno",
    )
    assert percentual_da_linha(fora_traj.geom, [rua], rua.buffer(30)) == 0


def test_pista_dupla_conta_a_outra_faixa_e_nao_a_continuacao():
    sul = _rua(0, 0, 400, 0)
    norte = _rua(0, 15, 400, 15)
    longe = _rua(0, 55, 400, 55)
    frente = _rua(400, 0, 800, 0)
    inicio = datetime(2026, 11, 11, 8, 0, 0)
    pontos = [_pulso(sul, dist, inicio + timedelta(seconds=12 * i)) for i, dist in enumerate(range(0, 401, 40))]
    trajeto = casar_pulsos(pontos, [sul, norte], date(2026, 11, 11), "diurno")
    setor = Point(_X + 200, _Y).buffer(500)
    duas = percentual_da_linha(trajeto.geom, [sul, norte], setor, ["Avenida Brasil", "Avenida Brasil"])
    uma = percentual_da_linha(trajeto.geom, [sul, norte], setor, ["Avenida Brasil", "Rua das Flores"])
    distante = percentual_da_linha(trajeto.geom, [sul, longe], setor, ["Avenida Brasil", "Avenida Brasil"])
    seguida = percentual_da_linha(trajeto.geom, [sul, frente], setor, ["Avenida Brasil", "Avenida Brasil"])
    assert duas is not None and uma is not None
    assert duas > uma + 20
    assert distante is not None and distante < duas - 20
    assert seguida is not None and seguida < duas - 20


def test_diurno_nao_credita_rota_de_noturno(monkeypatch):
    from inlog_agent import trajetoria

    sul = _rua(0, 0, 200, 0)
    inicio = datetime(2026, 11, 11, 8, 0, 0)
    pontos = [_pulso(sul, dist, inicio + timedelta(seconds=12 * i)) for i, dist in enumerate(range(0, 201, 40))]
    trajeto = casar_pulsos(pontos, [sul], date(2026, 11, 11), "diurno")
    setor = {
        "codigo": "66DN",
        "frequencia": "DIARIO - NOTURNO",
        "geom": sul.buffer(80),
        "ruas": [sul],
        "nomes": ["Avenida Brasil"],
    }
    monkeypatch.setattr(trajetoria, "trajetos_do_turno", lambda *_args, **_kwargs: {"SCN-4B49": trajeto})
    monkeypatch.setattr("inlog_agent.mapa_oficial.carregar_setores", lambda *_args, **_kwargs: [setor])
    assert trajetoria.percentual_das_rotas(date(2026, 11, 11), "diurno", ["66DN", "70AN"]) == {}
    noturno = trajetoria.percentual_das_rotas(date(2026, 11, 11), "noturno", ["66DN"])
    assert noturno.get("66DN", 0) >= 90
