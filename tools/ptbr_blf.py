"""Generate a Brazilian Portuguese (PT-BR) localization mod pack from the retail PT-PT .blf files.

Reads Frontend/Localised/{BootUp,MainGame,Career}/Portuguese.blf from the user's own USRDIR/offline.wad,
converts European Portuguese (PT-PT) spelling, grammar, and F1 terminology into natural Brazilian Portuguese
(PT-BR), and writes rebuilt .blf files into a MODS/<pack>/TRACKS/offline.wad/... overlay folder (runtime/mods.cpp).

Usage:
  python tools/ptbr_blf.py <path/to/USRDIR> <path/to/MODS/Traducao_PT_BR>
"""
import pathlib
import re
import struct
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import wad  # noqa: E402


# ------------------------------------------------------------------ BLF binary reader / writer

def parse_blf(data: bytes):
    """Parse a .blf file into (prefix_bytes, records[(hash, offset)], pool_start, strings)."""
    n = struct.unpack_from(">I", data, 0)[0]
    last_idx_off = None
    for o in range((len(data) - 20) & ~3, 40, -4):
        if struct.unpack_from(">I", data, o)[0] == n + 1 and struct.unpack_from(">I", data, o - 40)[0] == n:
            last_idx_off = o
            break
    if last_idx_off is None:
        raise ValueError("Could not locate 40-byte BLF record table")
    rec_last = last_idx_off - 16
    pool_start = rec_last + 40
    rec0 = pool_start - n * 40
    strings = []
    for i in range(n):
        r = rec0 + i * 40
        h, off = struct.unpack_from(">II", data, r)
        p = pool_start + off
        end = data.find(b"\x00", p)
        if end < 0:
            end = len(data)
        strings.append((i, h, off, data[p:end].decode("utf-8", errors="replace")))
    return rec0, pool_start, strings


def rebuild_blf(orig: bytes, rec0: int, pool_start: int, new_strings: list[str]) -> bytes:
    """Rebuild a .blf file with updated UTF-8 strings of arbitrary length, keeping the tree intact."""
    header = bytearray(orig[:pool_start])
    pool = bytearray(b"\x00")
    intern = {"": 0}
    for i, text in enumerate(new_strings):
        off = intern.get(text)
        if off is None:
            off = len(pool)
            pool.extend(text.encode("utf-8"))
            pool.append(0)
            intern[text] = off
        struct.pack_into(">I", header, rec0 + i * 40 + 4, off)
    return bytes(header) + bytes(pool)


# ------------------------------------------------------------------ Case-preserving PT-PT -> PT-BR

def match_case(src: str, repl: str) -> str:
    if src.isupper():
        return repl.upper()
    if src.islower():
        # Preserve deliberate uppercase acronyms inside replacement (e.g. "freios ABS")
        return " ".join(w if w.isupper() and len(w) >= 2 else w.lower() for w in repl.split(" "))
    if src[:1].isupper() and src[1:].islower():
        return repl[:1].upper() + repl[1:]
    # Preserve title-case multi-word phrases
    if all(w[:1].isupper() for w in src.split() if w and w[0].isalpha()):
        return " ".join(w if w.isupper() and len(w) >= 2 else (w[:1].upper() + w[1:] if w else w) for w in repl.split(" "))
    return repl[:1].upper() + repl[1:] if src[:1].isupper() else repl


# Exact whole-string UI label overrides (checked before regex rules)
EXACT_UI_LABELS = {
    "ATRÁS": "VOLTAR",
    "Atrás": "Voltar",
}


