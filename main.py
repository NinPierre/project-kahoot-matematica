import html
import json
import math
import os
import random
import re
import string
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen
# essa blibliote e melhor para traduzir
from deep_translator import GoogleTranslator
from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_socketio import SocketIO, emit, join_room, leave_room

#essa aplicaçao vai para o render
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
if os.environ.get("RENDER"):
    app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
socketio = SocketIO(app, async_mode="threading")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
QUESTOES_PATH = STATIC_DIR / "questoes.json"
QUESTOES_CRIADAS_PATH = STATIC_DIR / "questoes_criadas.json"
PERSONAGENS_DIR = STATIC_DIR / "imagens" / "personagens"
AVATAR_PADRAO = "imagens/personagens/avatar_padrao.svg"
SEGUNDOS_RESULTADO = 6
SEGUNDOS_HOST_DESCONECTADO = 18

DIFICULDADES = {
    "todas": "Todas",
    "facil": "Fácil",
    "medio": "Médio",
    "dificil": "Difícil",
}
DIFICULDADES_ESCOLHA = {
    "facil": "Fácil",
    "medio": "Médio",
    "dificil": "Difícil",
}

# serve para a api.
SERIES = [
    "1º ano",
    "2º ano",
    "3º ano",
    "4º ano",
    "5º ano",
    "6º ano",
    "7º ano",
    "8º ano",
    "9º ano",
    "Ensino médio",
]
SERIES_ORDEM = {serie: indice for indice, serie in enumerate(SERIES, start=1)}

USUARIOS = {
    os.environ.get("ADMIN_USER", "tito"): os.environ.get("ADMIN_PASSWORD", "123"),
}

salas = {}
sockets_por_sid = {}
tradutor_api = None


def garantir_arquivos():
    QUESTOES_CRIADAS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not QUESTOES_CRIADAS_PATH.exists():
        QUESTOES_CRIADAS_PATH.write_text("[]\n", encoding="utf-8")


def carregar_json(caminho, padrao):
    if not caminho.exists():
        return padrao

    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return padrao


