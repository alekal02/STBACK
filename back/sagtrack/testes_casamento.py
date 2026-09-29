"""Map matching: esquina, vias paralelas, retorno, salto e silêncio.

Roda com: py -m sagtrack.testes_casamento
A conta da rota não usa este traçado. Rua feita segue o pulso a 45 m.
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
    teste_malha_oficial_conecta()
    print("casamento ok")


if __name__ == "__main__":
    main()