# Multi-word F1 / UI phrases (applied first, longest/most specific first)
PHRASE_RULES = [
    # Qualifying / sessions / HUD
    (r"\bSessão de desempate\b", "Super Pole (Q3)"),
    (r"\bSessão de qualificação\b", "Sessão de classificação"),
    (r"\bSessões de qualificação\b", "Sessões de classificação"),
    (r"\bClassificação da sessão de qualificação\b", "Resultado da classificação"),
    (r"\bPosição de qualificação final\b", "Posição final na classificação"),
    (r"\bPosição de qualificação\b", "Posição na classificação"),
    (r"\bvolta de qualificação\b", "volta de classificação"),
    (r"\bQualificação 1 resultado\b", "Resultado do Q1"),
    (r"\bQualificação 2 resultado\b", "Resultado do Q2"),
    (r"\bQualificação 3 resultado\b", "Resultado do Q3"),
    (r"\bQualificaste-te com sucesso\b", "Você se classificou com sucesso"),
    (r"\bNão te qualificaste\b", "Você não se classificou"),
    # Pit stops / grid / race terms (with gender agreement)
    (r"\bParagem nas boxes\b", "Parada nos boxes"),
    (r"\bParagens nas boxes\b", "Paradas nos boxes"),
    (r"\bcorredor das boxes\b", "pit lane"),
    (r"\bvia das boxes\b", "pit lane"),
    (r"\bentrada das boxes\b", "entrada dos boxes"),
    (r"\bsaída das boxes\b", "saída dos boxes"),
    (r"\blimite de velocidade nas boxes\b", "limite de velocidade no pit lane"),
    (r"\bnas boxes\b", "nos boxes"),
    (r"\bdas boxes\b", "dos boxes"),
    (r"\bàs boxes\b", "aos boxes"),
    (r"\bGrelha de partida\b", "Grid de largada"),
    (r"\bPosição na grelha\b", "Posição no grid"),
    (r"\bPreparar a grelha\b", "Preparando o grid"),
    (r"\bna grelha\b", "no grid"),
    (r"\ba grelha\b", "o grid"),
    (r"\bda grelha\b", "do grid"),
    (r"\bà grelha\b", "ao grid"),
    (r"\buma grelha\b", "um grid"),
    (r"\besta grelha\b", "este grid"),
    (r"\bConduzir através da penalização\b", "Penalidade Drive-Through"),
    (r"\bFoste desqualificado\b", "Você foi desclassificado"),
    (r"\bExcelente condução, concluíste\b", "Excelente pilotagem, você concluiu"),
    (r"\bExcelente condução, acabaste\b", "Excelente pilotagem, você terminou"),
    (r"\bParabéns, concluíste com sucesso\b", "Parabéns, você concluiu com sucesso"),
    # Gender agreement for nouns whose gender changes in PT-BR
    (r"\bo ecrã\b", "a tela"),
    (r"\bno ecrã\b", "na tela"),
    (r"\bdo ecrã\b", "da tela"),
    (r"\bao ecrã\b", "à tela"),
    (r"\beste ecrã\b", "esta tela"),
    (r"\bum ecrã\b", "uma tela"),
    (r"\bos ecrãs\b", "as telas"),
    (r"\bnos ecrãs\b", "nas telas"),
    (r"\bdos ecrãs\b", "das telas"),
    (r"\bo varrimento progressivo\b", "a varredura progressiva"),
    (r"\bno varrimento progressivo\b", "na varredura progressiva"),
    (r"\bdo varrimento progressivo\b", "da varredura progressiva"),
    (r"\bvarrimento progressivo\b", "varredura progressiva"),
    (r"\ba repetição\b", "o replay"),
    (r"\bna repetição\b", "no replay"),
    (r"\bda repetição\b", "do replay"),
    (r"\buma repetição\b", "um replay"),
    (r"\besta repetição\b", "este replay"),
    (r"\bas repetições\b", "os replays"),
    (r"\bnas repetições\b", "nos replays"),
    (r"\bdas repetições\b", "dos replays"),
    (r"\ba afinação\b", "o acerto"),
    (r"\bda afinação\b", "do acerto"),
    (r"\bna afinação\b", "no acerto"),
    (r"\buma afinação\b", "um acerto"),
    (r"\bas afinações\b", "os acertos"),
    (r"\bdas afinações\b", "dos acertos"),
    (r"\ba caixa de velocidades\b", "o câmbio"),
    (r"\bda caixa de velocidades\b", "do câmbio"),
    (r"\bna caixa de velocidades\b", "no câmbio"),
    (r"\buma caixa de velocidades\b", "um câmbio"),
    (r"\bcaixa de velocidades\b", "câmbio"),
    (r"\bo sopé\b", "a cambagem"),
    (r"\bdo sopé\b", "da cambagem"),
    (r"\bno sopé\b", "na cambagem"),
    (r"\bum sopé\b", "uma cambagem"),
    (r"\bo manuseamento\b", "a dirigibilidade"),
    (r"\bdo manuseamento\b", "da dirigibilidade"),
    (r"\bno manuseamento\b", "na dirigibilidade"),
    (r"\bum pião\b", "uma rodada"),
    # Controls / car setup / cameras
    (r"\bTRAVÕES ANTI-BLOQUEIO\b", "FREIOS ABS"),
    (r"\bTravões anti-bloqueio\b", "Freios ABS"),
    (r"\bASSISTENTE TRAVAGEM\b", "ASSISTENTE DE FRENAGEM"),
    (r"\bPROPENSÃO DE TRAVAGEM\b", "BALANÇO DE FREIOS"),
    (r"\bPropensão de travagem\b", "Balanço de freios"),
    (r"\bDESLOCAR CÂMARA CICLICAMENTE\b", "ALTERNAR CÂMERA"),
    (r"\bNICHO DA CÂMARA\b", "CÂMERA"),
    (r"\bTABELA CONDUTORES\b", "CAMPEONATO DE PILOTOS"),
    (r"\bTabela condutores\b", "Campeonato de Pilotos"),
    (r"\bECRÃ DIVIDIDO\b", "TELA DIVIDIDA"),
    (r"\bEcrã dividido\b", "Tela dividida"),
    (r"\bGUARDAR A MEIO DA CORRIDA\b", "SALVAR NO MEIO DA CORRIDA"),
    (r"\bGUARDAR REPETIÇÃO\b", "SALVAR REPLAY"),
    (r"\bGUARDAR PERFIL\b", "SALVAR PERFIL"),
    (r"\bGUARDAR O JOGO\b", "SALVAR O JOGO"),
    (r"\bGUARDAR NO JOGO\b", "SALVAR NO JOGO"),
    (r"\bFim-de-semana de Grande Prémio\b", "Fim de semana de Grande Prêmio"),
    (r"\bFim-de-semana\b", "Fim de semana"),
    # 2nd person (tu -> você) common prompts & adverb-verb order
    (r"\bTens a certeza de que não pretendes\b", "Tem certeza de que não deseja"),
    (r"\bTens a certeza de que pretendes\b", "Tem certeza de que deseja"),
    (r"\bTens a certeza de que queres\b", "Tem certeza de que quer"),
    (r"\bTens a certeza\b", "Tem certeza"),
    (r"\bNão estás\b", "Você não está"),
    (r"\bNão tens\b", "Você não tem"),
    (r"\bNão podes\b", "Você não pode"),
    (r"\bNão queres\b", "Você não quer"),
    (r"\bNão vais\b", "Você não vai"),
    (r"\bNão deves\b", "Você não deve"),
    (r"\bNão terás\b", "Você não terá"),
    (r"\bAinda podes\b", "Você ainda pode"),
    (r"\bAinda tens\b", "Você ainda tem"),
    (r"\bTambém podes\b", "Você também pode"),
    (r"\bTambém tens\b", "Você também tem"),
    (r"\bCarrega e vê\b", "Carregue e veja"),
    (r"\bPretendes guardar\b", "Deseja salvar"),
    (r"\bPretendes continuar\b", "Deseja continuar"),
    (r"\bPretendes sair\b", "Deseja sair"),
    (r"\bPretendes substituir\b", "Deseja substituir"),
    (r"\bPretendes eliminar\b", "Deseja excluir"),
    (r"\bPretendes apagar\b", "Deseja apagar"),
    (r"\bPretendes\b", "Deseja"),
    (r"\bA guardar\b", "Salvando"),
    (r"\bA carregar\b", "Carregando"),
    (r"\bA mudar para\b", "Mudando para"),
    (r"\bA aguardar\b", "Aguardando"),
    (r"\bA iniciar\b", "Iniciando"),
    (r"\bA verificar\b", "Verificando"),
    (r"\bA criar\b", "Criando"),
    (r"\bA eliminar\b", "Excluindo"),
    (r"\bestiver a tremeluzir\b", "estiver tremendo"),
    (r"\bEscolhe novamente\b", "Escolha novamente"),
    (r"\bEscolhe um\b", "Escolha um"),
    (r"\bEscolhe uma\b", "Escolha uma"),
    (r"\bEscolhe o\b", "Escolha o"),
    (r"\bEscolhe a\b", "Escolha a"),
    (r"\bCarrega em\b", "Pressione"),
    (r"\bPrime o botão\b", "Pressione o botão"),
    (r"\bPrime\b", "Pressione"),
    (r"\bpara o teu\b", "para o seu"),
    (r"\bpara a tua\b", "para a sua"),
    (r"\bno teu\b", "no seu"),
    (r"\bna tua\b", "na sua"),
    (r"\bdo teu\b", "do seu"),
    (r"\bda tua\b", "da sua"),
    (r"\bo teu\b", "o seu"),
    (r"\ba tua\b", "a sua"),
    (r"\bos teus\b", "os seus"),
    (r"\bas tuas\b", "as suas"),
    (r"\bajudar-te\b", "ajudar você"),
    (r"\bpermite-te\b", "permite que você"),
]

