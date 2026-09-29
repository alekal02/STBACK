"""Map matching: esquina, vias paralelas, retorno, salto, silêncio e cobertura.

Roda com: py -m sagtrack.testes_casamento
A rua feita é o eixo que o trajeto confirmado percorre. Passar perto não conta.
"""

from __future__ import annotations

from sagtrack.gps import Malha, casar_tracos, haversine_m


def _pulso(lat: float, lon: float, hora: str) -> dict:
    return {"latitude": lat, "longitude": lon, "timestamp": f"29/09/2026 {hora}"}


def _distancia_ao_traco(ponto: tuple[float, float], tracos: list) -> float:
    melhor = 1e9
    for trecho in tracos:
        for coord in trecho:
            melhor = min(melhor, haversine_m(ponto, (float(coord[0]), float(coord[1]))))
    return melhor


def _linhas(casado: dict) -> list:
    return list(casado["confirmado"]) + list(casado["estimado"])


def teste_esquina() -> None:
    vias = [
        [[-16.8200, -49.2500], [-16.8170, -49.2500]],
        [[-16.8170, -49.2500], [-16.8170, -49.2470]],
    ]
    casado = casar_tracos(
        [_pulso(-16.8200, -49.2500, "15:00:00"), _pulso(-16.8170, -49.2470, "15:03:00")],
        vias=vias,
    )
    esquina = (-16.8170, -49.2500)
    corda = haversine_m(esquina, (-16.8185, -49.2485))
    na_via = _distancia_ao_traco(esquina, _linhas(casado))
    assert na_via < 15, na_via
    assert corda > 150, corda
    assert casado["resumo"]["indeterminado"] == 0


def teste_paralelas() -> None:
    sul = [[-16.82000, -49.2500], [-16.82000, -49.2470]]
    norte = [[-16.82028, -49.2500], [-16.82028, -49.2470]]
    pontos = [
        _pulso(-16.82000, -49.2497, "15:00:00"),
        _pulso(-16.82000, -49.2488, "15:00:40"),
        _pulso(-16.82000, -49.2476, "15:01:20"),
    ]
    casado = casar_tracos(pontos, vias=[sul, norte])
    for trecho in _linhas(casado):
        for lat, _lon in trecho:
            assert abs(lat - (-16.82000)) < abs(lat - (-16.82028)), lat


def teste_retorno() -> None:
    via = [[-16.8200, -49.2500], [-16.8200, -49.2470]]
    pontos = [
        _pulso(-16.8200, -49.2498, "15:00:00"),
        _pulso(-16.8200, -49.2472, "15:01:00"),
        _pulso(-16.8200, -49.2496, "15:02:00"),
    ]
    casado = casar_tracos(pontos, vias=[via])
    ida = haversine_m((-16.8200, -49.2498), (-16.8200, -49.2472))
    comprimento = 0.0
    anterior = None
    for trecho in _linhas(casado):
        for coord in trecho:
            ponto = (float(coord[0]), float(coord[1]))
            if anterior is not None:
                comprimento += haversine_m(anterior, ponto)
            anterior = ponto
            assert abs(ponto[0] - (-16.8200)) < 0.00015
    assert comprimento > ida * 1.6, comprimento


def teste_salto_de_gps() -> None:
    casado = casar_tracos(
        [_pulso(-16.8200, -49.2500, "15:00:00"), _pulso(-16.8000, -49.2500, "15:00:04")],
        vias=[[[-16.8200, -49.2500], [-16.8000, -49.2500]]],
    )
    assert casado["resumo"]["indeterminado"] >= 1
    assert _linhas(casado) == []


def teste_silencio() -> None:
    via = [[-16.8200, -49.2500], [-16.8200, -49.2470]]
    casado = casar_tracos(
        [_pulso(-16.8200, -49.2498, "15:00:00"), _pulso(-16.8200, -49.2474, "15:20:00")],
        vias=[via],
    )
    assert casado["resumo"]["indeterminado"] >= 1
    assert casado["estimado"] == []
    assert casado["confirmado"] == []


