from __future__ import annotations

import argparse
import getpass
import os
import sys

from .config import aplicar_periodo, load_settings, rotulo_periodo
from .server import servir


def cmd_rotas(
    headed: bool | None,
    data: str | None,
    veiculos: str | None,
    hora_inicio: str | None = None,
    hora_fim: str | None = None,
    data_fim: str | None = None,
) -> None:
    settings = aplicar_periodo(load_settings(), data=data, hora_inicio=hora_inicio, hora_fim=hora_fim, data_fim=data_fim)
    if veiculos:
        settings = type(settings)(**{
            **settings.__dict__,
            "filtro_veiculos": tuple(item.strip() for item in veiculos.split(",") if item.strip()),
        })

    from .rotas import executar_rotas

    print(
        f"Extraindo rotas de {rotulo_periodo(settings.data_rota, settings.hora_inicio, settings.hora_fim, settings.data_fim)}...",
        flush=True,
    )
    relatorio = executar_rotas(settings, headed=headed)
    print("Coleta de rotas concluída.", flush=True)
    print(f"Data: {relatorio.get('data')}", flush=True)
    print(f"Veículos: {relatorio.get('total_veiculos')}", flush=True)
    print(f"Pontos GPS: {relatorio.get('total_pontos')}", flush=True)
    if relatorio.get("percentual_medio") is not None:
        print(f"% médio concluído: {relatorio.get('percentual_medio')}")
    arquivos = relatorio.get("arquivos") or {}
    if arquivos.get("mapa"):
        print(f"Mapa: {arquivos['mapa']}")
    if arquivos.get("csv"):
        print(f"CSV: {arquivos['csv']}")
    if arquivos.get("resumo"):
        print(f"JSON: {arquivos['resumo']}")
    if arquivos.get("pdf"):
        print(f"PDF: {arquivos['pdf']}")
    if relatorio["total_pontos"] == 0:
        print("Nenhum ponto GPS nesta sessão. Confira o .env e a internet.")


def cmd_segtrack_rotas(
    headed: bool | None,
    data: str | None,
    hora_inicio: str | None = None,
    hora_fim: str | None = None,
    data_fim: str | None = None,
) -> None:
    from .segtrack_rotas import executar_segtrack

    settings = aplicar_periodo(load_settings(), data=data, hora_inicio=hora_inicio, hora_fim=hora_fim, data_fim=data_fim)
    print(
        f"Extraindo SEGTRACK de {rotulo_periodo(settings.data_rota, settings.hora_inicio, settings.hora_fim, settings.data_fim)}...",
        flush=True,
    )
    relatorio = executar_segtrack(settings, headed=headed)
    print("Coleta SEGTRACK concluída.", flush=True)
    print(f"Obra: {relatorio.get('obra')}", flush=True)
    print(f"Veículos: {relatorio.get('total_veiculos')}", flush=True)
    print(f"Pontos GPS: {relatorio.get('total_pontos')}", flush=True)
    arquivos = relatorio.get("arquivos") or {}
    if arquivos.get("relatorio"):
        print(f"Relatório: {arquivos['relatorio']}")
    if arquivos.get("mapa"):
        print(f"Mapa: {arquivos['mapa']}")


def cmd_argos_rotas(
    headed: bool | None,
    data: str | None,
    hora_inicio: str | None = None,
    hora_fim: str | None = None,
    data_fim: str | None = None,
) -> None:
    from .argos_rotas import executar_argos

    settings = aplicar_periodo(load_settings(), data=data, hora_inicio=hora_inicio, hora_fim=hora_fim, data_fim=data_fim)
    print(
        f"Extraindo Argos de {rotulo_periodo(settings.data_rota, settings.hora_inicio, settings.hora_fim, settings.data_fim)}...",
        flush=True,
    )
    relatorio = executar_argos(settings, headed=headed)
    print("Coleta Argos concluída.", flush=True)
    print(f"Obra: {relatorio.get('obra')}", flush=True)
    print(f"Veículos: {relatorio.get('total_veiculos')}", flush=True)
    print(f"Pontos GPS: {relatorio.get('total_pontos')}", flush=True)
    print(f"Casados com Inlog: {relatorio.get('cruzados_inlog')}", flush=True)
    arquivos = relatorio.get("arquivos") or {}
    if arquivos.get("relatorio"):
        print(f"Relatório: {arquivos['relatorio']}")
    if arquivos.get("mapa"):
        print(f"Mapa: {arquivos['mapa']}")