# Single-word PT-PT -> PT-BR vocabulary & modern orthography
WORD_RULES = [
    # F1 & UI vocabulary
    (r"\bequipa\b", "equipe"),
    (r"\bequipas\b", "equipes"),
    (r"\bprémio\b", "prêmio"),
    (r"\bprémios\b", "prêmios"),
    (r"\bqualificação\b", "classificação"),
    (r"\bqualificações\b", "classificações"),
    (r"\bdesqualificado\b", "desclassificado"),
    (r"\bdesqualificada\b", "desclassificada"),
    (r"\bdesqualificados\b", "desclassificados"),
    (r"\bcondutor\b", "piloto"),
    (r"\bcondutores\b", "pilotos"),
    (r"\bgrelha\b", "grid"),
    (r"\bgrelhas\b", "grids"),
    (r"\bparagem\b", "parada"),
    (r"\bparagens\b", "paradas"),
    (r"\btravão\b", "freio"),
    (r"\btravões\b", "freios"),
    (r"\btravagem\b", "frenagem"),
    (r"\btravagens\b", "frenagens"),
    (r"\btravar\b", "frear"),
    (r"\btrava\b", "freia"),
    (r"\bcondução\b", "pilotagem"),
    (r"\bconduzir\b", "pilotar"),
    (r"\bconduz\b", "pilota"),
    (r"\bconduzes\b", "você pilota"),
    (r"\bcâmara\b", "câmera"),
    (r"\bcâmaras\b", "câmeras"),
    (r"\becrã\b", "tela"),
    (r"\becrãs\b", "telas"),
    (r"\bsector\b", "setor"),
    (r"\bsectores\b", "setores"),
    (r"\bficheiro\b", "arquivo"),
    (r"\bficheiros\b", "arquivos"),
    (r"\bguardar\b", "salvar"),
    (r"\bguardado\b", "salvo"),
    (r"\bguardada\b", "salva"),
    (r"\bguardados\b", "salvos"),
    (r"\bguardadas\b", "salvas"),
    (r"\bdefinições\b", "configurações"),
    (r"\bdefinição\b", "configuração"),
    (r"\bafinação\b", "acerto"),
    (r"\bafinações\b", "acertos"),
    (r"\bdesportivo\b", "esportivo"),
    (r"\bdesportiva\b", "esportiva"),
    (r"\bdesportivos\b", "esportivos"),
    (r"\bdesportivas\b", "esportivas"),
    (r"\bdesporto\b", "esporte"),
    (r"\bdesportos\b", "esportes"),
    (r"\bépoca\b", "temporada"),
    (r"\bépocas\b", "temporadas"),
    (r"\bconnosco\b", "conosco"),
    (r"\bembraiagem\b", "embreagem"),
    (r"\bsopé\b", "cambagem"),
    (r"\bmanuseamento\b", "dirigibilidade"),
    (r"\bpião\b", "rodada"),
    (r"\bmonitorizar\b", "monitorar"),
    (r"\bpenalização\b", "penalidade"),
    (r"\bpenalizações\b", "penalidades"),
    (r"\brepetição\b", "replay"),
    (r"\brepetições\b", "replays"),
    (r"\bcontrolador\b", "controle"),
    (r"\bcontroladores\b", "controles"),
    (r"\bregisto\b", "registro"),
    (r"\bregistos\b", "registros"),
    (r"\bregistar\b", "registrar"),
    (r"\bregistado\b", "registrado"),
    (r"\beliminar\b", "excluir"),
    (r"\beliminado\b", "excluído"),
    (r"\bconforto\b", "conforto"),
    (r"\bcontacto\b", "contato"),
    (r"\bcontactos\b", "contatos"),
    (r"\bcontactado\b", "contatado"),
    (r"\bcontactada\b", "contatada"),
    (r"\bcontactar\b", "contatar"),
    # Pre-Acordo Ortográfico (silent c/p) -> modern PT-BR
    (r"\btrajectória\b", "trajetória"),
    (r"\btrajectórias\b", "trajetórias"),
    (r"\bespectacular\b", "espetacular"),
    (r"\bespectáculo\b", "espetáculo"),
    (r"\bsecção\b", "seção"),
    (r"\bsecções\b", "seções"),
    (r"\brecta\b", "reta"),
    (r"\brectas\b", "retas"),
    (r"\bafectam\b", "afetam"),
    (r"\bafecta\b", "afeta"),
    (r"\bafectado\b", "afetado"),
    (r"\bafectada\b", "afetada"),
    (r"\bafectados\b", "afetados"),
    (r"\bafectar\b", "afetar"),
    (r"\bcontracto\b", "contrato"),
    (r"\bcontractos\b", "contratos"),
    (r"\breflectir\b", "refletir"),
    (r"\breflecte\b", "reflete"),
    (r"\binteractivo\b", "interativo"),
    (r"\binteractiva\b", "interativa"),
    (r"\binteractivas\b", "interativas"),
    (r"\breactivares\b", "reativar"),
    (r"\breactivar\b", "reativar"),
    (r"\boptimista\b", "otimista"),
    (r"\boptimistas\b", "otimistas"),
    (r"\boptimismo\b", "otimismo"),
    (r"\badoptou\b", "adotou"),
    (r"\bdirector\b", "diretor"),
    (r"\bdirectores\b", "diretores"),
    (r"\bdirectora\b", "diretora"),
    (r"\bdirecção\b", "direção"),
    (r"\bdirecções\b", "direções"),
    (r"\bdirecto\b", "direto"),
    (r"\bdirecta\b", "direta"),
    (r"\bdirectos\b", "diretos"),
    (r"\bdirectas\b", "diretas"),
    (r"\bdirectamente\b", "diretamente"),
    (r"\btracção\b", "tração"),
    (r"\bacção\b", "ação"),
    (r"\bacções\b", "ações"),
    (r"\bprotecção\b", "proteção"),
    (r"\bselecção\b", "seleção"),
    (r"\bseleccionar\b", "selecionar"),
    (r"\bselecciona\b", "selecione"),
    (r"\bseleccionado\b", "selecionado"),
    (r"\bseleccionada\b", "selecionada"),
    (r"\bseleccionados\b", "selecionados"),
    (r"\bseleccionadas\b", "selecionadas"),
    (r"\bseleccionaste\b", "você selecionou"),
    (r"\bcorrecto\b", "correto"),
    (r"\bcorrecta\b", "correta"),
    (r"\bcorrectos\b", "corretos"),
    (r"\bcorrectas\b", "corretas"),
    (r"\bcorrectamente\b", "corretamente"),
    (r"\bincorrecto\b", "incorreto"),
    (r"\bincorrecta\b", "incorreta"),
    (r"\bobjectivo\b", "objetivo"),
    (r"\bobjectivos\b", "objetivos"),
    (r"\bactual\b", "atual"),
    (r"\bactuais\b", "atuais"),
    (r"\bactualmente\b", "atualmente"),
    (r"\bactualização\b", "atualização"),
    (r"\bactualizações\b", "atualizações"),
    (r"\bactualizar\b", "atualizar"),
    (r"\bactualizado\b", "atualizado"),
    (r"\bexacto\b", "exato"),
    (r"\bexacta\b", "exata"),
    (r"\bexactamente\b", "exatamente"),
    (r"\befectuar\b", "efetuar"),
    (r"\befectuado\b", "efetuado"),
    (r"\befectuada\b", "efetuada"),
    (r"\befectivamente\b", "efetivamente"),
    (r"\bóptimo\b", "ótimo"),
    (r"\bóptima\b", "ótima"),
    (r"\bóptimos\b", "ótimos"),
    (r"\bóptimas\b", "ótimas"),
    (r"\beléctrico\b", "elétrico"),
    (r"\beléctrica\b", "elétrica"),
    (r"\beléctricos\b", "elétricos"),
    (r"\belectrónico\b", "eletrônico"),
    (r"\belectrónica\b", "eletrônica"),
    (r"\bfactor\b", "fator"),
    (r"\bfactores\b", "fatores"),
    (r"\bprojecto\b", "projeto"),
    (r"\bprojectos\b", "projetos"),
    (r"\bactividade\b", "atividade"),
    (r"\bactividades\b", "atividades"),
    (r"\bactivo\b", "ativo"),
    (r"\bactiva\b", "ativa"),
    (r"\bactivar\b", "ativar"),
    (r"\bactivado\b", "ativado"),
    (r"\bactivada\b", "ativada"),
    (r"\bdesactivar\b", "desativar"),
    (r"\bdesactivado\b", "desativado"),
    (r"\bdesactivada\b", "desativada"),
    (r"\boptimizar\b", "otimizar"),
    (r"\boptimizado\b", "otimizado"),
    (r"\bexpectativa\b", "expectativa"),
    (r"\bexcepção\b", "exceção"),
    (r"\bexcepcional\b", "excepcional"),
    (r"\badoptar\b", "adotar"),
    (r"\badoptado\b", "adotado"),
    (r"\bopção\b", "opção"),
    (r"\bfacto\b", "fato"),
    (r"\bfactos\b", "fatos"),
    (r"\bsectorial\b", "setorial"),
    # Acute -> circumflex (PT-PT -> PT-BR accents)
    (r"\bcrónica\b", "crônica"),
    (r"\bcrónicas\b", "crônicas"),
    (r"\beconómico\b", "econômico"),
    (r"\beconómica\b", "econômica"),
    (r"\bpolémica\b", "polêmica"),
    (r"\bpolémico\b", "polêmico"),
    (r"\bfenómeno\b", "fenômeno"),
    (r"\bantónio\b", "antônio"),
    (r"\bmonaco\b", "mônaco"),
    (r"\bmónaco\b", "mônaco"),
    (r"\bcronómetro\b", "cronômetro"),
    (r"\bquilómetro\b", "quilômetro"),
    (r"\bquilómetros\b", "quilômetros"),
    (r"\bautomóvel\b", "automóvel"),
    (r"\bbónus\b", "bônus"),
    (r"\btónico\b", "tônico"),
    (r"\búnico\b", "único"),
    # 2nd person verbs -> 3rd person (você)
    (r"\bestás\b", "você está"),
    (r"\btens\b", "você tem"),
    (r"\bpodes\b", "você pode"),
    (r"\bqueres\b", "você quer"),
    (r"\bvais\b", "você vai"),
    (r"\bdeves\b", "você deve"),
    (r"\bconcluíste\b", "você concluiu"),
    (r"\bacabaste\b", "você terminou"),
    (r"\bterminaste\b", "você terminou"),
    (r"\bganhaste\b", "você venceu"),
    (r"\bperdeste\b", "você perdeu"),
    (r"\bconseguiste\b", "você conseguiu"),
    (r"\bfizeste\b", "você fez"),
    (r"\bentraste\b", "você entrou"),
    (r"\bsaíste\b", "você saiu"),
    (r"\bpassaste\b", "você passou"),
    (r"\bultrapassaste\b", "você ultrapassou"),
    (r"\bmelhoraste\b", "você melhorou"),
    (r"\batingiste\b", "você atingiu"),
    (r"\bchegaste\b", "você chegou"),
    (r"\bobtiveste\b", "você obteve"),
    (r"\bprecisas\b", "você precisa"),
    (r"\bconsegues\b", "você consegue"),
    (r"\bterás\b", "você terá"),
    (r"\bserás\b", "você será"),
    (r"\bfarás\b", "você fará"),
    (r"\bverás\b", "você verá"),
    (r"\bvoltarás\b", "você voltará"),
    (r"\bcontinuarás\b", "você continuará"),
    (r"\btiveste\b", "você teve"),
    (r"\bfalhaste\b", "você falhou"),
    (r"\bmostraste\b", "você mostrou"),
    (r"\bparticipaste\b", "você participou"),
    (r"\bimpressionaste\b", "você impressionou"),
    (r"\balcançaste\b", "você alcançou"),
    (r"\bguardaste\b", "você salvou"),
    (r"\befectuaste\b", "você efetuou"),
    (r"\bdesbloqueaste\b", "você desbloqueou"),
    (r"\bquiseres\b", "quiser"),
    (r"\bteres\b", "ter"),
    (r"\btiveres\b", "tiver"),
    (r"\bestiveres\b", "estiver"),
    (r"\bfazeres\b", "fazer"),
    (r"\bobteres\b", "obter"),
    (r"\bconseguires\b", "conseguir"),
    (r"\bcontinuares\b", "continuar"),
    (r"\bvoltares\b", "voltar"),
    (r"\bretirares\b", "retirar"),
    (r"\bbatas\b", "bata"),
    (r"\butiliza\b", "use"),
    (r"\bajusta\b", "ajuste"),
    (r"\bselecciona\b", "selecione"),
    (r"\bseleciona\b", "selecione"),
    (r"\bescolhe\b", "escolha"),
]