def teste_pulso_curto_confirmado() -> None:
    via = [[-16.8200, -49.2500], [-16.8200, -49.2470]]
    casado = casar_tracos(
        [_pulso(-16.8200, -49.2495, "15:00:00"), _pulso(-16.8200, -49.2492, "15:00:15")],
        vias=[via],
    )
    assert casado["confirmado"]
    assert casado["estimado"] == []
    assert casado["resumo"]["indeterminado"] == 0


def teste_sentido() -> None:
    malha = Malha.de_wgs([[[-16.8200, -49.2500], [-16.8200, -49.2470]]], sentidos=[1])
    assert malha.custo_volta[0] > malha.custo_ida[0]


def teste_sem_corda_fora_da_via() -> None:
    via = [[-16.8200, -49.2500], [-16.8200, -49.2470]]
    casado = casar_tracos(
        [
            _pulso(-16.8200, -49.2530, "15:00:00"),
            _pulso(-16.8200, -49.2524, "15:00:20"),
        ],
        vias=[via],
    )
    assert _linhas(casado) == []
    assert casado["resumo"]["indeterminado"] >= 1


def teste_nao_encurta_o_quarteirao() -> None:
    from sagtrack.cobertura import TO_UTM, TO_WGS

    lat0, lon0 = -16.82, -49.25

    def mover(leste: float, norte: float) -> tuple[float, float]:
        x, y = TO_UTM.transform(lon0, lat0)
        lon, lat = TO_WGS.transform(x + leste, y + norte)
        return lat, lon

    cantos = [mover(0, 0), mover(0, 160), mover(220, 160), mover(220, 0)]
    vias = [[list(cantos[i]), list(cantos[(i + 1) % 4])] for i in range(4)]
    pontos = []
    passo = 0
    for origem, destino in ((cantos[0], cantos[1]), (cantos[1], cantos[2]), (cantos[2], cantos[3])):
        for parte in range(5):
            lat = origem[0] + (destino[0] - origem[0]) * parte / 4
            lon = origem[1] + (destino[1] - origem[1]) * parte / 4
            minuto = 15 * 60 + passo * 12
            pontos.append(_pulso(lat, lon, f"15:{minuto // 60:02d}:{minuto % 60:02d}"))
            passo += 1
    casado = casar_tracos(pontos, vias=vias)
    canto = cantos[1]
    assert _distancia_ao_traco(canto, _linhas(casado)) < 20, _distancia_ao_traco(canto, _linhas(casado))
    atalho = mover(110, 0)
    assert _distancia_ao_traco(atalho, _linhas(casado)) > 40


def teste_cobertura_ignora_paralela() -> None:
    from shapely.geometry import LineString

    from sagtrack.cobertura import TO_UTM, avaliar_cobertura, faixa_de_tracos, pecas_da_rua

    sul = [[-16.82000, -49.2500], [-16.82000, -49.2470]]
    norte = [[-16.82070, -49.2500], [-16.82070, -49.2470]]
    pontos = [
        _pulso(-16.82000, -49.2496, "15:00:00"),
        _pulso(-16.82000, -49.2490, "15:00:25"),
        _pulso(-16.82000, -49.2484, "15:00:50"),
        _pulso(-16.82000, -49.2478, "15:01:15"),
    ]
    casado = casar_tracos(pontos, vias=[sul, norte])
    faixa_c = faixa_de_tracos(casado["confirmado"])
    faixa_e = faixa_de_tracos(casado["estimado"])

    def utm(linha: list) -> LineString:
        return LineString([TO_UTM.transform(float(p[1]), float(p[0])) for p in linha])

    pecas = pecas_da_rua([utm(sul), utm(norte)])
    cob = avaliar_cobertura([], pontos, pecas=pecas, faixa_confirmada=faixa_c, faixa_estimada=faixa_e)
    assert cob["metodo"] == "malha"
    assert 30 <= cob["percentual"] <= 70, cob["percentual"]
    norte_lat = -16.82070
    for trecho in cob["trechos_feitos"]:
        for lat, _lon in trecho:
            assert abs(lat - norte_lat) > abs(lat - (-16.82000))