def cmd_criar_admin() -> None:
    """Cria só o primeiro administrador. A senha não entra na linha de comando nem no código."""
    from .acesso import ErroAcesso, criar_primeiro_admin

    login = (os.getenv("AUTH_ADMIN_USUARIO") or "").strip()
    senha = os.getenv("AUTH_ADMIN_SENHA") or ""
    nome = (os.getenv("AUTH_ADMIN_NOME") or "").strip()
    if not login:
        if not sys.stdin.isatty():
            print(
                "Defina AUTH_ADMIN_USUARIO e AUTH_ADMIN_SENHA no ambiente e rode de novo. "
                "Não passe a senha como argumento.",
                flush=True,
            )
            raise SystemExit(1)
        login = input("Usuário ou e-mail do administrador: ").strip()
    if not senha:
        if not sys.stdin.isatty():
            print("Defina AUTH_ADMIN_SENHA no ambiente. Não passe a senha como argumento.", flush=True)
            raise SystemExit(1)
        senha = getpass.getpass("Senha do administrador: ")
        confirma = getpass.getpass("Repita a senha: ")
        if senha != confirma:
            print("As senhas não conferem.", flush=True)
            raise SystemExit(1)
    try:
        criado = criar_primeiro_admin(login, senha, nome)
    except ErroAcesso as exc:
        print(exc.mensagem, flush=True)
        raise SystemExit(1) from exc
    print(f"Administrador {criado['login']} criado.", flush=True)
    print("A senha não foi gravada em arquivo nem impressa.", flush=True)
    print("O login passa a ser exigido porque já existe um administrador ativo.", flush=True)


def cmd_frota_mapear(aplicar: bool) -> None:
    from .frota import aplicar_mapeamento, mapear_operacoes, salvar_mapeamento_csv

    print("Mapeando a frota pelos pulsos dos últimos 7 dias...", flush=True)
    try:
        resultado = mapear_operacoes()
    except Exception as exc:
        print(str(exc), flush=True)
        raise SystemExit(1) from exc
    caminho = salvar_mapeamento_csv(resultado)
    _imprimir_mapeamento(resultado, caminho)
    if not aplicar:
        print("Simulação: nenhuma operação nem placa foi gravada.", flush=True)
        return
    try:
        gravado = aplicar_mapeamento(resultado, ajustes=True)
    except Exception as exc:
        print(str(exc), flush=True)
        raise SystemExit(1) from exc
    print(f"Operações criadas: {len(gravado.get('criadas') or [])}", flush=True)
    for nome in gravado.get("criadas") or []:
        print(f"  + {nome}", flush=True)
    print(f"Placas fixadas: {len(gravado.get('fixadas') or [])}", flush=True)
    print(f"Vínculos manuais preservados: {len(gravado.get('preservadas') or [])}", flush=True)
    for placa in gravado.get("preservadas") or []:
        print(f"  preservada {placa}", flush=True)


def cmd_frota_importar(arquivo: str | None) -> None:
    from .frota import importar_operacoes_aprovadas, linha_import

    try:
        load_settings()
    except Exception:
        pass
    try:
        resultado = importar_operacoes_aprovadas(arquivo, exigir_arquivo=True)
    except Exception as exc:
        print(f"Frota import: {exc}", flush=True)
        raise SystemExit(1) from exc
    if not resultado.get("aplicado"):
        return
    print(linha_import(resultado), flush=True)