COMPILED_PHRASES = [(re.compile(pat, re.IGNORECASE), repl) for pat, repl in PHRASE_RULES]
COMPILED_WORDS = [(re.compile(pat, re.IGNORECASE), repl) for pat, repl in WORD_RULES]


def convert_ptbr(text: str) -> str:
    if not text:
        return text
    if text in EXACT_UI_LABELS:
        return EXACT_UI_LABELS[text]
    out = text
    for rx, repl in COMPILED_PHRASES:
        out = rx.sub(lambda m: match_case(m.group(0), repl), out)
    for rx, repl in COMPILED_WORDS:
        out = rx.sub(lambda m: match_case(m.group(0), repl), out)
    # Clean occasional double "você você" if a phrase and word both triggered
    out = re.sub(r"\b(você)\s+\1\b", r"\1", out, flags=re.IGNORECASE)
    # Fix adverb + "você" order produced when a 2nd-person verb followed "não"/"ainda"/"também"
    out = re.sub(
        r"\b(não|ainda|também|já|nunca)\s+você\b",
        lambda m: match_case(m.group(1), "você") + " " + m.group(1).lower(),
        out,
        flags=re.IGNORECASE,
    )
    # Final gender check for "grid" if a preceding preposition was not merged above
    out = re.sub(r"\b(da|na|à)\s+(grid)\b", lambda m: match_case(m.group(1), {"da": "do", "na": "no", "à": "ao"}[m.group(1).lower()]) + " " + m.group(2), out, flags=re.IGNORECASE)
    return out