def teste_cobertura_nao_dobra() -> None:
    from shapely.geometry import LineString

    from sagtrack.cobertura import TO_UTM, avaliar_cobertura, faixa_de_tracos, pecas_da_rua

    via = [[-16.8200, -49.2500], [-16.8200, -49.2470]]
    ida = [
        _pulso(-16.8200, -49.2496, "15:00:00"),
        _pulso(-16.8200, -49.2486, "15:00:30"),
        _pulso(-16.8200, -49.2476, "15:01:00"),
    ]
    volta = [
        _pulso(-16.8200, -49.2486, "15:01:30"),
        _pulso(-16.8200, -49.2496, "15:02:00"),
    ]
    casado = casar_tracos(ida + volta, vias=[via])
    linha = LineString([TO_UTM.transform(p[1], p[0]) for p in via])
    cob = avaliar_cobertura(
        [],
        ida + volta,
        pecas=pecas_da_rua([linha]),
        faixa_confirmada=faixa_de_tracos(casado["confirmado"]),
        faixa_estimada=faixa_de_tracos(casado["estimado"]),
    )
    assert cob["percentual"] <= 100
    assert cob["km_coleta"] < linha.length / 1000 * 1.25


def teste_emenda_de_cache() -> None:
    from sagtrack.gps import _emendar_listas

    unidos = _emendar_listas([[[-16.82, -49.25], [-16.82, -49.249]]], [[[-16.82, -49.249], [-16.82, -49.248]]])
    assert len(unidos) == 1
    assert unidos[0][-1][1] == -49.248
    separados = _emendar_listas([[[-16.82, -49.25], [-16.82, -49.249]]], [[[-16.81, -49.24], [-16.81, -49.239]]])
    assert len(separados) == 2


def teste_malha_oficial_conecta() -> None:
    from sagtrack.mapa import TO_WGS

    malha = Malha.oficial()
    assert len(malha.geoms) > 1000
    achou = False
    for indice, no in enumerate(malha.no1):
        vizinhos = [aresta for _outro, aresta, _frente in malha.adj.get(no, ()) if aresta != indice]
        if not vizinhos:
            continue
        outra = vizinhos[0]
        a = malha.geoms[indice].interpolate(malha.comp[indice] * 0.35)
        b = malha.geoms[outra].interpolate(malha.comp[outra] * 0.65)
        lon1, lat1 = TO_WGS.transform(a.x, a.y)
        lon2, lat2 = TO_WGS.transform(b.x, b.y)
        no_x, no_y = malha.geoms[indice].coords[-1]
        lon_no, lat_no = TO_WGS.transform(no_x, no_y)
        casado = casar_tracos(
            [_pulso(lat1, lon1, "15:00:00"), _pulso(lat2, lon2, "15:02:00")],
            malha=malha,
        )
        perto = _distancia_ao_traco((lat_no, lon_no), _linhas(casado))
        assert perto < 25, perto
        achou = True
        break
    assert achou


def main() -> None:
    teste_esquina()
    teste_paralelas()
    teste_retorno()
    teste_salto_de_gps()
    teste_silencio()
    teste_pulso_curto_confirmado()
    teste_sentido()
    teste_sem_corda_fora_da_via()
    teste_nao_encurta_o_quarteirao()
    teste_cobertura_ignora_paralela()
    teste_cobertura_nao_dobra()
    teste_emenda_de_cache()
    teste_malha_oficial_conecta()
    print("casamento ok")


if __name__ == "__main__":
    main()