def _imprimir_mapeamento(resultado: dict, caminho: str) -> None:
    verificacao = resultado.get("verificacao") or {}
    print(f"Veículos: {verificacao.get('total_veiculos', 0)}", flush=True)
    print(f"Com pulso nos últimos 7 dias: {verificacao.get('com_pulso', 0)}", flush=True)
    print(f"Sem pulso: {verificacao.get('sem_pulso', 0)}", flush=True)
    print(f"Sem posição válida: {verificacao.get('sem_posicao_valida', 0)}", flush=True)
    latlon = "sim" if verificacao.get("latlon_em_todos_os_pulsos") else "não"
    print(
        f"Lat/lon em todos os pulsos: {latlon} "
        f"(eventos sem coordenada: {verificacao.get('eventos_sem_latlon', 0)}; "
        f"histórico sem coordenada: {verificacao.get('historico_sem_latlon', 0)})",
        flush=True,
    )
    print("Resumo por operação:", flush=True)
    for item in resultado.get("resumo") or []:
        print(f"  {item.get('operacao')}: {item.get('qtd')}", flush=True)
    print(f"Revisar ({len(resultado.get('revisar') or [])}):", flush=True)
    for item in resultado.get("revisar") or []:
        print(
            f"  {item.get('placa')} | base {item.get('cidade_da_base') or '—'} | "
            f"sugestão {item.get('operacao_sugerida') or '—'}",
            flush=True,
        )
    print(f"Não associado ({len(resultado.get('nao_associado') or [])}):", flush=True)
    for item in resultado.get("nao_associado") or []:
        print(
            f"  {item.get('placa')} | base {item.get('cidade_da_base') or '—'} | "
            f"{item.get('base_lat')}, {item.get('base_lon')}",
            flush=True,
        )
    print(f"Sem dados: {len(resultado.get('sem_dados') or [])}", flush=True)
    print(f"CSV: {caminho}", flush=True)