def main():
    if len(sys.argv) != 3:
        print("Usage: python tools/ptbr_blf.py <USRDIR> <MODS/Traducao_PT_BR>")
        sys.exit(1)
    usrdir = pathlib.Path(sys.argv[1])
    out_pack = pathlib.Path(sys.argv[2])
    offline = usrdir / "offline.wad"
    if not offline.is_file():
        raise SystemExit(f"offline.wad not found in {usrdir}")

    archive = wad.Wad(offline)
    targets = [
        "Frontend/Localised/BootUp/Portuguese.blf",
        "Frontend/Localised/MainGame/Portuguese.blf",
        "Frontend/Localised/Career/Portuguese.blf",
    ]
    total_strs = 0
    total_changed = 0
    for rel in targets:
        raw = archive.get(rel)
        if raw is None:
            raise SystemExit(f"Entry {rel} not found in {offline}")
        rec0, pool_start, entries = parse_blf(raw)
        converted = []
        changed = 0
        for _, _, _, s in entries:
            ns = convert_ptbr(s)
            if ns != s:
                changed += 1
            converted.append(ns)
        rebuilt = rebuild_blf(raw, rec0, pool_start, converted)
        # Round-trip self-check!
        _, _, check = parse_blf(rebuilt)
        assert len(check) == len(converted), f"BLF count mismatch in {rel}"
        assert all(c[3] == ns for c, ns in zip(check, converted)), f"BLF roundtrip mismatch in {rel}"

        dst = out_pack / "LOCALISATION" / "offline.wad" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(rebuilt)
        total_strs += len(entries)
        total_changed += changed
        print(f"{rel}: {changed}/{len(entries)} strings converted to PT-BR ({len(raw)} -> {len(rebuilt)} bytes)")

    pack_ini = out_pack / "pack.ini"
    pack_ini.write_text(
        "[pack]\n"
        "name = Traducao PT-BR\n"
        "description = Traducao para Portugues Brasileiro baseada nos arquivos oficiais PT-PT (selecione Portugues no launcher).\n",
        encoding="utf-8",
    )
    print(f"Done! {total_changed}/{total_strs} strings adapted into {out_pack}")


if __name__ == "__main__":
    main()
