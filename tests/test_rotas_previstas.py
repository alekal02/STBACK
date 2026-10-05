"""Programação do turno ao vivo. Não fala com a Fulltrack."""

import os
import tempfile
from datetime import datetime
from pathlib import Path

os.environ["SAGTRACK_LIVE_DB"] = str(Path(tempfile.mkdtemp()) / "live.db")
os.environ["SAGTRACK_LIVE_SYNC"] = "0"

from inlog_agent.live_db import conectar, gravar_rotas
from inlog_agent.live_report import _resumo
from inlog_agent.live_sync import ciclo_gps, ler_painel, reconciliar_rotas_previstas
from inlog_agent.unificado import obter_rotas_previstas

SAIDA = Path(tempfile.mkdtemp())
DIA = "2026-10-05"
NOITE = datetime(2026, 10, 5, 19, 0, 0)
ANTES = datetime(2026, 10, 5, 18, 59, 59)


def sem_gps(*_args, **_kwargs):
    return []


def estado(data, turno):
    return ler_painel(SAIDA, data, turno)


def linhas(data, turno):
    painel = estado(data, turno)
    return {rota["codigo"]: rota for rota in painel["rotas"]}, painel


def test_programacao_segunda():
    diurno = [r["codigo"] for r in obter_rotas_previstas(datetime(2026, 10, 5).date(), "diurno")]
    noturno = [r["codigo"] for r in obter_rotas_previstas(datetime(2026, 10, 5).date(), "noturno")]
    assert diurno == [f"{n:02d}AD" for n in range(1, 20)]
    assert noturno == [f"{n:02d}DN" for n in range(60, 67)] + [f"{n:02d}AN" for n in range(67, 77)]
    assert "66DN" in noturno
    assert set(diurno).isdisjoint(noturno)


def test_antes_das_19_nao_abre_programacao():
    ciclo_gps(SAIDA, DIA, "noturno", agora=ANTES, buscar_historico=sem_gps, descobrir=sem_gps)
    rotas, painel = linhas(DIA, "noturno")
    assert rotas == {}
    assert painel["resumo"]["previstas"] == 0


def test_noturno_antes_do_gps():
    ciclo_gps(SAIDA, DIA, "noturno", agora=NOITE, buscar_historico=sem_gps, descobrir=sem_gps)
    rotas, painel = linhas(DIA, "noturno")
    assert painel["situacao"] == "ABERTO"
    assert painel["resumo"]["previstas"] == 17
    assert painel["resumo"]["nao_iniciadas"] == 17
    assert painel["resumo"]["passagem"] == 0
    assert painel["resumo"]["percentual_geral"] == 0
    assert set(rotas) == set(obter_rotas_previstas(NOITE.date(), "noturno") and [r["codigo"] for r in obter_rotas_previstas(NOITE.date(), "noturno")])
    for rota in rotas.values():
        assert rota["percentual"] == 0
        assert rota["status"] == "Não iniciada"
        assert rota["na_grade"] is True
        assert rota["veiculo"] in {"", "—"}
    assert rotas["66DN"]["rota"].startswith("66DN")


def test_gps_da_66dn_nao_apaga_as_outras():
    con = conectar()
    gravar_rotas(
        con,
        DIA,
        "noturno",
        [
            {
                "codigo": "66DN",
                "rota": "66DN - CENTRO",
                "frequencia": "DIARIO - NOTURNO",
                "veiculo": "VW/17.260 CRM 4X2 4P",
                "placa": "SCH-9B28",
                "status": "Só passagem",
                "grupo": "passagem",
                "percentual": 4,
                "inicio": "05/10 19:02",
                "ultimo_gps": "05/10 19:07",
                "na_grade": True,
                "_geo": True,
                "entrada_hora": "19:02",
            }
        ],
    )
    con.commit()
    segunda = reconciliar_rotas_previstas(con, NOITE.date(), "noturno")
    con.commit()
    con.close()
    rotas, painel = linhas(DIA, "noturno")
    assert segunda["inseridas"] == []
    assert segunda["extras"] == []
    assert rotas["66DN"]["percentual"] == 4
    assert rotas["66DN"]["placa"] == "SCH-9B28"
    assert rotas["66DN"]["entrada_hora"] == "19:02"
    assert painel["resumo"]["previstas"] == 17
    assert painel["resumo"]["passagem"] == 1
    assert painel["resumo"]["nao_iniciadas"] == 16
    assert painel["resumo"]["percentual_geral"] == round(4 / 17)
    outras = [rota for codigo, rota in rotas.items() if codigo != "66DN"]
    assert len(outras) == 16
    assert all(rota["percentual"] == 0 and rota["status"] == "Não iniciada" for rota in outras)