def cmd_realocar_horario(data: str, aplicar: bool) -> None:
    import json

    from .live_sync import realocar_pontos_pelo_horario

    resultado = realocar_pontos_pelo_horario(
        load_settings().output_dir,
        data,
        aplicar=bool(aplicar),
    )
    print(json.dumps(resultado, ensure_ascii=False, default=str), flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inlog_agent",
        description="Agente que entra no rastreamento Inlog e exporta rotas dos caminhões.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Abre o Chromium na tela (recomendado na primeira execução).",
    )
    sub = parser.add_subparsers(dest="comando", required=True)

    def add_periodo(p):
        p.add_argument("--hora-inicio", default=None, help="Hora inicial HH:MM (padrão 00:00)")
        p.add_argument("--hora-fim", default=None, help="Hora final HH:MM (padrão 23:59)")
        p.add_argument("--data-fim", default=None, help="Data final YYYY-MM-DD se o ciclo atravessa o dia")

    rotas = sub.add_parser("rotas", help="Extrai a rota do dia dos caminhões de coleta")
    rotas.add_argument("--data", help="Data no formato YYYY-MM-DD (padrão: hoje)")
    rotas.add_argument(
        "--veiculos",
        help="Prefixos/placas separados por vírgula. Ex.: 101,102,ABC1D23",
    )
    add_periodo(rotas)

    web = sub.add_parser("servir", help="Sobe o painel web local")
    web.add_argument("--porta", type=int, help="Porta HTTP (padrão: 5889)")
    web.add_argument("--host", default=None, help="Host de bind (padrão: HOST do .env ou 127.0.0.1)")

    pdf = sub.add_parser("pdf", help="Gera PDF do dia com rotas feitas e mapa desenhado")
    pdf.add_argument("--data", required=True, help="Data no formato YYYY-MM-DD")
    pdf.add_argument("--argos", action="store_true", help="Gera o PDF do relatório Argos")
    pdf.add_argument("--segtrack", action="store_true", help="Gera o PDF do relatório SEGTRACK")
    pdf.add_argument("--unificado", action="store_true", help="Gera o PDF da frota e rotas do dia (3 empresas)")
    pdf.add_argument("--turno", choices=["diurno", "noturno"], default=None)

    argos = sub.add_parser("argos-rotas", help="Extrai a frota e o rastreio do Argos")
    argos.add_argument("--data", help="Data no formato YYYY-MM-DD (padrão: hoje)")
    add_periodo(argos)
    gpkg = sub.add_parser("argos-gpkg", help="Exporta o percurso Argos do dia em GeoPackage")
    gpkg.add_argument("--data", required=True, help="Data no formato YYYY-MM-DD")
    gpkg_inlog = sub.add_parser("gpkg", help="Exporta as rotas Inlog do dia em GeoPackage")
    gpkg_inlog.add_argument("--data", required=True, help="Data no formato YYYY-MM-DD")
    seg = sub.add_parser("segtrack-rotas", help="Extrai compactadores SEGTRACK em Aparecida")
    seg.add_argument("--data", help="Data no formato YYYY-MM-DD (padrão: hoje)")
    add_periodo(seg)
    gpkg_seg = sub.add_parser("segtrack-gpkg", help="Exporta o percurso SEGTRACK do dia em GeoPackage")
    gpkg_seg.add_argument("--data", required=True, help="Data no formato YYYY-MM-DD")
    uni = sub.add_parser("unificado", help="Coleta do dia: login nas 3 empresas, mapa e relatório")
    uni.add_argument("--data", help="Data no formato YYYY-MM-DD (padrão: hoje)")
    add_periodo(uni)
    uni.add_argument("--extrair", action="store_true", help="Faz login e puxa o GPS das 3 empresas antes do relatório")
    uni.add_argument("--so-relatorio", action="store_true", help="Só monta o relatório com o GPS já extraído")
    uni.add_argument(
        "--turno",
        choices=["diurno", "noturno"],
        help="Janela operacional do turno. Sábado: diurno 05:00–14:59 e noturno 15:00–03:00",
    )
    sub.add_parser(
        "criar-admin",
        help="Cria o primeiro administrador. A senha vem de AUTH_ADMIN_SENHA ou é digitada, nunca de um argumento.",
    )
    realocar = sub.add_parser(
        "realocar-horario",
        help="Mostra ou grava a realocação dos pulsos para o turno do horário",
    )
    realocar.add_argument("--data", required=True, help="Data operacional YYYY-MM-DD")
    realocar.add_argument(
        "--aplicar",
        action="store_true",
        help="Grava a realocação. Sem esta opção, só mostra a prévia.",
    )
    mapa = sub.add_parser(
        "frota-mapear",
        help="Associa cada placa à operação pela base dos últimos 7 dias",
    )
    modo = mapa.add_mutually_exclusive_group()
    modo.add_argument(
        "--simular",
        action="store_true",
        help="Não grava operações nem placas (padrão). Gera o CSV de revisão.",
    )
    modo.add_argument(
        "--aplicar",
        action="store_true",
        help="Cria as operações que faltam e fixa as placas de situação associado.",
    )
    imp = sub.add_parser(
        "frota-importar",
        help="Importa o snapshot aprovado de operações e placas fixadas",
    )
    imp.add_argument(
        "--arquivo",
        default=None,
        help="JSON do snapshot (padrão: data/frota_operacoes_aprovado.json)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    headed = True if args.headed else None

    if args.comando == "rotas":
        cmd_rotas(
            headed,
            getattr(args, "data", None),
            getattr(args, "veiculos", None),
            getattr(args, "hora_inicio", None),
            getattr(args, "hora_fim", None),
            getattr(args, "data_fim", None),
        )
        return
    if args.comando == "servir":
        servir(porta=args.porta, host=args.host)
        return
    if args.comando == "criar-admin":
        cmd_criar_admin()
        return
    if args.comando == "realocar-horario":
        cmd_realocar_horario(args.data, bool(args.aplicar))
        return
    if args.comando == "frota-mapear":
        cmd_frota_mapear(bool(getattr(args, "aplicar", False)))
        return
    if args.comando == "frota-importar":
        cmd_frota_importar(getattr(args, "arquivo", None))
        return
    if args.comando == "pdf":
        from .pdf import gerar_pdf_dia

        settings = load_settings()
        if args.argos:
            pasta = settings.output_dir / "argos" / args.data
        elif args.segtrack:
            pasta = settings.output_dir / "segtrack" / args.data
        elif args.unificado:
            from .pdf_cache import resolver_trabalho

            pasta = resolver_trabalho(settings.output_dir / "unificado" / args.data, getattr(args, "turno", None))
        else:
            pasta = settings.output_dir / "rotas" / args.data
        caminho = gerar_pdf_dia(pasta)
        print(f"PDF: {caminho}", flush=True)
        return
    if args.comando == "argos-rotas":
        cmd_argos_rotas(
            headed,
            getattr(args, "data", None),
            getattr(args, "hora_inicio", None),
            getattr(args, "hora_fim", None),
            getattr(args, "data_fim", None),
        )
        return
    if args.comando == "argos-gpkg":
        from .export_gpkg import gerar_gpkg_argos

        settings = load_settings()
        caminho = gerar_gpkg_argos(settings.output_dir / "argos" / args.data)
        print(f"GPKG: {caminho}", flush=True)
        return
    if args.comando == "gpkg":
        from .export_gpkg import gerar_gpkg_inlog

        settings = load_settings()
        caminho = gerar_gpkg_inlog(settings.output_dir / "rotas" / args.data)
        print(f"GPKG: {caminho}", flush=True)
        return
    if args.comando == "segtrack-rotas":
        cmd_segtrack_rotas(
            headed,
            getattr(args, "data", None),
            getattr(args, "hora_inicio", None),
            getattr(args, "hora_fim", None),
            getattr(args, "data_fim", None),
        )
        return
    if args.comando == "segtrack-gpkg":
        from .export_gpkg import gerar_gpkg_segtrack

        settings = load_settings()
        caminho = gerar_gpkg_segtrack(settings.output_dir / "segtrack" / args.data)
        print(f"GPKG: {caminho}", flush=True)
        return
    if args.comando == "unificado":
        settings = aplicar_periodo(
            load_settings(),
            data=args.data,
            hora_inicio=getattr(args, "hora_inicio", None),
            hora_fim=getattr(args, "hora_fim", None),
            data_fim=getattr(args, "data_fim", None),
        )
        from .unificado import executar_coleta_do_dia

        extrair = bool(args.extrair) and not bool(args.so_relatorio)
        relatorio = executar_coleta_do_dia(
            settings,
            extrair=extrair,
            turno=getattr(args, "turno", None),
        )
        print(f"Feitas: {relatorio.get('feitas')}", flush=True)
        print(f"Parciais: {relatorio.get('parciais')}", flush=True)
        print(f"Apoios: {relatorio.get('apoios')}", flush=True)
        print(f"Sem coleta: {relatorio.get('nao_feitas')}", flush=True)
        arquivos = relatorio.get("arquivos") or {}
        if arquivos.get("relatorio"):
            print(f"Relatório: {arquivos['relatorio']}", flush=True)
        if arquivos.get("mapa"):
            print(f"Mapa: {arquivos['mapa']}", flush=True)
        if arquivos.get("pdf"):
            print(f"PDF: {arquivos['pdf']}", flush=True)
        falhas = relatorio.get("falhas") or {}
        if falhas:
            print("Falhas: " + " · ".join(f"{k}: {v}" for k, v in falhas.items()), flush=True)
        return
    parser.error("Comando inválido")
