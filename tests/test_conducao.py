"""Condução estimada. Não chama a Fulltrack."""

from datetime import datetime, timedelta

from inlog_agent.conducao import (
    FRENAGEM_QUEDA_KMH,
    analisar,
    diferenca_rumo,
    normalizar_alerta,
)

BASE = datetime(2026, 10, 21, 8, 0, 0)
TRECHOS = (
    (0, 40, 40, 10),
    (40, 80, 5, 10),
    (80, 120, 40, 10),
    (120, 160, 40, 90),
)


def _serie(passo, trechos, ignicao=True):
    pontos = []
    for inicio, fim, vel, rumo in trechos:
        cursor = inicio
        while cursor < fim:
            pontos.append(
                {
                    "ts": BASE + timedelta(seconds=cursor),
                    "lat": -16.82,
                    "lon": -49.25,
                    "vel": vel,
                    "direcao": rumo,
                    "ignicao": ignicao,
                }
            )
            cursor += passo
    return pontos


def _contagem(passo):
    return analisar(_serie(passo, TRECHOS))["contagem"]


def test_reamostragem_de_2s_e_10s_da_a_mesma_contagem():
    fino = _contagem(2)
    grosso = _contagem(10)
    assert fino["frenagem_brusca"] == grosso["frenagem_brusca"] == 1
    assert fino["aceleracao_brusca"] == grosso["aceleracao_brusca"] == 1
    assert fino["curva_brusca_direita"] == grosso["curva_brusca_direita"] == 1
    assert fino["curva_brusca_esquerda"] == grosso["curva_brusca_esquerda"] == 0
    assert FRENAGEM_QUEDA_KMH == 30


def test_curva_esquerda_e_direita_e_o_cruzamento_de_350_para_20():
    assert diferenca_rumo(350, 20) == 30
    assert diferenca_rumo(0, 90) == 90
    assert diferenca_rumo(90, 0) == -90
    esquerda = analisar(_serie(10, ((0, 20, 30, 100), (20, 40, 30, 20))))
    assert esquerda["contagem"]["curva_brusca_esquerda"] == 1
    assert esquerda["contagem"]["curva_brusca_direita"] == 0
    cruzamento = analisar(_serie(10, ((0, 20, 30, 350), (20, 40, 30, 20))))
    assert cruzamento["contagem"]["curva_brusca_esquerda"] == 0
    assert cruzamento["contagem"]["curva_brusca_direita"] == 0


def test_lacuna_acima_de_15s_nao_vira_frenagem():
    pontos = _serie(10, ((0, 10, 50, 0), (40, 50, 10, 0)))
    assert analisar(pontos)["contagem"]["frenagem_brusca"] == 0


def test_episodio_de_excesso_agrupa_pulsos_seguidos_e_respeita_o_limite():
    pontos = [
        _pulso(0, 70),
        _pulso(10, 80),
        _pulso(20, 55),
        _pulso(30, 90),
        _pulso(40, 91),
        _pulso(50, 60),
    ]
    corpo = analisar(pontos)
    excessos = [item for item in corpo["eventos"] if item["tipo"] == "excesso_velocidade"]
    assert [item["velocidade"] for item in excessos] == [80, 91]
    assert excessos[0]["data_hora"].endswith("08:00:00")
    assert excessos[0]["fim"].endswith("08:00:10")
    assert excessos[0]["origem"] == "estimado"
    assert excessos[0]["link"].startswith("https://www.google.com/maps?q=")
    assert analisar(pontos, limite=0)["limite_kmh"] == 60
    assert analisar(pontos, limite=85)["contagem"]["excesso_velocidade"] == 1
    separados = analisar([_pulso(0, 70), _pulso(40, 75)])
    assert separados["contagem"]["excesso_velocidade"] == 2


def test_ocioso_so_passa_de_5_minutos_com_ignicao_ligada():
    curto = [_pulso(segundo, 0, ignicao=True) for segundo in range(0, 301, 60)]
    assert analisar(curto)["contagem"]["motor_ocioso"] == 0
    longo = [_pulso(segundo, 0, ignicao=True) for segundo in range(0, 361, 60)]
    ocioso = analisar(longo)
    assert ocioso["contagem"]["motor_ocioso"] == 1
    assert ocioso["eventos"][0]["origem"] == "estimado"
    assert ocioso["ocioso"]["episodios"] == 1
    andando = [_pulso(segundo, 4, ignicao=True) for segundo in range(0, 601, 60)]
    assert analisar(andando)["contagem"]["motor_ocioso"] == 0
    desligado = [_pulso(segundo, 0, ignicao=False) for segundo in range(0, 601, 60)]
    assert analisar(desligado)["contagem"]["motor_ocioso"] == 0


def test_ignicao_sempre_ligada_exige_todos_os_pulsos():
    assert analisar([_pulso(0, 10, ignicao=True), _pulso(10, 10, ignicao=True)])["ignicao_sempre_ligada"] is True
    assert analisar([_pulso(0, 10, ignicao=True), _pulso(10, 10, ignicao=False)])["ignicao_sempre_ligada"] is False
    assert analisar([_pulso(0, 10, ignicao=True), _pulso(10, 10, ignicao=None)])["ignicao_sempre_ligada"] is False
    assert analisar([])["ignicao_sempre_ligada"] is False


def test_alerta_da_api_substitui_o_estimado_do_mesmo_tipo():
    alerta = normalizar_alerta(
        {
            "tipo": "Frenagem brusca",
            "placa": "ABC1D23",
            "data": "2026-10-21 08:00:40",
            "velocidade": "22",
            "latitude": "-16.82",
            "longitude": "-49.25",
        }
    )
    assert alerta is not None
    assert alerta["origem"] == "api"
    assert alerta["placa"] == "ABC1D23"
    corpo = analisar(_serie(10, TRECHOS), alertas=[alerta])
    frenagens = [item for item in corpo["eventos"] if item["tipo"] == "frenagem_brusca"]
    assert len(frenagens) == 1
    assert frenagens[0]["origem"] == "api"
    assert corpo["contagem"]["aceleracao_brusca"] == 1
    assert corpo["contagem"]["curva_brusca_direita"] == 1
    curva = normalizar_alerta(
        {
            "descricao": "Curva brusca à esquerda",
            "ras_vei_placa": "ABC-1D23",
            "ras_ale_data": "21/10/2026 08:02:00",
            "ras_eve_velocidade": "30",
            "ras_eve_latitude": "-16.82",
            "ras_eve_longitude": "-49.25",
        }
    )
    sem_curva = analisar(_serie(10, TRECHOS), alertas=[curva])
    curvas = [item for item in sem_curva["eventos"] if "curva" in item["tipo"]]
    assert len(curvas) == 1
    assert curvas[0]["origem"] == "api"
    assert curvas[0]["tipo"] == "curva_brusca_esquerda"


def test_percentual_ocioso_usa_o_total_da_api():
    corpo = analisar([], ocioso_api={"ocioso_min": 30, "ligado_min": 120})
    assert corpo["ocioso"]["origem"] == "api"
    assert corpo["ocioso"]["percentual"] == 25
    assert corpo["ocioso"]["tempo_min"] == 30


def _pulso(segundo, vel, ignicao=True):
    return {
        "ts": BASE + timedelta(seconds=segundo),
        "lat": -16.82,
        "lon": -49.25,
        "vel": vel,
        "direcao": 0,
        "ignicao": ignicao,
    }
