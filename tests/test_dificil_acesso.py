"""Camada de difcil acesso. No fala com a Segtrack e no grava rota."""

import time
from datetime import datetime

from inlog_agent.dificil_acesso import DIFICIL, SEM_RASTREAMENTO, montar_trajetoria, presentes_no_cadastro


def _fora(_lat, _lon):
    return False


def _linha(base, n, passo, hora, segundo=0):
    pontos = []
    for i in range(n):
        pontos.append((f"2026-10-06T{hora:02d}:00:{segundo + i * 10:02d}", base - i * passo, -49.25))
    return pontos


def test_ordem_temporal_e_passagem_unica():
    pontos = [
        ("2026-10-06T08:00:20", -16.822, -49.25),
        ("2026-10-06T08:00:00", -16.820, -49.25),
        ("2026-10-06T08:00:40", -16.824, -49.25),
    ]
    desenho = montar_trajetoria(pontos, _fora)
    assert desenho["pulsos"] == 3
    assert desenho["pontos_fora"] == 3
    assert desenho["dificil"] == []
    linha = desenho["deslocamento"][0]
    assert [round(p[0], 3) for p in linha] == [-16.820, -16.822, -16.824]


def test_gap_de_tempo_nao_vira_reta():
    pontos = [
        ("2026-10-06T08:00:00", -16.82, -49.25),
        ("2026-10-06T08:11:00", -16.8202, -49.2502),
    ]
    desenho = montar_trajetoria(list(reversed(pontos)), _fora)
    assert desenho["pulsos"] == 2
    assert desenho["deslocamento"] == []
    assert desenho["dificil"] == []
    assert desenho["na_rota"] == []


def test_corda_longa_com_amostra_rara_quebra():
    pontos = [
        ("2026-10-06T08:00:00", -16.82, -49.25),
        ("2026-10-06T08:03:00", -16.83, -49.25),
    ]
    desenho = montar_trajetoria(pontos, _fora)
    assert desenho["deslocamento"] == []
    assert desenho["dificil"] == []


def test_amostra_curta_permanece_ligada():
    pontos = [
        ("2026-10-06T08:00:00", -16.82, -49.25),
        ("2026-10-06T08:00:40", -16.824, -49.25),
    ]
    desenho = montar_trajetoria(pontos, _fora)
    assert len(desenho["deslocamento"]) == 1
    assert desenho["dificil"] == []


def test_ponto_fora_nao_e_descartado():
    pontos = [
        ("2026-10-06T08:00:00", -16.8200, -49.25),
        ("2026-10-06T08:00:20", -16.8230, -49.25),
        ("2026-10-06T08:00:40", -16.8201, -49.25),
    ]
    desenho = montar_trajetoria(pontos, lambda lat, _lon: lat > -16.821)
    fora = [p for linha in desenho["deslocamento"] + desenho["dificil"] for p in linha]
    assert any(abs(p[0] + 16.823) < 0.0001 for p in fora)
    assert desenho["na_rota"]


def test_repeticao_fora_da_rota_vira_dificil():
    ida = _linha(-16.83, 6, 0.0003, 8)
    volta = _linha(-16.83, 6, 0.0003, 9)
    unico = montar_trajetoria(ida, _fora)
    assert unico["dificil"] == []
    assert unico["deslocamento"]
    repetido = montar_trajetoria(ida + volta, _fora)
    assert repetido["pontos_fora"] == 12
    assert repetido["na_rota"] == []
    assert repetido["dificil"]
    assert repetido["deslocamento"] == []


def test_repeticao_dentro_da_rota_nao_vira_dificil():
    ida = _linha(-16.83, 6, 0.0003, 8)
    volta = _linha(-16.83, 6, 0.0003, 9)
    desenho = montar_trajetoria(ida + volta, lambda _lat, _lon: True)
    assert desenho["dificil"] == []
    assert desenho["deslocamento"] == []
    assert desenho["na_rota"]
    assert desenho["pontos_fora"] == 0