def test_rota_extra_nao_entra_nas_previstas_e_nao_e_apagada():
    con = conectar()
    gravar_rotas(
        con,
        DIA,
        "noturno",
        [
            {
                "codigo": "99ZZ",
                "rota": "99ZZ - OBSERVADA",
                "frequencia": "",
                "status": "Só passagem",
                "grupo": "passagem",
                "percentual": 9,
                "na_grade": False,
            }
        ],
    )
    con.commit()
    info = reconciliar_rotas_previstas(con, NOITE.date(), "noturno")
    con.commit()
    con.close()
    rotas, painel = linhas(DIA, "noturno")
    assert "99ZZ" in rotas
    assert info["extras"] == ["99ZZ"]
    assert painel["resumo"]["previstas"] == 17
    assert rotas["66DN"]["percentual"] == 4


def test_diurno_nao_muda():
    previstos = obter_rotas_previstas(NOITE.date(), "diurno")
    con = conectar()
    gravar_rotas(
        con,
        DIA,
        "diurno",
        [
            {
                "codigo": rota["codigo"],
                "rota": rota["rotulo"],
                "frequencia": rota["frequencia"],
                "status": "Concluída",
                "grupo": "concluida",
                "percentual": 80 + (indice % 5),
                "na_grade": True,
                "veiculo": "CAMINHAO",
                "placa": f"DIA-{indice:02d}",
            }
            for indice, rota in enumerate(previstos)
        ],
    )
    con.commit()
    con.close()
    escritos = {rota["codigo"]: rota for rota in previstos}
    ciclo_gps(SAIDA, DIA, "diurno", agora=datetime(2026, 10, 5, 10, 0, 0), buscar_historico=sem_gps, descobrir=sem_gps)
    depois = linhas(DIA, "diurno")[0]
    assert list(depois) == [f"{n:02d}AD" for n in range(1, 20)]
    for indice, rota in enumerate(previstos):
        assert depois[rota["codigo"]]["percentual"] == 80 + (indice % 5)
        assert depois[rota["codigo"]]["placa"] == f"DIA-{indice:02d}"
    antes = {codigo: {"percentual": 80 + (indice % 5)} for indice, codigo in enumerate(escritos)}
    painel = estado(DIA, "diurno")
    assert painel["resumo"]["previstas"] == 19
    ciclo_gps(SAIDA, DIA, "diurno", agora=datetime(2026, 10, 5, 19, 10, 0), buscar_historico=sem_gps, descobrir=sem_gps)
    finalizando = estado(DIA, "diurno")
    assert finalizando["situacao"] == "FINALIZANDO"
    assert finalizando["resumo"]["previstas"] == 19
    assert linhas(DIA, "diurno")[0]["01AD"]["percentual"] == antes["01AD"]["percentual"]


def test_seis_da_manha_carrega_o_diurno_novo():
    quando = datetime(2026, 10, 6, 6, 0, 0)
    esperadas = [rota["codigo"] for rota in obter_rotas_previstas(quando.date(), "diurno")]
    assert esperadas and set(esperadas).isdisjoint({f"{n:02d}AD" for n in range(1, 20)})
    ciclo_gps(SAIDA, "2026-10-06", "diurno", agora=quando, buscar_historico=sem_gps, descobrir=sem_gps)
    rotas, painel = linhas("2026-10-06", "diurno")
    assert painel["situacao"] == "ABERTO"
    assert list(rotas) == esperadas
    assert painel["resumo"]["previstas"] == len(esperadas)
    assert all(rota["percentual"] == 0 and rota["status"] == "Não iniciada" for rota in rotas.values())
    assert linhas(DIA, "noturno")[0]["66DN"]["percentual"] == 4


def test_encerrado_nao_recebe_programacao():
    con = conectar()
    con.execute(
        "UPDATE turno SET situacao = 'ENCERRADO', aberto = 0 WHERE data = ? AND turno = 'noturno'",
        (DIA,),
    )
    con.commit()
    antes = len(linhas(DIA, "noturno")[0])
    con.close()
    ciclo_gps(SAIDA, DIA, "noturno", agora=datetime(2026, 10, 6, 7, 0, 0), buscar_historico=sem_gps, descobrir=sem_gps)
    assert len(linhas(DIA, "noturno")[0]) == antes
    assert estado(DIA, "noturno")["situacao"] == "ENCERRADO"


def test_idempotente():
    con = conectar()
    primeira = reconciliar_rotas_previstas(con, NOITE.date(), "noturno")
    segunda = reconciliar_rotas_previstas(con, NOITE.date(), "noturno")
    con.close()
    assert primeira["inseridas"] == []
    assert segunda["inseridas"] == []
    assert linhas(DIA, "noturno")[0]["66DN"]["percentual"] == 4


if __name__ == "__main__":
    test_programacao_segunda()
    test_antes_das_19_nao_abre_programacao()
    test_noturno_antes_do_gps()
    test_gps_da_66dn_nao_apaga_as_outras()
    test_rota_extra_nao_entra_nas_previstas_e_nao_e_apagada()
    test_diurno_nao_muda()
    test_seis_da_manha_carrega_o_diurno_novo()
    test_encerrado_nao_recebe_programacao()
    test_idempotente()
    print("ok")