def salvar_json(caminho, dados):
    caminho.write_text(
        json.dumps(dados, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def check_login(username, password):
    return USUARIOS.get(username) == password


def token_jogador(token_informado=None):
    # Cada aba pode ter um token próprio
    token = (token_informado or "").strip() or session.get("player_token")
    if not token:
        token = uuid.uuid4().hex
        session["player_token"] = token
    return token


def token_da_requisicao(data=None):
    # O token pode vir do formulário da URl do header ou do Socket.IO
    if data and data.get("player_token"):
        return str(data.get("player_token")).strip()
    return (
        request.values.get("player_token")
        or request.headers.get("X-Player-Token")
        or session.get("player_token")
    )


def gerar_codigo_sala():
    alfabeto = string.ascii_uppercase + string.digits
    while True:
        codigo = "".join(random.choice(alfabeto) for _ in range(6))
        if codigo not in salas:
            return codigo


def normalizar_dificuldade(valor):
    valor = (valor or "todas").strip().lower()
    if valor in DIFICULDADES:
        return valor
    if valor == "medium":
        return "medio"
    if valor == "easy":
        return "facil"
    if valor == "hard":
        return "dificil"
    return "todas"

#serve para na repeti!!
def normalizar_dificuldade_escolha(valor):
    dificuldade = normalizar_dificuldade(valor)
    return dificuldade if dificuldade in DIFICULDADES_ESCOLHA else "facil"


def label_dificuldade(valor):
    return DIFICULDADES.get(normalizar_dificuldade(valor), "Todas")


def dificuldades_permitidas(valor):
    dificuldade = normalizar_dificuldade(valor)
    if dificuldade == "facil":
        return {"facil"}
    if dificuldade == "medio":
        return {"facil", "medio"}
    if dificuldade == "dificil":
        return {"facil", "medio", "dificil"}
    return {"facil", "medio", "dificil"}


def indice_serie(valor):
    serie = str(valor or "").strip()
    if serie in SERIES_ORDEM:
        return SERIES_ORDEM[serie]

    serie_limpa = (
        serie.lower()
        .replace("é", "e")
        .replace("ê", "e")
        .replace("í", "i")
    )
    if "ensino" in serie_limpa and "medio" in serie_limpa:
        return SERIES_ORDEM["Ensino médio"]

    numero = re.search(r"\d+", serie_limpa)
    if numero:
        return max(1, min(SERIES_ORDEM["9º ano"], int(numero.group())))

    return None


def normalizar_serie(valor, padrao=None):
    serie = str(valor or "").strip()
    if serie in SERIES:
        return serie

    indice = indice_serie(serie)
    if indice:
        return SERIES[indice - 1]

    return padrao or SERIES[0]

#as questoes dos nossos colegas
def serie_coringa(valor):
    return not str(valor or "").strip() or str(valor or "").strip() == "Geral"


def pergunta_permitida_na_serie(pergunta, serie_sala):
    serie_pergunta = pergunta.get("serie")
    if serie_coringa(serie_pergunta):
        return True

    ordem_sala = indice_serie(normalizar_serie(serie_sala)) or 1
    ordem_pergunta = indice_serie(serie_pergunta)
    if ordem_pergunta is None:
        return True
    return ordem_pergunta <= ordem_sala

# nao repeti na salas criadas
def filtrar_perguntas_locais(perguntas, dificuldade, serie):
    permitidas = dificuldades_permitidas(dificuldade)
    return [
        pergunta for pergunta in perguntas
        if pergunta.get("dificuldade") in permitidas
        and pergunta_permitida_na_serie(pergunta, serie)
    ]


def normalizar_pergunta(item, fonte, prefixo):
    pergunta = str(item.get("pergunta", "")).strip()
    resposta = str(
        item.get("respostaCorreta") or item.get("resposta_correta") or ""
    ).strip()
    opcoes = [
        str(opcao).strip()
        for opcao in item.get("opcoes", [])
        if str(opcao).strip()
    ]

    if resposta and resposta not in opcoes:
        opcoes.append(resposta)

    opcoes_unicas = []
    for opcao in opcoes:
        if opcao not in opcoes_unicas:
            opcoes_unicas.append(opcao)

    if not pergunta or not resposta or len(opcoes_unicas) < 2:
        return None

    pergunta_id = str(item.get("id") or uuid.uuid4().hex[:8])
    return {
        "id": pergunta_id,
        "uid": f"{prefixo}:{pergunta_id}",
        "pergunta": pergunta,
        "opcoes": opcoes_unicas,
        "respostaCorreta": resposta,
        "dificuldade": normalizar_dificuldade(item.get("dificuldade", "todas")),
        "dificuldadeLabel": label_dificuldade(item.get("dificuldade", "todas")),
        "serie": str(item.get("serie") or "Geral").strip() or "Geral",
        "fonte": fonte,
        "prefixo": prefixo,
    }


def carregar_perguntas_base():
    perguntas = []
    for item in carregar_json(QUESTOES_PATH, []):
        normalizada = normalizar_pergunta(item, "Banco principal", "base")
        if normalizada:
            perguntas.append(normalizada)
    return perguntas


def carregar_perguntas_criadas():
    perguntas = []
    for item in carregar_json(QUESTOES_CRIADAS_PATH, []):
        normalizada = normalizar_pergunta(item, "Criada no dashboard", "criadas")
        if normalizada:
            perguntas.append(normalizada)
    return perguntas


def carregar_todas_perguntas():
    return carregar_perguntas_criadas() + carregar_perguntas_base()

#aqui e ap arti da traduçao
def traduzir_com_deep_translator(texto):
    global tradutor_api

    texto = html.unescape(str(texto or "").strip())
    if not texto:
        return texto

    try:
        if tradutor_api is None:
            tradutor_api = GoogleTranslator(source="en", target="pt")
        traducao = tradutor_api.translate(texto[:450])
    except Exception:
        # Se o tradutor externo falhar, a pergunta ainda aparece no idioma original.
        return texto

    return html.unescape(str(traducao or texto).strip())


def buscar_perguntas_opentdb(quantidade, dificuldade):
    if quantidade <= 0:
        return []

    dificuldades_api = {
        "facil": "easy",
        "medio": "medium",
        "dificil": "hard",
    }
    permitidas = [
        dificuldades_api[item]
        for item in sorted(dificuldades_permitidas(dificuldade))
        if item in dificuldades_api
    ]
    dificuldade_api = random.choice(permitidas) if permitidas else None

    parametros = {
        "amount": min(quantidade, 50),
        "category": 19,
        "type": "multiple",
    }
    if dificuldade_api:
        parametros["difficulty"] = dificuldade_api

    # api
    url = "https://opentdb.com/api.php?" + urlencode(parametros)

    try:
        with urlopen(url, timeout=5) as resposta:
            payload = json.loads(resposta.read().decode("utf-8"))
    except Exception:
        return []

    if payload.get("response_code") != 0:
        return []

    perguntas = []
    for indice, item in enumerate(payload.get("results", []), start=1):
        correta = html.unescape(item.get("correct_answer", ""))
        opcoes = [html.unescape(opcao) for opcao in item.get("incorrect_answers", [])]
        opcoes.append(correta)
        random.shuffle(opcoes)
        traducoes = {
            texto: traduzir_com_deep_translator(texto)
            for texto in set(opcoes + [correta])
        }
        perguntas.append(
            {
                "id": f"api-{int(time.time())}-{indice}",
                "uid": f"api:{int(time.time())}-{indice}",
                "pergunta": traduzir_com_deep_translator(item.get("question", "")),
                "opcoes": [traducoes[opcao] for opcao in opcoes],
                "respostaCorreta": traducoes[correta],
                "dificuldade": normalizar_dificuldade(item.get("difficulty")),
                "dificuldadeLabel": label_dificuldade(item.get("difficulty")),
                "serie": "OpenTDB",
                "fonte": "Open Trivia DB",
                "prefixo": "api",
            }
        )
    return perguntas


def sincronizar_historico_json(container, historico):
    if container is None:
        return
    if isinstance(container, set):
        container.clear()
        container.update(historico)
        return
    container[:] = sorted(historico)


def selecionar_perguntas_json_sem_repetir(
    perguntas,
    quantidade,
    historico,
    ja_selecionadas=None,
):
    selecionadas = []
    bloqueadas = set(ja_selecionadas or set())
    historico = set(historico or set())

    while len(selecionadas) < quantidade and perguntas:
        disponiveis = [
            pergunta for pergunta in perguntas
            if pergunta["uid"] not in historico
            and pergunta["uid"] not in bloqueadas
        ]

        if not disponiveis:
            # Quando acaba o ciclo, libera o banco local de novo.
            historico.clear()
            disponiveis = [
                pergunta for pergunta in perguntas
                if pergunta["uid"] not in bloqueadas
            ]
            if not disponiveis:
                break

        random.shuffle(disponiveis)
        escolhida = disponiveis[0]
        selecionadas.append(escolhida)
        bloqueadas.add(escolhida["uid"])
        historico.add(escolhida["uid"])

    return selecionadas, historico

#modos
def calcular_quantidade_api(quantidade, api_frequente=False, api_desativada=False):
    if api_desativada:
        
        return 0

    if api_frequente:
        
        return 0 if quantidade <= 1 else max(1, round(quantidade * 0.5))

    
    return 0 if quantidade <= 3 else max(1, round(quantidade * 0.2))


def aplicar_rodadas_bonus_x2(perguntas, chance):
    chance = max(0, min(100, int(chance or 0)))
    for pergunta in perguntas:
        # Cada pergunta decide sozinha se vira bônus.
        pergunta["bonus_x2"] = random.randint(1, 100) <= chance
    return perguntas


def sortear_perguntas(
    quantidade,
    dificuldade,
    serie=None,
    api_frequente=False,
    api_desativada=False,
    ids_json_usados=None,
):
    locais = filtrar_perguntas_locais(
        carregar_todas_perguntas(),
        dificuldade,
        normalizar_serie(serie),
    )

    random.shuffle(locais)

    qtd_api = calcular_quantidade_api(quantidade, api_frequente, api_desativada)
    qtd_local = max(0, quantidade - qtd_api)
    historico_json = set(ids_json_usados or set())
    selecionadas, historico_json = selecionar_perguntas_json_sem_repetir(
        locais,
        qtd_local,
        historico_json,
    )

    if len(selecionadas) < qtd_local and not api_desativada:
        qtd_api += qtd_local - len(selecionadas)

    if not api_desativada:
        selecionadas.extend(
            buscar_perguntas_opentdb(quantidade - len(selecionadas), dificuldade)
        )

    if len(selecionadas) < quantidade:
        ids_partida = {
            pergunta["uid"] for pergunta in selecionadas
            if pergunta.get("prefixo") != "api"
        }
        extras, historico_json = selecionar_perguntas_json_sem_repetir(
            locais,
            quantidade - len(selecionadas),
            historico_json,
            ids_partida,
        )
        selecionadas.extend(extras)

    random.shuffle(selecionadas)
    sincronizar_historico_json(ids_json_usados, historico_json)
    return selecionadas[:quantidade]


def listar_avatares():
    extensoes = {".png", ".jpg", ".jpeg", ".webp", ".svg"}
    avatares = []
    if PERSONAGENS_DIR.exists():
        for caminho in sorted(PERSONAGENS_DIR.iterdir(), key=lambda p: p.name.lower()):
            if caminho.suffix.lower() in extensoes:
                avatares.append(f"imagens/personagens/{caminho.name}")

    if AVATAR_PADRAO in avatares:
        avatares.remove(AVATAR_PADRAO)
    return [AVATAR_PADRAO] + avatares


def sala_ou_404(codigo):
    sala = salas.get(codigo.upper())
    if not sala:
        abort(404)
    return sala


def jogador_atual(sala, token=None):
    return sala["jogadores"].get(token or token_da_requisicao())


def eh_dono(sala, token=None):
    return (token or token_da_requisicao()) == sala.get("dono_token")


def jogadores_ordenados(sala):
    return sorted(
        sala["jogadores"].values(),
        key=lambda jogador: (-jogador.get("pontos", 0), jogador.get("entrou_em", 0)),
    )


def jogador_publico(jogador):
    return {
        "nome": jogador["nome"],
        "avatar": jogador["avatar"],
        "pontos": jogador.get("pontos", 0),
        "ultimo_delta": jogador.get("ultimo_delta", 0),
        "respondeu": jogador.get("respondeu", False),
    }

#cofigs da salinha pesonalizada xtreino
def resumo_sala(sala):
    return {
        "codigo": sala["codigo"],
        "nome": sala["nome"],
        "dificuldade": sala["dificuldade"],
        "dificuldadeLabel": label_dificuldade(sala["dificuldade"]),
        "serie": sala["serie"],
        "max_jogadores": sala["max_jogadores"],
        "qtd_jogadores": len(sala["jogadores"]),
        "tempo_por_questao": sala["tempo_por_questao"],
        "rodadas": sala["rodadas"],
        "api_frequente": bool(sala.get("api_frequente")),
        "api_desativada": bool(sala.get("api_desativada")),
        "chance_x2": int(sala.get("chance_x2", 10)),
        "tem_senha": bool(sala.get("senha")),
        "status": sala["status"],
    }


def filtrar_salas_publicas(args):
    busca = args.get("busca", "").strip().lower()
    dificuldade = normalizar_dificuldade(args.get("dificuldade"))
    serie = args.get("serie", "todas")
    max_jogadores = args.get("max_jogadores", "todos")

    try:
        limite_jogadores = None if max_jogadores == "todos" else int(max_jogadores)
    except ValueError:
        limite_jogadores = None
        max_jogadores = "todos"

    salas_publicas = []
    for sala in salas.values():
        if sala["status"] != "espera":
            continue
        if len(sala["jogadores"]) >= sala["max_jogadores"]:
            continue

        resumo = resumo_sala(sala)
        texto_busca = f"{resumo['nome']} {resumo['codigo']}".lower()
        if busca and busca not in texto_busca:
            continue
        if dificuldade != "todas" and sala["dificuldade"] != dificuldade:
            continue
        if serie != "todas" and sala["serie"] != serie:
            continue
        if limite_jogadores and sala["max_jogadores"] > limite_jogadores:
            continue
        salas_publicas.append(resumo)

    return salas_publicas, {
        "busca": args.get("busca", ""),
        "dificuldade": dificuldade,
        "serie": serie,
        "max_jogadores": max_jogadores,
    }


def emitir_salas_atualizadas():
    # A home fica ouvindo esse evento
    socketio.emit("salas_atualizadas", {"ok": True})


def estado_espera(sala):
    return {
        **resumo_sala(sala),
        "jogadores": [jogador_publico(j) for j in jogadores_ordenados(sala)],
        "chat": sala.get("chat", [])[-40:],
    }


def emitir_estado_espera(codigo):
    sala = salas.get(codigo)
    if sala:
        socketio.emit("estado_espera", estado_espera(sala), to=codigo)

#aqui esxcluir a sala se o dono sair
def host_tem_socket_ativo(codigo, token):
    return any(
        info["codigo"] == codigo and info["token"] == token
        for info in sockets_por_sid.values()
    )


def excluir_sala_por_host(codigo, motivo=None):
    sala = salas.get(codigo)
    if not sala:
        return False


    mensagem = motivo or "A sala foi encerrada porque o host saiu."
    socketio.emit(
        "sala_excluida",
        {"codigo": codigo, "mensagem": mensagem},
        to=codigo,
    )
    salas.pop(codigo, None)
    emitir_salas_atualizadas()
    return True


def checar_host_desconectado(codigo, token):
    # Em celular lento isso pode demorar mas to com preguisa de fazer melhor (:
    socketio.sleep(SEGUNDOS_HOST_DESCONECTADO)
    sala = salas.get(codigo)
    if not sala:
        return
    if not host_tem_socket_ativo(codigo, token):
        if sala.get("dono_token") == token:
            excluir_sala_por_host(
                codigo,
                "A sala foi excluída porque o host desconectou.",
            )
            return
        if token in sala["jogadores"]:
            sala["jogadores"].pop(token)
            if not sala["jogadores"]:
                salas.pop(codigo, None)
            else:
                emitir_estado_espera(codigo)
            emitir_salas_atualizadas()


def segundos_restantes(sala):
    if sala["status"] != "jogando":
        return 0
    gasto = time.time() - sala.get("rodada_inicio", time.time())
    return max(0, math.ceil(sala["tempo_por_questao"] - gasto))


def pergunta_atual(sala):
    perguntas = sala.get("perguntas", [])
    indice = sala.get("rodada_atual", 0)
    if not perguntas or indice >= len(perguntas):
        return None
    return perguntas[indice]


def calcular_pontos(sala, pergunta, correta, posicao_resposta):
    if not correta:
        
        return 0

    dificuldade = normalizar_dificuldade(pergunta.get("dificuldade"))
    tabelas_por_dificuldade = {
        # Fácil entrega menos ponto porque costuma ser mais rápida de resolver.
        "facil": [4, 3, 2, 1],
        
        "medio": [6, 5, 4, 3, 2, 1],
        # Difícil vale mais
        "dificil": [8, 7, 6, 5, 4, 3, 2, 1],
        "todas": [6, 5, 4, 3, 2, 1],
    }
    tabela_por_ordem = tabelas_por_dificuldade.get(
        dificuldade,
        tabelas_por_dificuldade["medio"],
    )
    if posicao_resposta <= len(tabela_por_ordem):
        pontos = tabela_por_ordem[posicao_resposta - 1]
    else:
        pontos = 1

    # Rodada x2 dobra só a pergunta atual
    return pontos * 2 if pergunta.get("bonus_x2") else pontos


def finalizar_rodada(sala):
    if sala["status"] != "jogando":
        return

    pergunta = pergunta_atual(sala)
    if not pergunta:
        sala["status"] = "final"
        return

    for jogador in sala["jogadores"].values():
        jogador["respondeu"] = False

    for token, jogador in sala["jogadores"].items():
        if token in sala["respostas"]:
            continue
       
        delta = 0
        jogador["pontos"] = max(0, jogador.get("pontos", 0) + delta)
        jogador["ultimo_delta"] = delta
        sala["respostas"][token] = {
            "nome": jogador["nome"],
            "avatar": jogador["avatar"],
            "resposta": "",
            "correta": False,
            "delta": delta,
            "ordem": None,
            "sem_resposta": True,
        }

    sala["status"] = "resultado"
    sala["resultado_inicio"] = time.time()
    sala["resultado"] = {
        "pergunta": pergunta["pergunta"],
        "opcoes": pergunta["opcoes"],
        "respostaCorreta": pergunta["respostaCorreta"],
        "bonus_x2": bool(pergunta.get("bonus_x2")),
        "dificuldade": pergunta.get("dificuldade", "medio"),
        "dificuldadeLabel": pergunta.get(
            "dificuldadeLabel",
            label_dificuldade(pergunta.get("dificuldade")),
        ),
        "ranking": [jogador_publico(j) for j in jogadores_ordenados(sala)],
    }


def avancar_estado_automatico(sala):
    # A tela chama a API todo segundo; aqui o servidor aproveita para trocar
    # de pergunta ou fechar a partida
    if sala["status"] == "jogando" and segundos_restantes(sala) <= 0:
        finalizar_rodada(sala)

    if sala["status"] != "resultado":
        return

    if time.time() - sala.get("resultado_inicio", time.time()) < SEGUNDOS_RESULTADO:
        return

    if sala["rodada_atual"] + 1 >= len(sala.get("perguntas", [])):
        sala["status"] = "final"
        sala["final_inicio"] = time.time()
        return

    sala["rodada_atual"] += 1
    sala["status"] = "jogando"
    sala["rodada_inicio"] = time.time()
    sala["respostas"] = {}
    sala["resultado"] = {}
    for jogador in sala["jogadores"].values():
        jogador["respondeu"] = False
        jogador["ultimo_delta"] = 0


def estado_jogo(sala):
    token = token_da_requisicao()
    avancar_estado_automatico(sala)

    # Esse pacote é o que o JavaScript da sala usa para redesenhar a tela.
    estado = {
        "sala": resumo_sala(sala),
        "status": sala["status"],
        "is_owner": eh_dono(sala, token),
        "jogadores": [jogador_publico(j) for j in jogadores_ordenados(sala)],
    }

    if sala["status"] == "jogando":
        pergunta = pergunta_atual(sala)
        estado.update(
            {
                "rodada_atual": sala["rodada_atual"] + 1,
                "total_rodadas": len(sala.get("perguntas", [])),
                "tempo_restante": segundos_restantes(sala),
                "pergunta": {
                    "texto": pergunta["pergunta"],
                    "opcoes": pergunta["opcoes"],
                    "fonte": pergunta["fonte"],
                    "bonus_x2": bool(pergunta.get("bonus_x2")),
                    "dificuldade": pergunta.get("dificuldade", "medio"),
                    "dificuldadeLabel": pergunta.get(
                        "dificuldadeLabel",
                        label_dificuldade(pergunta.get("dificuldade")),
                    ),
                },
                "respondidas": len(sala["respostas"]),
                "respondi": token in sala["respostas"],
                "minha_resposta": sala["respostas"].get(token, {}).get("resposta"),
            }
        )

    if sala["status"] == "resultado":
        resposta = sala["respostas"].get(token, {})
        estado.update(
            {
                "rodada_atual": sala["rodada_atual"] + 1,
                "total_rodadas": len(sala.get("perguntas", [])),
                "resultado": sala.get("resultado", {}),
                "minha_resposta": resposta.get("resposta"),
                "meu_delta": resposta.get("delta", 0),
            }
        )

    if sala["status"] == "final":
        ranking = [jogador_publico(j) for j in jogadores_ordenados(sala)]
        estado["ranking_final"] = ranking
        estado["top3"] = ranking[:3]

    return estado


def localizar_pergunta(pergunta_id):
    if ":" not in pergunta_id:
        abort(404)

    prefixo, id_original = pergunta_id.split(":", 1)
    caminho = QUESTOES_CRIADAS_PATH if prefixo == "criadas" else QUESTOES_PATH
    dados = carregar_json(caminho, [])

    for indice, item in enumerate(dados):
        if str(item.get("id")) == id_original:
            return caminho, dados, indice

    abort(404)


def montar_item_pergunta(formulario, pergunta_id=None):
    alternativas = [
        formulario.get("alternativa_a", "").strip(),
        formulario.get("alternativa_b", "").strip(),
        formulario.get("alternativa_c", "").strip(),
        formulario.get("alternativa_d", "").strip(),
    ]
    letra_correta = formulario.get("resposta_correta", "A").upper()
    indice_correto = {"A": 0, "B": 1, "C": 2, "D": 3}.get(letra_correta, 0)

    return {
        "id": pergunta_id or uuid.uuid4().hex[:8],
        "pergunta": formulario.get("pergunta", "").strip(),
        "opcoes": alternativas,
        "respostaCorreta": alternativas[indice_correto],
        "dificuldade": normalizar_dificuldade_escolha(formulario.get("dificuldade")),
        "serie": normalizar_serie(formulario.get("serie")),
    }


def formulario_pergunta_valido(item):
    return (
        bool(item["pergunta"])
        and len(item["opcoes"]) == 4
        and all(item["opcoes"])
        and bool(item["respostaCorreta"])
    )


def paginar_itens(itens, pagina_atual, por_pagina=12):
    try:
        pagina_atual = int(pagina_atual)
    except (TypeError, ValueError):
        pagina_atual = 1

    total = len(itens)
    total_paginas = max(1, math.ceil(total / por_pagina))
    pagina_atual = max(1, min(total_paginas, pagina_atual))
    inicio = (pagina_atual - 1) * por_pagina
    fim = inicio + por_pagina

    # Deixo tudo junto para o HTML só se preocupar em desenhar os botões.
    return {
        "itens": itens[inicio:fim],
        "pagina": pagina_atual,
        "total_paginas": total_paginas,
        "total": total,
        "tem_anterior": pagina_atual > 1,
        "tem_proxima": pagina_atual < total_paginas,
        "anterior": max(1, pagina_atual - 1),
        "proxima": min(total_paginas, pagina_atual + 1),
        "inicio": 0 if total == 0 else inicio + 1,
        "fim": min(fim, total),
    }


@app.route("/")
def home():
    salas_publicas, filtros = filtrar_salas_publicas(request.args)

    return render_template(
        "home.html",
        salas_publicas=salas_publicas,
        filtros=filtros,
        dificuldades=DIFICULDADES_ESCOLHA,
        series=SERIES,
    )


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True})