def test_leitura_nao_altera_rota(monkeypatch, tmp_path):
    banco = tmp_path / "live.db"
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(banco))
    monkeypatch.setenv("SAGTRACK_LIVE_SYNC", "0")
    from inlog_agent.live_db import conectar, gravar_rotas, inserir_pontos
    from inlog_agent.live_mapa import dificil_acesso

    con = conectar()
    gravar_rotas(
        con,
        "2026-10-06",
        "diurno",
        [
            {
                "codigo": "66DN",
                "rota": "66DN",
                "percentual": 42,
                "status": "Conclu\u00edda parcialmente",
                "grupo": "parcial",
                "placa": "SCH-9B28",
            }
        ],
    )
    inserir_pontos(
        con,
        "2026-10-06",
        "diurno",
        "SCN-1G89",
        [
            {"timestamp": "2026-10-06T08:00:00", "latitude": -16.82, "longitude": -49.25},
            {"timestamp": "2026-10-06T08:00:30", "latitude": -16.821, "longitude": -49.251},
        ],
    )
    for i in range(40):
        inserir_pontos(
            con,
            "2026-10-06",
            "diurno",
            "SCH-9B28",
            [{"timestamp": f"2026-10-06T10:00:{i:02d}", "latitude": -16.7, "longitude": -49.1}],
        )
    con.commit()
    antes = tuple(con.execute("SELECT percentual, status, grupo FROM rota WHERE codigo = '66DN'").fetchone())
    con.close()
    inicio = time.perf_counter()
    pacote = dificil_acesso("2026-10-06", "diurno")
    gasto = (time.perf_counter() - inicio) * 1000
    con = conectar()
    depois = tuple(con.execute("SELECT percentual, status, grupo FROM rota WHERE codigo = '66DN'").fetchone())
    con.close()
    assert antes == depois == (42.0, "Conclu\u00edda parcialmente", "parcial")
    assert pacote["fonte"] == "live_sync"
    assert [item["codigo"] for item in pacote["veiculos"]] == ["CBT018", "CBT012"]
    cbt018 = pacote["veiculos"][0]
    cbt012 = pacote["veiculos"][1]
    assert cbt018["pulsos"] == 0
    assert cbt012["pulsos"] == 2
    assert cbt012["placa"] == "SCN1G89"
    desenhos = cbt012["na_rota"] + cbt012["deslocamento"] + cbt012["dificil"]
    assert desenhos
    lats = [p[0] for linha in desenhos for p in linha]
    assert any(abs(lat + 16.82) < 0.0002 for lat in lats)
    assert any(abs(lat + 16.821) < 0.0002 for lat in lats)
    assert gasto < 3000
    assert pacote["resumo"]["ms"] < 3000


def test_sem_cadastro_nao_consulta():
    assert presentes_no_cadastro([]) == []
    assert presentes_no_cadastro([{"ras_vei_placa": "SCH9B28", "ras_vei_id": "1"}]) == []
    achados = presentes_no_cadastro(
        [{"ras_vei_placa": "SCN4B49", "ras_vei_id": "9", "ras_vei_veiculo": "IVECO"}]
    )
    assert achados == [{"placa": "SCN-4B49", "veiculo": "CBT018", "vehicle_id": "9"}]


def test_um_ponto_nao_inventa_linha():
    desenho = montar_trajetoria([("2026-10-06T08:00:00", -15.5, -47.8)], _fora)
    assert desenho["pulsos"] == 1
    assert desenho["situacao"] != SEM_RASTREAMENTO
    assert desenho["deslocamento"] == []
    assert desenho["dificil"] == []
    assert desenho["na_rota"] == []
    assert desenho["lat"] == -15.5


def _passo(indice, hora, segundo):
    return {
        "timestamp": f"2026-10-06T{hora:02d}:00:{segundo:02d}",
        "latitude": -15.5 - indice * 0.0003,
        "longitude": -47.8,
    }