@app.route("/api/salas")
def api_salas():
    # Endpoint leve usado pela home para mostrar salas novas sem precisar atualizar a página.
    salas_publicas, _filtros = filtrar_salas_publicas(request.args)
    for sala in salas_publicas:
        sala["entrar_url"] = url_for("entrar_sala", codigo=sala["codigo"])
    return jsonify({"salas": salas_publicas})


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None

    if "user" in session:
        return redirect(url_for("home"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        if not username or not password:
            error = "Preencha todos os campos."
        elif check_login(username, password):
            session["user"] = username
            session.permanent = True
            return redirect(url_for("dashboard"))
        else:
            error = "Usuário ou senha incorretos."

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("home"))


@app.route("/dashboard", methods=["GET", "POST"])
def dashboard():
    if "user" not in session:
        return redirect(url_for("login"))

    if request.method == "POST":
        item = montar_item_pergunta(request.form)
        if not formulario_pergunta_valido(item):
            flash("Preencha a pergunta, as 4 alternativas e a resposta correta.", "error")
            return redirect(url_for("dashboard"))

        # As perguntas do professor ficam separadas do banco principal
        criadas = carregar_json(QUESTOES_CRIADAS_PATH, [])
        criadas.append(item)
        salvar_json(QUESTOES_CRIADAS_PATH, criadas)
        flash("Pergunta criada e salva no banco questoes_criadas.json.", "success")
        return redirect(url_for("dashboard"))

    perguntas_criadas = carregar_perguntas_criadas()
    perguntas_base = carregar_perguntas_base()
    banco_aberto = request.args.get("banco", "criadas")
    if banco_aberto not in {"criadas", "base"}:
        banco_aberto = "criadas"
    pagina_criadas = paginar_itens(perguntas_criadas, request.args.get("pagina_criadas"))
    pagina_base = paginar_itens(perguntas_base, request.args.get("pagina_base"))

    return render_template(
        "DashBoard.html",
        perguntas_criadas=perguntas_criadas,
        perguntas_base=perguntas_base,
        pagina_criadas=pagina_criadas,
        pagina_base=pagina_base,
        banco_aberto=banco_aberto,
        todas_perguntas=perguntas_criadas + perguntas_base,
        dificuldades=DIFICULDADES_ESCOLHA,
        series=SERIES,
    )


@app.route("/perguntas/<path:pergunta_id>/editar", methods=["POST"])
def editar_pergunta(pergunta_id):
    if "user" not in session:
        return redirect(url_for("login"))

    caminho, dados, indice = localizar_pergunta(pergunta_id)
    id_original = dados[indice].get("id")
    item = montar_item_pergunta(request.form, id_original)

    if not formulario_pergunta_valido(item):
        flash("Não consegui editar: revise os campos da pergunta.", "error")
        return redirect(url_for("dashboard"))

    dados[indice].update(item)
    salvar_json(caminho, dados)
    flash("Pergunta editada com sucesso.", "success")
    return redirect(url_for("dashboard"))


@app.route("/perguntas/<path:pergunta_id>/excluir", methods=["POST"])
def excluir_pergunta(pergunta_id):
    if "user" not in session:
        return redirect(url_for("login"))

    caminho, dados, indice = localizar_pergunta(pergunta_id)
    dados.pop(indice)
    salvar_json(caminho, dados)
    flash("Pergunta excluída do banco selecionado.", "success")
    return redirect(url_for("dashboard"))


@app.route("/entra_sala_create", methods=["GET", "POST"])
def room_create():
    token = token_jogador(request.form.get("player_token") if request.method == "POST" else None)

    if request.method == "POST":
        # Tempo e rodadas podem vir como opção pronta
        tempo_opcao = request.form.get("tempo_opcao", "60")
        tempo = (
            request.form.get("tempo_customizado", "60")
            if tempo_opcao == "personalizado"
            else tempo_opcao
        )
        rodada_opcao = request.form.get("rodadas", "4")
        rodadas = (
            request.form.get("rodadas_customizadas", "4")
            if rodada_opcao == "personalizado"
            else rodada_opcao
        )

        try:
            tempo = max(10, min(300, int(tempo)))
            rodadas = max(1, min(30, int(rodadas)))
            max_jogadores = max(1, min(100, int(request.form.get("max_jogadores", 20))))
            chance_x2 = max(0, min(100, int(request.form.get("chance_x2", 10))))
        except ValueError:
            flash("Use apenas números válidos para tempo, rodadas e jogadores.", "error")
            return redirect(url_for("room_create"))

        codigo = gerar_codigo_sala()
        api_desativada = request.form.get("api_desativada") == "on"
        # A sala nasce em modo espera
        salas[codigo] = {
            "codigo": codigo,
            "nome": request.form.get("nome_sala", "Sala de Matemática").strip()
            or "Sala de Matemática",
            "senha": request.form.get("senha", "").strip(),
            "max_jogadores": max_jogadores,
            "tempo_por_questao": tempo,
            "rodadas": rodadas,
            "api_frequente": request.form.get("api_frequente") == "on" and not api_desativada,
            "api_desativada": api_desativada,
            "chance_x2": chance_x2,
            "dificuldade": normalizar_dificuldade_escolha(request.form.get("dificuldade")),
            "serie": normalizar_serie(request.form.get("serie")),
            "dono_token": token,
            "status": "espera",
            "criada_em": time.time(),
            "questoes_json_usadas": [],
            "jogadores": {},
            "chat": [],
            "perguntas": [],
            "rodada_atual": 0,
            "rodada_inicio": 0,
            "respostas": {},
            "resultado": {},
        }
        emitir_salas_atualizadas()
        flash("Sala criada. Agora escolha seu nome e personagem.", "success")
        return redirect(url_for("entrar_sala", codigo=codigo, dono="1", player_token=token))

    return render_template(
        "criaçao_de_sala.html",
        dificuldades=DIFICULDADES_ESCOLHA,
        series=SERIES,
    )


@app.route("/sala/<codigo>/entrar", methods=["GET", "POST"])
def entrar_sala(codigo):
    sala = sala_ou_404(codigo)
    token = token_jogador(request.values.get("player_token"))
    avatares = listar_avatares()
    dono = eh_dono(sala, token)

    if request.method == "POST":
        if sala["status"] != "espera":
            flash("Essa sala já começou. Aguarde ela voltar para a espera.", "error")
            return redirect(url_for("home"))

        if (
            sala.get("senha")
            and not dono
            and request.form.get("senha", "").strip() != sala["senha"]
        ):
            flash("Senha da sala incorreta.", "error")
            return redirect(url_for("entrar_sala", codigo=sala["codigo"]))

        if token not in sala["jogadores"] and len(sala["jogadores"]) >= sala["max_jogadores"]:
            flash("Essa sala está cheia.", "error")
            return redirect(url_for("home"))

        avatar = request.form.get("avatar", AVATAR_PADRAO)
        if avatar not in avatares:
            avatar = AVATAR_PADRAO

        nome = request.form.get("nome_jogador", "").strip() or "Jogador"
        sala["jogadores"][token] = {
            "token": token,
            "nome": nome[:24],
            "avatar": avatar,
            "pontos": sala["jogadores"].get(token, {}).get("pontos", 0),
            "ultimo_delta": 0,
            "respondeu": False,
            "entrou_em": sala["jogadores"].get(token, {}).get("entrou_em", time.time()),
        }
        session["sala_atual"] = sala["codigo"]
        emitir_estado_espera(sala["codigo"])
        emitir_salas_atualizadas()
        return redirect(url_for("espera_sala", codigo=sala["codigo"], player_token=token))

    return render_template(
        "entrar_sala.html",
        sala=sala,
        resumo=resumo_sala(sala),
        avatares=avatares,
        avatar_padrao=AVATAR_PADRAO,
        precisa_senha=bool(sala.get("senha") and not dono),
        dono=dono,
        player_token=token,
        usar_token_inicial=bool(request.values.get("player_token")),
    )


@app.route("/sala/<codigo>/espera")
def espera_sala(codigo):
    sala = sala_ou_404(codigo)
    token = token_da_requisicao()
    if sala["status"] != "espera":
        return redirect(url_for("sala_jogo", codigo=sala["codigo"], player_token=token))
    if not jogador_atual(sala, token):
        return redirect(url_for("entrar_sala", codigo=sala["codigo"], player_token=token))

    return render_template(
        "espera_sala.html",
        sala=sala,
        resumo=resumo_sala(sala),
        jogadores=jogadores_ordenados(sala),
        is_owner=eh_dono(sala, token),
        player_token=token,
        dificuldades=DIFICULDADES_ESCOLHA,
        series=SERIES,
    )


@app.route("/sala/<codigo>/configurar", methods=["POST"])
def configurar_sala(codigo):
    sala = sala_ou_404(codigo)
    token = token_da_requisicao()
    if not eh_dono(sala, token):
        abort(403)
    if sala["status"] != "espera":
        flash("Só dá para configurar a sala enquanto ela está na espera.", "error")
        return redirect(url_for("sala_jogo", codigo=sala["codigo"], player_token=token))

    try:
        sala["max_jogadores"] = max(
            len(sala["jogadores"]),
            min(100, int(request.form.get("max_jogadores", sala["max_jogadores"]))),
        )
        sala["tempo_por_questao"] = max(
            10,
            min(300, int(request.form.get("tempo_por_questao", sala["tempo_por_questao"]))),
        )
        sala["rodadas"] = max(1, min(30, int(request.form.get("rodadas", sala["rodadas"]))))
        sala["chance_x2"] = max(
            0,
            min(100, int(request.form.get("chance_x2", sala.get("chance_x2", 10)))),
        )
    except ValueError:
        flash("Configurações numéricas inválidas.", "error")
        return redirect(url_for("espera_sala", codigo=sala["codigo"], player_token=token))

    sala["nome"] = request.form.get("nome_sala", sala["nome"]).strip() or sala["nome"]
    sala["senha"] = request.form.get("senha", "").strip()
    sala["dificuldade"] = normalizar_dificuldade_escolha(request.form.get("dificuldade"))
    sala["serie"] = normalizar_serie(request.form.get("serie", sala["serie"]))
    sala["api_desativada"] = request.form.get("api_desativada") == "on"
    sala["api_frequente"] = (
        request.form.get("api_frequente") == "on"
        and not sala["api_desativada"]
    )
    emitir_estado_espera(sala["codigo"])
    emitir_salas_atualizadas()
    flash("Sala reconfigurada sem precisar recriar.", "success")
    return redirect(url_for("espera_sala", codigo=sala["codigo"], player_token=token))


@app.route("/sala/<codigo>/iniciar", methods=["POST"])
def iniciar_jogo(codigo):
    sala = sala_ou_404(codigo)
    token = token_da_requisicao()
    if not eh_dono(sala, token):
        abort(403)

    perguntas = sortear_perguntas(
        sala["rodadas"],
        sala["dificuldade"],
        sala["serie"],
        sala.get("api_frequente", False),
        sala.get("api_desativada", False),
        sala.setdefault("questoes_json_usadas", []),
    )
    perguntas = aplicar_rodadas_bonus_x2(perguntas, sala.get("chance_x2", 10))
    if not perguntas:
        flash("Não encontrei perguntas para iniciar esta sala.", "error")
        return redirect(url_for("espera_sala", codigo=sala["codigo"], player_token=token))

    for jogador in sala["jogadores"].values():
        jogador["pontos"] = 0
        jogador["ultimo_delta"] = 0
        jogador["respondeu"] = False

    sala["perguntas"] = perguntas
    sala["rodada_atual"] = 0
    sala["rodada_inicio"] = time.time()
    sala["respostas"] = {}
    sala["resultado"] = {}
    sala["status"] = "jogando"
    emitir_estado_espera(sala["codigo"])
    emitir_salas_atualizadas()
    return redirect(url_for("sala_jogo", codigo=sala["codigo"], player_token=token))


@app.route("/sala/<codigo>/jogo")
def sala_jogo(codigo):
    sala = sala_ou_404(codigo)
    token = token_da_requisicao()
    if not jogador_atual(sala, token):
        return redirect(url_for("entrar_sala", codigo=sala["codigo"], player_token=token))
    if sala["status"] == "espera":
        return redirect(url_for("espera_sala", codigo=sala["codigo"], player_token=token))
    return render_template(
        "SaladeJogo.html",
        sala=sala,
        resumo=resumo_sala(sala),
        jogador=jogador_atual(sala, token),
        is_owner=eh_dono(sala, token),
        player_token=token,
    )


@app.route("/sala/<codigo>/voltar-espera", methods=["POST"])
def voltar_espera(codigo):
    sala = sala_ou_404(codigo)
    token = token_da_requisicao()
    if not eh_dono(sala, token):
        abort(403)

    sala["status"] = "espera"
    sala["perguntas"] = []
    sala["respostas"] = {}
    sala["resultado"] = {}
    sala["rodada_atual"] = 0
    for jogador in sala["jogadores"].values():
        jogador["respondeu"] = False
        jogador["ultimo_delta"] = 0
    emitir_estado_espera(sala["codigo"])
    emitir_salas_atualizadas()
    return redirect(url_for("espera_sala", codigo=sala["codigo"], player_token=token))


@app.route("/sala/<codigo>/sair", methods=["POST", "GET"])
def sair_sala(codigo):
    sala = sala_ou_404(codigo)
    token = token_da_requisicao()
    if sala.get("dono_token") == token:
        excluir_sala_por_host(
            sala["codigo"],
            "A sala foi excluída porque o host saiu.",
        )
        session.pop("sala_atual", None)
        flash("Você encerrou a sala.", "success")
        return redirect(url_for("home"))

    if token in sala["jogadores"]:
        sala["jogadores"].pop(token)

    if not sala["jogadores"]:
        salas.pop(sala["codigo"], None)
        emitir_salas_atualizadas()
    else:
        emitir_estado_espera(sala["codigo"])
        emitir_salas_atualizadas()

    session.pop("sala_atual", None)
    flash("Você saiu da sala.", "success")
    return redirect(url_for("home"))


@app.route("/api/sala/<codigo>/estado")
def api_estado_sala(codigo):
    sala = sala_ou_404(codigo)
    return jsonify(estado_jogo(sala))


@app.route("/api/sala/<codigo>/responder", methods=["POST"])
def api_responder(codigo):
    sala = sala_ou_404(codigo)
    token = token_da_requisicao()
    jogador = jogador_atual(sala, token)
    if not jogador:
        abort(403)

    avancar_estado_automatico(sala)
    if sala["status"] != "jogando":
        return jsonify(estado_jogo(sala))
    if token in sala["respostas"]:
        return jsonify(estado_jogo(sala))

    pergunta = pergunta_atual(sala)
    resposta = (request.get_json(silent=True) or {}).get("resposta", "")
    if resposta not in pergunta["opcoes"]:
        abort(400)

    correta = resposta == pergunta["respostaCorreta"]
    posicao_resposta = len(sala["respostas"]) + 1
    posicao_acerto = 1 + sum(
        1 for resposta_anterior in sala["respostas"].values()
        if resposta_anterior.get("correta")
    )
    # A pontuação por velocidade conta a ordem dos acertos.
    # Assim, quem erra rápido não atrapalha o primeiro que acertou de verdade.
    delta = calcular_pontos(sala, pergunta, correta, posicao_acerto)
    jogador["pontos"] = max(0, jogador.get("pontos", 0) + delta)
    jogador["ultimo_delta"] = delta
    jogador["respondeu"] = True

    sala["respostas"][token] = {
        "nome": jogador["nome"],
        "avatar": jogador["avatar"],
        "resposta": resposta,
        "correta": correta,
        "delta": delta,
        "ordem": posicao_resposta,
        "ordem_acerto": posicao_acerto if correta else None,
        "sem_resposta": False,
    }

    if len(sala["respostas"]) >= len(sala["jogadores"]):
        finalizar_rodada(sala)

    return jsonify(estado_jogo(sala))


@socketio.on("entrar_sala_socket")
def socket_entrar_sala(data):
    codigo = str(data.get("codigo", "")).upper()
    sala = salas.get(codigo)
    if not sala:
        return
    token = token_da_requisicao(data)
    sockets_por_sid[request.sid] = {"codigo": codigo, "token": token}
    join_room(codigo)
    emit("estado_espera", estado_espera(sala))


@socketio.on("sair_sala_socket")
def socket_sair_sala(data):
    codigo = str(data.get("codigo", "")).upper()
    sockets_por_sid.pop(request.sid, None)
    leave_room(codigo)

#chat
@socketio.on("mensagem_chat")
def socket_mensagem_chat(data):
    codigo = str(data.get("codigo", "")).upper()
    texto = str(data.get("texto", "")).strip()
    sala = salas.get(codigo)
    jogador = jogador_atual(sala, token_da_requisicao(data)) if sala else None
    if not sala or not jogador or not texto:
        return

    mensagem = {
        "nome": jogador["nome"],
        "avatar": jogador["avatar"],
        "texto": texto[:240],
        "hora": time.strftime("%H:%M"),
    }
    sala.setdefault("chat", []).append(mensagem)
    sala["chat"] = sala["chat"][-50:]
    emit("nova_mensagem", mensagem, to=codigo)


@socketio.on("disconnect")
def socket_desconectou():
    info = sockets_por_sid.pop(request.sid, None)
    if not info:
        return
    socketio.start_background_task(
        checar_host_desconectado,
        info["codigo"],
        info["token"],
    )


@app.errorhandler(404)
def pagina_nao_encontrada(_erro):
    flash("Sala ou página não encontrada.", "error")
    return redirect(url_for("home"))


garantir_arquivos()


if __name__ == "__main__":
    porta = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG") == "1"
    socketio.run(
        app,
        host="0.0.0.0",
        port=porta,
        debug=debug,
        allow_unsafe_werkzeug=True,
    )