def test_cenario_do_primeiro_gps_ate_recorrencia(monkeypatch, tmp_path):
    banco = tmp_path / "live.db"
    monkeypatch.setenv("SAGTRACK_LIVE_DB", str(banco))
    monkeypatch.setenv("SAGTRACK_LIVE_SYNC", "0")
    from inlog_agent.live_db import conectar
    from inlog_agent.live_mapa import dificil_acesso, trilhas
    from inlog_agent.live_sync import _limpar_cache_frota, ciclo_gps

    _limpar_cache_frota()
    agora = datetime(2026, 10, 6, 10, 0, 0)
    dia = "2026-10-06"
    brutos = []
    serie = []
    chamadas = []
    caixa = []

    def descobrir():
        from inlog_agent.dificil_acesso import presentes_no_cadastro

        return [{"placa": "SCH-9B28", "veiculo": "VW/18.260", "vehicle_id": "1"}, *presentes_no_cadastro(brutos)]

    def historico(placa, _vid, _inicio, _fim):
        chave = str(placa or "").upper().replace("-", "")
        if chave == "SCN4B49":
            return list(serie)
        if chave == "SCH9B28":
            return [{"timestamp": "2026-10-06T08:00:00", "latitude": -16.82, "longitude": -49.25}]
        return []

    def recalcular(*_args, **_kwargs):
        chamadas.append("rota")
        return []

    def ciclo():
        _limpar_cache_frota()
        return ciclo_gps(
            tmp_path,
            dia,
            "diurno",
            agora=agora,
            buscar_historico=historico,
            descobrir=descobrir,
            recalcular=recalcular,
            limites=list(caixa),
        )

    ciclo()
    pacote = dificil_acesso(dia, "diurno")
    assert [item["situacao"] for item in pacote["veiculos"]] == [SEM_RASTREAMENTO, SEM_RASTREAMENTO]
    assert all(item["pulsos"] == 0 and not item["dificil"] and not item["deslocamento"] for item in pacote["veiculos"])

    brutos.append({"ras_vei_placa": "SCN4B49", "ras_vei_id": "9", "ras_vei_veiculo": "IVECO"})
    caixa.append({"codigo": "01AD", "minx": -1e7, "miny": -1e7, "maxx": 1e7, "maxy": 1e7, "folga": 0})
    serie.append(_passo(0, 8, 0))
    ciclo()
    cbt018 = next(item for item in dificil_acesso(dia, "diurno")["veiculos"] if item["codigo"] == "CBT018")
    assert cbt018["pulsos"] == 1
    assert cbt018["situacao"] != SEM_RASTREAMENTO
    assert cbt018["ultimo_gps"].startswith("2026-10-06T08:00:00")
    assert cbt018["lat"] is not None
    assert cbt018["deslocamento"] == [] and cbt018["dificil"] == []

    serie.extend(_passo(i, 8, i * 10) for i in range(1, 6))
    ciclo()
    cbt018 = next(item for item in dificil_acesso(dia, "diurno")["veiculos"] if item["codigo"] == "CBT018")
    assert cbt018["pulsos"] == 6
    assert cbt018["deslocamento"]
    assert cbt018["dificil"] == []
    assert cbt018["situacao"] == "Deslocamento"
    linha = cbt018["deslocamento"][0]
    assert linha[0][0] > linha[-1][0]

    serie.extend(_passo(i, 9, i * 10) for i in range(6))
    ciclo()
    cbt018 = next(item for item in dificil_acesso(dia, "diurno")["veiculos"] if item["codigo"] == "CBT018")
    assert cbt018["dificil"]
    assert cbt018["situacao"] == DIFICIL
    placas_trilha = {item["placa"] for item in trilhas(dia, "diurno")["veiculos"]}
    assert "SCN-4B49" not in placas_trilha

    con = conectar()
    rotas = con.execute("SELECT percentual, status, grupo FROM rota").fetchall()
    con.close()
    assert rotas
    assert all(row["percentual"] == 0 and row["status"] == "N\u00e3o iniciada" and row["grupo"] == "nao" for row in rotas)
    assert chamadas == []
    _limpar_cache_frota()
